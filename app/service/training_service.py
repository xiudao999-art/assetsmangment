"""规则训练服务 —— 基于人工标注样本,AI 迭代调优项目规则直到收敛。

流程:
1. 快照当前项目规则
2. 用当前规则重审所有训练样本物料 → 与实际命中对比
3. AI 逐规则分析漏判/多判原因 → 调整 keywords/condition/guidance
4. 重新审核 → 重复直到 漏判=0 且 多判率≤max_fp_ratio 或达到最大迭代次数
"""
from __future__ import annotations
from typing import Optional
from app.domain.models import TrainingSet, TrainingExample, AuditJob, MaterialType, AuditRule
from app.domain.ports import TrainingSetRepo, TrainingExampleRepo, AuditRuleRepo, MaterialRepo
from app.infrastructure.snowflake import next_id_str

# ── AI 规则调优系统提示词 ──
_RULE_ADJUST_SYS = (
    "你是审核规则调优引擎。你会收到一条审核规则的当前配置，以及它在实际审核中的表现数据"
    "（哪些物料漏判了——应该命中但没命中；哪些物料多判了——不应该命中却命中了）。"
    "你的任务是分析原因并给出微调后的规则配置，使规则能准确命中该命中的物料、避免误命中不该命中的物料。\n\n"
    "核心原则：**最小改动**。规则已经过人工精心配置，你只需要做最少量的调整来修复具体问题，"
    "不要推翻重写、不要改变规则的核心意图、不要大幅修改 condition/guidance 的结构。\n\n"
    "微调策略:\n"
    "- 漏判 → 在现有 keywords 末尾追加 1-3 个漏判物料中出现的同义词/近义表述（保留全部原有关键词）；"
    "如果漏判是因为 condition 措辞没覆盖到漏判物料的典型表述，在 condition 末尾补一句「也适用于xxx等情况」，不要改写整段 condition。\n"
    "- 多判 → 在 guidance 末尾追加 1-2 条反例（什么不算违规、什么情况应放行），不要改动原有的 guidance 内容。\n"
    "- match_level 尽量保持原值不变，除非明确判断出严格程度确实用错了才改。\n"
    "- 如果分析后认为规则本身没问题、只是案例特殊，返回原有配置不变，在 analysis 中说明原因。\n\n"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释,字段:\n"
    "analysis(中文,简要分析漏判/多判的原因,≤150字),\n"
    "keywords(在原有 keywords 基础上微调后的关键词数组,尽量保留原有、末尾追加少量新词),\n"
    "condition(微调后的自然语言条件,尽量保持原文、末尾追加限定或扩展),\n"
    "guidance(微调后的尺度说明,保持原文、末尾追加反例或说明),\n"
    "match_level(保持原值,仅当严格程度明显用错时才建议修改:literal/metaphor/regex)。"
)


class TrainingService:
    def __init__(self, training_set_repo: TrainingSetRepo,
                 training_example_repo: TrainingExampleRepo,
                 rule_repo: AuditRuleRepo, material_repo: MaterialRepo,
                 report_repo, audit_pipeline, llm) -> None:
        self._ts_repo = training_set_repo
        self._te_repo = training_example_repo
        self._rules = rule_repo
        self._repo = material_repo
        self._reports = report_repo
        self._audit = audit_pipeline
        self._llm = llm

    # ── 训练集管理 ──

    def get_or_create_set(self, project_id: str, by: str = "") -> TrainingSet:
        """获取项目已有的训练集,没有则新建(状态=collecting)。"""
        ts = self._ts_repo.get_by_project(project_id)
        if ts is not None:
            return ts
        ts = TrainingSet(id=next_id_str(), project_id=project_id,
                         name="", status="collecting", created_by=by)
        self._ts_repo.add(ts, by=by)
        return ts

    def add_example(self, project_id: str, material_id: str,
                    expected_rule_ids: list[str], source_note: str = "",
                    by: str = "") -> TrainingExample:
        """往项目训练集添加一条样本。自动创建训练集(若不存在)。校验物料存在且归属该项目。"""
        # 校验物料存在
        m = self._repo.get(material_id)
        if m is None:
            raise TrainingError(f"物料 {material_id} 不存在")
        # 校验物料归属该项目
        m_pid = getattr(m, "project_id", "") or ""
        if m_pid != project_id:
            raise TrainingError(f"物料 {material_id} 不属于项目 {project_id}")

        ts = self.get_or_create_set(project_id, by=by)
        # 去重:同一训练集内同物料覆盖
        existing = [e for e in self._te_repo.list_for_set(ts.id)
                    if e.material_id == material_id]
        te = TrainingExample(
            id=next_id_str() if not existing else existing[0].id,
            training_set_id=ts.id,
            material_id=material_id,
            expected_rule_ids=list(dict.fromkeys(expected_rule_ids)),  # 去重保序
            source_note=source_note,
            created_by=by,
        )
        self._te_repo.add(te, by=by)
        return te

    def remove_example(self, example_id: str, by: str = "") -> None:
        self._te_repo.delete(example_id, by=by)

    def list_examples(self, project_id: str) -> list[TrainingExample]:
        ts = self._ts_repo.get_by_project(project_id)
        if ts is None:
            return []
        return self._te_repo.list_for_set(ts.id)

    def get_status(self, project_id: str) -> Optional[TrainingSet]:
        return self._ts_repo.get_by_project(project_id)

    # ── 训练执行 ──

    def start_training(self, project_id: str, by: str = "",
                       max_fp_ratio: float | None = None,
                       max_iterations: int | None = None) -> TrainingSet:
        """启动训练(同步准备,异步执行)。校验前置条件后提交到线程池。"""
        ts = self._ts_repo.get_by_project(project_id)
        if ts is None:
            raise TrainingError("请先添加训练样本再开始训练")
        examples = self._te_repo.list_for_set(ts.id)
        if not examples:
            raise TrainingError("训练集为空,请先添加至少一条样本")
        if ts.status == "training":
            raise TrainingError("训练正在进行中,请等待完成")

        # 应用训练配置(可选覆盖)
        if max_fp_ratio is not None and 0 < max_fp_ratio <= 1:
            ts.max_fp_ratio = max_fp_ratio
        if max_iterations is not None and 1 <= max_iterations <= 50:
            ts.max_iterations = max_iterations

        # 加载待训练规则:该项目的项目规则 + 全部全局规则
        trainable_rules = self._trainable_rules(project_id)
        if not trainable_rules:
            raise TrainingError("没有可训练的规则,请先添加项目规则或全局规则")

        import datetime
        ts.rule_snapshot = {
            r.id: {"no": getattr(r, "no", 0), "source_type": r.source_type,
                   "keywords": list(r.keywords), "condition": r.condition,
                   "action": r.action, "guidance": getattr(r, "guidance", ""),
                   "match_level": getattr(r, "match_level", "metaphor"),
                   "regex": getattr(r, "regex", "")}
            for r in trainable_rules
        }
        ts.status = "training"
        ts.started_at = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
        ts.completed_at = ""
        ts.training_result = {}
        self._ts_repo.add(ts, by=by)
        return ts

    def run_training(self, project_id: str, by: str = "") -> TrainingSet:
        """执行训练循环(在后台线程中调用)。"""
        ts = self._ts_repo.get_by_project(project_id)
        if ts is None or ts.status != "training":
            return ts

        examples = self._te_repo.list_for_set(ts.id)
        ground_truth: dict[str, set[str]] = {
            e.material_id: set(e.expected_rule_ids) for e in examples
        }

        # 加载待训练规则:项目规则 + 全局规则
        trainable_rules = self._trainable_rules(project_id)
        rule_by_id = {r.id: r for r in trainable_rules}

        max_iter = ts.max_iterations
        max_fp = ts.max_fp_ratio
        all_changes: list[dict] = []
        final_metrics: dict = {}
        converged = False

        for iteration in range(1, max_iter + 1):
            # 1) 用当前规则重审所有训练样本物料
            current_results: dict[str, set[str]] = {}
            for material_id in ground_truth:
                triggered = self._reaudit_material(material_id, project_id)
                current_results[material_id] = triggered

            # 2) 对比 ground_truth → 计算指标
            metrics = self._calc_metrics(ground_truth, current_results, rule_by_id)
            final_metrics = metrics

            # 3) 收敛判定
            if metrics["missed_hits"] == 0:
                fp_ratio = metrics["fp_ratio"]
                if fp_ratio <= max_fp:
                    converged = True
                    break

            # 4) AI 逐规则分析并调整
            iter_changes: list[dict] = []
            per_rule = metrics.get("per_rule", {})
            for rid, rm in per_rule.items():
                if rm["missed"] == 0 and rm["extra"] == 0:
                    continue  # 该规则完美,跳过
                rule = rule_by_id.get(rid)
                if rule is None:
                    continue
                # 收集该规则的漏判/多判案例
                missed_cases = self._collect_cases(
                    rid, ground_truth, current_results, missing=True)
                extra_cases = self._collect_cases(
                    rid, ground_truth, current_results, missing=False)
                try:
                    change = self._ai_adjust_rule(rule, missed_cases, extra_cases)
                    if change:
                        self._apply_change(rule, change, by)
                        change["rule_id"] = rid
                        change["rule_no"] = getattr(rule, "no", 0)
                        iter_changes.append(change)
                except Exception:
                    pass  # 单条规则调整失败不阻塞整体

            all_changes.extend(iter_changes)

            # 5) 重新加载规则(可能有 AI 调整)
            trainable_rules = self._trainable_rules(project_id)
            rule_by_id = {r.id: r for r in trainable_rules}

        # 写训练结果
        import datetime
        ts.training_result = {
            "iterations": iteration,
            "converged": converged,
            "final_metrics": final_metrics,
            "rule_changes": all_changes,
        }
        ts.status = "completed" if converged else "failed"
        ts.completed_at = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
        self._ts_repo.add(ts, by=by)
        return ts

    # ── 内部方法 ──

    def _trainable_rules(self, project_id: str) -> list:
        """待训练规则:项目的项目规则 + 全部全局规则。"""
        return [r for r in self._rules.list()
                if getattr(r, "project_id", "") in ("", project_id)]

    def _reaudit_material(self, material_id: str, project_id: str) -> set[str]:
        """对单个物料用当前规则重新审核,返回命中规则 ID 集合。无现有报告则跑完整审核。"""
        m = self._repo.get(material_id)
        if m is None:
            return set()
        rid = getattr(m, "audit_report_id", "")
        old = self._reports.get(rid) if rid else None
        job = AuditJob(id="", material_type=m.type, oss_key=m.oss_key,
                       owner_id=m.owner_id, material_id=material_id,
                       video_kind=("work" if getattr(m, "project_id", "") else "material"),
                       project_id=project_id)
        try:
            if old is not None:
                report = self._audit.recheck(job, old)
            else:
                # 无现有报告 → 跑完整审核
                report = self._audit.run(job, getattr(m, "description", ""))
        except Exception:
            return set()
        return {t.get("rule_id", "") for t in report.triggered
                if t.get("rule_id") and t["rule_id"] not in ("blockword", "content-safety", "")}

    @staticmethod
    def _calc_metrics(ground_truth: dict[str, set[str]],
                      current: dict[str, set[str]],
                      rule_by_id: dict[str, AuditRule]) -> dict:
        """计算漏判/多判指标。"""
        total_expected = 0
        total_actual = 0
        total_missed = 0
        total_extra = 0
        per_rule: dict[str, dict] = {}

        for rid in rule_by_id:
            expected = sum(1 for gt in ground_truth.values() if rid in gt)
            actual = sum(1 for cur in current.values() if rid in cur)
            missed = sum(1 for mid, gt in ground_truth.items()
                        if rid in gt and rid not in current.get(mid, set()))
            extra = sum(1 for mid, cur in current.items()
                       if rid in cur and rid not in ground_truth.get(mid, set()))
            per_rule[rid] = {"expected": expected, "actual": actual,
                             "missed": missed, "extra": extra}
            total_expected += expected
            total_actual += actual
            total_missed += missed
            total_extra += extra

        fp_ratio = (total_extra / total_expected) if total_expected > 0 else 0.0
        return {
            "total_materials": len(ground_truth),
            "total_expected_hits": total_expected,
            "actual_hits": total_actual,
            "missed_hits": total_missed,
            "extra_hits": total_extra,
            "fp_ratio": round(fp_ratio, 4),
            "per_rule": per_rule,
        }

    @staticmethod
    def _collect_cases(rule_id: str, ground_truth: dict[str, set[str]],
                       current: dict[str, set[str]], missing: bool) -> list[dict]:
        """收集某规则的漏判(missing=True)或多判(missing=False)案例。
        返回 [{material_id, description, segments_text}] 供 AI 分析。"""
        cases: list[dict] = []
        for mid, gt in ground_truth.items():
            cur = current.get(mid, set())
            if missing and rule_id in gt and rule_id not in cur:
                cases.append({"material_id": mid})
            elif not missing and rule_id in cur and rule_id not in gt:
                cases.append({"material_id": mid})
        return cases[:10]  # 每规则最多传 10 个案例,防 prompt 过长

    def _ai_adjust_rule(self, rule: AuditRule,
                        missed_cases: list[dict],
                        extra_cases: list[dict]) -> dict | None:
        """AI 分析一条规则的漏判/多判案例,返回调整建议。无漏判无多判→None。"""
        if not missed_cases and not extra_cases:
            return None

        # 构建案例描述
        missed_text = self._build_cases_text(missed_cases, "漏判")
        extra_text = self._build_cases_text(extra_cases, "多判")

        rule_desc = (
            f"规则编号:{getattr(rule, 'no', 0)}\n"
            f"来源类型:{rule.source_type}\n"
            f"当前关键词:{rule.keywords}\n"
            f"当前条件:{rule.condition}\n"
            f"当前尺度说明:{getattr(rule, 'guidance', '')}\n"
            f"当前严格程度:{getattr(rule, 'match_level', 'metaphor')}"
        )

        user = (
            f"【当前规则】\n{rule_desc}\n\n"
            f"{missed_text}\n{extra_text}\n\n"
            "请分析原因并给出调整后的规则配置(JSON)。"
        )

        try:
            out = self._llm.chat_json(_RULE_ADJUST_SYS, user)
        except Exception:
            return None

        if not isinstance(out, dict) or not out:
            return None

        return {
            "analysis": str(out.get("analysis", ""))[:200],
            "keywords": self._norm_strlist(out.get("keywords")),
            "condition": str(out.get("condition", "")).strip(),
            "guidance": str(out.get("guidance", "")).strip(),
            "match_level": self._norm_match_level(str(out.get("match_level", ""))),
        }

    def _apply_change(self, rule: AuditRule, change: dict, by: str) -> None:
        """把 AI 调整应用到规则并持久化。只改 keywords/condition/guidance/match_level。"""
        if "keywords" in change:
            rule.keywords = change["keywords"]
        if "condition" in change:
            rule.condition = change["condition"]
        if "guidance" in change:
            rule.guidance = change["guidance"]
        if "match_level" in change:
            rule.match_level = change["match_level"]
        self._rules.add(rule, by=by)

    def _build_cases_text(self, cases: list[dict], label: str) -> str:
        """构建漏判/多判案例的描述文本,包含物料摘要和审核内容(截断)。"""
        if not cases:
            return f"【{label}案例】无"
        lines = [f"【{label}案例】共 {len(cases)} 个:"]
        for i, c in enumerate(cases[:5], 1):  # 最多展示 5 个详细案例
            mid = c.get("material_id", "")
            m = self._repo.get(mid)
            if m is None:
                lines.append(f"  {i}. 物料 {mid} (已删除)")
                continue
            # 获取物料描述/反解内容
            desc = getattr(m, "description", "") or ""
            ai_summary = getattr(m, "ai_summary", "") or ""
            text = (ai_summary or desc)[:300]
            lines.append(f"  {i}. 物料 {mid} [{m.type.value}] {text}")
        return "\n".join(lines)

    @staticmethod
    def _norm_strlist(v, cap: int = 30) -> list[str]:
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            return []
        out = [str(x).strip() for x in v if isinstance(x, (str, int)) and str(x).strip()]
        return list(dict.fromkeys(out))[:cap]

    @staticmethod
    def _norm_match_level(v: str) -> str:
        return v if v in ("literal", "regex") else "metaphor"


class TrainingError(Exception):
    """训练前置条件不满足。"""

"""规则训练服务 —— 基于人工标注样本,AI 迭代调优项目规则直到收敛。

流程:
1. 快照当前项目规则
2. 用当前规则重审所有训练样本物料 → 与实际命中对比
3. AI 逐规则分析漏判/多判原因 → 调整 keywords/condition/guidance
4. 重新审核 → 重复直到 漏判=0 且 多判率≤max_fp_ratio 或达到最大迭代次数
"""
from __future__ import annotations
import logging
from typing import Optional
from app.domain.models import TrainingSet, TrainingExample, AuditJob, MaterialType, AuditRule
from app.domain.ports import TrainingSetRepo, TrainingExampleRepo, AuditRuleRepo, MaterialRepo
from app.infrastructure.snowflake import next_id_str

logger = logging.getLogger(__name__)

# ── AI 规则调优系统提示词 ──
_RULE_ADJUST_SYS = (
    "你是审核规则调优引擎。你会收到一条审核规则的当前配置，以及它在实际审核中的表现数据"
    "（哪些物料漏判了——应该命中但没命中；哪些物料多判了——不应该命中却命中了）。"
    "你的任务是分析原因并给出微调后的规则配置，使规则能准确命中该命中的物料、避免误命中不该命中的物料。\n\n"
    "核心原则：**最小改动**。规则已经过人工精心配置，你只需要做最少量的调整来修复具体问题，"
    "不要推翻重写、不要改变规则的核心意图。\n\n"
    "## condition 与 guidance 的分工\n"
    "- **condition（条件）**：简略达意，一句话说清这条规则要拦截什么。只写核心判定逻辑，"
    "不展开列举场景、不写例子、不写「包括但不限于」式的枚举。像法典条文——定义边界，不啰嗦。\n"
    "- **guidance（尺度）**：承载所有细节。反例（什么不算违规）、边界情形（模糊地带怎么判）、"
    "典型场景举例、容易误判的情况及处理方式——全部放在 guidance 里。尺度越详细，大模型判定越准。\n\n"
    "微调策略:\n"
    "- 漏判 → 在现有 keywords 末尾追加 1-3 个漏判物料中出现的同义词/近义表述（保留全部原有关键词）；"
    "如果漏判是因为 condition 没覆盖到漏判物料的典型表述，在 condition 末尾补半句限定（≤15字），"
    "同时在 guidance 里展开说明新增覆盖的场景。不要改写整段 condition。\n"
    "- 多判 → 在 guidance 末尾追加 1-2 条反例（什么不算违规、什么情况应放行），不要改动原有的 guidance 内容。"
    "如果多判暴露出 condition 措辞过于宽泛，可以收紧 condition（删减冗余限定词），但优先改 guidance。\n"
    "- match_level 尽量保持原值不变，除非明确判断出严格程度确实用错了才改。\n"
    "- 如果分析后认为规则本身没问题、只是案例特殊，返回原有配置不变，在 analysis 中说明原因。\n\n"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释,字段:\n"
    "analysis(中文,简要分析漏判/多判的原因,≤150字),\n"
    "keywords(在原有 keywords 基础上微调后的关键词数组,尽量保留原有、末尾追加少量新词),\n"
    "condition(微调后的条件,简略达意,一句话说清拦截什么,不展开场景/例子/枚举),\n"
    "guidance(微调后的尺度说明,承载所有细节:反例、边界情形、典型场景、易误判情况及处理方式),\n"
    "match_level(保持原值,仅当严格程度明显用错时才建议修改:literal/metaphor/regex)。"
)


class TrainingService:
    def __init__(self, training_set_repo: TrainingSetRepo,
                 training_example_repo: TrainingExampleRepo,
                 rule_repo: AuditRuleRepo, material_repo: MaterialRepo,
                 report_repo, task_repo, audit_pipeline, llm) -> None:
        self._ts_repo = training_set_repo
        self._te_repo = training_example_repo
        self._rules = rule_repo
        self._repo = material_repo
        self._reports = report_repo
        self._tasks = task_repo
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

    def update_example(self, project_id: str, example_id: str,
                       expected_rule_ids: list[str] | None = None,
                       source_note: str | None = None, by: str = "") -> TrainingExample:
        """编辑训练样本:修改预期规则或备注。校验样本存在且属于该项目。"""
        ts = self._ts_repo.get_by_project(project_id)
        if ts is None:
            raise TrainingError("训练集不存在")
        te = self._te_repo.get(example_id)
        if te is None:
            raise TrainingError(f"样本 {example_id} 不存在")
        if te.training_set_id != ts.id:
            raise TrainingError(f"样本不属于该项目")
        if expected_rule_ids is not None:
            te.expected_rule_ids = list(dict.fromkeys(expected_rule_ids))
        if source_note is not None:
            te.source_note = source_note
        self._te_repo.add(te, by=by)
        return te

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

        logger.info(
            "训练启动: project=%s, samples=%d, rules=%d, max_iter=%d, max_fp=%.2f, by=%s",
            project_id, len(examples), len(trainable_rules),
            ts.max_iterations, ts.max_fp_ratio, by,
        )

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
            logger.warning("训练跳过: project=%s, ts=%s", project_id,
                           "None" if ts is None else ts.status)
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
        iteration = 0

        logger.info(
            "训练开始: project=%s, samples=%d, rules=%d, max_iter=%d, max_fp=%.2f",
            project_id, len(ground_truth), len(trainable_rules), max_iter, max_fp,
        )

        try:
            for iteration in range(1, max_iter + 1):
                logger.info("--- 迭代 %d/%d ---", iteration, max_iter)

                # 1) 用当前规则重审所有训练样本物料
                current_results: dict[str, set[str]] = {}
                for material_id in ground_truth:
                    triggered = self._reaudit_material(material_id, project_id)
                    current_results[material_id] = triggered

                # 2) 对比 ground_truth → 计算指标
                metrics = self._calc_metrics(ground_truth, current_results, rule_by_id)
                final_metrics = metrics
                logger.info(
                    "迭代 %d 指标: materials=%d, expected_hits=%d, actual_hits=%d, "
                    "missed=%d, extra=%d, fp_ratio=%.4f",
                    iteration, metrics["total_materials"], metrics["total_expected_hits"],
                    metrics["actual_hits"], metrics["missed_hits"], metrics["extra_hits"],
                    metrics["fp_ratio"],
                )

                # 3) 收敛判定
                if metrics["missed_hits"] == 0:
                    fp_ratio = metrics["fp_ratio"]
                    if fp_ratio <= max_fp:
                        logger.info(
                            "收敛: iteration=%d, missed=0, fp_ratio=%.4f ≤ %.4f",
                            iteration, fp_ratio, max_fp,
                        )
                        converged = True
                        break
                    else:
                        logger.info(
                            "未收敛(多判超标): iteration=%d, fp_ratio=%.4f > %.4f, 继续调整",
                            iteration, fp_ratio, max_fp,
                        )

                # 4) AI 逐规则分析并调整
                iter_changes: list[dict] = []
                per_rule = metrics.get("per_rule", {})
                rules_with_issues = sum(1 for rm in per_rule.values()
                                       if rm["missed"] > 0 or rm["extra"] > 0)
                logger.info("迭代 %d: %d 条规则需要调整", iteration, rules_with_issues)

                for rid, rm in per_rule.items():
                    if rm["missed"] == 0 and rm["extra"] == 0:
                        continue  # 该规则完美,跳过
                    rule = rule_by_id.get(rid)
                    if rule is None:
                        continue
                    rule_no = getattr(rule, "no", 0)
                    logger.info(
                        "  规则 #%d: missed=%d, extra=%d → 调用 AI 调整...",
                        rule_no, rm["missed"], rm["extra"],
                    )
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
                            change["rule_no"] = rule_no
                            iter_changes.append(change)
                            logger.info(
                                "  规则 #%d 已调整: %s",
                                rule_no, change.get("analysis", "")[:100],
                            )
                        else:
                            logger.info(
                                "  规则 #%d AI 未返回调整建议,跳过", rule_no,
                            )
                    except Exception as e:
                        logger.warning(
                            "  规则 #%d 调整异常(不阻塞整体): %s", rule_no, e,
                        )

                if not iter_changes:
                    logger.info(
                        "迭代 %d: 本轮无规则变更,提前结束", iteration,
                    )
                    break

                all_changes.extend(iter_changes)

                # 5) 重新加载规则(可能有 AI 调整)
                trainable_rules = self._trainable_rules(project_id)
                rule_by_id = {r.id: r for r in trainable_rules}

        except Exception as e:
            logger.exception("训练异常终止: project=%s, error=%s", project_id, e)
            final_metrics = final_metrics or {}
            final_metrics["error"] = str(e)
            converged = False

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

        logger.info(
            "训练完成: project=%s, status=%s, iterations=%d, converged=%s, "
            "total_changes=%d, final_fp_ratio=%.4f",
            project_id, ts.status, iteration, converged,
            len(all_changes), final_metrics.get("fp_ratio", 0),
        )
        return ts

    # ── 内部方法 ──

    def _trainable_rules(self, project_id: str) -> list:
        """待训练规则:项目的项目规则 + 全部全局规则。"""
        return [r for r in self._rules.list()
                if getattr(r, "project_id", "") in ("", project_id)]

    def _reaudit_material(self, material_id: str, project_id: str) -> set[str]:
        """对单个物料用当前规则重新审核,返回命中规则 ID 集合。无现有报告则跑完整审核。
        recheck 后同步关联 audit_task 的 report_id/verdict,确保「待审核任务」页与训练结论一致。"""
        import datetime
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
        except Exception as e:
            logger.debug("物料 %s 重审失败: %s", material_id, e)
            return set()
        # recheck/run 内部 _persist 已更新 material.audit_report_id;
        # 同步关联 audit_task,避免「待审核任务」页仍指向旧报告。
        self._sync_task_after_recheck(material_id)
        return {t.get("rule_id", "") for t in report.triggered
                if t.get("rule_id") and t["rule_id"] not in ("blockword", "content-safety", "")}

    def _sync_task_after_recheck(self, material_id: str) -> None:
        """按物料 ID 找到关联 AuditTask,把 report_id / verdict / status 同步到最新。
        best-effort:没有任务或找不到就跳过。"""
        try:
            m = self._repo.get(material_id)
            if m is None:
                return
            new_rid = getattr(m, "audit_report_id", "")
            if not new_rid:
                return
            import datetime
            # task_repo 无 material_id 索引,扫全部匹配(训练量小,<100 条)
            for t in self._tasks.list_all():
                if getattr(t, "material_id", "") != material_id:
                    continue
                if getattr(t, "del_flag", 0) != 0:
                    continue
                t.report_id = new_rid
                t.verdict = getattr(m, "audit_status", "review")
                t.status = "done"
                t.report_generated_at = datetime.datetime.now(
                    datetime.timezone(datetime.timedelta(hours=8))
                ).isoformat()
                self._tasks.save(t)
                break   # 一个物料只对应一个任务
        except Exception as e:
            logger.debug("同步 audit_task 失败(material=%s): %s", material_id, e)

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
            f"当前条件(简略):{rule.condition}\n"
            f"当前尺度(详细):{getattr(rule, 'guidance', '')}\n"
            f"当前严格程度:{getattr(rule, 'match_level', 'metaphor')}"
        )

        user = (
            f"【当前规则】\n{rule_desc}\n\n"
            f"{missed_text}\n{extra_text}\n\n"
            "请分析原因并给出调整后的规则配置(JSON)。"
        )

        try:
            out = self._llm.chat_json(_RULE_ADJUST_SYS, user)
        except Exception as e:
            logger.warning(
                "AI 调整规则 #%d 调用失败: %s", getattr(rule, "no", 0), e,
            )
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

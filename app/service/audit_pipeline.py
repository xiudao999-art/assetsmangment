"""多模态内容审核引擎(只依赖 domain 端口)。
核心:任意素材 → 按类型走链路 → List[TextSegment](带来源类型的文字) →
关键词快筛 + qwen-plus 按规则兜底判 → 取最严 → 审核报告。
视频链路里截出的帧顺带自动存为可复用物料。"""
from __future__ import annotations
import uuid
from typing import Optional

from app.domain.models import (
    MaterialType, AuditStatus, JobStatus, TextSourceType,
    TextSegment, AuditReport, AuditJob, Material, MaterialCandidate,
)

_SRC_CN = {
    TextSourceType.ORIGINAL_TEXT: "上传原文",
    TextSourceType.TRANSCRIPT: "语音转写",
    TextSourceType.IMAGE_CONTENT: "图像反解画面",
    TextSourceType.VIDEO_FRAME: "视频关键帧画面",
}


def _act_cn(v: str) -> str:
    return "拦截" if v == "block" else "待复核"

_JUDGE_SYS = (
    "你是内容审核引擎。根据管理员定义的规则,逐条判断给定文本是否违规。"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释。"
    "字段:decision(只能是 pass、review 或 block),"
    "triggered_rule_ids(命中的规则编号数组,未命中为空数组),"
    "reason(中文,简述判定依据)。block=明确违规;review=疑似需人工复核;pass=无问题。"
)
_MOMENT_SYS = (
    "你是视频审核助理。给你一段带毫秒时间轴的语音转写,"
    "请挑出最多 5 个「可能需要结合画面才能判断是否违规」的重点时间段。"
    "只返回一个合法 JSON 对象:{\"moments_ms\":[毫秒整数数组]}。"
)
_MOMENT_RULE_SYS = (
    "你是视频内容审核助理。下面给你【管理员配置的画面审核规则】和一段【带毫秒时间轴的语音转写】。"
    "请结合语义反推:视频画面在哪些重点时间段/时间点最可能出现与这些规则相关的内容"
    "(例如规则涉及的物品、场景、行为、人物或画面文字),需要抽取该处画面来核验。"
    "只围绕规则涉及的风险方向选择,不要泛泛而选;拿不准就不选。最多返回 8 个时间点。"
    "只返回一个合法 JSON 对象:{\"moments_ms\":[毫秒整数数组]},不要 markdown、不要解释。"
)
_WORK_MAX_FRAMES = 12          # 作品单条抽帧上限(Qwen-VL + 内容安全逐帧成本封顶)
_WORK_NET_INTERVAL_MS = 20000  # 作品安全网:每 ~20s 均匀补一帧(覆盖静音段 + 常开黄暴政硬拦)
_SUMMARY_SYS = (
    "你是物料档案摘要引擎。根据一条物料解析出的文字内容,提炼它的可复用档案。"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释,字段:"
    "summary(这个物料是什么、包含什么内容,一两句中文),"
    "scene(适合的使用场景,如 开场/转场/结尾/产品介绍/情感渲染 等),"
    "emotion(表达的情绪,如 温馨/欢快/紧张/悲伤/激昂/治愈 等,简短),"
    "atmosphere(营造的氛围,如 宁静/热闹/神秘/高级感/复古/科技感 等,简短),"
    "tags(3~6 个便于检索的中文关键词标签数组)。"
)


class AuditPipelineService:
    def __init__(self, transcriber, vision, llm, rule_repo, report_repo,
                 storage, material_repo, embedder, index, auditor=None) -> None:
        self._transcriber = transcriber
        self._vision = vision
        self._llm = llm
        self._rules = rule_repo
        self._reports = report_repo
        self._storage = storage
        self._repo = material_repo
        self._embedder = embedder
        self._index = index
        self._auditor = auditor  # 阿里云内容安全硬拦兜底(可选);假实现时恒 pass 无影响

    def submit(self, material_type: MaterialType, oss_key: str = "",
               owner_id: str = "", material_id: str = "",
               video_kind: str = "material") -> AuditJob:
        return AuditJob(id=uuid.uuid4().hex, material_type=material_type, oss_key=oss_key,
                        owner_id=owner_id, material_id=material_id,
                        video_kind=video_kind, status=JobStatus.RUNNING)

    # ── 主流程 ──
    def run(self, job: AuditJob, text: str = "") -> AuditReport:
        try:
            segments = self._to_segments(job, text)
            report = self._evaluate(job, segments)
            job.status = JobStatus.DONE
        except Exception as e:  # 审核异常不放行,转人工
            report = AuditReport(verdict=AuditStatus.REVIEW, segments=[], triggered=[],
                                 summary=f"审核过程异常,转人工复核:{e}")
            job.status = JobStatus.FAILED
        return self._persist(job, report, summary_segments=report.segments)  # 首审顺带生成 AI 摘要

    def recheck(self, job: AuditJob, old_report: AuditReport) -> AuditReport:
        """只重判:用**当前**白名单/规则对已存报告的 segments 重新评估。
        不调 _to_segments → 不重转写、不重抽帧、不重复生成帧素材、不重跑摘要(已有)。"""
        try:
            report = self._evaluate(job, old_report.segments)
            job.status = JobStatus.DONE
        except Exception as e:
            report = AuditReport(verdict=AuditStatus.REVIEW, segments=old_report.segments, triggered=[],
                                 summary=f"重新审核异常,转人工复核:{e}")
            job.status = JobStatus.FAILED
        return self._persist(job, report)   # summary_segments=None → 不重跑摘要

    def _evaluate(self, job: AuditJob, segments: list[TextSegment]) -> AuditReport:
        """纯评估(不抽帧/不转写):关键词快筛 + 大模型判 + 内容安全 → 取最严 → 报告。"""
        triggered = (self._prefilter(segments) + self._llm_judge(segments)
                     + self._content_safety(job, segments))
        verdict = self._combine(triggered)
        summary = self._summary(verdict, triggered)
        return AuditReport(verdict=verdict, segments=segments, triggered=triggered, summary=summary)

    def _persist(self, job: AuditJob, report: AuditReport, summary_segments=None) -> AuditReport:
        """存报告 + 回写物料 audit_status/audit_report_id;summary_segments 非 None 时顺带生成 AI 摘要。"""
        report_id = uuid.uuid4().hex
        self._reports.save(report_id, report)
        if job.material_id:
            m = self._repo.get(job.material_id)
            if m is not None:
                m.audit_status = report.verdict
                m.audit_report_id = report_id
                if summary_segments is not None:
                    self._apply_summary(m, summary_segments)
                self._repo.save(m)
        job.report = report
        return report

    # ── AI 摘要(情绪/氛围/场景/标签)+ 按摘要重嵌入(情绪氛围可搜)──
    def summarize_material(self, material) -> None:
        """对一条物料按需生成摘要(重新解析内容)。供批量导入未审核的物料补摘要。"""
        job = self.submit(material.type, oss_key=material.oss_key,
                          material_id=material.id, owner_id=material.owner_id)
        segments = self._to_segments(job, material.description)
        self._apply_summary(material, segments)
        self._repo.save(material)

    def _generate_summary(self, mtype: MaterialType, segments: list[TextSegment]) -> dict:
        text = "\n".join(s.text for s in segments if s.text)[:6000]
        if not text.strip():
            return {}
        return self._llm.chat_json(_SUMMARY_SYS,
                                   f"物料类型:{mtype.value}\n解析内容:\n{text}\n\n请以 json 返回档案摘要。")

    def _apply_summary(self, m: Material, segments: list[TextSegment]) -> None:
        try:
            s = self._generate_summary(m.type, segments)
        except Exception:
            return
        if not isinstance(s, dict) or not s:
            return
        m.ai_summary = (s.get("summary") or "").strip()
        m.ai_scene = (s.get("scene") or "").strip()
        m.ai_emotion = (s.get("emotion") or "").strip()
        m.ai_atmosphere = (s.get("atmosphere") or "").strip()
        ai_tags = [t.strip() for t in (s.get("tags") or []) if isinstance(t, str) and t.strip()]
        m.tags = list(dict.fromkeys(list(m.tags or []) + ai_tags))[:12]  # 合并去重,保留用户已有标签
        # 用「摘要+情绪+氛围」重嵌入 → 语义搜索能按情绪/氛围命中
        try:
            rich = f"{m.ai_summary} 场景:{m.ai_scene} 情绪:{m.ai_emotion} 氛围:{m.ai_atmosphere} 标签:{' '.join(m.tags)}"
            vec = self._embedder.embed(MaterialCandidate(type=m.type, thumb=m.thumb,
                                                         source_timecode=0.0, description=rich))
            m.embedding = vec
            self._index.add(m.id, vec)
        except Exception:
            pass

    # ── 各素材 → 文字段 ──
    def _to_segments(self, job: AuditJob, text: str) -> list[TextSegment]:
        t = job.material_type
        if t in (MaterialType.CORPUS,) or (t == MaterialType.IMAGE and not job.oss_key):
            return [TextSegment(TextSourceType.ORIGINAL_TEXT, (text or "").strip())]
        if t in (MaterialType.IMAGE, MaterialType.MEME, MaterialType.STYLE):
            desc = self._vision.describe_image(self._storage.signed_url(job.oss_key))
            return [TextSegment(TextSourceType.IMAGE_CONTENT, desc)]
        if t in (MaterialType.AUDIO, MaterialType.MUSIC):
            return self._transcriber.transcribe(self._storage.signed_url(job.oss_key))
        if t == MaterialType.VIDEO:
            return self._video_segments(job)
        # 兜底:当作原文
        return [TextSegment(TextSourceType.ORIGINAL_TEXT, (text or "").strip())]

    @staticmethod
    def _material_moments(dur_ms) -> list[int]:
        """物料(≤20s 短片)分级抽帧:
        ≤5s → 1 帧(中间);≤10s → 2 帧(~33%/66%);≤20s → 3 帧(~25/50/75%)。
        拿不到时长 → 保守取前中 1 帧(短片安全)。天然封顶 3 帧。"""
        if not dur_ms or dur_ms <= 0:
            return [500]
        if dur_ms <= 5000:
            return [int(dur_ms * 0.50)]
        if dur_ms <= 10000:
            return [int(dur_ms * 0.33), int(dur_ms * 0.66)]
        return [int(dur_ms * 0.25), int(dur_ms * 0.50), int(dur_ms * 0.75)]

    @staticmethod
    def _safety_net(dur_ms) -> list[int]:
        """作品均匀安全网:每 ~20s 一帧(封顶 _WORK_MAX_FRAMES,避开首尾),
        覆盖无语音提示的静音段 + 常开的内容安全黄暴政硬拦。"""
        if not dur_ms or dur_ms <= 0:
            return [1000]
        n = max(1, min(_WORK_MAX_FRAMES, round(dur_ms / _WORK_NET_INTERVAL_MS)))
        if n == 1:
            return [int(dur_ms * 0.5)]
        step = dur_ms / (n + 1)
        return [int(step * (i + 1)) for i in range(n)]

    def _frame_rules_digest(self) -> str:
        """把「适用于视频关键帧」的审核规则(含 any)整理成清单,给 LLM 反推相关画面时刻。"""
        lines: list[str] = []
        for i, r in enumerate(self._rules.list_for(TextSourceType.VIDEO_FRAME.value), 1):
            parts = []
            if r.keywords:
                parts.append("关键词:" + "、".join(k for k in r.keywords if k))
            if r.condition.strip():
                parts.append("条件:" + r.condition.strip())
            if parts:
                lines.append(f"{i}. " + ";".join(parts))
        return "\n".join(lines)

    def _pick_visual_moments(self, transcript: list[TextSegment]) -> list[int]:
        """规则反推抽帧点(作品用)。有画面规则 → 让 LLM 依据规则在时间轴定位相关画面时刻;
        无画面规则 → 退回通用「可能需结合画面判定」的重点时刻。无转写/异常 → [](交安全网兜底)。"""
        if not transcript:
            return []
        tl = "\n".join(f"[{(s.begin_ms or 0)}ms] {s.text}" for s in transcript if s.text)
        digest = self._frame_rules_digest()
        try:
            if digest:
                out = self._llm.chat_json(
                    _MOMENT_RULE_SYS,
                    f"审核规则(画面):\n{digest}\n\n带毫秒时间轴的语音转写:\n{tl}\n\n请以 json 返回。")
            else:
                out = self._llm.chat_json(_MOMENT_SYS, f"语音转写(请返回 json):\n{tl}")
            ms = [int(x) for x in (out.get("moments_ms") or []) if str(x).strip() != ""]
            return ms[:_WORK_MAX_FRAMES]
        except Exception:
            return []

    def _work_moments(self, transcript: list[TextSegment], dur_ms) -> list[int]:
        """作品抽帧点 = 规则反推点 ∪ 均匀安全网,去重排序;超上限时优先保留规则命中点。"""
        net = self._safety_net(dur_ms)
        rule_pts = self._pick_visual_moments(transcript)   # 无转写→[];无规则→通用挑取;异常→[]
        merged = sorted(set(net) | set(rule_pts))
        if len(merged) <= _WORK_MAX_FRAMES:
            return merged
        keep = sorted(set(rule_pts))[:_WORK_MAX_FRAMES]
        for m in sorted(net):
            if len(keep) >= _WORK_MAX_FRAMES:
                break
            if m not in keep:
                keep.append(m)
        return sorted(keep)

    def _video_segments(self, job: AuditJob) -> list[TextSegment]:
        url = self._storage.signed_url(job.oss_key)
        transcript = self._transcriber.transcribe(url)          # 两条链路都转写音轨审核
        dur = self._storage.video_duration_ms(job.oss_key)
        is_work = (job.video_kind == "work")
        moments = self._work_moments(transcript, dur) if is_work else self._material_moments(dur)
        if dur:  # 钳制在时长内并去重,避免超时长截到同一最后帧
            moments = sorted({min(m, max(0, dur - 100)) for m in moments if m is not None})
        else:
            moments = sorted({m for m in moments if m is not None})
        frame_segs: list[TextSegment] = []
        for ms in moments:
            dest = f"frames/{job.oss_key.rsplit('/', 1)[-1]}-{uuid.uuid4().hex[:8]}.jpg"
            try:
                if not self._storage.snapshot_frame(job.oss_key, ms, dest):
                    continue
                fdesc = self._vision.describe_image(self._storage.signed_url(dest))
                frame_segs.append(TextSegment(TextSourceType.VIDEO_FRAME, fdesc,
                                              begin_ms=ms, frame_oss_key=dest))
                if not is_work:                              # 仅物料把帧存为可复用素材;作品只核验不入库
                    self._save_frame_material(dest, fdesc, ms / 1000.0, job)
            except Exception:
                continue
        merged = transcript + frame_segs
        merged.sort(key=lambda s: (s.begin_ms if s.begin_ms is not None else 0))
        return merged

    def _save_frame_material(self, oss_key: str, desc: str, timecode: float, job: AuditJob) -> None:
        try:
            cand = MaterialCandidate(type=MaterialType.IMAGE, thumb=oss_key,
                                     source_timecode=timecode, description=desc, oss_key=oss_key)
            emb = self._embedder.embed(cand)
            m = Material(id=uuid.uuid4().hex, type=MaterialType.IMAGE, thumb=oss_key,
                         source_timecode=timecode, embedding=emb, audit_status=AuditStatus.REVIEW,
                         source_job=job.id, oss_key=oss_key, description=desc, owner_id=job.owner_id)
            self._repo.save(m)
            self._index.add(m.id, emb)
        except Exception:
            pass  # 入库是副产物,失败不影响审核

    # ── 关键词快筛(纯逻辑)——命中项带上具体片段(文本/帧),供报告标红定位 ──
    def _prefilter(self, segments: list[TextSegment]) -> list[dict]:
        triggered: list[dict] = []
        for seg in segments:
            for rule in self._rules.list_for(seg.source_type.value):
                for kw in rule.keywords:
                    if kw and kw in seg.text:
                        triggered.append({"rule_id": rule.id, "source_type": seg.source_type.value,
                                          "action": rule.action, "reason": f"命中关键词「{kw}」",
                                          "text": seg.text, "begin_ms": seg.begin_ms,
                                          "frame_oss_key": seg.frame_oss_key})
                        break
        return triggered

    # ── 阿里云内容安全硬拦兜底(黄暴政治等,与规则引擎并存取最严)——命中项定位到具体片段/帧 ──
    def _content_safety(self, job: AuditJob, segments: list[TextSegment]) -> list[dict]:
        if self._auditor is None:
            return []
        import types

        def _run(oss_key: str, description: str) -> tuple[str, str]:
            """返回 (verdict, risk_words)。审核器可选实现 audit_detail 交出命中词;否则退回 audit()。"""
            try:
                obj = types.SimpleNamespace(oss_key=oss_key, description=description)
                fn = getattr(self._auditor, "audit_detail", None)
                return fn(obj) if fn is not None else (self._auditor.audit(obj), "")
            except Exception:
                return "review", ""  # 内容安全异常/超时 → 不放行

        triggered: list[dict] = []
        # 原图(图片物料):审像素 → 命中标出这张图(图片接口不返回具体词)
        if job.material_type in (MaterialType.IMAGE, MaterialType.MEME, MaterialType.STYLE) and job.oss_key:
            v, _ = _run(job.oss_key, "")
            if v in ("block", "review"):
                triggered.append({"rule_id": "content-safety", "source_type": TextSourceType.IMAGE_CONTENT.value,
                                  "action": v, "reason": f"图片画面被阿里云内容安全判为{_act_cn(v)}",
                                  "frame_oss_key": job.oss_key, "begin_ms": None, "text": ""})
        # 每个视频帧:逐帧审像素 → 命中定位到该帧(报告里显示这张帧图)
        for s in segments:
            if s.frame_oss_key:
                v, _ = _run(s.frame_oss_key, "")
                if v in ("block", "review"):
                    triggered.append({"rule_id": "content-safety", "source_type": TextSourceType.VIDEO_FRAME.value,
                                      "action": v, "reason": f"该视频帧画面被阿里云内容安全判为{_act_cn(v)}",
                                      "frame_oss_key": s.frame_oss_key, "begin_ms": s.begin_ms, "text": s.text})
        # 真实文本(原文/转写):先合并审一次;命中了再逐段定位到具体的那段文字 + 命中词(供标红/加白)
        text_segs = [s for s in segments if s.text and s.source_type in
                     (TextSourceType.ORIGINAL_TEXT, TextSourceType.TRANSCRIPT)]
        merged = "\n".join(s.text for s in text_segs)[:9000]
        if merged.strip():
            mv, mwords = _run("", merged)
            if mv in ("block", "review"):
                hit = False
                for s in text_segs:
                    vs, words = _run("", s.text[:6000])
                    if vs in ("block", "review"):
                        hit = True
                        triggered.append({"rule_id": "content-safety", "source_type": s.source_type.value,
                                          "action": vs, "reason": f"该{_SRC_CN.get(s.source_type, '文本')}片段被阿里云内容安全判为{_act_cn(vs)}",
                                          "text": s.text, "begin_ms": s.begin_ms, "risk_words": words})
                if not hit:  # 合并命中但逐段都不单独命中(上下文叠加)→ 记一条整体
                    triggered.append({"rule_id": "content-safety", "source_type": "text", "action": "review",
                                      "reason": "文本整体被阿里云内容安全判为待复核", "text": merged[:300],
                                      "begin_ms": None, "risk_words": mwords})
        return triggered

    # ── 大模型按自然语言规则兜底 ──
    def _llm_judge(self, segments: list[TextSegment]) -> list[dict]:
        by_src: dict[str, list[str]] = {}
        for seg in segments:
            if seg.text.strip():
                by_src.setdefault(seg.source_type.value, []).append(seg.text)
        triggered: list[dict] = []
        for src, texts in by_src.items():
            rules = [r for r in self._rules.list_for(src) if r.condition.strip()]
            if not rules:
                continue
            numbered = "\n".join(f"{i + 1}. {r.condition}" for i, r in enumerate(rules))
            body = "\n".join(texts)[:6000]
            try:
                out = self._llm.chat_json(
                    _JUDGE_SYS,
                    f"审核规则:\n{numbered}\n\n待审文本(来源:{src}):\n{body}\n\n请以 json 返回。")
            except Exception:
                # 判定失败 → 该组转人工
                triggered.append({"rule_id": "", "source_type": src, "action": "review",
                                  "reason": "大模型判定异常,转人工"})
                continue
            for rid in (out.get("triggered_rule_ids") or []):
                try:
                    rule = rules[int(rid) - 1]
                except (ValueError, IndexError, TypeError):
                    continue
                triggered.append({"rule_id": rule.id, "source_type": src, "action": rule.action,
                                  "reason": out.get("reason", "命中规则")})
        return triggered

    @staticmethod
    def _combine(triggered: list[dict]) -> AuditStatus:
        actions = {t.get("action") for t in triggered}
        if "block" in actions:
            return AuditStatus.BLOCK
        if "review" in actions:
            return AuditStatus.REVIEW
        return AuditStatus.PASS

    @staticmethod
    def _summary(verdict: AuditStatus, triggered: list[dict]) -> str:
        if verdict == AuditStatus.PASS:
            return "未命中任何审核规则,通过。"
        reasons = "; ".join(t.get("reason", "") for t in triggered if t.get("reason"))
        label = "拦截" if verdict == AuditStatus.BLOCK else "待人工复核"
        return f"{label}:{reasons}"

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
               owner_id: str = "", material_id: str = "") -> AuditJob:
        return AuditJob(id=uuid.uuid4().hex, material_type=material_type, oss_key=oss_key,
                        owner_id=owner_id, material_id=material_id, status=JobStatus.RUNNING)

    # ── 主流程 ──
    def run(self, job: AuditJob, text: str = "") -> AuditReport:
        try:
            segments = self._to_segments(job, text)
            triggered = (self._prefilter(segments) + self._llm_judge(segments)
                         + self._content_safety(job, segments))
            verdict = self._combine(triggered)
            summary = self._summary(verdict, triggered)
            report = AuditReport(verdict=verdict, segments=segments, triggered=triggered, summary=summary)
            job.status = JobStatus.DONE
        except Exception as e:  # 审核异常不放行,转人工
            report = AuditReport(verdict=AuditStatus.REVIEW, segments=[], triggered=[],
                                 summary=f"审核过程异常,转人工复核:{e}")
            job.status = JobStatus.FAILED

        report_id = uuid.uuid4().hex
        self._reports.save(report_id, report)
        if job.material_id:
            m = self._repo.get(job.material_id)
            if m is not None:
                m.audit_status = report.verdict
                m.audit_report_id = report_id
                self._apply_summary(m, self._safe_segments(report, job, text))  # 审核时顺带生成 AI 摘要
                self._repo.save(m)
        job.report = report
        return report

    @staticmethod
    def _safe_segments(report, job, text):
        # 审核成功→用报告里的 segments;异常兜底→尽量再取一次(失败则空)
        return report.segments

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
    def _sample_moments(dur_ms) -> list[int]:
        """按视频时长在其范围内均匀取抽帧时间点(避开首尾),避免超时长截到重复的最后一帧。"""
        if not dur_ms or dur_ms <= 0:
            return [500, 1000, 1500]        # 拿不到时长 → 只取前几秒(短视频安全)
        n = min(5, max(1, round(dur_ms / 2000)))
        lo, hi = dur_ms * 0.08, dur_ms * 0.92
        if n == 1:
            return [int((lo + hi) / 2)]
        step = (hi - lo) / (n - 1)
        return [int(lo + i * step) for i in range(n)]

    def _video_segments(self, job: AuditJob) -> list[TextSegment]:
        url = self._storage.signed_url(job.oss_key)
        transcript = self._transcriber.transcribe(url)
        dur = self._storage.video_duration_ms(job.oss_key)
        if transcript:
            moments = self._pick_visual_moments(transcript)
        else:
            moments = self._sample_moments(dur)  # 无语音→按真实时长均匀抽帧
        if dur:  # 钳制在时长内并去重,避免超时长截到同一最后帧
            moments = sorted({min(m, max(0, dur - 100)) for m in moments if m is not None})
        frame_segs: list[TextSegment] = []
        for ms in moments:
            dest = f"frames/{job.oss_key.rsplit('/', 1)[-1]}-{uuid.uuid4().hex[:8]}.jpg"
            try:
                if not self._storage.snapshot_frame(job.oss_key, ms, dest):
                    continue
                fdesc = self._vision.describe_image(self._storage.signed_url(dest))
                frame_segs.append(TextSegment(TextSourceType.VIDEO_FRAME, fdesc,
                                              begin_ms=ms, frame_oss_key=dest))
                self._save_frame_material(dest, fdesc, ms / 1000.0, job)  # 顺带自动入库
            except Exception:
                continue
        merged = transcript + frame_segs
        merged.sort(key=lambda s: (s.begin_ms if s.begin_ms is not None else 0))
        return merged

    def _pick_visual_moments(self, transcript: list[TextSegment]) -> list[int]:
        if not transcript:
            return []
        lines = "\n".join(
            f"[{(s.begin_ms or 0)}ms] {s.text}" for s in transcript if s.text
        )
        try:
            out = self._llm.chat_json(_MOMENT_SYS, f"语音转写(请返回 json):\n{lines}")
            ms_list = [int(x) for x in (out.get("moments_ms") or []) if str(x).strip() != ""]
            return ms_list[:5]
        except Exception:
            # 兜底:均匀取最多 3 个时间点
            times = [s.begin_ms for s in transcript if s.begin_ms is not None]
            if not times:
                return []
            step = max(1, len(times) // 3)
            return times[::step][:3]

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

    # ── 关键词快筛(纯逻辑)──
    def _prefilter(self, segments: list[TextSegment]) -> list[dict]:
        triggered: list[dict] = []
        for seg in segments:
            for rule in self._rules.list_for(seg.source_type.value):
                for kw in rule.keywords:
                    if kw and kw in seg.text:
                        triggered.append({"rule_id": rule.id, "source_type": seg.source_type.value,
                                          "action": rule.action, "reason": f"关键词命中「{kw}」"})
                        break
        return triggered

    # ── 阿里云内容安全硬拦兜底(黄暴政治等,与规则引擎并存取最严)──
    def _content_safety(self, job: AuditJob, segments: list[TextSegment]) -> list[dict]:
        if self._auditor is None:
            return []
        import types
        targets: list[tuple] = []
        if job.material_type in (MaterialType.IMAGE, MaterialType.MEME, MaterialType.STYLE) and job.oss_key:
            targets.append(("原图", types.SimpleNamespace(oss_key=job.oss_key, description="")))
        for s in segments:
            if s.frame_oss_key:
                targets.append((f"帧{s.begin_ms}ms", types.SimpleNamespace(oss_key=s.frame_oss_key, description="")))
        # 文本审核只审「真实内容」(原文/转写);AI 反解出的画面描述会点名"暴力/色情"等风险词,
        # 交给图片审核(直接审像素)即可,不能拿描述去过文本审核(否则正常图也被误判)。
        real_text = "\n".join(s.text for s in segments
                              if s.text and s.source_type in (TextSourceType.ORIGINAL_TEXT, TextSourceType.TRANSCRIPT))[:9000]
        if real_text.strip():
            targets.append(("文本", types.SimpleNamespace(oss_key="", description=real_text)))
        triggered: list[dict] = []
        for label, obj in targets:
            try:
                v = self._auditor.audit(obj)  # 'pass'/'review'/'block';FakePassAuditor 恒 pass
            except Exception:
                v = "review"  # 内容安全异常/超时 → 不放行
            if v in ("block", "review"):
                triggered.append({"rule_id": "content-safety", "source_type": label,
                                  "action": v, "reason": f"阿里云内容安全:{label}判为{v}"})
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

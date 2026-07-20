"""多模态内容审核引擎(只依赖 domain 端口)。
核心:任意素材 → 按类型走链路 → List[TextSegment](带来源类型的文字) →
三波级联判定:① 绝对禁词硬拦 ② 阿里云内容安全通用拦截 ③ 语义整篇审核(物料+规则各打包成文本交大模型整判) → 报告。
视频链路里截出的帧顺带自动存为可复用物料。"""
from __future__ import annotations
import uuid
from typing import Optional

from app.domain.models import (
    MaterialType, AuditStatus, JobStatus, TextSourceType,
    TextSegment, AuditReport, AuditJob, Material, MaterialCandidate, AuditRule,
)

_SRC_CN = {
    TextSourceType.ORIGINAL_TEXT: "上传原文",
    TextSourceType.TRANSCRIPT: "语音转写",
    TextSourceType.IMAGE_CONTENT: "图像反解画面",
    TextSourceType.VIDEO_FRAME: "视频关键帧画面",
}


def _act_cn(v: str) -> str:
    return "拦截" if v == "block" else "待复核"


# 物料打包文本里每段的类型标签(口播/画面/文案)
_MAT_LABEL = {
    TextSourceType.TRANSCRIPT: "口播", TextSourceType.VIDEO_FRAME: "画面",
    TextSourceType.IMAGE_CONTENT: "画面", TextSourceType.ORIGINAL_TEXT: "文案",
}
# 规则的 source_type(字符串)→ 规则清单里的「目标」中文
_RULE_TARGET_CN = {"any": "不限", "transcript": "口播", "image_content": "画面",
                   "video_frame": "画面", "original_text": "文案"}
# 规则的 source_type → 无法定位到具体段落时,triggered 项落的 source_type
_RULE_TARGET_SRC = {"any": "text", "transcript": TextSourceType.TRANSCRIPT.value,
                    "image_content": TextSourceType.IMAGE_CONTENT.value,
                    "video_frame": TextSourceType.VIDEO_FRAME.value,
                    "original_text": TextSourceType.ORIGINAL_TEXT.value}


def _fmt_source_type(raw: str) -> str:
    """逗号分隔的 source_type → 中文标签,如 video_frame,image_content → 画面"""
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    labels = {_RULE_TARGET_CN.get(p, p) for p in parts}
    return "/".join(sorted(labels)) if labels else "不限"


def _source_type_fallback(raw: str) -> str:
    """多值 source_type 取第一个匹配的 TextSourceType 用于整体 findings 定位;否则 text"""
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    for p in parts:
        if p in _RULE_TARGET_SRC and p != "any":
            return _RULE_TARGET_SRC[p]
    return "text"

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
    "你是短视频素材档案师。根据一条物料解析出的文字内容,提炼它的可复用检索档案。"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释,字段:"
    "summary(这个物料是什么、包含什么内容,一句中文),"
    "emotions(3~6 个它能表达的情绪词数组,如 温馨/欢快/紧张/无语/治愈),"
    "scenarios(3~6 条具体的「什么时候能用它」的使用情境句数组,如『开场需要活跃气氛时』『表达无奈又好笑时』),"
    "atmosphere(营造的氛围,如 宁静/热闹/神秘/高级感/复古/科技感 等,简短),"
    "tags(3~6 个便于检索的中文关键词标签数组)。"
    "情绪和场景要具体、能直接拿去匹配短视频片段的情绪/场景需求。"
)

# 大模型有时会在 findings 里输出"不违规"条目 → 代码兜底过滤
_FALSE_POSITIVE_TOKENS = ["不违规", "不构成违规", "不应命中", "不纳入", "不判违规",
                           "符合要求", "未违反", "可以放行", "不算命中",
                           "不符合", "不构成", "不属于", "不涉及",
                           "不触发", "不视为", "不命中", "不应计入"]


def _reason_says_pass(reason: str) -> bool:
    """大模型 reason 里写了不违规/符合要求 → 这条不该算 finding。"""
    return any(tok in reason for tok in _FALSE_POSITIVE_TOKENS)


def _norm_level(v) -> str:
    """严格程度归一:literal=字面、regex=正则(不走大模型)保留原值;其余(缺省/非法)→ metaphor(隐喻,安全默认)。"""
    return v if v in ("literal", "regex") else "metaphor"


# 正则规则编译:管理员写的自然语言 → 大模型提取关键词 + 构造一条正则(只在建/编辑规则时用一次,审核时不再调)
_REGEX_COMPILE_SYS = (
    "你是审核规则的正则编译器。管理员会用自然语言描述这条规则要拦什么(可能是一句话或几个词)。"
    "你要:① 提取其中的核心关键词;② 用这些关键词构造一条 Python 正则表达式,"
    "用 | 连接各关键词及其常见变体(谐音、错别字、字之间可能夹词/空格,如 躺.{0,2}赚、日\\s*入\\s*\\d+、稳\\s*赚)。"
    "只匹配真正该拦的表达,别写得过宽而误伤(不要用 .* 之类会命中一切的写法)。正则要能被 Python re 直接编译。"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释:{\"keywords\":[提取的关键词数组],\"regex\":\"构造的正则字符串\"}。"
)

# 音乐物料档案:歌名 + 联网检索资料 → 大模型合成「情绪/场景」多值配乐档案
_MUSIC_ARCHIVE_SYS = (
    "你是短视频配乐档案师。根据歌名和联网检索到的资料,总结这首歌可复用的配乐档案。"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释,字段:"
    "summary(一句话:这首歌是什么风格/主题),"
    "emotions(3~6 个它能烘托的情绪词数组,如 治愈/热血/怀旧/浪漫/悲伤/励志),"
    "scenarios(3~6 条具体的「适合配在什么短视频片段」情境句数组,"
    "如『旅行 vlog 的日落空镜』『情侣纪念日回忆杀』『健身冲刺高潮段』),"
    "atmosphere(整体氛围,简短),"
    "tags(3~6 个曲风/主题关键词数组,如 流行/民谣/电子/国风)。"
    "情绪和场景要具体、能直接拿去匹配短视频片段的配乐需求;资料不足就依据歌名合理推断。"
)


# 规则解析:把管理员粘贴的整篇「卡审/审核标准」文案拆成一条条结构化规则
_RULE_SOURCE_TYPES = {"any", "original_text", "transcript", "image_content", "video_frame"}
_RULE_PARSE_SYS = (
    "你是审核规则解析引擎。管理员会粘贴一整篇「卡审/审核标准」文案(针对某个项目的作品),"
    "你要把它拆解成一条条可执行的结构化规则。"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释,顶层字段 rules 是规则数组,每条规则字段:"
    "category(该规则所属分类,取文案里的小标题如 国家标志类/网赚风险类,没有就留空),"
    "source_type(检查哪种文字来源,可取:any=不限、original_text=上传原文语料、"
    "transcript=视频口播或音频转写、image_content=图片画面、video_frame=视频画面帧;"
    "支持逗号多选如 video_frame,image_content 表示同时检查视频帧和图片画面;"
    "明显只针对画面(国旗/二维码/人物形象/字幕水印)用 video_frame,image_content,只针对口播文案用 transcript,拿不准用 any),"
    "condition(必填,一句话清晰描述这条规则禁止或限制什么——审核靠它做语义判断,务必写清楚,不能留空),"
    "keywords(可选,列几个有代表性的参考词/示例词,仅供大模型参考、不做硬匹配,没有就给空数组),"
    "action(命中动作:block=直接拦截/拒审、review=转人工复核;"
    "明确违法违规/政治敏感/国家标志滥用用 block,需人工再看的疑似项用 review,拿不准用 review),"
    "match_level(严格程度:涉及国家政治/领导人/民族宗教/国旗国徽国歌等严重类填 metaphor=隐喻,连影射暗示谐音都要拦;"
    "其余一律填 literal=字面,只按表面意思拦、避免误伤)。"
    "把长长的敏感词清单按语义拆成若干条,每条聚焦一个主题、都写清 condition,单条 keywords 控制在 30 个以内。"
)

# 语义整篇审核:物料打包文本 + 规则清单文本 → 大模型整体判哪里违规
_RULE_JUDGE_SYS = (
    "你是作品内容审核引擎。下面给你【物料内容】(按段落编号 〖i〗 组织,含口播文字与画面描述,各带时间)"
    "和【审核规则清单】(每条含编号、目标、动作、严格程度〔字面/隐喻判定〕、说明,可能还带「尺度说明」和「已确认可放行的例外」)。请逐条对照,找出物料中所有违反规则之处。"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释,字段 findings 是数组,每个元素:"
    "rule(命中的规则编号,整数 —— 就是【审核规则清单】里每条最前面的那个数字,务必原样返回该数字、不要自己重排),"
    "segment(违规所在的物料段落编号,即某个 〖i〗 的整数;若无法定位到具体段落/整体判断则为 null),"
    "reason(中文,简述这里为什么违反该规则)。"
    "只标真实违规;同一处命中多条规则就各记一条;"
    "参考词只是方向示例,请按语义判断,不要机械按字匹配(例如「去」「来」「上」等常见字不要仅因出现就判违规);"
    "每条规则都标了【字面判定】或【隐喻判定】两种严格程度,务必严格区分、按对应标准判:"
    "【字面判定】= 只有当物料【直接、明确地说出/主张】了该规则禁止的那件事、其表面意思本身就构成违规,才算命中。"
    "凡是需要【结合上下文/语境去推断、由场景描述引申、暗示、隐含、联想、影射、谐音、隐喻、语义延伸、把某段话『归为/可理解为』某违规类别】才扯得上关系的,一律【不算命中(放行)】。"
    "自检:若你写命中理由时用到了『结合上下文/语境』『暗示』『隐含』『引申』『延伸』『可理解为』『属…类(表达)』这类措辞,就说明它并非字面直接违规 —— 放行。"
    "举例:规则禁『躺赚』,字面命中是话里直接宣称『躺着/不劳动就能赚到钱』这类主张;而只是描述一个『躺床上听歌、金币自己涨』的产品场景、需要你推断『这可理解为躺赚』的,属引申,放行。"
    "字面判定宁可漏、不可误伤(它也判「表面意思」而非机械逐字匹配参考词,但表面意思必须自身就违规)。"
    "【隐喻判定】= 除字面直接违反外,【影射、暗示、隐喻、谐音、代称、擦边、结合语境的引申】等间接表达也要揪出来算命中(仅用于国家政治/领导人/民族宗教/国家标志等严重项,隐晦也不放过)。"
    "规则若带「尺度说明」,按它把握违规程度、不要过严;若某处情形和该规则「已确认可放行的例外」里列的类似,则视为通过、不要标为违规;"
    "**极其重要**:如果你逐条对照后确认某条规则**未被违反**，**严禁在 findings 里输出对应条目**。"
    "不要输出理由为'不违规''不应命中''符合要求''通过'等否定判定的条目;findings 里每一条都必须是确认违规的。"
    "拿不准偏向标出交人工;没有任何违规时 findings 返回空数组。"
)


class AuditPipelineService:
    def __init__(self, transcriber, vision, llm, rule_repo, report_repo,
                 storage, material_repo, embedder, index, auditor=None, blockwords=None,
                 archiver=None, tavily=None) -> None:
        self._transcriber = transcriber
        self._vision = vision
        self._llm = llm
        self._archiver = archiver  # 物料档案器(豆包 pro 2.1);提情绪/场景多值标签;None=用 qwen 文本兜底
        self._tavily = tavily      # 联网搜索(Tavily);音乐物料按歌名联网搜情绪/场景;None=走文本兜底
        self._rules = rule_repo
        self._reports = report_repo
        self._storage = storage
        self._repo = material_repo
        self._embedder = embedder
        self._index = index
        self._auditor = auditor  # 阿里云内容安全硬拦兜底(可选);假实现时恒 pass 无影响
        self._blockwords = blockwords  # 绝对禁词读取回调 lambda:set(),第一波硬拦;None=无

    def submit(self, material_type: MaterialType, oss_key: str = "",
               owner_id: str = "", material_id: str = "",
               video_kind: str = "material", project_id: str = "") -> AuditJob:
        return AuditJob(id=uuid.uuid4().hex, material_type=material_type, oss_key=oss_key,
                        owner_id=owner_id, material_id=material_id,
                        video_kind=video_kind, project_id=project_id, status=JobStatus.RUNNING)

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
        画面反解(IMAGE_CONTENT/VIDEO_FRAME)用当前 vision 提示词重新生成;
        口播转写/原文复用。不重抽帧、不重复生成帧素材、不重跑摘要(已有)。"""
        try:
            segments = self._refresh_visual_segments(job, old_report.segments)
            report = self._evaluate(job, segments)
            job.status = JobStatus.DONE
        except Exception as e:
            report = AuditReport(verdict=AuditStatus.REVIEW, segments=old_report.segments, triggered=[],
                                 summary=f"重新审核异常,转人工复核:{e}")
            job.status = JobStatus.FAILED
        return self._persist(job, report)   # summary_segments=None → 不重跑摘要

    def _refresh_visual_segments(self, job: AuditJob, segments: list[TextSegment]) -> list[TextSegment]:
        """对画面类 segment 用当前 vision 模型重新反解(提示词可能已调整);
        口播转写/原文保持不变。单段失败保留原文,不阻塞整体。"""
        refreshed: list[TextSegment] = []
        for seg in segments:
            if seg.source_type == TextSourceType.IMAGE_CONTENT and job.oss_key:
                try:
                    new_desc = self._vision.describe_image(self._storage.signed_url(job.oss_key))
                    refreshed.append(TextSegment(TextSourceType.IMAGE_CONTENT, new_desc))
                except Exception:
                    refreshed.append(seg)   # 反解失败保留原文
            elif seg.source_type == TextSourceType.VIDEO_FRAME and seg.frame_oss_key:
                try:
                    new_desc = self._vision.describe_image(self._storage.signed_url(seg.frame_oss_key))
                    refreshed.append(TextSegment(TextSourceType.VIDEO_FRAME, new_desc,
                                                begin_ms=seg.begin_ms, frame_oss_key=seg.frame_oss_key))
                except Exception:
                    refreshed.append(seg)   # 反解失败保留原文
            else:
                refreshed.append(seg)       # 口播转写/原文:保持不变
        return refreshed

    def _evaluate(self, job: AuditJob, segments: list[TextSegment]) -> AuditReport:
        """三波级联(短路,便宜→贵、粗→细):
        ① 绝对禁词命中 → 转人工审核,停;
        ② 阿里云内容安全(普通模式)发现问题 → 转人工审核,停;
        ③ 前两波都放行 → 语义整篇审核(全局∪项目规则 vs 打包物料),最细。
        机器只出 pass / review(发现任何问题都转人工),**永不直接 block**;block 只由人工拒绝产生。
        作品(job.project_id 非空)吃「全局 ∪ 该项目」规则;物料只吃全局。"""
        # 第一波:绝对禁词(管理员精选、非常确定不能讲)——命中即转人工,停
        bw = self._blockword_scan(segments)
        if bw:
            return AuditReport(verdict=AuditStatus.REVIEW, segments=segments,
                               triggered=bw, summary=self._summary(AuditStatus.REVIEW, bw))
        # 第二波:阿里云内容安全(普通模式)通用拦截——发现问题即停
        cs = self._content_safety(job, segments)
        if cs:
            v = self._combine(cs)
            return AuditReport(verdict=v, segments=segments, triggered=cs, summary=self._summary(v, cs))
        # 第三波:规则判定。按严格程度拆两支——
        #   正则规则(match_level=="regex")→ 纯正则精确命中,**零大模型**;
        #   隐喻/字面规则 → 语义整篇审核(大模型)。两支命中合并。
        pid = getattr(job, "project_id", "")
        applicable = [r for r in self._rules.list()
                      if r.enabled and (r.project_id == "" or r.project_id == pid)]
        regex_rules = [r for r in applicable if _norm_level(getattr(r, "match_level", "metaphor")) == "regex"]
        sem_rules = [r for r in applicable if _norm_level(getattr(r, "match_level", "metaphor")) != "regex"]
        trig = self._regex_scan(segments, regex_rules, job) + self._semantic_judge(segments, sem_rules, job)
        v = self._combine(trig)
        return AuditReport(verdict=v, segments=segments, triggered=trig, summary=self._summary(v, trig))

    def _regex_scan(self, segments: list[TextSegment], rules: list[AuditRule], job: AuditJob) -> list[dict]:
        """正则规则匹配(零大模型):每条规则用其已编译的 regex(或退化成 keywords 字面 OR)
        对适用段落(按 source_type + 项目过滤)精确命中;命中→review(由 _combine 保证)。
        非法正则退化为逐关键词子串匹配,绝不抛异常搞崩审核。"""
        import re
        if not rules or not segments:
            return []
        pid = getattr(job, "project_id", "")
        triggered: list[dict] = []
        seen: set = set()
        for r in rules:
            kws = [k for k in (r.keywords or []) if k and k.strip()]
            pat = (getattr(r, "regex", "") or "").strip() or "|".join(re.escape(k) for k in kws)
            if not pat:
                continue
            try:
                rx = re.compile(pat)
                matcher = lambda text: rx.search(text) is not None
            except re.error:                              # 正则写坏 → 退化成关键词子串 OR,不抛
                matcher = lambda text: any(k in text for k in kws)
            rule_desc = (r.condition or "").strip()[:80] or ("、".join(kws)[:80] or "(正则规则)")
            for seg in segments:
                if not r.applies_to(seg.source_type.value, pid):   # 按来源类型 + 项目过滤(修好 stage③ 不过滤的缺陷)
                    continue
                if not matcher(seg.text or ""):
                    continue
                key = (r.id, seg.begin_ms, seg.frame_oss_key)
                if key in seen:
                    continue
                seen.add(key)
                triggered.append({"rule_id": r.id, "rule_no": getattr(r, "no", 0), "rule_desc": rule_desc,
                                  "source_type": seg.source_type.value, "action": r.action,
                                  "reason": f"命中正则规则「{rule_desc}」(精确匹配,未走大模型)",
                                  "text": seg.text, "begin_ms": seg.begin_ms,
                                  "frame_oss_key": seg.frame_oss_key})
        return triggered

    def _blockword_scan(self, segments: list[TextSegment]) -> list[dict]:
        """第一波:绝对禁词硬拦。命中任一 segment 文本即记一条 block(不塞 risk_words,禁词不该「加白」)。"""
        words = self._blockwords() if self._blockwords else set()
        if not words:
            return []
        triggered: list[dict] = []
        for seg in segments:
            text = seg.text or ""
            for w in words:
                if w and w in text:
                    triggered.append({"rule_id": "blockword", "source_type": seg.source_type.value,
                                      "action": "block", "reason": f"命中绝对禁词「{w}」",
                                      "text": text, "begin_ms": seg.begin_ms,
                                      "frame_oss_key": seg.frame_oss_key})
                    break   # 一段命中一条即可
        return triggered

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

    # ── 正则规则编译:自然语言 → 大模型提取关键词 + 构造正则(只在建/编辑规则时用一次,不落库,供预览)──
    def compile_regex(self, text: str) -> dict:
        """把管理员的自然语言描述交大模型编译成 {keywords:[...], regex:"..."}。
        审核时用返回的 regex 纯正则匹配、零大模型。失败/空 → {"keywords":[],"regex":""}。"""
        text = (text or "").strip()
        if not text:
            return {"keywords": [], "regex": ""}
        try:
            out = self._llm.chat_json(_REGEX_COMPILE_SYS,
                                      f"要拦的内容(自然语言):\n{text[:4000]}\n\n请以 json 返回 keywords 和 regex。")
        except Exception:
            return {"keywords": [], "regex": ""}
        if not isinstance(out, dict):
            return {"keywords": [], "regex": ""}
        return {"keywords": self._norm_strlist(out.get("keywords"), cap=30),
                "regex": str(out.get("regex") or "").strip()}

    # ── 规则解析:粘贴整篇审核文案 → 大模型拆成结构化规则草案(不落库,供前端预览确认)──
    def parse_rules(self, text: str) -> list[dict]:
        """把管理员粘贴的整篇审核文案交大模型拆成规则草案。
        返回 [{category, source_type, keywords, condition, action, match_level}] —— 已归一化并滤掉空规则(无词无条件)。
        纯解析、不落库:前端预览可删个别条后再批量确认。"""
        text = (text or "").strip()
        if not text:
            return []
        try:
            out = self._llm.chat_json(
                _RULE_PARSE_SYS, f"审核文案:\n{text[:20000]}\n\n请以 json 返回 rules 数组。")
        except Exception:
            return []
        raw = out.get("rules") if isinstance(out, dict) else None
        if not isinstance(raw, list):
            return []
        drafts: list[dict] = []
        for r in raw[:200]:
            if not isinstance(r, dict):
                continue
            st = str(r.get("source_type") or "any").strip()
            if st not in _RULE_SOURCE_TYPES:
                st = "any"
            kws = [str(k).strip() for k in (r.get("keywords") or [])
                   if isinstance(k, (str, int)) and str(k).strip()]
            kws = list(dict.fromkeys(kws))[:30]
            cond = str(r.get("condition") or "").strip()
            act = str(r.get("action") or "review").strip()
            if act not in ("block", "review"):
                act = "review"
            cat = str(r.get("category") or "").strip()
            lvl = _norm_level(str(r.get("match_level") or "").strip())
            if not kws and not cond:
                continue   # 空规则(无关键词、无条件)—— 无法命中,丢弃
            drafts.append({"category": cat, "source_type": st, "keywords": kws,
                           "condition": cond, "action": act, "match_level": lvl})
        return drafts

    def _generate_summary(self, mtype: MaterialType, segments: list[TextSegment]) -> dict:
        text = "\n".join(s.text for s in segments if s.text)[:6000]
        if not text.strip():
            return {}
        return self._llm.chat_json(_SUMMARY_SYS,
                                   f"物料类型:{mtype.value}\n解析内容:\n{text}\n\n请以 json 返回档案。")

    @staticmethod
    def _norm_strlist(v, cap: int = 6) -> list[str]:
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            return []
        out = [str(x).strip() for x in v if isinstance(x, (str, int)) and str(x).strip()]
        return list(dict.fromkeys(out))[:cap]

    @staticmethod
    def _song_name_from(oss_key: str) -> str:
        """从 oss_key(materials/{uuid}-{文件名} 或 audit/{uuid}-{文件名})取歌名:
        去目录、去 uuid4().hex 前缀(32 位 alnum、无 '-')、去扩展名。歌名本身含 '-' 也不误伤。"""
        if not oss_key:
            return ""
        base = oss_key.rsplit("/", 1)[-1]                    # 去目录
        if "-" in base:
            head, rest = base.split("-", 1)
            base = rest if (len(head) >= 8 and head.isalnum()) else base   # 去 uuid 前缀
        return base.rsplit(".", 1)[0].strip()               # 去扩展名

    def _music_archive(self, m: Material, segments: list[TextSegment]) -> dict:
        """音乐物料:歌名 → Tavily 联网搜「情绪/场景/曲风」→ 大模型合成配乐档案(情绪/场景多值)。
        歌名取不到 / 联网 / 合成任一失败 → 返回 {} 让上层回退 qwen 文本档案,绝不阻塞。"""
        song = self._song_name_from(m.oss_key)
        if not song:
            return {}
        try:
            brief = self._tavily.search(f"歌曲《{song}》 表达的情绪 适合的短视频场景 曲风")
            transcript = "\n".join(s.text for s in segments if s.text)[:1500]
            out = self._llm.chat_json(
                _MUSIC_ARCHIVE_SYS,
                f"歌曲名:{song}\n联网检索资料:\n{(brief or '(暂无联网资料,请依据歌名推断)')[:4000]}\n"
                f"音频转写(可空):\n{transcript}\n\n请以 json 返回这首歌的配乐档案。")
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}

    def _make_archive(self, m: Material, segments: list[TextSegment]) -> dict:
        """物料档案:音乐 → 歌名联网搜(Tavily)合成;图片/视频 → 豆包 pro 2.1 直接看;
        语料/无媒体或上述失败 → qwen 文本兜底。"""
        if self._tavily is not None and m.type == MaterialType.MUSIC:   # 音乐:联网搜情绪/场景
            a = self._music_archive(m, segments)
            if a:
                return a
        media_types = (MaterialType.IMAGE, MaterialType.MEME, MaterialType.STYLE, MaterialType.VIDEO)
        if self._archiver is not None and m.oss_key and m.type in media_types:
            text = "\n".join(s.text for s in segments if s.text)[:2000]
            a = self._archiver.tag(m.type.value, media_url=self._storage.signed_url(m.oss_key),
                                   is_video=(m.type == MaterialType.VIDEO), text=text)
            if a:
                return a
        return self._generate_summary(m.type, segments)   # 兜底:qwen 文本

    def _apply_summary(self, m: Material, segments: list[TextSegment]) -> None:
        try:
            s = self._make_archive(m, segments)
        except Exception:
            return
        if not isinstance(s, dict) or not s:
            return
        m.ai_summary = (s.get("summary") or "").strip()
        m.ai_scenarios = self._norm_strlist(s.get("scenarios"))   # 多值具体场景(豆包/qwen 皆返回数组)
        m.ai_emotions = self._norm_strlist(s.get("emotions"))     # 多值情绪
        m.ai_atmosphere = (s.get("atmosphere") or "").strip()
        m.tags = list(dict.fromkeys(list(m.tags or []) + self._norm_strlist(s.get("tags"), cap=8)))[:12]
        # 用「摘要+情绪+场景+氛围+标签」重嵌入 → 语义搜索能按情绪/场景命中
        try:
            rich = (f"{m.ai_summary} 场景:{' '.join(m.ai_scenarios)} 情绪:{' '.join(m.ai_emotions)} "
                    f"氛围:{m.ai_atmosphere} 标签:{' '.join(m.tags)}")
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

    def _frame_rules_digest(self, project_id: str = "") -> str:
        """把「适用于视频关键帧」的审核规则(含 any + 该项目)整理成清单,给 LLM 反推相关画面时刻。"""
        lines: list[str] = []
        for i, r in enumerate(self._rules.list_for(TextSourceType.VIDEO_FRAME.value, project_id), 1):
            parts = []
            if r.keywords:
                parts.append("关键词:" + "、".join(k for k in r.keywords if k))
            if r.condition.strip():
                parts.append("条件:" + r.condition.strip())
            if parts:
                lines.append(f"{i}. " + ";".join(parts))
        return "\n".join(lines)

    def _pick_visual_moments(self, transcript: list[TextSegment], project_id: str = "") -> list[int]:
        """规则反推抽帧点(作品用)。有画面规则 → 让 LLM 依据规则在时间轴定位相关画面时刻;
        无画面规则 → 退回通用「可能需结合画面判定」的重点时刻。无转写/异常 → [](交安全网兜底)。"""
        if not transcript:
            return []
        tl = "\n".join(f"[{(s.begin_ms or 0)}ms] {s.text}" for s in transcript if s.text)
        digest = self._frame_rules_digest(project_id)
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

    def _work_moments(self, transcript: list[TextSegment], dur_ms, project_id: str = "") -> list[int]:
        """作品抽帧点 = 规则反推点 ∪ 均匀安全网,去重排序;超上限时优先保留规则命中点。"""
        net = self._safety_net(dur_ms)
        rule_pts = self._pick_visual_moments(transcript, project_id)   # 无转写→[];无规则→通用挑取;异常→[]
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
        moments = (self._work_moments(transcript, dur, getattr(job, "project_id", ""))
                   if is_work else self._material_moments(dur))
        if dur:  # 钳制在时长内并去重,避免超时长截到同一最后帧
            moments = sorted({min(m, max(0, dur - 100)) for m in moments if m is not None})
        else:
            moments = sorted({m for m in moments if m is not None})
        def _one_frame(ms):
            dest = f"frames/{job.oss_key.rsplit('/', 1)[-1]}-{uuid.uuid4().hex[:8]}.jpg"
            try:
                if not self._storage.snapshot_frame(job.oss_key, ms, dest):
                    return None
                fdesc = self._vision.describe_image(self._storage.signed_url(dest))
                if not is_work:                              # 仅物料把帧存为可复用素材;作品只核验不入库
                    self._save_frame_material(dest, fdesc, ms / 1000.0, job)
                return TextSegment(TextSourceType.VIDEO_FRAME, fdesc, begin_ms=ms, frame_oss_key=dest)
            except Exception:
                return None

        # 逐帧 OSS 截图 + Qwen-VL 反解并发跑(各帧独立):把串行 N 帧的时延压到约 1 帧,是审核时延大头
        frame_segs: list[TextSegment] = []
        if moments:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(len(moments), 5)) as ex:
                frame_segs = [s for s in ex.map(_one_frame, moments) if s is not None]
        merged = transcript + frame_segs
        merged.sort(key=lambda s: (s.begin_ms if s.begin_ms is not None else 0))
        return merged

    def _save_frame_material(self, oss_key: str, desc: str, timecode: float, job: AuditJob) -> None:
        try:
            cand = MaterialCandidate(type=MaterialType.IMAGE, thumb=oss_key,
                                     source_timecode=timecode, description=desc, oss_key=oss_key)
            emb = self._embedder.embed(cand)
            m = Material(id=uuid.uuid4().hex, type=MaterialType.IMAGE, thumb=oss_key,
                         source_timecode=timecode, embedding=emb, audit_status=AuditStatus.PROCESSING,
                         source_job=job.id, oss_key=oss_key, description=desc, owner_id=job.owner_id)
            self._repo.save(m)
            self._index.add(m.id, emb)
        except Exception:
            pass  # 入库是副产物,失败不影响审核

    # ── 关键词快筛(纯逻辑)——命中项带上具体片段(文本/帧),供报告标红定位 ──
    # ── 阿里云内容安全硬拦兜底(黄暴政治等,第二波通用拦截)——命中项定位到具体片段/帧 ──
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

    # ── 第三波:语义整篇审核(物料 + 规则各打包成一份文本,大模型整体判)──
    def _semantic_judge(self, segments: list[TextSegment], rules: list[AuditRule],
                        job: AuditJob) -> list[dict]:
        """把物料整体打包 + 适用规则(全局∪项目)整合成规则清单,一次交大模型整判违规处。
        findings 里的 rule/segment 编号映射回规则(取 action)与段落(取 begin_ms/frame_oss_key 定位)。"""
        rules = [r for r in rules if r.enabled]
        if not rules or not segments:
            return []
        material_doc = self._pack_material(segments, job)
        rules_doc = self._pack_rules(rules)
        try:
            out = self._llm.chat_json(
                _RULE_JUDGE_SYS,
                f"【物料内容】\n{material_doc}\n\n【审核规则清单】\n{rules_doc}\n\n请以 json 返回 findings。")
        except Exception:
            return [{"rule_id": "", "source_type": "text", "action": "review",
                     "reason": "语义审核异常,转人工复核"}]
        findings = out.get("findings") if isinstance(out, dict) else None
        if not isinstance(findings, list):
            return []
        img_key = (job.oss_key if job.material_type in
                   (MaterialType.IMAGE, MaterialType.MEME, MaterialType.STYLE) else "")
        by_no = {(getattr(r, "no", 0) or i): r for i, r in enumerate(rules, 1)}   # 按稳定编号映射(未回填号兜底用位置)
        triggered: list[dict] = []
        seen: set = set()
        for f in findings[:50]:
            if not isinstance(f, dict):
                continue
            try:
                ri = int(f.get("rule"))
            except (TypeError, ValueError):
                continue
            rule = by_no.get(ri)
            if rule is None:
                continue   # 幻觉/越界规则号,丢弃
            seg = None
            si = f.get("segment")
            if si is not None:
                try:
                    si = int(si)
                    if 1 <= si <= len(segments):
                        seg = segments[si - 1]
                except (TypeError, ValueError):
                    seg = None
            reason = str(f.get("reason") or "命中规则").strip()
            if _reason_says_pass(reason):
                continue   # 大模型自己说没违规 → 不纳入 triggered
            rule_desc = (rule.condition or "").strip()[:80] or "（见参考词）"   # 报告显示「因哪条规则」
            if seg is not None:
                item = {"rule_id": rule.id, "rule_no": getattr(rule, "no", 0), "rule_desc": rule_desc,
                        "source_type": seg.source_type.value,
                        "action": rule.action, "reason": reason, "text": seg.text,
                        "begin_ms": seg.begin_ms, "frame_oss_key": seg.frame_oss_key}
            else:  # 整体/无法定位到具体段落 → 用规则目标类型,图片物料落原图供预览
                item = {"rule_id": rule.id, "rule_no": getattr(rule, "no", 0), "rule_desc": rule_desc,
                        "source_type": _source_type_fallback(rule.source_type),
                        "action": rule.action, "reason": reason, "text": "",
                        "begin_ms": None, "frame_oss_key": img_key}
            key = (item["rule_id"], item["begin_ms"], item["frame_oss_key"])
            if key in seen:
                continue
            seen.add(key)
            triggered.append(item)
        return triggered

    @staticmethod
    def _pack_material(segments: list[TextSegment], job: AuditJob) -> str:
        """把物料整体打包成一份带段落锚点的文本:〖i〗时间 类型 文字。
        段落编号 i 即 segments[i-1](已按时间排序),供大模型在 findings.segment 里引用定位。"""
        type_cn = {MaterialType.VIDEO: "视频作品", MaterialType.IMAGE: "图片", MaterialType.MEME: "表情包",
                   MaterialType.STYLE: "风格图", MaterialType.AUDIO: "音频", MaterialType.MUSIC: "音乐",
                   MaterialType.CORPUS: "语料"}.get(job.material_type, "物料")
        lines = [f"类型:{type_cn} · 共 {len(segments)} 段(按时间先后编号)\n"]
        total = 0
        for i, s in enumerate(segments, 1):
            ms = s.begin_ms
            tc = f"{int(ms) // 60000:02d}:{int(ms) // 1000 % 60:02d}" if ms is not None else "—"
            lab = _MAT_LABEL.get(s.source_type, "文字")
            txt = (s.text or "").strip().replace("\n", " ")[:600]
            lines.append(f"〖{i}〗{tc} {lab}  {txt}")
            total += len(txt)
            if total > 9000:                 # 整体封顶,防超长
                lines.append("(后续段落略)")
                break
        return "\n".join(lines)

    @staticmethod
    def _pack_rules(rules: list[AuditRule]) -> str:
        """把适用规则(全局 + 项目)整合成一份编号规则文本;编号=规则自己的稳定 `no`(与列表/报告一致),供 findings.rule 引用。
        keywords 仅作「参考词」示例,绝不硬匹配。"""
        lines = ["(命中即按「动作」处置;参考词仅为方向示例,请按语义判断、勿机械按字匹配)\n"]
        for i, r in enumerate(rules, 1):
            n = getattr(r, "no", 0) or i          # 未回填编号(旧数据/单测)兜底用位置号
            tgt = _fmt_source_type(r.source_type)
            act = "拦截" if r.action == "block" else "待复核"
            lvl = "字面" if _norm_level(getattr(r, "match_level", "metaphor")) == "literal" else "隐喻"
            desc = (r.condition or "").strip() or "(见参考词)"
            kw = "、".join(k for k in (r.keywords or []) if k)
            kw_s = f"(参考词:{kw})" if kw else ""
            lines.append(f"{n}.【{tgt}｜{act}｜{lvl}判定】{desc}{kw_s}")
            guide = (getattr(r, "guidance", "") or "").strip()
            if guide:
                lines.append(f"   尺度说明:{guide[:600]}")
            exc = [e for e in (getattr(r, "exceptions", None) or []) if (e.get('text') or '').strip()]
            if exc:
                lines.append("   已确认可放行的例外(命中类似情形不要判违规):")
                for e in exc[:8]:
                    lines.append(f"     - {(e.get('text') or '').strip()[:200]}")
        return "\n".join(lines)

    @staticmethod
    def _combine(triggered: list[dict]) -> AuditStatus:
        # 机器裁定只出 review / pass:发现任何问题(拦截级/待复核级一视同仁)→ 转人工复核,
        # 永不直接 block(block 只由人工拒绝产生)。
        return AuditStatus.REVIEW if triggered else AuditStatus.PASS

    @staticmethod
    def _summary(verdict: AuditStatus, triggered: list[dict]) -> str:
        if verdict == AuditStatus.PASS:
            return "未命中任何审核规则,通过。"
        reasons = "; ".join(t.get("reason", "") for t in triggered if t.get("reason"))
        return f"待人工复核:{reasons}"

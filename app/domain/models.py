"""领域模型(纯净核心,零外向依赖)。"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MaterialType(str, Enum):
    IMAGE = "image"
    MEME = "meme"
    VIDEO = "video"
    STYLE = "style"
    CORPUS = "corpus"     # 文字/语料
    MUSIC = "music"       # 背景乐
    AUDIO = "audio"       # 声音(待转写审核,区别于 music)


class AuditStatus(str, Enum):
    PROCESSING = "processing"   # 机器审核中(尚未出裁定;不进人工队列、不可检索)
    PASS = "pass"
    REVIEW = "review"   # 机器发现问题→待人工复核(不默认放行)
    BLOCK = "block"     # 只由人工拒绝产生(机器永不直接拦截)


class TextSourceType(str, Enum):
    """审核链路里「文字」的来源类型 —— 规则按此归类判定。"""
    ORIGINAL_TEXT = "original_text"   # 上传的原文/语料
    TRANSCRIPT = "transcript"         # 声音/视频音轨 ASR 转写
    IMAGE_CONTENT = "image_content"   # 图片 Qwen-VL 反解出的画面内容
    VIDEO_FRAME = "video_frame"       # 视频关键帧 Qwen-VL 反解出的画面内容


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class MaterialCandidate:
    """反解产出的物料候选(尚未审核/入库)。"""
    type: MaterialType
    thumb: str
    source_timecode: float
    description: str = ""
    oss_key: str = ""   # 该候选对应的真实文件(如反解截帧图),用于预览/下载;空=无独立文件


@dataclass
class Material:
    """已生成的物料(带向量与审核结果)。"""
    id: str
    type: MaterialType
    thumb: str
    source_timecode: float
    embedding: list[float]
    audit_status: AuditStatus
    source_job: str
    oss_key: str = ""
    description: str = ""
    owner_id: str = ""        # 物料归属(我的物料库按此归属)
    is_public: bool = False   # 是否已发布到公共物料库(管理员发布)
    audit_report_id: str = ""  # 指向持久化的审核报告(可回看链路)
    content_hash: str = ""    # 内容 MD5(同一 owner 库内去重,防重复上传)
    project_id: str = ""      # 作品所属项目(仅作品有;物料/其它恒空)
    # 分类与 AI 摘要
    tags: list[str] = field(default_factory=list)  # 标签/项目分类(用户手动 + AI 建议);空=归"全部"
    ai_summary: str = ""      # 是什么 + 包含什么内容
    ai_scenarios: list[str] = field(default_factory=list)  # 适用使用情境(多值,具体到「什么时候用」;供短视频按场景匹配)
    ai_emotions: list[str] = field(default_factory=list)   # 能表达的情绪(多值;主要搜索维度)
    ai_atmosphere: str = ""   # 营造的氛围(主要搜索维度)
    # 退回历史(作品审核记录用):每次被判 block(机审拦截 或 人工退回)追加一条
    reject_events: list[dict] = field(default_factory=list)  # [{"ms":int,"reason":str,"by":"机审"|"人工"}]


@dataclass
class VideoJob:
    """视频反解任务。"""
    id: str
    oss_key: str
    size_bytes: int
    status: JobStatus = JobStatus.PENDING
    retry: int = 0


# ── 审核引擎 ──
@dataclass
class Project:
    """作品项目:每个项目有自己的一组审核规则。作品必须同时过标准规则 + 所属项目规则。"""
    id: str
    name: str
    created_by: str = ""
    created_ms: int = 0


@dataclass
class AuditRule:
    """管理员配置的审核规则:某来源类型的文字,出现关键词或满足自然语言条件 → 定级。"""
    id: str
    source_type: str            # TextSourceType 值,或 "any"(对所有文字生效)
    no: int = 0                 # 规则编号(全局唯一、稳定、从 1 递增;0=未分配待回填):列表/报告/大模型判定三处对齐,供人快速定位是哪条规则
    keywords: list[str] = field(default_factory=list)  # 关键词快筛(命中即定级)
    condition: str = ""         # 自然语言条件(交 qwen-plus 判),可空
    action: str = "block"       # 命中动作:block | review
    enabled: bool = True
    created_by: str = ""
    project_id: str = ""        # 空=标准/全局(物料+作品都生效);非空=只对该项目的作品额外生效
    guidance: str = ""          # 尺度说明:到什么程度算违规、好例子/坏例子(喂给语义判定把握程度)
    match_level: str = "metaphor"  # 严格程度:literal=字面 | metaphor=隐喻(默认) | regex=正则(不走大模型,用 regex 精确命中)
    regex: str = ""             # 正则模式(match_level=="regex")的已编译正则:建规则时大模型把自然语言编译成它,审核时纯正则匹配
    exceptions: list[dict] = field(default_factory=list)  # 审核员「忽略」积累的可放行例外 [{text,note,by,ms}]

    def applies_to(self, source_type: str, project_id: str = "") -> bool:
        """规则是否适用:启用 + 来源类型匹配(逗号分隔多选,向后兼容单值) + (全局 或 命中当前项目)。
        全局规则(project_id=="")永远生效;项目规则只在审核对象属于同一项目时生效。"""
        types = [t.strip() for t in self.source_type.split(",") if t.strip()]
        return (self.enabled
                and ("any" in types or source_type in types)
                and (self.project_id == "" or self.project_id == project_id))


@dataclass
class TextSegment:
    """审核链路的中间产物:一段带来源类型的文字(视频/声音段带时间轴)。"""
    source_type: TextSourceType
    text: str
    begin_ms: Optional[int] = None
    end_ms: Optional[int] = None
    frame_oss_key: str = ""     # 视频帧对应的截图 OSS key(若有)


@dataclass
class AuditReport:
    """审核报告:总判定 + 各文字链路 + 命中的规则。"""
    verdict: AuditStatus
    segments: list[TextSegment] = field(default_factory=list)
    triggered: list[dict] = field(default_factory=list)  # [{rule_id, source_type, reason, action}]
    summary: str = ""


@dataclass
class AuditJob:
    """审核任务(视频/音频异步;图片/文字可同步)。"""
    id: str
    material_type: MaterialType
    oss_key: str = ""
    material_id: str = ""
    owner_id: str = ""
    status: JobStatus = JobStatus.PENDING
    report: Optional[AuditReport] = None
    video_kind: str = "material"    # 视频:material(物料,≤20s,存素材)| work(作品,仅扫描)
    project_id: str = ""            # 作品所属项目(驱动项目级规则);物料恒空


@dataclass
class AuditTask:
    """用户可见的「待审核任务」(持久化):一次提交 = 一条,单条/批量都汇聚到「待审核」页。
    上传与审核解耦——提交即受理(pending),后台上传+审核,页面轮询状态。"""
    id: str
    owner_id: str
    name: str                       # 文件名 / "文字审核"
    material_type: MaterialType
    material_id: str = ""           # 审核前可能还没建物料(批量后台建)
    content_hash: str = ""
    status: JobStatus = JobStatus.PENDING   # pending/running/done/failed
    verdict: str = ""               # pass/review/block(done 后)
    report_id: str = ""             # 指向 audit_reports
    created_ms: int = 0
    error: str = ""
    video_kind: str = "material"    # 视频:material(物料)| work(作品)
    project_id: str = ""            # 作品所属项目(穿到 Material/AuditJob 驱动项目规则)


@dataclass
class User:
    """用户(密码只存加盐哈希)。"""
    id: str
    name: str
    pwd_hash: str
    role: str = "viewer"
    status: str = "active"

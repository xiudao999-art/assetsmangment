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
    PASS = "pass"
    REVIEW = "review"   # 待人工复核(不默认放行)
    BLOCK = "block"


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
    # 分类与 AI 摘要
    tags: list[str] = field(default_factory=list)  # 标签/项目分类(用户手动 + AI 建议);空=归"全部"
    ai_summary: str = ""      # 是什么 + 包含什么内容
    ai_scene: str = ""        # 适合的使用场景
    ai_emotion: str = ""      # 表达的情绪(主要搜索维度)
    ai_atmosphere: str = ""   # 营造的氛围(主要搜索维度)


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
class AuditRule:
    """管理员配置的审核规则:某来源类型的文字,出现关键词或满足自然语言条件 → 定级。"""
    id: str
    source_type: str            # TextSourceType 值,或 "any"(对所有文字生效)
    keywords: list[str] = field(default_factory=list)  # 关键词快筛(命中即定级)
    condition: str = ""         # 自然语言条件(交 qwen-plus 判),可空
    action: str = "block"       # 命中动作:block | review
    enabled: bool = True
    created_by: str = ""

    def applies_to(self, source_type: str) -> bool:
        return self.enabled and (self.source_type == "any" or self.source_type == source_type)


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


@dataclass
class User:
    """用户(密码只存加盐哈希)。"""
    id: str
    name: str
    pwd_hash: str
    role: str = "viewer"
    status: str = "active"

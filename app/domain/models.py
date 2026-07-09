"""领域模型(纯净核心,零外向依赖)。"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class MaterialType(str, Enum):
    IMAGE = "image"
    MEME = "meme"
    VIDEO = "video"
    STYLE = "style"
    CORPUS = "corpus"


class AuditStatus(str, Enum):
    PASS = "pass"
    REVIEW = "review"   # 待人工复核(不默认放行)
    BLOCK = "block"


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


@dataclass
class VideoJob:
    """视频反解任务。"""
    id: str
    oss_key: str
    size_bytes: int
    status: JobStatus = JobStatus.PENDING
    retry: int = 0


@dataclass
class User:
    """用户(密码只存加盐哈希)。"""
    id: str
    name: str
    pwd_hash: str
    role: str = "viewer"
    status: str = "active"

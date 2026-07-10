"""API 请求/响应模型(Pydantic)。"""
from __future__ import annotations
from pydantic import BaseModel
from app.domain.models import MaterialType


class MaterialCreate(BaseModel):
    type: MaterialType = MaterialType.IMAGE
    oss_key: str
    # 归属由 token 里的当前用户决定,不接受客户端指定(防越权伪造归属)。


class VideoUpload(BaseModel):
    oss_key: str
    size_bytes: int = 0


class RegisterIn(BaseModel):
    name: str
    password: str


class LoginIn(BaseModel):
    name: str
    password: str


class GrantIn(BaseModel):
    role: str
    permission: str


class AuditSet(BaseModel):
    status: str  # pass / review / block


class RuleIn(BaseModel):
    source_type: str = "any"        # any 或 TextSourceType 值
    keywords: list[str] = []        # 关键词快筛
    condition: str = ""             # 自然语言条件(交大模型判)
    action: str = "block"           # block / review

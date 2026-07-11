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


class TagsIn(BaseModel):
    tags: list[str] = []            # 物料标签(项目分类)


class UserCreate(BaseModel):
    name: str
    password: str                   # 管理员创建的账号默认为普通用户


class UserPermsIn(BaseModel):
    permissions: list[str] = []     # 给某用户设置的功能权限(整套替换)


class WhitelistIn(BaseModel):
    words: list[str] = []           # 内容安全白名单:加入这些词(命中即便阿里云判违规也放行)

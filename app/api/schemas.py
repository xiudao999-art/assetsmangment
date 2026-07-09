"""API 请求/响应模型(Pydantic)。"""
from __future__ import annotations
from pydantic import BaseModel
from app.domain.models import MaterialType


class MaterialCreate(BaseModel):
    type: MaterialType = MaterialType.IMAGE
    oss_key: str
    owner_id: str = "u1"


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

"""物料管理服务(REQ-101/102/103)。只依赖 domain 端口。"""
from __future__ import annotations
import uuid
from typing import Optional
from app.domain.models import Material, MaterialType, AuditStatus
from app.domain.rules import is_available
from app.domain.ports import MaterialRepo, ObjectStorage


class MaterialNotFound(Exception):
    pass


class MaterialService:
    def __init__(self, repo: MaterialRepo, storage: ObjectStorage) -> None:
        self._repo = repo
        self._storage = storage

    def create(self, type: MaterialType, oss_key: str, data: bytes, owner_id: str) -> Material:
        """REQ-101:存 OSS + 落库元数据 + 返回物料。默认审核态 review(fail-safe)。"""
        self._storage.put(oss_key, data)
        material = Material(
            id=uuid.uuid4().hex, type=type, thumb=f"{oss_key}#thumb",
            source_timecode=0.0, embedding=[], audit_status=AuditStatus.REVIEW,
            source_job="", oss_key=oss_key,
        )
        self._repo.save(material)
        return material

    def get_signed_url(self, material_id: str) -> str:
        """REQ-102:返回受时限签名 URL。物料不存在(含已删)→ 抛错。"""
        material = self._repo.get(material_id)
        if material is None:
            raise MaterialNotFound(material_id)
        return self._storage.signed_url(material.oss_key)

    def get_download_url(self, material_id: str) -> str:
        """REQ-502:仅审核通过可下载;block/review → 拒绝。"""
        material = self._repo.get(material_id)
        if material is None or not is_available(material):
            raise PermissionError("物料不可下载(未通过审核或不存在)")
        return self._storage.signed_url(material.oss_key)

    def delete(self, material_id: str) -> None:
        """REQ-103:删除文件与元数据 → 不可访问、不可检索。"""
        material = self._repo.get(material_id)
        if material is not None:
            self._storage.delete(material.oss_key)
            self._repo.delete(material_id)

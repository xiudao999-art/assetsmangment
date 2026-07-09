"""自动内容审核服务(REQ-501/502/503)。只依赖 domain 端口。"""
from __future__ import annotations
from app.domain.models import Material, AuditStatus
from app.domain.ports import Auditor, MaterialRepo


class AuditService:
    def __init__(self, auditor: Auditor, repo: MaterialRepo) -> None:
        self._auditor = auditor
        self._repo = repo

    def run(self, material: Material) -> AuditStatus:
        """REQ-501:审核并写回结果;REQ-503:超时→review,不默认放行。"""
        try:
            status = AuditStatus(self._auditor.audit(material))
        except TimeoutError:
            status = AuditStatus.REVIEW  # 不默认放行
        material.audit_status = status
        self._repo.save(material)
        return status

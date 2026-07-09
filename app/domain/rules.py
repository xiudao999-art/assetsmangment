"""领域规则(纯函数,零外向依赖)。"""
from app.domain.models import Material, AuditStatus


def is_available(material: Material) -> bool:
    """REQ-502/303:仅审核通过(pass)的物料可检索/下载;review/block 不可用。"""
    return material.audit_status == AuditStatus.PASS

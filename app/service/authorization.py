"""功能权限 RBAC 服务(REQ-701/702)。只依赖 domain 端口。"""
from __future__ import annotations
from app.domain.models import User
from app.domain.ports import RbacRepo, AuditLog


class PermissionDenied(Exception):
    code = 403


class AuthorizationService:
    def __init__(self, rbac: RbacRepo, audit: AuditLog) -> None:
        self._rbac = rbac
        self._audit = audit

    def authorize(self, user: User, permission: str) -> None:
        """REQ-701:无权限 → 403 + 审计。权限 = 角色默认权限 ∪ 该用户被单独授予的权限。"""
        allowed = self._rbac.permissions_of(user.role) | self._rbac.user_permissions(user.id)
        if permission not in allowed:
            self._audit.record(f"DENY user={user.id} perm={permission}")
            raise PermissionDenied(permission)

    # ── 后台管理(REQ-702:改动即时生效)──
    def grant(self, role: str, permission: str) -> None:
        self._rbac.grant(role, permission)

    def revoke(self, role: str, permission: str) -> None:
        self._rbac.revoke(role, permission)

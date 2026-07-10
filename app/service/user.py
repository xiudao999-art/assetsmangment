"""用户管理服务(REQ-601/602)。只依赖 domain 端口。"""
from __future__ import annotations
import uuid
from app.domain.models import User
from app.domain.ports import UserRepo, PasswordHasher, TokenIssuer


class InvalidCredentials(Exception):
    pass


class UserService:
    def __init__(self, repo: UserRepo, hasher: PasswordHasher, tokens: TokenIssuer) -> None:
        self._repo = repo
        self._hasher = hasher
        self._tokens = tokens

    def register(self, name: str, password: str) -> User:
        """REQ-602:密码加盐哈希存储,绝不明文。新用户默认普通用户角色。"""
        user = User(id=uuid.uuid4().hex, name=name, pwd_hash=self._hasher.hash(password), role="user")
        self._repo.save(user)
        return user

    def login(self, name: str, password: str) -> str:
        """REQ-601:凭据正确 → 签发受时限 token。"""
        user = self._repo.get_by_name(name)
        if user is None or not self._hasher.verify(password, user.pwd_hash):
            raise InvalidCredentials()
        return self._tokens.issue(user.id)

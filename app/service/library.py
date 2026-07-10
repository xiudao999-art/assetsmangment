"""物料库服务:我的库 / 公共库 / 全部(管理员)/ 发布 / 收藏。只依赖 domain 端口。"""
from __future__ import annotations
from app.domain.models import Material
from app.domain.ports import MaterialRepo, FavoriteRepo


class LibraryService:
    def __init__(self, repo: MaterialRepo, favorites: FavoriteRepo) -> None:
        self._repo = repo
        self._fav = favorites

    # ── 各物料库视图 ──
    def mine(self, user_id: str) -> list[Material]:
        """我的物料库 = 我上传的 + 我收藏的(公共物料)。"""
        fav_ids = self._fav.material_ids(user_id)
        return [m for m in self._repo.list()
                if m.owner_id == user_id or m.id in fav_ids]

    def public(self) -> list[Material]:
        """公共物料库 = 已发布且审核通过的物料(所有人可见)。"""
        return [m for m in self._repo.list()
                if m.is_public and m.audit_status.value == "pass"]

    def all(self) -> list[Material]:
        """管理员:全部用户的物料。"""
        return list(self._repo.list())

    # ── 管理员发布 ──
    def publish(self, material_id: str, public: bool = True) -> Material | None:
        m = self._repo.get(material_id)
        if m is None:
            return None
        m.is_public = public
        self._repo.save(m)
        return m

    # ── 收藏(把公共物料收进自己的库)──
    def favorite(self, user_id: str, material_id: str) -> None:
        self._fav.add(user_id, material_id)

    def unfavorite(self, user_id: str, material_id: str) -> None:
        self._fav.remove(user_id, material_id)

    def is_favorited(self, user_id: str, material_id: str) -> bool:
        return self._fav.has(user_id, material_id)

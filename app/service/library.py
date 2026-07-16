"""物料库服务:我的库 / 公共库 / 全部(管理员)/ 发布 / 收藏。只依赖 domain 端口。"""
from __future__ import annotations
from typing import Optional
from app.domain.models import Material
from app.domain.ports import MaterialRepo, FavoriteRepo
from app.domain.query import MaterialQuery


class LibraryService:
    def __init__(self, repo: MaterialRepo, favorites: FavoriteRepo) -> None:
        self._repo = repo
        self._fav = favorites

    # ── 各物料库视图(服务端翻页/筛选,返回 (当页, 总数)) ──
    def mine(self, user_id: str, *, type: Optional[str] = None, tag: Optional[str] = None,
             keyword: Optional[str] = None, project_id: Optional[str] = None, offset: int = 0,
             limit: Optional[int] = None) -> tuple[list[Material], int]:
        """我的物料库 = 我上传的 + 我收藏的(公共物料)。"""
        favs = frozenset(self._fav.material_ids(user_id))
        return self._repo.query(MaterialQuery(
            owner_id=user_id, include_ids=favs, owner_or_include=True,
            type=type, tag=tag, keyword=keyword, project_id=project_id, offset=offset, limit=limit))

    def public(self, *, type: Optional[str] = None, tag: Optional[str] = None,
               keyword: Optional[str] = None, project_id: Optional[str] = None, offset: int = 0,
               limit: Optional[int] = None) -> tuple[list[Material], int]:
        """公共物料库 = 已发布且审核通过的物料(所有人可见)。"""
        return self._repo.query(MaterialQuery(
            public_only=True, pass_only=True,
            type=type, tag=tag, keyword=keyword, project_id=project_id, offset=offset, limit=limit))

    def all(self, *, status: Optional[str] = None, type: Optional[str] = None,
            tag: Optional[str] = None, keyword: Optional[str] = None,
            project_id: Optional[str] = None, offset: int = 0,
            limit: Optional[int] = None) -> tuple[list[Material], int]:
        """管理员:全部用户的物料(可按状态/类型/关键词/项目筛)。"""
        return self._repo.query(MaterialQuery(
            status=status, type=type, tag=tag, keyword=keyword, project_id=project_id,
            offset=offset, limit=limit))

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

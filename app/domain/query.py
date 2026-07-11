"""物料查询值对象 + 关键词匹配纯函数(纯净核心,零外向依赖)。

服务端翻页/筛选的领域契约:service 组 MaterialQuery,repo(infra)执行 query(spec)。
未来换真 Postgres 物料表时只改 adapter——MaterialQuery 1:1 映射 WHERE + LIMIT/OFFSET + count(*)。
haystack/matches_keyword 是关键词语料的唯一来源(消除 jsonstore/fakes 里重复的 haystack)。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from app.domain.models import Material, AuditStatus


@dataclass(frozen=True)
class MaterialQuery:
    # ── 归属 / 可见性门(由 service 组装,绝不来自客户端) ──
    owner_id: Optional[str] = None            # 限定归属
    include_ids: Optional[frozenset] = None   # 额外纳入的 id 集(如收藏)
    owner_or_include: bool = False            # True → 门 = owner 匹配 或 id∈include_ids(「我的库」唯一 OR 语义)
    public_only: bool = False                 # is_public
    pass_only: bool = False                   # audit_status == PASS
    # ── 精确匹配筛选 ──
    status: Optional[str] = None              # "pass"/"review"/"block"(管理视图)
    type: Optional[str] = None                # MaterialType 值(image…)
    tag: Optional[str] = None                 # 精确命中 m.tags
    keyword: Optional[str] = None             # haystack 子串(含 tags)
    # ── 窗口 ──
    offset: int = 0
    limit: Optional[int] = None               # None = 不限(取全/向后兼容)


def haystack(m: Material) -> str:
    """关键词子串语料的唯一来源。"""
    return " ".join([m.thumb, m.description, m.ai_summary, m.ai_emotion,
                     m.ai_atmosphere, m.ai_scene, " ".join(m.tags or [])])


def matches_keyword(m: Material, keyword: str) -> bool:
    return bool(keyword) and keyword in haystack(m)


def matches(m: Material, q: MaterialQuery) -> bool:
    """一条物料是否满足查询谓词(纯逻辑,repo 复用;未命中即丢弃)。"""
    # 归属门
    if q.owner_or_include:                     # 我的库 = owner 或 收藏
        if not ((q.owner_id is not None and m.owner_id == q.owner_id)
                or (q.include_ids is not None and m.id in q.include_ids)):
            return False
    else:
        if q.owner_id is not None and m.owner_id != q.owner_id:
            return False
        if q.include_ids is not None and m.id not in q.include_ids:
            return False
    # AND 筛选(枚举一律 .value 比较,消除裸比/值比不一致)
    if q.public_only and not m.is_public:
        return False
    if q.pass_only and m.audit_status != AuditStatus.PASS:
        return False
    if q.status is not None and m.audit_status.value != q.status:
        return False
    if q.type is not None and m.type.value != q.type:
        return False
    if q.tag is not None and q.tag not in (m.tags or []):
        return False
    if q.keyword and not matches_keyword(m, q.keyword):
        return False
    return True


def paginate(pool, q: MaterialQuery) -> tuple[list, int]:
    """对一个物料可迭代对象套用谓词 → 先算 total(切片前)→ 再切片。
    JSON/内存 repo 共用;插入序即分页序(静态数据集稳定)。"""
    matched = [m for m in pool if matches(m, q)]
    total = len(matched)
    end = None if q.limit is None else q.offset + q.limit
    return matched[q.offset:end], total

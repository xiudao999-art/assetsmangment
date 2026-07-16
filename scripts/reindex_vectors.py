"""一次性:重建物料语义向量索引 —— 清掉历史「文件名/零向量」污染。

背景:早期 create 把直传物料按 OSS 文件名嵌入、空描述→零向量,污染了语义近邻,
导致「按含义搜」返回无关物料。代码已根治(只按内容嵌入 + add 跳零向量 + 查询过滤 NaN 安全)。
本脚本把线上索引重建一遍:有内容的物料按真内容重嵌入、无内容的从索引删除。

在 assets-api 容器内跑(有 env + 在 assets-net 上能连 assets-pgvector):
    docker exec assets-api python scripts/reindex_vectors.py
    docker exec assets-api python scripts/reindex_vectors.py "服务营销"   # 附带打印该查询距离分布做阈值校准
"""
import sys
from app.api import deps
from app.config import settings
from app.domain.models import MaterialCandidate


def content_text(m) -> str:
    """物料的可嵌入内容文本:优先 AI 档案(摘要/场景/情绪/氛围/标签),否则描述(语料/帧)。
    直传且未摘要的物料两者皆空 → 返回 "" → 从语义索引剔除。"""
    if m.ai_summary or m.ai_scene or m.ai_emotion or m.ai_atmosphere or (m.tags or []):
        return (f"{m.ai_summary} 场景:{m.ai_scene} 情绪:{m.ai_emotion} "
                f"氛围:{m.ai_atmosphere} 标签:{' '.join(m.tags or [])}").strip()
    return (m.description or "").strip()


def reindex() -> None:
    if not getattr(deps, "_vector_search", False):
        print("⚠ 未启用真 pgvector(_vector_search=False):检查 AM_DATABASE_URL + AM_DASHSCOPE_API_KEY。中止,未改动任何数据。")
        return
    idx, emb, repo = deps.index, deps._embedder, deps.material_repo
    kept = dropped = 0
    for m in repo.list():
        txt = content_text(m)
        if not txt:
            idx.delete(m.id)                # 无内容(文件名/零向量)→ 移出语义索引
            dropped += 1
            continue
        vec = emb.embed(MaterialCandidate(type=m.type, thumb="", source_timecode=0.0, description=txt))
        idx.add(m.id, vec)                  # 按真内容重嵌入(add 已跳过零向量)
        m.embedding = vec
        repo.save(m)
        kept += 1
    print(f"reindex done: kept={kept} dropped={dropped} · index.size={idx.size()}")


def calibrate(query: str) -> None:
    """打印该查询最近邻的排序距离,据实分布确认/微调 AM_SEARCH_MAX_DISTANCE。"""
    if not getattr(deps, "_vector_search", False):
        return
    qvec = deps._query_embedder.embed_text(query)
    scored = deps.index.query_scored(qvec, k=15)
    print(f"\n距离校准 · 查询「{query}」(当前阈值 AM_SEARCH_MAX_DISTANCE={settings.search_max_distance}):")
    for mid, dist in scored:
        m = deps.material_repo.get(mid)
        label = ((getattr(m, "ai_summary", "") or getattr(m, "description", "") or mid)[:40]) if m else mid
        mark = "✓保留" if dist <= settings.search_max_distance else "×丢弃"
        print(f"  {mark} dist={dist:.3f}  {label}")


if __name__ == "__main__":
    reindex()
    if len(sys.argv) > 1:
        calibrate(sys.argv[1])

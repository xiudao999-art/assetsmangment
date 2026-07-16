"""真实多模态向量适配器 —— 百炼 DashScope multimodal-embedding-v1(1024 维)。
实现 domain.ports.Embedder(物料入库向量)与 QueryEmbedder(查询向量),同一向量空间可跨模态检索。infra→domain。"""
from __future__ import annotations

from app.domain.models import MaterialCandidate

_DIM = 1024


def _embed_input(api_key: str, model: str, item: dict) -> list[float]:
    from dashscope import MultiModalEmbedding
    resp = MultiModalEmbedding.call(api_key=api_key, model=model, input=[item])
    if getattr(resp, "status_code", None) != 200:
        raise RuntimeError(f"embedding 失败: {getattr(resp, 'status_code', '?')} "
                           f"{getattr(resp, 'code', '')} {getattr(resp, 'message', '')}")
    return resp.output["embeddings"][0]["embedding"]


class DashScopeEmbedder:
    """物料入库向量:优先对描述文本做 embedding(反解描述很丰富);无描述回退零向量。"""
    def __init__(self, api_key: str, model: str = "multimodal-embedding-v1") -> None:
        import dashscope  # 延迟导入,校验依赖存在
        self._dashscope = dashscope
        self._api_key = api_key
        self._model = model

    def embed(self, candidate: MaterialCandidate) -> list[float]:
        text = (candidate.description or "").strip()   # 只嵌内容描述,绝不用 thumb/文件名充数
        if not text:
            return [0.0] * _DIM                          # 无内容 → 零向量(调用方 add 守卫会跳过入索引)
        return _embed_input(self._api_key, self._model, {"text": text[:2000]})


class DashScopeQueryEmbedder:
    """查询文本向量(与物料同模型同空间)。"""
    def __init__(self, api_key: str, model: str = "multimodal-embedding-v1") -> None:
        import dashscope
        self._dashscope = dashscope
        self._api_key = api_key
        self._model = model

    def embed_text(self, text: str) -> list[float]:
        text = (text or "").strip()
        if not text:
            return [0.0] * _DIM
        return _embed_input(self._api_key, self._model, {"text": text[:2000]})

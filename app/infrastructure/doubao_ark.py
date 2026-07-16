"""火山方舟 ARK · 豆包 pro 2.1 物料档案器(实现 ArchiveTagger 端口)。
直接看图片/整段视频 → 提「情绪标签 + 场景标签(多值)+ summary/tags」,供短视频按情绪/场景匹配复用。
走 ARK 的 OpenAI 兼容 /chat/completions(用已装的 httpx,不引新 SDK)。审核链路不经这里。"""
from __future__ import annotations
import json
import re

_ARCHIVE_SYS = (
    "你是短视频素材档案师。看下面这段图片或视频,产出一份可复用的检索档案。"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释,字段:"
    "summary(一句话:这是什么内容),"
    "emotions(3~6 个它能表达的情绪词数组,如 得意/无语/治愈/尴尬/激动/心疼),"
    "scenarios(3~6 条具体的「什么时候能用它」的使用情境句数组,"
    "如『群里有人炫耀想回怼时』『视频转场需要一个搞笑停顿时』『表达无奈又好笑的心情时』),"
    "atmosphere(营造的氛围,简短),"
    "tags(3~6 个视觉/主题标签数组)。"
    "情绪和场景要具体、贴合真实使用,能直接拿去匹配短视频片段的情绪/场景需求。"
)


def _strlist(v, cap: int) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out = [str(x).strip() for x in v if isinstance(x, (str, int)) and str(x).strip()]
    return list(dict.fromkeys(out))[:cap]


def parse_archive(content: str) -> dict:
    """把大模型返回的文本解析成归一化档案(容错:剥 markdown 围栏、取第一个 JSON 对象)。"""
    text = (content or "").strip()
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return {}
    if not isinstance(obj, dict):
        return {}
    return {
        "summary": str(obj.get("summary") or "").strip(),
        "emotions": _strlist(obj.get("emotions"), 6),
        "scenarios": _strlist(obj.get("scenarios"), 6),
        "atmosphere": str(obj.get("atmosphere") or "").strip(),
        "tags": _strlist(obj.get("tags"), 8),
    }


class DoubaoArchiver:
    """ARK 豆包视觉档案器。tag() 失败返回 {},绝不阻塞审核/入库。"""
    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")

    def tag(self, material_type: str, media_url: str = "", is_video: bool = False,
            text: str = "") -> dict:
        from app.config import settings
        from app.infrastructure.retry import call_ai
        import httpx

        parts: list[dict] = []
        if media_url:
            if is_video:
                parts.append({"type": "video_url", "video_url": {"url": media_url}})
            else:
                parts.append({"type": "image_url", "image_url": {"url": media_url}})
        prompt = "请产出这条物料的检索档案 JSON。"
        if text.strip():
            prompt += f"\n补充文字内容:{text.strip()[:2000]}"
        parts.append({"type": "text", "text": prompt})

        def _call():
            r = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                json={"model": self._model,
                      "messages": [{"role": "system", "content": _ARCHIVE_SYS},
                                   {"role": "user", "content": parts}]},
                timeout=settings.ai_timeout_s)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]

        try:
            content = call_ai(_call, timeout_s=settings.ai_timeout_s, retries=settings.ai_retries)
            return parse_archive(content)
        except Exception:
            return {}   # 档案失败不阻塞审核/入库(物料仍可用,只是暂无情绪/场景标签)

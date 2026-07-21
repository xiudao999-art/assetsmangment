"""真实视频反解适配器 —— 百炼 DashScope Qwen-VL(实现 domain.ports.VideoParser)。
把 OSS 里的视频签成 HTTPS URL 交给 Qwen-VL,按 fps 抽帧理解,返回结构化物料候选;
再用 OSS 视频截帧把每个候选对应的关键帧存回 OSS(真图,可预览/下载)。infra→domain。"""
from __future__ import annotations
import json
import re
import uuid

from app.domain.models import MaterialCandidate, MaterialType
from app.infrastructure.aliyun_oss import OssStorage

_ALLOWED = {t.value for t in MaterialType}


class QwenVLVisionDescriber:
    """图像反解成画面内容文字(实现 domain.ports.VisionDescriber)。用于图片/视频帧审核。"""
    _PROMPT = (
        "请详细描述这张图片的画面内容(中文),用于内容审核。"
        "只输出纯文本,不要使用 Markdown,不要标题,不要分隔线,不要项目符号,不要编号。"
        "按自然语序连续描述主体、场景、动作、画面中的文字、服饰、姿态、道具、环境和风格,"
        "以及任何可能涉及违规的风险点(如暴力、色情、政治敏感、违禁品等)。"
        "【重要】请逐字抄录画面中出现的所有文字,包括小字、水印、按钮文案、"
        "免责声明、金额数字、提现门槛、平台名称、下载引导语等。"
        "即使文字很小或位于边缘,也必须抄录。"
        "抄录每处文字时,必须同时说明该文字在画面中的具体位置,"
        "使用「画面顶部/下方/底部/左侧/右侧/左上角/右上角/左下角/右下角/居中/下方居中」等方位词。"
        "例如:「画面下方居中位置有三行白色小字:第一行'满0.3元能提现'……」。"
        "文字位置是审核的关键依据,必须逐一标注。"
        "如果画面中确实没有任何文字,直接描述画面内容即可,无需提及文字的有无。"
        "只客观描述实际看到的内容,不要额外做风险总结或抽象归类;"
        "不要输出否定句,例如「未见」「没有」「无」「不涉及」这类表述。"
    )

    def __init__(self, api_key: str, model: str = "qwen3-vl-plus") -> None:
        import dashscope  # 延迟导入
        self._dashscope = dashscope
        self._api_key = api_key
        self._model = model

    def describe_image(self, url: str) -> str:
        from dashscope import MultiModalConversation
        from app.config import settings
        from app.infrastructure.retry import call_ai

        def _call():
            resp = MultiModalConversation.call(
                api_key=self._api_key, model=self._model,
                messages=[{"role": "user", "content": [{"image": url}, {"text": self._PROMPT}]}])
            if getattr(resp, "status_code", None) != 200:
                raise RuntimeError(f"图像反解失败: {getattr(resp, 'status_code', '?')} "
                                   f"{getattr(resp, 'message', '')}")
            content = resp.output.choices[0].message.content
            return content[0]["text"] if isinstance(content, list) else str(content)
        return call_ai(_call, timeout_s=settings.ai_timeout_s, retries=settings.ai_retries)

_PROMPT = (
    "你是视频物料反解引擎。请把这段视频拆解成若干可复用的物料候选。"
    "只返回一个 JSON 对象,形如 {\"candidates\":[{...}]},不要 markdown、不要任何解释文字。"
    "每个候选字段:"
    "source_timecode(数字,秒,该物料在视频中的时间码);"
    "type(只能是 image|meme|video|style|corpus|music 之一);"
    "description(中文,详细且可检索的描述)。"
    "含义:image=值得截取的静态画面,meme=可做表情包的画面,video=可裁剪的连续片段,"
    "style=画面/调色/构图风格,corpus=可复用的文案或台词语料,music=背景音乐或音效。"
    "覆盖不同时间点,数量控制在 4~12 个。"
)


class QwenVLVideoParser:
    def __init__(self, api_key: str, storage: OssStorage, model: str = "qwen3-vl-plus",
                 fps: float = 2.0, max_frames: int = 512, url_ttl: int = 3600) -> None:
        import dashscope  # 延迟导入
        self._dashscope = dashscope
        self._api_key = api_key
        self._storage = storage
        self._model = model
        self._fps = fps
        self._max_frames = max_frames
        self._ttl = url_ttl

    def parse_video(self, oss_key: str) -> list[MaterialCandidate]:
        from dashscope import MultiModalConversation
        url = self._storage.signed_url(oss_key)
        resp = MultiModalConversation.call(
            api_key=self._api_key,
            model=self._model,
            messages=[{"role": "user", "content": [
                {"video": url, "fps": self._fps},
                {"text": _PROMPT},
            ]}],
        )
        if getattr(resp, "status_code", None) != 200:
            # 交给 service 层重试/FAILED(不静默放行)
            raise RuntimeError(f"Qwen-VL 反解失败: {getattr(resp, 'status_code', '?')} "
                               f"{getattr(resp, 'code', '')} {getattr(resp, 'message', '')}")
        content = resp.output.choices[0].message.content
        text = content[0]["text"] if isinstance(content, list) else str(content)
        cands = self._parse_json(text)
        # 为每个候选截帧存回 OSS(真图);失败则无独立图但保留描述
        for c in cands:
            ms = int(max(0.0, c.source_timecode) * 1000)
            dest = f"frames/{oss_key.rsplit('/', 1)[-1]}-{uuid.uuid4().hex[:8]}.jpg"
            if self._storage.snapshot_frame(oss_key, ms, dest):
                c.oss_key = dest
                c.thumb = dest
        return cands

    @staticmethod
    def _parse_json(text: str) -> list[MaterialCandidate]:
        text = (text or "").strip()
        m = re.search(r"\{.*\}", text, re.S)  # 剥离偶发 ```json 包裹
        try:
            data = json.loads(m.group(0) if m else text)
        except Exception:
            # 兜底:整段作为一条语料候选,不丢结果
            return [MaterialCandidate(type=MaterialType.CORPUS, thumb="", source_timecode=0.0,
                                      description=text[:500])]
        out: list[MaterialCandidate] = []
        for c in data.get("candidates", []):
            t = c.get("type")
            if t not in _ALLOWED:
                t = "image"
            try:
                tc = float(c.get("source_timecode", 0.0))
            except (TypeError, ValueError):
                tc = 0.0
            out.append(MaterialCandidate(
                type=MaterialType(t), thumb="", source_timecode=tc,
                description=(c.get("description") or "").strip(),
            ))
        return out

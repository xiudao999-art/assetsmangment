"""语音转写适配器 —— 百炼 paraformer-v2 异步文件转写(实现 domain.ports.Transcriber)。
直接把媒体(含 mp4)的 OSS 签名 URL 丢进去转写音轨,带句级毫秒时间轴。infra→domain。"""
from __future__ import annotations

from app.domain.models import TextSegment, TextSourceType


class DashScopeTranscriber:
    def __init__(self, api_key: str, model: str = "paraformer-v2") -> None:
        import dashscope  # 延迟导入
        self._dashscope = dashscope
        self._api_key = api_key
        self._model = model

    def transcribe(self, url: str) -> list[TextSegment]:
        from dashscope.audio.asr import Transcription
        from http import HTTPStatus
        import httpx
        from app.config import settings
        from app.infrastructure.retry import call_ai

        def _submit_and_wait():
            task = Transcription.async_call(
                api_key=self._api_key, model=self._model, file_urls=[url],
                language_hints=["zh", "en"],           # paraformer-v2 专有
                timestamp_alignment_enabled=True,      # 句/词级时间轴
            )
            resp = Transcription.wait(task=task.output.task_id, api_key=self._api_key)
            if getattr(resp, "status_code", None) != HTTPStatus.OK:
                raise RuntimeError(f"ASR 失败: {getattr(resp, 'status_code', '?')} "
                                   f"{getattr(resp, 'code', '')} {getattr(resp, 'message', '')}")
            return resp
        # 文件转写可能比一般调用久 → 给更宽的上限(≥5分钟);仍封顶,避免 wait() 无限阻塞
        resp = call_ai(_submit_and_wait, timeout_s=max(settings.ai_timeout_s, 300), retries=settings.ai_retries)

        segs: list[TextSegment] = []
        results = resp.output["results"] if isinstance(resp.output, dict) else resp.output.get("results", [])
        for r in results:
            if r.get("subtask_status") != "SUCCEEDED":
                continue
            data = httpx.get(r["transcription_url"], timeout=30).json()  # 结果 JSON 在 OSS 上
            for tr in data.get("transcripts", []):
                for s in tr.get("sentences", []):
                    segs.append(TextSegment(
                        source_type=TextSourceType.TRANSCRIPT, text=s.get("text", ""),
                        begin_ms=s.get("begin_time"), end_ms=s.get("end_time")))
        return segs

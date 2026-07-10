"""真实内容安全审核适配器 —— 阿里云内容安全增强版 green20220302(实现 domain.ports.Auditor)。
返回 pass/review/block 三态;任何异常→抛 TimeoutError,交 service 兜底为 review(不默认放行,REQ-503)。
需在阿里云控制台开通「内容安全(增强版)」并给 RAM 用户授权 AliyunYundunGreenWebFullAccess。infra→domain。"""
from __future__ import annotations
import json
import uuid

from app.domain.models import MaterialCandidate, Material


class AliyunAuditor:
    def __init__(self, access_key_id: str, access_key_secret: str, storage,
                 region: str = "cn-beijing", image_service: str = "baselineCheck") -> None:
        from alibabacloud_green20220302.client import Client
        from alibabacloud_tea_openapi.models import Config
        self._models = __import__("alibabacloud_green20220302.models", fromlist=["models"])
        from alibabacloud_tea_util import models as util_models
        self._util = util_models
        self._storage = storage
        self._image_service = image_service
        cfg = Config(access_key_id=access_key_id, access_key_secret=access_key_secret,
                     endpoint=f"green-cip.{region}.aliyuncs.com", region_id=region)
        self._client = Client(cfg)

    def audit(self, content) -> str:
        """content 可为 MaterialCandidate 或 Material。对其图片(有 oss_key)+ 文本描述取最严结果。"""
        try:
            verdicts = []
            oss_key = getattr(content, "oss_key", "") or ""
            desc = getattr(content, "description", "") or ""
            if oss_key:
                verdicts.append(self._moderate_image(self._storage.signed_url(oss_key)))
            if desc.strip():
                verdicts.append(self._moderate_text(desc))
            if not verdicts:
                return "pass"
            order = {"block": 0, "review": 1, "pass": 2}
            return min(verdicts, key=lambda v: order.get(v, 1))  # 取最严
        except Exception as e:  # 网络/权限/超时 → 不放行,转人工
            raise TimeoutError(str(e))

    def _moderate_image(self, url: str) -> str:
        req = self._models.ImageModerationRequest(
            service=self._image_service,
            service_parameters=json.dumps({"imageUrl": url, "dataId": uuid.uuid4().hex}))
        resp = self._client.image_moderation_with_options(req, self._util.RuntimeOptions())
        if resp.status_code != 200 or resp.body.code != 200:
            raise RuntimeError(f"image moderation {resp.body.code} {resp.body.msg}")
        risk = (getattr(resp.body.data, "risk_level", "") or "").lower()
        return {"none": "pass", "low": "review", "medium": "review", "high": "block"}.get(risk, "review")

    def _moderate_text(self, text: str) -> str:
        req = self._models.TextModerationRequest(
            service="comment_detection",
            service_parameters=json.dumps({"content": text[:9000], "dataId": uuid.uuid4().hex}))
        resp = self._client.text_moderation_with_options(req, self._util.RuntimeOptions())
        if resp.status_code != 200 or resp.body.code != 200:
            raise RuntimeError(f"text moderation {resp.body.code} {resp.body.msg}")
        labels = (getattr(resp.body.data, "labels", "") or "").strip()
        return "pass" if not labels else "block"

"""真实内容安全审核适配器 —— 阿里云内容安全增强版 green20220302(实现 domain.ports.Auditor)。
返回 pass/review/block 三态;任何异常→抛 TimeoutError,交 service 兜底为 review(不默认放行,REQ-503)。
需在阿里云控制台开通「内容安全(增强版)」并给 RAM 用户授权 AliyunYundunGreenWebFullAccess。infra→domain。"""
from __future__ import annotations
import json
import re
import uuid

from app.domain.models import MaterialCandidate, Material


# 文本命中哪些风险标签 → 硬拦 block(按严格度档位);其余有标签 → 转人工 review;无标签 → pass。
# 阿里云文本标签(小写子串匹配):sexual_content 色情、political_content 政治、violence 暴力、
# contraband 违禁、cyberbullying 网暴、profanity 辱骂、religion 宗教、ad 广告、terror 暴恐…
_BLOCK_SUBSTR = {
    "strict":   ("sexual", "porn", "political", "violence", "violen", "terror", "contraband", "gambl", "drug"),
    "balanced": ("sexual", "porn", "political", "terror"),   # 只硬拦 色情/政治/暴恐;暴力/违禁/辱骂/广告等→转人工
    "loose":    (),                                          # 从不硬拦,任何标签都转人工
}


def _text_verdict(labels: str, mode: str) -> str:
    labels = (labels or "").strip().lower()
    if not labels:
        return "pass"
    subs = _BLOCK_SUBSTR.get(mode, _BLOCK_SUBSTR["strict"])
    return "block" if any(s in labels for s in subs) else "review"


def _image_verdict(risk_level: str, mode: str) -> str:
    risk = (risk_level or "").lower()
    if mode == "loose":                       # 宽松:图片也只转人工,不硬拦
        return "pass" if risk in ("", "none") else "review"
    # 图片是像素信号、比文本可靠:高危硬拦,中低危转人工(strict 与 balanced 一致)
    return {"none": "pass", "low": "review", "medium": "review", "high": "block"}.get(risk, "review")


def _parse_risk_words(reason) -> str:
    """从阿里云返回的 reason(JSON 字符串或 dict)里取命中的具体风险词 riskWords。"""
    if not reason:
        return ""
    if isinstance(reason, str):
        try:
            reason = json.loads(reason)
        except Exception:
            return ""
    if isinstance(reason, dict):
        return reason.get("riskWords") or reason.get("RiskWords") or ""
    return ""


def _apply_whitelist(verdict: str, risk_words: str, whitelist) -> str:
    """白名单治误伤:阿里云命中的风险词若**全部**落在白名单里 → 放行 pass。
    risk_words 形如 '词1,词2' 或 '词1&词2';whitelist 为词集合。"""
    if verdict == "pass" or not risk_words or not whitelist:
        return verdict
    words = [w.strip() for w in re.split(r"[,&、;;\s]+", risk_words) if w.strip()]
    if not words:
        return verdict
    def covered(w: str) -> bool:
        return any(white and (white in w or w in white) for white in whitelist)
    return "pass" if all(covered(w) for w in words) else verdict


class AliyunAuditor:
    def __init__(self, access_key_id: str, access_key_secret: str, storage,
                 region: str = "cn-beijing", image_service: str = "baselineCheck",
                 mode: str = "strict", whitelist=None) -> None:
        self._mode = mode if mode in _BLOCK_SUBSTR else "strict"
        self._whitelist = whitelist or (lambda: set())   # 返回当前白名单词集合(每次审核实时读)
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
        return self.audit_detail(content)[0]

    def audit_detail(self, content) -> tuple[str, str]:
        """同 audit(),但**额外返回**阿里云命中的具体风险词 riskWords(供报告标红 + 一键加白)。
        图片接口不返回词 → 图片分支词为空;文本分支给词。返回 (verdict, risk_words)。"""
        try:
            verdicts = []
            risk_words = ""
            oss_key = getattr(content, "oss_key", "") or ""
            desc = getattr(content, "description", "") or ""
            if oss_key:
                verdicts.append(self._moderate_image(self._storage.signed_url(oss_key)))
            if desc.strip():
                v, risk_words = self._moderate_text_detail(desc)
                verdicts.append(v)
            if not verdicts:
                return "pass", ""
            order = {"block": 0, "review": 1, "pass": 2}
            return min(verdicts, key=lambda v: order.get(v, 1)), risk_words  # 取最严
        except Exception as e:  # 网络/权限/超时 → 不放行,转人工
            raise TimeoutError(str(e))

    def _ropts(self):
        """RuntimeOptions:超时 + SDK 自带重试(内容安全默认关,开启时才走真调用)。"""
        from app.config import settings
        r = self._util.RuntimeOptions()
        r.read_timeout = max(1000, int(settings.ai_timeout_s) * 1000)
        r.connect_timeout = 10000
        r.autoretry = True
        r.max_attempts = max(1, int(settings.ai_retries))
        return r

    def _moderate_image(self, url: str) -> str:
        req = self._models.ImageModerationRequest(
            service=self._image_service,
            service_parameters=json.dumps({"imageUrl": url, "dataId": uuid.uuid4().hex}))
        resp = self._client.image_moderation_with_options(req, self._ropts())
        if resp.status_code != 200 or resp.body.code != 200:
            raise RuntimeError(f"image moderation {resp.body.code} {getattr(resp.body, 'msg', '')}")
        return _image_verdict(getattr(resp.body.data, "risk_level", ""), self._mode)

    def _moderate_text_detail(self, text: str) -> tuple[str, str]:
        """文本审核 → (白名单处理后的 verdict, 阿里云命中的具体风险词 riskWords)。"""
        req = self._models.TextModerationRequest(
            service="comment_detection",
            service_parameters=json.dumps({"content": text[:9000], "dataId": uuid.uuid4().hex}))
        resp = self._client.text_moderation_with_options(req, self._ropts())
        if resp.status_code != 200 or resp.body.code != 200:
            raise RuntimeError(f"text moderation {resp.body.code} {getattr(resp.body, 'msg', '')}")
        verdict = _text_verdict(getattr(resp.body.data, "labels", ""), self._mode)
        risk_words = _parse_risk_words(getattr(resp.body.data, "reason", ""))
        return _apply_whitelist(verdict, risk_words, self._whitelist()), risk_words   # 白名单治误伤

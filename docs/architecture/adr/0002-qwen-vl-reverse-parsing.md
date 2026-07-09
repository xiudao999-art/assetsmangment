# ADR-0002:视频/图片反解用百炼 Qwen-VL

- 状态:已接受
- 日期:2026-07-09
- 关联需求:REQ-012 上传视频反解物料并自动送审

## 决策
用**阿里云百炼 Qwen-VL(qwen3-vl-plus / qwen-vl-max)+ DashScope SDK** 做视频/图片反解;向量用**百炼 multimodal-embedding**。

## 理由
- 直接吃 OSS 签名 URL,支持 `fps` 抽帧、`max_frames` 控帧;与部署环境(阿里云)同域,稳定省事。
- 免自建 GPU 集群;大批量非实时用 Batch 异步接口省 50%。

## 后果
- 反解在 Celery Worker 异步执行(避免阻塞 API,满足 10s 内返回的 REQ-012)。
- 反解结果 → 内容安全审核 → 写回 pgvector;prompt/抽帧参数由 ai-effect-validator 持续调优。

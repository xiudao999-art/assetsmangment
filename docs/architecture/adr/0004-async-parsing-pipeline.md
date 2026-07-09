# ADR-0004:视频反解异步流水线(Celery + 重试)

- 状态:已接受 · 日期:2026-07-09 · 关联:REQ-201/204

## 决策
上传视频只做"受理"(存 OSS + 建 job + 返 jobId,≤10s),反解全部丢 **Celery** 异步:抽帧→Qwen-VL→embedding→审核→写回。失败**重试 ≤3 次**,仍失败 job=failed 且保留原视频。

## 理由
反解耗时长,同步会阻塞、无法满足 10s 受理与 500 并发。异步 + 独立 Worker 队列可单独扩缩。

## 后果
- 前端轮询 `GET /videos/{jobId}` 或 WebSocket 通知。
- 反解为重活,Worker 与 API 分开部署、分开 HPA(见 [[0008-scaling]])。

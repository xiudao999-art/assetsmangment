"""视频反解编排服务(REQ-201/202/204)。只依赖 domain 端口,infra 由外部注入。"""
from __future__ import annotations
import uuid
from app.domain.models import (
    VideoJob, JobStatus, Material, AuditStatus,
)
from app.domain.ports import VideoParser, Embedder, Auditor, MaterialRepo, ObjectStorage


class VideoParsingService:
    def __init__(
        self,
        parser: VideoParser,
        embedder: Embedder,
        auditor: Auditor,
        repo: MaterialRepo,
        storage: ObjectStorage,
    ) -> None:
        self._parser = parser
        self._embedder = embedder
        self._auditor = auditor
        self._repo = repo
        self._storage = storage

    def accept_upload(self, oss_key: str, size_bytes: int) -> VideoJob:
        """REQ-201:仅受理(建 job 立即返回),反解异步进行。这里同步很快返回。"""
        return VideoJob(id=uuid.uuid4().hex, oss_key=oss_key, size_bytes=size_bytes)

    def run_job(self, job: VideoJob, owner_id: str = "", max_retry: int = 3) -> list[Material]:
        """REQ-202/204:反解→embedding→审核→入库;审核超时→review(不放行);
        反解失败重试≤3,耗尽→FAILED(不抛异常、不 500);单帧失败跳过;原视频始终保留。"""
        job.status = JobStatus.RUNNING
        materials: list[Material] = []

        # REQ-204:反解失败重试(≤max_retry);全失败 → 标记 FAILED 并保留原视频
        cands = None
        for attempt in range(1, max_retry + 1):
            try:
                cands = self._parser.parse_video(job.oss_key)
                break
            except Exception:
                job.retry = attempt
                if attempt >= max_retry:
                    job.status = JobStatus.FAILED  # 不删 oss_key(原视频保留)
                    return materials

        for cand in (cands or []):
            try:
                embedding = self._embedder.embed(cand)
                try:
                    status = AuditStatus(self._auditor.audit(cand))
                except TimeoutError:
                    status = AuditStatus.REVIEW  # REQ-503/204:超时不默认放行
                material = Material(
                    id=uuid.uuid4().hex,
                    type=cand.type,
                    thumb=cand.thumb,
                    source_timecode=cand.source_timecode,
                    embedding=embedding,
                    audit_status=status,
                    source_job=job.id,
                    oss_key=cand.oss_key,   # 反解截帧图(可预览/下载),假实现为空
                    owner_id=owner_id,
                    description=cand.description,
                )
                self._repo.save(material)
                materials.append(material)
            except Exception:
                continue  # 单帧失败不拖垮整段反解
        job.status = JobStatus.DONE
        return materials

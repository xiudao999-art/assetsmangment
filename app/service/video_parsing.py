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

    def run_job(self, job: VideoJob) -> list[Material]:
        """REQ-202/204:反解→embedding→审核→入库;审核超时→review(不放行);保留原视频。"""
        job.status = JobStatus.RUNNING
        materials: list[Material] = []
        for cand in self._parser.parse_video(job.oss_key):
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
            )
            self._repo.save(material)
            materials.append(material)
        # REQ-204:原视频保留(不删除 oss_key)
        job.status = JobStatus.DONE
        return materials

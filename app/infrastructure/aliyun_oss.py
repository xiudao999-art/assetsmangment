"""真实 OSS 适配器(实现 ObjectStorage 端口)。Phase 2 用;需配 AM_OSS_* 环境变量。
SDK 延迟导入,保证无 oss2/无密钥时模块仍可 import(仅实例化时才连)。"""
from __future__ import annotations
from app.config import settings
from app.domain.mp4 import parse_mp4_duration_ms


class OssStorage:
    def __init__(self) -> None:
        import oss2  # 延迟导入
        auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
        self._bucket = oss2.Bucket(auth, settings.oss_endpoint, settings.oss_bucket)

    def put(self, oss_key: str, data: bytes) -> None:
        self._bucket.put_object(oss_key, data)

    def put_fileobj(self, oss_key: str, fileobj) -> None:
        """流式上传到 OSS。≥10MB 用分片并发(UCloud 多连接),小文件直传。"""
        # 取文件大小,判断是否走分片
        try:
            fileobj.seek(0, 2)
            size = fileobj.tell()
            fileobj.seek(0)
        except Exception:
            size = None
        if size and size >= 10 * 1024 * 1024:
            self._multipart_upload(oss_key, fileobj, size)
        else:
            self._bucket.put_object(oss_key, fileobj)

    def _multipart_upload(self, oss_key: str, fileobj, total_size: int) -> None:
        """分片并发上传:多线程并行 PUT 各分片,大文件显著快于单连接 PUT。"""
        import oss2 as _oss2
        from concurrent.futures import ThreadPoolExecutor, as_completed

        part_size = max(1024 * 1024, total_size // 10)  # 1MB ~ total/10
        # 预读全部分片到内存(上游已有 data bytes,不增加峰值)
        chunks: list[bytes] = []
        while True:
            chunk = fileobj.read(part_size)
            if not chunk:
                break
            chunks.append(chunk)

        if not chunks:
            self._bucket.put_object(oss_key, b"")
            return

        upload_id = self._bucket.init_multipart_upload(oss_key).upload_id
        part_infos: list = [None] * len(chunks)
        try:
            workers = min(4, len(chunks))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(self._bucket.upload_part, oss_key, upload_id, i + 1, chunks[i]): i
                    for i in range(len(chunks))
                }
                for fut in as_completed(futures):
                    idx = futures[fut]
                    result = fut.result()
                    part_infos[idx] = _oss2.models.PartInfo(idx + 1, result.etag)
            self._bucket.complete_multipart_upload(oss_key, upload_id, part_infos)
        except Exception:
            self._bucket.abort_multipart_upload(oss_key, upload_id)
            raise

    def signed_url(self, oss_key: str) -> str:
        # 受时限签名 URL(REQ-102)—— 预览用
        return self._bucket.sign_url("GET", oss_key, settings.oss_url_expire_seconds)

    def download_url(self, oss_key: str) -> str:
        # 受时限签名 URL(强制浏览器下载:response-content-disposition=attachment)
        fname = oss_key.split("/")[-1] or "download"
        params = {"response-content-disposition": f'attachment; filename="{fname}"'}
        return self._bucket.sign_url("GET", oss_key, settings.oss_url_expire_seconds, params=params)

    def snapshot_url(self, oss_key: str, ms: int = 1000) -> str:
        # 视频封面:签名 URL 带 video/snapshot 处理,浏览器 <img> 直接拿到某帧 JPG(H264/H265)
        params = {"x-oss-process": f"video/snapshot,t_{int(ms)},f_jpg,w_640,m_fast"}
        return self._bucket.sign_url("GET", oss_key, settings.oss_url_expire_seconds, params=params)

    def exists(self, oss_key: str) -> bool:
        return self._bucket.object_exists(oss_key)

    def delete(self, oss_key: str) -> None:
        self._bucket.delete_object(oss_key)

    def video_duration_ms(self, oss_key: str):
        """取视频时长(毫秒)。解析 mp4 的 mvhd atom(timescale+duration),无需 IMM/ffmpeg。
        moov 可能在文件头(faststart)或尾部,两处都试;拿不到返回 None。"""
        try:
            head = self._bucket.get_object(oss_key, byte_range=(0, 512 * 1024 - 1)).read()
            d = parse_mp4_duration_ms(head)
            if d:
                return d
            size = self._bucket.head_object(oss_key).content_length
            if size and size > 512 * 1024:
                tail = self._bucket.get_object(oss_key, byte_range=(size - 512 * 1024, size - 1)).read()
                return parse_mp4_duration_ms(tail)
        except Exception:
            return None
        return None

    def snapshot_frame(self, video_key: str, ms: int, dest_key: str) -> bool:
        """用 OSS 视频截帧(video/snapshot,无需 ffmpeg)取某时间点的帧图,存回 OSS。
        用精确模式(不加 m_fast)——取时间点 t 的真实那一帧,不同时间点得到不同画面
        (m_fast 会吸附到最近关键帧,短视频会导致多帧相同)。仅 H264/H265;失败返回 False。"""
        try:
            data = self._bucket.get_object(
                video_key, process=f"video/snapshot,t_{int(ms)},f_jpg"
            ).read()
            if not data:
                return False
            self._bucket.put_object(dest_key, data)
            return True
        except Exception:
            return False

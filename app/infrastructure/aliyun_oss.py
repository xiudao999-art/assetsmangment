"""真实 OSS 适配器(实现 ObjectStorage 端口)。Phase 2 用;需配 AM_OSS_* 环境变量。
SDK 延迟导入,保证无 oss2/无密钥时模块仍可 import(仅实例化时才连)。"""
from __future__ import annotations
from app.config import settings


def _mvhd_duration_ms(data: bytes):
    """从 mp4 字节里找 mvhd atom,读 timescale/duration 算时长(ms)。"""
    i = data.find(b"mvhd")
    if i < 0:
        return None
    v = data[i + 4]  # mvhd 内容首字节 = version
    try:
        if v == 0:
            if i + 24 > len(data):
                return None
            ts = int.from_bytes(data[i + 16:i + 20], "big")
            du = int.from_bytes(data[i + 20:i + 24], "big")
        else:  # version 1:创建/修改时间各 8 字节
            if i + 36 > len(data):
                return None
            ts = int.from_bytes(data[i + 24:i + 28], "big")
            du = int.from_bytes(data[i + 28:i + 36], "big")
        return int(du / ts * 1000) if ts else None
    except Exception:
        return None


class OssStorage:
    def __init__(self) -> None:
        import oss2  # 延迟导入
        auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
        self._bucket = oss2.Bucket(auth, settings.oss_endpoint, settings.oss_bucket)

    def put(self, oss_key: str, data: bytes) -> None:
        self._bucket.put_object(oss_key, data)

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
            d = _mvhd_duration_ms(head)
            if d:
                return d
            size = self._bucket.head_object(oss_key).content_length
            if size and size > 512 * 1024:
                tail = self._bucket.get_object(oss_key, byte_range=(size - 512 * 1024, size - 1)).read()
                return _mvhd_duration_ms(tail)
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

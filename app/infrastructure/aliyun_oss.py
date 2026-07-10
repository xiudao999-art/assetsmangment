"""真实 OSS 适配器(实现 ObjectStorage 端口)。Phase 2 用;需配 AM_OSS_* 环境变量。
SDK 延迟导入,保证无 oss2/无密钥时模块仍可 import(仅实例化时才连)。"""
from __future__ import annotations
from app.config import settings


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

    def exists(self, oss_key: str) -> bool:
        return self._bucket.object_exists(oss_key)

    def delete(self, oss_key: str) -> None:
        self._bucket.delete_object(oss_key)

    def snapshot_frame(self, video_key: str, ms: int, dest_key: str) -> bool:
        """用 OSS 视频截帧(video/snapshot,无需 ffmpeg)取某时间点的帧图,存回 OSS。
        仅 H264/H265 有效;失败返回 False(反解仍继续,只是该帧无独立图)。"""
        try:
            data = self._bucket.get_object(
                video_key, process=f"video/snapshot,t_{int(ms)},f_jpg,m_fast"
            ).read()
            if not data:
                return False
            self._bucket.put_object(dest_key, data)
            return True
        except Exception:
            return False

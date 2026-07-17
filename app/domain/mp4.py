"""MP4 二进制解析工具。纯函数,零外部依赖,供 infra 与 api 层复用。"""
from __future__ import annotations


def parse_mp4_duration_ms(data: bytes) -> int | None:
    """从 mp4 字节里找 mvhd atom,读 timescale/duration 算时长(ms)。拿不到返回 None。"""
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
        else:  # version 1: timescale + duration 各 4/8 字节
            if i + 36 > len(data):
                return None
            ts = int.from_bytes(data[i + 24:i + 28], "big")
            du = int.from_bytes(data[i + 28:i + 36], "big")
        return int(du / ts * 1000) if ts else None
    except Exception:
        return None

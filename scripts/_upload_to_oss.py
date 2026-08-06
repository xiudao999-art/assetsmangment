"""将本地文件直传 OSS（不启动服务、不走 AI 解析/标签）。
用法：.venv\Scripts\python scripts\_upload_to_oss.py <文件路径> [oss目录前缀]
示例：.venv\Scripts\python scripts\_upload_to_oss.py "resources\物料\music_batch\0001_茶花开了，该回家了_王睿卓,加木.mp3"
      .venv\Scripts\python scripts\_upload_to_oss.py "resources\物料\music_batch\0001_茶花开了，该回家了_王睿卓,加木.mp3" audio
"""
from __future__ import annotations
import sys, os, uuid, dotenv

# 确保项目根在 sys.path 里
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

dotenv.load_dotenv(".env")

from app.infrastructure.aliyun_oss import OssStorage


def main(file_path: str, prefix: str = "materials") -> None:
    if not os.path.isfile(file_path):
        print(f"[ERROR] 文件不存在: {file_path}")
        sys.exit(1)

    fname = os.path.basename(file_path)
    oss_key = f"{prefix}/{uuid.uuid4().hex[:12]}-{fname}"
    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    print(f"文件: {file_path} ({size_mb:.1f} MB)")
    print(f"OSS:  oss://{oss_key}")
    print("上传中...")

    storage = OssStorage()
    with open(file_path, "rb") as f:
        storage.put_fileobj(oss_key, f)

    print(f"[OK] 上传完成")
    print(f"     oss_key = {oss_key}")
    print(f"     预览URL = {storage.signed_url(oss_key)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    prefix = sys.argv[2] if len(sys.argv) > 2 else "materials"
    main(sys.argv[1], prefix)

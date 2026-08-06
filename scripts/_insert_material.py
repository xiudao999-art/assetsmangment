"""将已上传 OSS 的文件写入 material 表（不走服务、不触发 AI 解析）。
用法：.venv\Scripts\python scripts\_insert_material.py <文件路径> <oss_key> [owner_id]
示例：.venv\Scripts\python scripts\_insert_material.py "resources\物料\music_batch\0001_茶花开了，该回家了_王睿卓,加木.mp3" "materials/e65b4a072cbb-0001_茶花开了，该回家了_王睿卓,加木.mp3"
"""
from __future__ import annotations
import sys, os, hashlib, dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
dotenv.load_dotenv(".env")

from app.infrastructure.snowflake import next_id
import psycopg


def main(file_path: str, oss_key: str, owner_id: str | None = None) -> None:
    dsn = os.getenv("AM_DATABASE_URL")
    if not dsn:
        print("[ERROR] AM_DATABASE_URL 未配置")
        sys.exit(1)

    if not os.path.isfile(file_path):
        print(f"[ERROR] 文件不存在: {file_path}")
        sys.exit(1)

    fname = os.path.basename(file_path)

    # 推断物料类型
    ext = os.path.splitext(file_path)[1].lower()
    type_map = {
        ".mp3": "music", ".wav": "audio", ".flac": "audio", ".aac": "audio",
        ".ogg": "audio", ".m4a": "audio", ".wma": "audio",
        ".mp4": "video", ".avi": "video", ".mov": "video", ".mkv": "video",
        ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "meme",
        ".webp": "image", ".bmp": "image",
        ".txt": "corpus",
    }
    mtype = type_map.get(ext, "image")

    # 计算 content_hash (MD5)
    print(f"计算 MD5...")
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            md5.update(chunk)
    content_hash = md5.hexdigest()

    # 查找 owner（默认用第一个 admin 的 domain_id，未传 owner_id 时）
    # 注意：material.owner_id 存的是 domain_id（如 "admin"），不是雪花 BIGINT id
    if not owner_id:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT domain_id FROM app_user WHERE role = 'admin' AND del_flag = 0 ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                owner_id = row[0]  # domain_id，如 "admin"
                print(f"owner_id: {owner_id} (auto-detected admin)")
            else:
                owner_id = ""
                print("[WARN] 未找到 admin 用户，owner_id 留空")

    # 雪花 ID
    mid = next_id()
    print(f"material id: {mid}")
    print(f"type: {mtype}")
    print(f"oss_key: {oss_key}")
    print(f"content_hash: {content_hash}")

    # 写入 material 表
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO material
                (id, type, thumb, source_timecode, audit_status, source_job,
                 oss_key, description, owner_id, is_public, audit_report_id,
                 content_hash, project_id, tags, ai_summary, ai_scenarios,
                 ai_emotions, ai_atmosphere, reject_events,
                 create_by, update_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (mid, mtype, f"{oss_key}#thumb", 0.0, "review", "",
             oss_key, fname, owner_id, False, "",
             content_hash, "",
             "[]", "", "[]", "[]", "", "[]",
             owner_id, owner_id),
        )

    print(f"[OK] 物料已入库")
    print(f"     id = {mid}")
    print(f"     type = {mtype}")
    print(f"     status = review（待人工审核）")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    owner = sys.argv[3] if len(sys.argv) > 3 else None
    main(sys.argv[1], sys.argv[2], owner)

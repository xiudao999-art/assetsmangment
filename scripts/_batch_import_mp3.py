"""批量导入 MP3 文件：上传 OSS + 写入 material 表（不走 AI 解析）。
用法：.venv\Scripts\python scripts\_batch_import_mp3.py "resources\物料\music_batch"
"""
from __future__ import annotations
import sys, os, hashlib, uuid, time, dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
dotenv.load_dotenv(".env")

from app.infrastructure.aliyun_oss import OssStorage
from app.infrastructure.snowflake import next_id
import psycopg


def main(src_dir: str) -> None:
    dsn = os.getenv("AM_DATABASE_URL")
    if not dsn:
        print("[ERROR] AM_DATABASE_URL 未配置")
        sys.exit(1)

    if not os.path.isdir(src_dir):
        print(f"[ERROR] 目录不存在: {src_dir}")
        sys.exit(1)

    # 找 admin 的 domain_id 作为 owner
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT domain_id FROM app_user WHERE role = 'admin' AND del_flag = 0 ORDER BY id LIMIT 1"
        ).fetchone()
        owner_id = row[0] if row else ""
        if not owner_id:
            print("[ERROR] 未找到 admin 用户")
            sys.exit(1)
        print(f"owner_id: {owner_id}")

        # 取已有 content_hash 集合，跳过去重
        existing = set()
        rows = conn.execute(
            "SELECT content_hash FROM material WHERE owner_id = %s AND del_flag = 0",
            (owner_id,)
        ).fetchall()
        for r in rows:
            if r[0]:
                existing.add(r[0])
        print(f"已有 {len(existing)} 条物料（按 content_hash 去重）")

    storage = OssStorage()
    files = sorted(
        [f for f in os.listdir(src_dir) if f.lower().endswith(".mp3") and os.path.isfile(os.path.join(src_dir, f))]
    )
    total = len(files)
    print(f"待处理: {total} 个 MP3 文件\n")

    ok, skip, fail = 0, 0, 0
    t0 = time.time()

    for i, fname in enumerate(files, 1):
        fpath = os.path.join(src_dir, fname)
        try:
            # 计算 MD5
            md5 = hashlib.md5()
            with open(fpath, "rb") as f:
                while chunk := f.read(65536):
                    md5.update(chunk)
            content_hash = md5.hexdigest()

            # 去重
            if content_hash in existing:
                try:
                    print(f"[{i}/{total}] SKIP {fname} (重复)")
                except UnicodeEncodeError:
                    print(f"[{i}/{total}] SKIP (unicode name, 重复)")
                skip += 1
                continue

            # 上传 OSS
            oss_key = f"materials/{uuid.uuid4().hex[:12]}-{fname}"
            size_mb = os.path.getsize(fpath) / (1024 * 1024)
            with open(fpath, "rb") as f:
                storage.put_fileobj(oss_key, f)

            # 写入 material 表
            mid = next_id()
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(
                    """INSERT INTO material
                        (id, type, thumb, source_timecode, audit_status, source_job,
                         oss_key, description, owner_id, is_public, audit_report_id,
                         content_hash, project_id, tags, ai_summary, ai_scenarios,
                         ai_emotions, ai_atmosphere, reject_events, create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (mid, "music", f"{oss_key}#thumb", 0.0, "review", "",
                     oss_key, fname, owner_id, False, "",
                     content_hash, "",
                     "[]", "", "[]", "[]", "", "[]",
                     owner_id, owner_id),
                )

            existing.add(content_hash)
            # 安全打印（文件名可能含 emoji 等 GBK 不支持的字符）
            try:
                print(f"[{i}/{total}] OK  {fname} ({size_mb:.1f}MB)")
            except UnicodeEncodeError:
                print(f"[{i}/{total}] OK  (file with unicode name, {size_mb:.1f}MB)")
            ok += 1

        except Exception as e:
            try:
                print(f"[{i}/{total}] FAIL {fname}: {e}")
            except UnicodeEncodeError:
                print(f"[{i}/{total}] FAIL (unicode name): {e}")
            fail += 1

    elapsed = time.time() - t0
    print(f"\n--- 完成 ---")
    print(f"成功: {ok}, 跳过(重复): {skip}, 失败: {fail}")
    print(f"耗时: {elapsed:.0f}s")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])

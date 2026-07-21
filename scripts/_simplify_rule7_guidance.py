"""精简 Rule #7 的 guidance（507 chars → ~180 chars），去重去啰嗦。"""
import os, dotenv, psycopg

dotenv.load_dotenv(".env")
dsn = os.getenv("AM_DATABASE_URL")

NEW_GUIDANCE = (
    "检查视频帧描述：仅「底部居中」或等效表述（底部正中、底端中央、正下方居中）才算合规。"
    "免责声明出现在其他位置（右上角、左上角、顶部、底部左侧/右侧等）= 未展示 = 违规。"
    "仅提「底部」但未体现「居中」（如底部右侧、底部滚动字幕）→ 仍属违规。\n"
    "3D/MG动画等动态素材：免责声明若持续稳定悬浮于画面底部水平中心区域"
    "（底部1/5高度内、左右大致居中），视为等效合规。"
    "若左右明显偏移（偏一侧超过画面宽度2/3）→ 违规。"
    "描述模糊（仅「底部显示文字」「底部有小字」等，无位置精度信息）→ 不可推定合规。"
)

with psycopg.connect(dsn, autocommit=True) as conn:
    row = conn.execute(
        "SELECT id, guidance FROM audit_rule WHERE no=7 AND del_flag=0"
    ).fetchone()

    if row is None:
        print("Rule #7 not found!")
        exit(1)

    old_len = len(row[1] or "")
    conn.execute(
        "UPDATE audit_rule SET guidance=%s, update_time=now() WHERE id=%s",
        (NEW_GUIDANCE, row[0]),
    )

    print(f"Rule #7 guidance: {old_len} → {len(NEW_GUIDANCE)} chars")
    print(f"NEW:\n{NEW_GUIDANCE}")

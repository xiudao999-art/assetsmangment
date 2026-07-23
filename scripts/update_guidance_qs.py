"""重写 Rule #8 和 #13 的完整 guidance，控制在 300 字内"""
import os, dotenv, psycopg

dotenv.load_dotenv(".env")
dsn = os.getenv("AM_DATABASE_URL")

UPDATES = {
    # Rule #8: 整条重写，融入主观感受+独家放行
    203708811905597440: (
        "仅限同时满足两要件才构成违规：①存在对比、拉踩、贬损其他平台或产品的表述；"
        "②使用《广告法》禁用词或具至高性暗示的网络化表达（如「神器」「天花板」「王炸」「YYDS」等）。"
        "放行：平台功能描述无对比不触发；「没有之一」「最好用」「最爱」等主观感受不在此列；"
        "「独家听歌」「独家曲库」等版权/功能描述属正常商业用语；"
        "「急得想砸手机」等夸张修辞属日常口语。"
        "网络流行语不视为禁用词替代词——即使存在功能对比，要件②不满足不触发。"
    ),
    # Rule #13: 整条重写，融入硬性前提
    203708814623506432: (
        "【硬性前提】本规则仅审视包含明确比较词（比/更/不如/秒杀/碾压等）"
        "或贬损语义（low/垃圾/太烂等）的表述。"
        "仅描述自身功能即使提及竞品也不得推定为拉踩——必须有显性的比较或贬损措辞。"
        "无「比」字直接放行。"
        "「比」字与贬损词直接关联（如「比XX low」）才违规。"
        "放行：「抖音同款」「XX同款」——「同款」是正向关联；"
        "「碎片时间」「刷视频」等中性场景描述；"
        "「以前到处找歌」「过去听歌太麻烦」——用户个人体验不构成拉踩。"
        "不得从非贬损性表述中推断隐性对比或贬低意味。"
    ),
    # Rule #2: 只追加（不会超300），恢复后重新执行追加
    203708803638624256: None,  # skip, already correct
}

with psycopg.connect(dsn, autocommit=True) as conn:
    for rid, new_guidance in UPDATES.items():
        if new_guidance is None:
            continue
        row = conn.execute(
            "SELECT no, guidance FROM audit_rule WHERE id=%s AND del_flag=0",
            (rid,),
        ).fetchone()
        no, old = row
        conn.execute(
            "UPDATE audit_rule SET guidance=%s, update_time=now() WHERE id=%s",
            (new_guidance, rid),
        )
        print(f"Rule #{no}: {len(old)}字 → {len(new_guidance)}字")
        print(f"新: {new_guidance}")
        print()

print("Done")

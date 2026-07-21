"""
精简规则 condition，把细节/场景/反例从 condition 移到 guidance。
原则：condition 简略达意（一句话说清拦截什么），guidance 承载所有细节。

运行前请确认已备份数据库。
"""
import os, json, dotenv, psycopg

dotenv.load_dotenv(".env")
dsn = os.getenv("AM_DATABASE_URL")

# 每条规则的调整：(rule_no, new_condition, extra_guidance)
# extra_guidance 为从 condition 移到 guidance 的内容，会追加到现有 guidance 末尾
ADJUSTMENTS = [
    # ── Rule #3 明星肖像：括号内排除项 → guidance ──
    (3,
     "画面中出现未授权的明星、真人、影视角色、知名IP形象或领导人等具象化形象",
     "判断标准：须为可指向特定真人或官方IP的具象化还原。不包括无真实人物映射的抽象AI形象、"
     "通用种族/性别表情包、或仅含通用职业/情绪特征的抽象化形象。"),

    # ── Rule #4 网赚诈骗：不包含 → guidance ──
    (4,
     "宣称可高额赚取现金收益（日入过百/月入过万级别）的网赚或理财诈骗话术",
     "不包含：音乐/视频/阅读平台内听歌看视频赚小额金币/积分/红包"
     "（含自动兑换、自动提现等产品功能描述），此类一律不视为违规。"),

    # ── Rule #5 功能虚假声称：操作指引 → guidance ──
    (5,
     "宣称app内无广告但实际存在激励视频广告，或宣称app内赚钱无提现门槛但实际设有提现门槛",
     "仅针对这两类明确的功能虚假声称，不要扩展到其他功能描述。"
     "也适用于以文字、语音等直接陈述式声明的情形。"),

    # ── Rule #7 免责声明位置（最冗余，241 chars → 30）──
    (7,
     "视频画面底部居中位置必须展示免责提示语，其他位置一律视为未展示",
     # 所有细节已在现有 guidance 中充分覆盖，condition 不再重复
     ""),

    # ── Rule #10 外跳引导：字节系枚举 + 豁免 + 判定逻辑 → guidance ──
    (10,
     "出现非字节跳动系App名称并伴有外跳号召（如「下载XX」「去XX看」），构成外跳引导",
     "字节系App（抖音、汽水音乐、今日头条、西瓜视频、飞书、番茄小说等）互相提及"
     "属于正常生态内导流，不触发。若仅推广字节系App且无站外跳转指令，不构成外跳引导。"
     "判定须满足「出现站外App名称」或「存在明确跳转指令」之一。"),

    # ── Rule #11 第三方平台标识：App 罗列已在 keywords，condition 精简 ──
    (11,
     "画面中出现非字节跳动系的第三方平台标识（logo或文字）",
     "如微信、QQ、快手、B站、小红书等logo或文字标识。不包括抖音等字节跳动自有平台。"),

    # ── Rule #13 拉踩贬损：碎片化细节 → guidance ──
    (13,
     "出现拉踩或贬损其他短视频平台的内容",
     "包括但不限于映射、对比、负面评价等形式。"
     "「碎片化」仅在明确搭配负面结果（如「导致注意力涣散」「没营养」）时才触发；"
     "单纯描述使用场景（如「利用碎片时间」）不构成违规。"),

    # ── Rule #17 播放键：补主语 ──
    (17,
     "视频画面中出现非功能性的播放键图标",
     ""),

    # ── Rule #18 赚生活费：不包含 → guidance ──
    (18,
     "描述网赚收益可覆盖基本生活开销（如买菜、生活费、房租），暗示稳定收入来源",
     "不包含：奶茶、零食、水钱、零花钱等娱乐轻消费。"
     "也不包含音乐/视频平台内通过听歌、看视频等基础行为获取的小额金币等非现金兑换型虚拟奖励。"),

    # ── Rule #19 版权IP：补主语 ──
    (19,
     "使用知名IP形象作为视频素材，且无版权授权证明",
     ""),

    # ── Rule #25 免责声明(简版)：精简 ──
    (25,
     "视频画面底部居中位置必须展示免责提示语，未展示即违规",
     "示例免责提示语：「所示金额为广告创意」「实际奖励以活动规则为准」等。"),

    # ── Rule #26 字幕口播一致：适用范围 → guidance ──
    (26,
     "字幕内容需与口播语音一致，不得出现错字、漏字、多字等不符情况",
     "本规则仅适用于含真实人声口播的视频物料。不含纯UI演示、图文轮播、"
     "无配音旁白或仅有背景音乐/音效的素材。AI合成语音、TTS播报、画外旁白（无对应口型）均不在此列。"),
]


def main():
    with psycopg.connect(dsn, autocommit=True) as conn:
        for rule_no, new_condition, extra_guidance in ADJUSTMENTS:
            # 读取当前规则
            row = conn.execute(
                "SELECT id, condition, guidance FROM audit_rule WHERE no=%s AND del_flag=0",
                (rule_no,)
            ).fetchone()

            if row is None:
                print(f"⚠ Rule #{rule_no} 未找到，跳过")
                continue

            rule_id, old_cond, old_guide = row
            old_cond = old_cond or ""
            old_guide = old_guide or ""

            # 构建新 guidance：旧 guidance + 从 condition 移入的内容
            if extra_guidance:
                if old_guide:
                    new_guidance = old_guide.rstrip() + "\n" + extra_guidance
                else:
                    new_guidance = extra_guidance
            else:
                new_guidance = old_guide  # 不变

            # 更新
            conn.execute(
                "UPDATE audit_rule SET condition=%s, guidance=%s, update_time=now() WHERE id=%s",
                (new_condition, new_guidance, rule_id),
            )

            print(f"✓ Rule #{rule_no}:")
            print(f"  condition: {len(old_cond)} → {len(new_condition)} chars")
            print(f"    OLD: {old_cond[:80]}...")
            print(f"    NEW: {new_condition}")
            if extra_guidance:
                print(f"  guidance: +{len(extra_guidance)} chars appended")
            print()

    print("Done. 所有规则已更新。")


if __name__ == "__main__":
    main()

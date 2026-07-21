"""Merge rule #19 into #3 — both are about unauthorized IP/celebrity content."""
import os, json, dotenv
dotenv.load_dotenv('.env')
import psycopg
from psycopg.types.json import Jsonb
from app.infrastructure.snowflake import next_id

dsn = os.getenv('AM_DATABASE_URL')

# 合并后的 Rule #3
R3_ID = '203708805328928768'
R19_ID = '203708806595608576'

new_keywords = ['杰瑞', '海绵宝宝', '玉桂狗', '小黄人', '蜡笔小新']
new_condition = (
    '内容中出现未授权的明星、真人、影视角色、知名IP形象或领导人等'
    '可明确识别的具象化形象或IP名称'
)

# 保留 #3 的完整 guidance，末尾追加 #19 的文本场景覆盖说明
new_guidance = (
    '仅限明确可识别的真人明星、影视动漫IP角色'
    '（如知名演员、迪士尼角色、日漫主角、明星肖像等）。以下情况不算违规：\n'
    '1. 普通网络表情包风格插画（如简笔猪头、蘑菇、太阳笑脸云朵等通用装饰元素）\n'
    '2. 素人/普通人的日常生活或场景照片（非明星、非公众人物、非知名IP角色）\n'
    '3. 音乐/视频App界面截图中出现的歌手/音乐人名字文本标注'
    '（属正常作品署名，不是盗用形象）\n'
    '4. App宣传图中使用的通用模特/素材照片（非特定明星）\n'
    '5. 经过艺术化设计的拟人化动物、卡通形象（如小熊/小狗/IP热带鱼）'
    '或通用黑人/黄种人/白人表情包'
    '（无具体真人指向、非特定明星仿妆或肖像还原）\n'
    '6. AI助手、虚拟客服等无真实人物映射的抽象AI形象\n'
    '7. AI生成内容中仅含通用种族/职业/情绪特征的抽象化形象'
    '（如"微笑黑人剪影""穿白大褂的AI医生"），'
    '未还原任何真实人物五官、标志性造型或公开影像特征。\n'
    '8. 明确为平台自有或通用版权的拟人化IP形象'
    '（如汽水音乐的卡通牙齿、小熊/小狗、热带鱼等无真人映射的原创视觉符号），'
    '即使具备拟人特征也不构成侵权。\n'
    '9. 多个豁免类形象（如平台IP+通用表情包+AI助手）在同一画面中组合出现，'
    '且无任何未授权真人/知名IP形象混入时，整体不构成违规。\n'
    '10. 用户上传的语料/文案中仅提及IP名称作为关键词标签或素材描述，'
    '但未在视频/图片画面中实际使用该IP形象、标志性造型或衍生变体的，'
    '不属于画面形象侵权——纯文本IP名称引用不触发此规则。'
    '仅当文案明确描述「使用XX形象作为视频素材」且画面中确有该IP形象时才构成违规。'
)

new_source_type = 'video_frame,image_content,original_text'

with psycopg.connect(dsn, autocommit=True) as conn:
    # 1. 更新 Rule #3
    conn.execute(
        """UPDATE audit_rule
           SET keywords=%s, condition=%s, guidance=%s, source_type=%s,
               update_time=now()
           WHERE id=%s AND del_flag=0""",
        (Jsonb(new_keywords), new_condition, new_guidance,
         new_source_type, R3_ID),
    )
    print('Rule #3 已更新（合并了 Rule #19 的内容）')

    # 2. 软删 Rule #19（不重编号）
    conn.execute(
        "UPDATE audit_rule SET del_flag=%s, update_time=now() WHERE id=%s",
        (next_id(), R19_ID),
    )
    print('Rule #19 已软删（保留空号，不重编号）')

    # 3. 验证
    print('\n── 验证 ──')
    rows = conn.execute(
        """SELECT id, no, keywords, condition, source_type, del_flag, guidance
           FROM audit_rule WHERE id IN (%s, %s) ORDER BY no""",
        (R3_ID, R19_ID),
    ).fetchall()
    for r in rows:
        flag = '在用' if r[5] == 0 else f'已删({r[5]})'
        print(f'  #{r[1]} {flag} | source={r[4]} | keywords={r[2]}')
        print(f'       condition={r[3][:80]}')
        if r[6]:
            print(f'       guidance 尾={r[6][-120:]}')
        print()

print('Done.')

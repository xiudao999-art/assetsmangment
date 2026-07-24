"""Standalone recheck for 汽水音乐11改.mp4 — verify Rule #7 fix."""
import os, sys, json, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv
dotenv.load_dotenv('.env')

from app.config import settings
from app.domain.models import AuditRule, AuditReport, MaterialType, TextSourceType, TextSegment, JobStatus, AuditJob, AuditStatus

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s [%(name)s] %(message)s')

# Load material segments from DB
import psycopg
dsn = os.getenv('AM_DATABASE_URL')
with psycopg.connect(dsn, autocommit=True) as conn:
    # Get the report segments
    r = conn.execute(
        'SELECT report_id, segments FROM audit_report WHERE report_id=%s',
        ('a9c13492ea284914b0169f715598a344',)
    ).fetchone()
    segs_raw = r[1] if r else []

    # Build TextSegment list
    segments = []
    for s in segs_raw:
        st = s.get('source_type', 'transcript')
        try:
            src_type = TextSourceType(st)
        except ValueError:
            src_type = TextSourceType.TRANSCRIPT
        segments.append(TextSegment(
            source_type=src_type,
            text=s.get('text', ''),
            begin_ms=s.get('begin_ms'),
            end_ms=s.get('end_ms'),
            frame_oss_key=s.get('frame_oss_key', ''),
        ))

    print(f'Loaded {len(segments)} segments')

    # Get current Rule #7 from DB
    r7 = conn.execute(
        'SELECT id, no, source_type, keywords, condition, action, enabled, project_id, guidance, match_level, regex, exceptions, create_by FROM audit_rule WHERE no=7 AND del_flag=0'
    ).fetchone()
    from app.infrastructure.pg_rule_repo import PgAuditRuleRepo
    rule7 = PgAuditRuleRepo._to_rule(r7)
    print(f'\nRule #7: match_level={rule7.match_level}')
    print(f'condition: {rule7.condition}')
    print(f'guidance: {rule7.guidance}')

# Print what the judge would see
from app.service.audit_pipeline import AuditPipelineService

# Simulate _pack_material
job = AuditJob(
    id='test', material_type=MaterialType.VIDEO, oss_key='test',
    owner_id='test', material_id='test', video_kind='material',
    project_id='203714824369078272', status=JobStatus.RUNNING
)

# Build rules list
all_rules = []
with psycopg.connect(dsn, autocommit=True) as conn:
    rows = conn.execute(
        'SELECT id, no, source_type, keywords, condition, action, enabled, project_id, guidance, match_level, regex, exceptions, create_by FROM audit_rule WHERE del_flag=0 ORDER BY no'
    ).fetchall()
    for row in rows:
        all_rules.append(PgAuditRuleRepo._to_rule(row))

# Filter applicable rules (same as _evaluate)
pid = '203714824369078272'
applicable = [r for r in all_rules if r.enabled and (r.project_id == '' or r.project_id == pid)]
sem_rules = [r for r in applicable if r.match_level != 'regex']

print(f'\nApplicable rules: {len(applicable)}, Semantic rules: {len(sem_rules)}')

# Generate what the LLM sees
material_doc = AuditPipelineService._pack_material(segments, job)
rules_doc = AuditPipelineService._pack_rules(sem_rules)

print('\n=== WHAT THE LLM SEES ===')
print('\n--- Material doc (first 2000 chars) ---')
print(material_doc[:2000])
print('\n--- Rules doc (Rule #7 portion) ---')
# Find the Rule #7 section in rules_doc
for line in rules_doc.split('\n'):
    if '7.' in line and ('视频帧' in line or '画面' in line):
        idx = rules_doc.index(line)
        print(rules_doc[idx:idx+600])
        break

# Now actually call the LLM judge
print('\n=== CALLING LLM JUDGE ===')
from app.infrastructure.dashscope_llm import DashScopeLlm
llm = DashScopeLlm(api_key=settings.dashscope_api_key, model=settings.qwen_llm_model)

_RULE_JUDGE_SYS = (
    "你是作品内容审核引擎。下面给你【物料内容】(按段落编号 〖i〗 组织,含口播文字与画面描述,各带时间)"
    "和【审核规则清单】(每条含编号、目标、动作、严格程度〔字面/隐喻判定〕、条件,以及「尺度说明」和「已确认可放行的例外」)。找出物料中所有违反规则之处,只列出确认违规的条目。\n\n"
    "## 规则解读方式\n"
    "每条规则由「条件」+「尺度说明」两部分组成:\n"
    "- **条件**:一句话定义这条规则要拦截什么(核心定义,简略)。\n"
    "- **尺度说明**:这条规则的权威详细解释——包含什么算违规的具体示例、什么不算违规的反例、"
    "边界情形怎么判、容易误判的情况及正确处理方式。**尺度说明是规则不可分割的组成部分,"
    "其中的反例和放行情景具有约束力,必须严格遵守,不得自行扩展或收紧。**\n\n"
    "## 输出格式\n"
    "只返回一个合法 JSON 对象,不要 markdown、不要多余解释,字段 findings 是数组,每个元素:"
    "rule(命中的规则编号,整数 —— 就是【审核规则清单】里每条最前面的那个数字,务必原样返回该数字、不要自己重排),"
    "segment(违规所在的物料段落编号,即某个 〖i〗 的整数;若无法定位到具体段落/整体判断则为 null),"
    "reason(中文,简述这里为什么违反该规则)。\n\n"
    "## 判定纪律\n"
    "只标真实违规;同一处命中多条规则就各记一条;"
    "参考词只是方向示例,请按语义判断,不要机械按字匹配(例如「去」「来」「上」等常见字不要仅因出现就判违规);\n\n"
    "每条规则都标了【字面判定】或【隐喻判定】两种严格程度,务必严格区分、按对应标准判:\n"
    "【字面判定】= 只有当物料【直接、明确地说出/主张】了该规则禁止的那件事、其表面意思本身就构成违规,才算命中。"
    "凡是需要【结合上下文/语境去推断、由场景描述引申、暗示、隐含、联想、影射、谐音、隐喻、语义延伸、把某段话『归为/可理解为』某违规类别】才扯得上关系的,一律【不算命中(放行)】。"
    "自检:若你写命中理由时用到了『结合上下文/语境』『暗示』『隐含』『引申』『延伸』『可理解为』『属…类(表达)』这类措辞,就说明它并非字面直接违规 —— 放行。"
    "举例:规则禁『躺赚』,字面命中是话里直接宣称『躺着/不劳动就能赚到钱』这类主张;而只是描述一个『躺床上听歌、金币自己涨』的产品场景、需要你推断『这可理解为躺赚』的,属引申,放行。"
    "字面判定宁可漏、不可误伤(它也判「表面意思」而非机械逐字匹配参考词,但表面意思必须自身就违规)。\n"
    "【隐喻判定】= 除字面直接违反外,【影射、暗示、隐喻、谐音、代称、擦边、结合语境的引申】等间接表达也要揪出来算命中(仅用于国家政治/领导人/民族宗教/国家标志等严重项,隐晦也不放过)。\n\n"
    "## 尺度说明的使用\n"
    "尺度说明中的反例和放行情景**不是建议,是硬约束**——若物料情形与尺度说明中列出的放行情况一致,"
    "必须放行,不得以其他理由判违规。尺度说明中明确排除的情形,不要自作主张纳入。"
    "若某处情形和该规则「已确认可放行的例外」里列的类似,则视为通过、不要标为违规。\n\n"
    "## 底线——你只输出违规项\n"
    "你的 findings 数组里**只能**包含确认违反规则的材料片段。以下行为**绝对禁止**:\n"
    "- 禁止为「未违反」的规则输出条目(如「规则X不适用」「按尺度说明放行」「符合规则要求」「未命中」等)\n"
    "- 禁止逐条汇报每条规则的判定结论\n\n"
    "**输出前自检**:逐条读你的 findings,问自己:"
    "「这一条是在说『此处违规』还是在解释『为什么没违规』?」如果是后者,**立即删除该条**。\n"
    "拿不准是否违规的 → 保留(宁可标出交人工);确定没违规的 → findings 里不出现。\n"
    "没有任何违规时 → findings 返回空数组 []。"
)

user_prompt = f"【物料内容】\n{material_doc}\n\n【审核规则清单】\n{rules_doc}\n\n请以 json 返回 findings。"

print(f'User prompt length: {len(user_prompt)} chars')
print('\nSending to LLM...')

try:
    out = llm.chat_json(_RULE_JUDGE_SYS, user_prompt)
    print(f'\n=== LLM RESPONSE ===')
    print(json.dumps(out, ensure_ascii=False, indent=2))

    findings = out.get('findings', []) if isinstance(out, dict) else []
    rule7_findings = [f for f in findings if f.get('rule') == 7]
    if rule7_findings:
        print(f'\n✅ Rule #7 TRIGGERED! Found {len(rule7_findings)} finding(s):')
        for f in rule7_findings:
            print(f'  segment={f.get("segment")} reason={f.get("reason")}')
    else:
        print(f'\n❌ Rule #7 did NOT trigger.')
        print(f'Total findings: {len(findings)}')
        for f in findings:
            print(f'  rule={f.get("rule")} segment={f.get("segment")} reason={f.get("reason", "")[:100]}')
except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()

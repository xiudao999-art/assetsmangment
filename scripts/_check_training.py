"""检查训练集、样本、报告、规则 —— 用于规则调优"""
import os, json, sys
import dotenv
import psycopg
from psycopg.rows import dict_row

sys.stdout.reconfigure(encoding='utf-8')
dotenv.load_dotenv('.env')
dsn = os.getenv('AM_DATABASE_URL')
if not dsn:
    print("ERROR: AM_DATABASE_URL not set")
    sys.exit(1)

conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

# ── 预加载规则（两个索引：by_no, by_id） ──
all_rules = conn.execute(
    "SELECT id, no, keywords, condition, guidance, match_level, action, source_type, enabled FROM audit_rule WHERE del_flag=0 ORDER BY no"
).fetchall()
rule_by_no = {}
rule_by_id = {}
for rl in all_rules:
    rule_by_no[rl['no']] = rl
    rule_by_id[str(rl['id'])] = rl

# ── 1. 所有训练集 ──
print("=" * 80)
print("1. 训练集列表")
print("=" * 80)
rows = conn.execute("""
    SELECT ts.id, ts.project_id, ts.status, ts.max_fp_ratio, ts.max_iterations,
           ts.started_at, ts.completed_at,
           p.name as project_name
    FROM rule_training_set ts
    LEFT JOIN project p ON p.id = ts.project_id AND p.del_flag = 0
    WHERE ts.del_flag = 0
    ORDER BY ts.id
""").fetchall()
for r in rows:
    print(f"  id={r['id']} project={r['project_name'] or r['project_id']} status={r['status']} "
          f"fp<={r['max_fp_ratio']} max_iter={r['max_iterations']}")

# ── 2. 已完成/失败训练集的详细迭代结果 ──
print("\n" + "=" * 80)
print("2. 训练结果详情（规则ID→编号已映射）")
print("=" * 80)
rows = conn.execute("""
    SELECT ts.id, ts.project_id, ts.status, ts.training_result,
           p.name as project_name
    FROM rule_training_set ts
    LEFT JOIN project p ON p.id = ts.project_id AND p.del_flag = 0
    WHERE ts.del_flag = 0 AND ts.status IN ('completed','failed','validated')
    ORDER BY ts.id
""").fetchall()

for r in rows:
    pname = r['project_name'] or r['project_id']
    print(f"\n{'='*60}")
    print(f"训练集 {r['id']} [{pname}] status={r['status']}")
    print(f"{'='*60}")
    tr = r.get('training_result')
    if not tr or not isinstance(tr, dict):
        print("  (无 training_result)")
        continue
    iters = tr.get('iterations', [])
    fm = tr.get('final_metrics')
    if fm:
        per_rule = fm.get('per_rule', {})
        print(f"  最终指标: total_expected={fm.get('total_expected_hits')} "
              f"missed={fm.get('missed_hits')} extra={fm.get('extra_hits')} "
              f"fp_ratio={fm.get('fp_ratio')}")
        for rid_str, rm in per_rule.items():
            rl = rule_by_id.get(str(rid_str), {})
            rno = rl.get('no', f'?({rid_str})')
            print(f"    Rule #{rno}: expected={rm['expected']} actual={rm['actual']} "
                  f"missed={rm['missed']} extra={rm['extra']} "
                  f"cond={rl.get('condition','')[:60]}")
    print(f"  收敛: {tr.get('converged')}")
    err = tr.get('error')
    if err:
        print(f"  错误: {str(err)[:300]}")
    # 每轮迭代
    print(f"  迭代轮数: {len(iters)}")
    for i, it in enumerate(iters):
        m = it.get('metrics', {})
        per_rule = m.get('per_rule', {})
        print(f"  --- 轮{i+1}: expected={m.get('total_expected_hits')} "
              f"actual={m.get('actual_hits')} "
              f"missed={m.get('missed_hits')} extra={m.get('extra_hits')} "
              f"fp_ratio={m.get('fp_ratio')} converged={it.get('converged','?')}")
        for rid_str, rm in per_rule.items():
            exp = rm.get('expected', 0)
            act = rm.get('actual', 0)
            missed = rm.get('missed', 0)
            extra = rm.get('extra', 0)
            if missed > 0 or extra > 0:
                rl = rule_by_id.get(str(rid_str), {})
                rno = rl.get('no', f'?')
                print(f"        Rule #{rno}: exp={exp} act={act} missed={missed} extra={extra} | {rl.get('condition','')[:80]}")

# ── 3. 每个训练集的样本+报告对照 ──
print("\n" + "=" * 80)
print("3. 样本 → Ground Truth vs 最新报告触发")
print("=" * 80)
for ts_row in conn.execute("""
    SELECT id, project_id, status FROM rule_training_set
    WHERE del_flag = 0 AND status IN ('completed','failed','validated')
    ORDER BY id
""").fetchall():
    ts_id = ts_row['id']
    examples = conn.execute("""
        SELECT e.material_id, e.expected_rule_ids, e.source_note,
               m.oss_key, m.audit_report_id, m.ai_summary, m.audit_status
        FROM rule_training_example e
        LEFT JOIN material m ON m.id = e.material_id AND m.del_flag = 0
        WHERE e.training_set_id = %s AND e.del_flag = 0
        ORDER BY e.id
    """, (ts_id,)).fetchall()
    print(f"\n--- 训练集 {ts_id} ({ts_row['status']}): {len(examples)} 样本 ---")
    for ex in examples:
        print(f"\n  material_id={ex['material_id']} file={ex.get('oss_key','?')[:70]}")
        print(f"    audit_status={ex.get('audit_status')} report_id={ex.get('audit_report_id')}")
        exp_ids = ex['expected_rule_ids'] or []
        print(f"    expected_rules: {json.dumps(exp_ids, ensure_ascii=False)}")
        note = ex.get('source_note') or ''
        if note:
            print(f"    source_note: {note[:150]}")
        summary = (ex.get('ai_summary') or '')[:200]
        if summary:
            print(f"    ai_summary: {summary}")

        rid = ex.get('audit_report_id')
        if rid:
            report = conn.execute("""
                SELECT report_id, verdict, triggered, summary
                FROM audit_report WHERE report_id = %s
            """, (rid,)).fetchone()
            if report:
                triggered = report.get('triggered') or []
                if isinstance(triggered, dict):
                    triggered = [triggered]
                triggered_nos = sorted(set(
                    t.get('rule_no', '?') for t in triggered if isinstance(t, dict)
                ))
                print(f"    报告 verdict={report.get('verdict')} triggered={triggered_nos}")
                print(f"    报告 summary={str(report.get('summary',''))[:250]}")
                # 对比
                exp_set = set(str(x) for x in (ex['expected_rule_ids'] or []))
                trig_set = set(str(x) for x in triggered_nos)
                missed_by_report = exp_set - trig_set
                extra_by_report = trig_set - exp_set
                if missed_by_report:
                    print(f"    ❌ 漏判(报告比GT少): {sorted(missed_by_report)}")
                if extra_by_report:
                    print(f"    ⚠️ 多判(报告比GT多): {sorted(extra_by_report)}")
                if not missed_by_report and not extra_by_report:
                    print(f"    ✅ 报告与GT完全一致")
            else:
                print(f"    ⚠️ report_id={rid} 未找到报告记录!")

# ── 4. 当前规则全貌 ──
print("\n" + "=" * 80)
print("4. 当前在用规则")
print("=" * 80)
for rl in all_rules:
    kw = json.dumps(rl['keywords'] or [], ensure_ascii=False)
    print(f"\n  #{rl['no']:02d} [{rl['action']}] [{rl['match_level']}] [{rl['source_type']}] enabled={rl['enabled']}")
    print(f"    id={rl['id']}")
    print(f"    keywords: {kw}")
    print(f"    condition: {rl['condition'] or '(空)'}")
    guide = rl['guidance'] or ''
    if guide:
        print(f"    guidance: {guide[:300]}")

# ── 5. 训练集 205196375153967104 每轮 rule_changes ──
print("\n" + "=" * 80)
print("5. 营销号金币: 每轮 rule_changes 详情")
print("=" * 80)
ts_row = conn.execute(
    "SELECT training_result FROM rule_training_set WHERE id = 205196375153967104"
).fetchone()
tr = ts_row['training_result'] if ts_row else None
if tr and isinstance(tr, dict):
    its = tr.get('iterations', [])
    for i, it in enumerate(its):
        print(f"\n--- 第{i+1}轮 ---")
        rc = it.get('rule_changes', {})
        if not rc:
            print("  (无规则变更)")
        if isinstance(rc, list):
            rc = {ch.get('rule_id', f'unknown_{i}'): ch for i, ch in enumerate(rc)}
        for rid, ch in rc.items():
            if isinstance(ch, dict):
                old = ch.get('old', {}) or {}
                new = ch.get('new', {}) or {}
                if isinstance(old, dict) and isinstance(new, dict):
                    old_g = old.get('guidance', '')
                    new_g = new.get('guidance', '')
                    rl = rule_by_id.get(str(rid), {})
                    rno = rl.get('no', '?')
                    print(f"  Rule #{rno} (id={rid}):")
                    print(f"    old guidance ({len(old_g)}字): {old_g[:250]}")
                    print(f"    new guidance ({len(new_g)}字): {new_g[:250]}")
                else:
                    print(f"  Rule id={rid}: old={str(old)[:250]} new={str(new)[:250]}")

# ── 6. 营销号金币: 5个样本的报告 segments 详情 ──
print("\n" + "=" * 80)
print("6. 营销号金币: 样本报告 segments 详情")
print("=" * 80)
mids = [204885901233356800, 204884448796213248, 203820889685360640, 204885086326226944, 204885427855818752]
for mid in mids:
    mat = conn.execute(
        "SELECT id, oss_key, ai_summary, audit_report_id FROM material WHERE id = %s", (mid,)
    ).fetchone()
    if not mat:
        print(f"\n  material={mid}: NOT FOUND")
        continue
    print(f"\n  material={mid}")
    print(f"  file: {mat['oss_key']}")
    summary = (mat['ai_summary'] or '')[:200]
    print(f"  ai_summary: {summary}")
    rid = mat['audit_report_id']
    if rid:
        report = conn.execute(
            "SELECT report_id, verdict, triggered, summary FROM audit_report WHERE report_id = %s", (rid,)
        ).fetchone()
        if report:
            triggered = report['triggered'] or []
            if isinstance(triggered, dict):
                triggered = [triggered]
            print(f"  report_id={report['report_id']} verdict={report['verdict']}")
            for t in triggered:
                if isinstance(t, dict):
                    seg = t.get('segment_index', '?')
                    rno = t.get('rule_no', '?')
                    reason = (t.get('reason', '') or '')[:250]
                    print(f"    seg={seg} rule_no={rno}")
                    print(f"      reason: {reason}")
    else:
        print(f"  (no report)")

conn.close()
print("\nDone.")

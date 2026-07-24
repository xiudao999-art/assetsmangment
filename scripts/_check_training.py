"""Check training results for 汽水音乐-营销号金币 project."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dotenv, psycopg
dotenv.load_dotenv('.env')
dsn = os.getenv('AM_DATABASE_URL')
conn = psycopg.connect(dsn, autocommit=True)

proj = conn.execute(
    "SELECT id, name FROM project WHERE name ILIKE %s AND del_flag=0",
    ('%汽水音乐%金币%',)
).fetchone()
if not proj:
    proj = conn.execute(
        "SELECT id, name FROM project WHERE name ILIKE %s AND del_flag=0",
        ('%汽水音乐%',)
    ).fetchone()
pid = proj[0]
print(f"Project: {proj[1]} (id={pid})")

ts = conn.execute(
    'SELECT id, status, training_result, max_fp_ratio, max_iterations, started_at, completed_at '
    'FROM rule_training_set WHERE project_id=%s AND del_flag=0 ORDER BY create_time DESC LIMIT 1',
    (pid,)
).fetchone()
print(f"Training Set: id={ts[0]} status={ts[1]} max_fp={ts[3]} max_iter={ts[4]}")
print(f"started={ts[5]}  completed={ts[6]}")

result = ts[2]
if not result or not isinstance(result, dict):
    print("No result dict")
    conn.close()
    exit()

print(f"Converged: {result.get('converged')}")
iterations = result.get('iterations', [])
print(f"Iterations: {len(iterations)}")

for it in iterations:
    if not isinstance(it, dict):
        continue
    n = it.get('iteration', '?')
    conv = it.get('converged', '?')
    metrics = it.get('metrics', {})
    mats = it.get('current_materials', {})
    changes = it.get('rule_changes', [])

    print(f"\n--- Iteration {n} | converged={conv} ---")

    if metrics:
        per_rule = metrics.get('per_rule', {})
        print(f"  fp_ratio={metrics.get('fp_ratio')} missed_hits={metrics.get('missed_hits')} extra_hits={metrics.get('extra_hits')} total_expected={metrics.get('total_expected_hits')}")
        for rid, m in per_rule.items():
            if m.get('expected', 0) == 0 and m.get('actual', 0) == 0:
                continue  # skip inactive rules
            rn = conn.execute('SELECT no, condition FROM audit_rule WHERE id=%s', (rid,)).fetchone()
            no = rn[0] if rn else '?'
            cond = (rn[1] or '')[:45] if rn else ''
            flag = "!!" if (m.get('missed',0) > 0 or m.get('extra',0) > 0) else "OK"
            print(f"  {flag} #{no} {cond}: missed={m.get('missed',0)} extra={m.get('extra',0)} expected={m.get('expected',0)} actual={m.get('actual',0)}")

    if mats:
        for mid, hits in mats.items():
            mr = conn.execute('SELECT oss_key FROM material WHERE id=%s AND del_flag=0', (mid,)).fetchone()
            name = mr[0].split('/')[-1][:45] if mr else mid[:20]
            ex = conn.execute(
                'SELECT expected_rule_ids FROM rule_training_example '
                'WHERE material_id=%s AND training_set_id=%s AND del_flag=0',
                (mid, ts[0])
            ).fetchone()
            exp_ids = ex[0] if ex else []
            exp_nos = sorted([conn.execute('SELECT no FROM audit_rule WHERE id=%s',(rid,)).fetchone()[0] for rid in exp_ids])
            hit_nos = sorted([conn.execute('SELECT no FROM audit_rule WHERE id=%s',(rid,)).fetchone()[0] for rid in hits])
            missed_nos = [n for n in exp_nos if n not in hit_nos]
            extra_nos = [n for n in hit_nos if n not in exp_nos]
            ok = "OK" if (not missed_nos and not extra_nos) else "!!"
            print(f"  {ok} {name}")
            print(f"      expected={exp_nos}  actual={hit_nos}  missed={missed_nos}  extra={extra_nos}")

    if changes:
        for rc in changes:
            print(f"  AI changed Rule #{rc.get('rule_no')}: guidance updated")

fm = result.get('final_metrics', {})
if fm:
    per_rule = fm.get('per_rule', fm)
    if isinstance(per_rule, dict):
        total_m = 0; total_e = 0; total_exp = 0
        for rid, m in per_rule.items():
            if isinstance(m, dict):
                total_m += m.get('missed', 0)
                total_e += m.get('extra', 0)
                total_exp += m.get('expected', 0)
        fp = total_e / total_exp if total_exp > 0 else 0
        print(f"\n=== FINAL ===")
        print(f"missed={total_m} extra={total_e} expected={total_exp} fp_ratio={fp:.1%}")

# Deep dive: 汽水音乐11改.mp4 report
print("\n=== 汽水音乐11改.mp4 REPORT ===")
mid = '204885427855818752'
m = conn.execute(
    'SELECT oss_key, audit_report_id FROM material WHERE id=%s AND del_flag=0', (mid,)
).fetchone()
if m and m[1]:
    rp = conn.execute(
        'SELECT segments, triggered FROM audit_report WHERE report_id=%s', (m[1],)
    ).fetchone()
    if rp:
        segs = rp[0] or []
        triggered = rp[1] or []
        print(f"Triggered rules: {[t.get('rule_no') for t in triggered]}")
        for i, s in enumerate(segs):
            st = s.get('source_type', '?')
            if st == 'video_frame':
                txt = s.get('text', '')
                # Find disclaimer position
                pos_tag = ''
                if '免责文字位置:' in txt:
                    idx = txt.index('免责文字位置:')
                    pos_tag = txt[idx:idx+30]
                print(f"Seg{i+1} [{s.get('begin_ms')}ms] prefix={txt[:100]}...")
                if pos_tag:
                    print(f"  >>> {pos_tag}")

conn.close()

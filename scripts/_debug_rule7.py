"""Debug Rule #7 false negative for 汽水音乐11改.mp4"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv
dotenv.load_dotenv('.env')

import psycopg
from psycopg.types.json import Jsonb

dsn = os.getenv('AM_DATABASE_URL')
with psycopg.connect(dsn, autocommit=True) as conn:
    # Rule #7
    r = conn.execute(
        'SELECT id, no, keywords, condition, action, match_level, guidance, source_type '
        'FROM audit_rule WHERE no=7 AND del_flag=0'
    ).fetchone()
    if r:
        print('=== Rule #7 ===')
        print(f'id={r[0]} no={r[1]} action={r[4]} match_level={r[5]} source_type={r[7]}')
        print(f'condition: {r[3]}')
        print(f'guidance: {r[6]}')
        print(f'keywords: {json.dumps(r[2], ensure_ascii=False)}')
    else:
        print('Rule #7 not found!')

    # Find material
    mats = conn.execute(
        "SELECT id, oss_key, audit_report_id, audit_status FROM material "
        "WHERE oss_key ILIKE '%汽水音乐11%' AND del_flag=0"
    ).fetchall()
    print(f'\n=== Materials matching 汽水音乐11 ({len(mats)} found) ===')
    for mt in mats:
        print(f'id={mt[0]} oss_key={mt[1]} status={mt[3]} report_id={mt[2]}')

        if mt[2]:
            rp = conn.execute(
                'SELECT report_id, verdict, summary, segments, triggered FROM audit_report WHERE report_id=%s',
                (mt[2],)
            ).fetchone()
            if rp:
                print(f'\n--- Report {rp[0]} ---')
                print(f'verdict={rp[1]}')
                print(f'summary={json.dumps(rp[2], ensure_ascii=False)[:300] if rp[2] else "None"}')
                triggered = rp[4] or []
                print(f'triggered rules: {json.dumps(triggered, ensure_ascii=False)}')

                segs = rp[3] or []
                print(f'\nSegments ({len(segs)} total):')
                for i, seg in enumerate(segs):
                    print(f'\n  --- Seg {i+1}: type={seg.get("type")} '
                          f'begin={seg.get("begin_ms")}ms end={seg.get("end_ms")}ms ---')
                    desc = seg.get('description', '')
                    print(f'  desc: {desc[:800]}')
                    tr = seg.get('triggered_rules', [])
                    if tr:
                        print(f'  triggered: {json.dumps(tr, ensure_ascii=False)}')

        # Also check associated audit tasks
        tasks = conn.execute(
            "SELECT id, report_id, status, verdict, create_time, update_time FROM audit_task "
            "WHERE material_id=%s AND del_flag=0 ORDER BY create_time DESC LIMIT 5",
            (str(mt[0]),)
        ).fetchall()
        if tasks:
            print(f'\nAssociated audit tasks:')
            for t in tasks:
                print(f'  task_id={t[0]} report_id={t[1]} status={t[2]} verdict={t[3]} '
                      f'created={t[4]} updated={t[5]}')

# Check training set separately
print('\n=== Training Set for project 203714824369078272 ===')
with psycopg.connect(dsn, autocommit=True) as conn:
    ts = conn.execute(
        'SELECT id, project_id, status, training_result, rule_snapshot FROM rule_training_set '
        'WHERE project_id=%s AND del_flag=0 ORDER BY create_time DESC LIMIT 1',
        ('203714824369078272',)
    ).fetchone()
    if ts:
        print(f'training_set_id={ts[0]} status={ts[2]}')
        result = ts[3]
        if result:
            if isinstance(result, dict):
                print(f'result keys: {list(result.keys())}')
                iterations = result.get('iterations', [])
                print(f'iterations count: {len(iterations)}')
                for it in iterations:
                    if isinstance(it, dict):
                        mats = it.get('current_materials', {})
                        mat_id_str = '204885427855818752'
                        if mat_id_str in mats:
                            print(f'Iteration {it.get("iteration")}: material hits={mats[mat_id_str]}')
                        # Also show metrics per iteration
                        m = it.get('metrics', {})
                        if m:
                            rule7 = m.get('203708807950368768')
                            if rule7:
                                print(f'  Rule#7 metrics: {json.dumps(rule7, ensure_ascii=False)}')
                # Show final metrics
                fm = result.get('final_metrics', {})
                if fm:
                    rule7 = fm.get('203708807950368768')
                    if rule7:
                        print(f'Final Rule#7: {json.dumps(rule7, ensure_ascii=False)}')
                # Show converged
                print(f'converged: {result.get("converged")}')
    else:
        print('No training set found')

import os, json, sys, dotenv, psycopg
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
dotenv.load_dotenv('.env')
dsn = os.getenv('AM_DATABASE_URL')

with psycopg.connect(dsn) as conn:
    materials = [
        ('203820889685360640', '059ea073dad7413da60f1c2b3ebee55f'),
        ('204884448796213248', '38e3ae3e25a64478be1ab5e5a479b9c6'),
        ('204885086326226944', '1300c61bbd6344e89826a9000f29c23b'),
        ('204885427855818752', '84d6766cccb9408d83a31584ffd26774'),
        ('204885901233356800', 'c322cf1b36f8404e8f64d0f9dcdf9d9b'),
    ]

    rules = {r[0]: {'no': r[1], 'condition': r[2][:80] if r[2] else ''}
             for r in conn.execute('SELECT id, no, condition FROM audit_rule WHERE del_flag=0').fetchall()}

    for material_id, report_id in materials:
        print(f'=== Material {material_id}, report {report_id} ===')

        r = conn.execute(
            'SELECT report_id, verdict, summary, triggered, segments FROM audit_report WHERE report_id=%s',
            (report_id,)
        ).fetchone()
        if not r:
            print('  REPORT NOT FOUND!')
            alt = conn.execute(
                'SELECT audit_report_id FROM material WHERE id=%s AND del_flag=0', (material_id,)
            ).fetchone()
            if alt and alt[0]:
                print(f'  Material audit_report_id={alt[0]}')
                r = conn.execute(
                    'SELECT report_id, verdict, summary, triggered, segments FROM audit_report WHERE report_id=%s',
                    (alt[0],)
                ).fetchone()
        if r:
            triggered = r[3]
            if isinstance(triggered, str):
                triggered = json.loads(triggered)
            print(f'  verdict: {r[1]}')
            print(f'  triggered count: {len(triggered) if triggered else 0}')
            if triggered:
                rule_counts = Counter()
                for t in triggered:
                    rid = t.get('rule_id', '?')
                    rule_counts[rid] += 1
                print(f'  Unique rule_ids in triggered: {len(rule_counts)}')
                for rid, count in rule_counts.most_common():
                    rule_no = rules.get(rid, {}).get('no', '?')
                    rule_cond = rules.get(rid, {}).get('condition', '')
                    print(f'    rule #{rule_no} (id={rid}): {count} segment hits -- {rule_cond}')
                    for t in triggered:
                        if t.get('rule_id') == rid:
                            reason = t.get('reason', '')
                            print(f'      sample reason: {reason[:150]}')
                            break

        task = conn.execute(
            'SELECT id, report_id, verdict, status FROM audit_task WHERE material_id=%s AND del_flag=0 ORDER BY create_time DESC LIMIT 1',
            (material_id,)
        ).fetchone()
        if task:
            print(f'  audit_task: id={task[0]}, report_id={task[1]}, verdict={task[2]}, status={task[3]}')
        print()

import os, json, sys, dotenv, psycopg
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
dotenv.load_dotenv('.env')
dsn = os.getenv('AM_DATABASE_URL')

with psycopg.connect(dsn) as conn:
    # For each training material, get the audit_task's report (what training actually uses)
    materials = [
        '203820889685360640',
        '204884448796213248',
        '204885086326226944',
        '204885427855818752',
        '204885901233356800',
    ]

    for material_id in materials:
        # Get the audit task
        task = conn.execute(
            'SELECT id, report_id, verdict, status, material_id FROM audit_task WHERE material_id=%s AND del_flag=0 ORDER BY create_time DESC LIMIT 1',
            (material_id,)
        ).fetchone()

        print(f'=== Material {material_id} ===')
        if not task:
            print('  No audit task found!')
            continue

        task_report_id = task[1]
        print(f'  Task id={task[0]}, report_id={task_report_id}, verdict={task[2]}, status={task[3]}')

        # Get the task's report
        r = conn.execute(
            'SELECT report_id, verdict, triggered FROM audit_report WHERE report_id=%s',
            (task_report_id,)
        ).fetchone()

        if r:
            triggered = r[2]
            if isinstance(triggered, str):
                triggered = json.loads(triggered)
            print(f'  Report verdict: {r[1]}')
            print(f'  Triggered count: {len(triggered) if triggered else 0}')

            if triggered:
                rule_counts = Counter()
                for t in triggered:
                    rid = t.get('rule_id', '?')
                    rule_counts[rid] += 1

                for rid, count in rule_counts.most_common():
                    # Get rule info
                    rule = conn.execute(
                        'SELECT no, condition FROM audit_rule WHERE id=%s AND del_flag=0', (rid,)
                    ).fetchone()
                    rule_no = rule[0] if rule else '?'
                    rule_cond = rule[1][:60] if rule and rule[1] else ''
                    print(f'    Rule #{rule_no} ({rid}): {count} hits -- {rule_cond}')

                    # Show each hit's reason
                    for t in triggered:
                        if t.get('rule_id') == rid:
                            reason = t.get('reason', '')
                            text = t.get('text', '')[:80]
                            print(f'      reason: {reason[:200]}')
                            print(f'      text: {text}')
                            print(f'      action: {t.get("action", "")}')
                            # Check _reason_says_pass
                            from app.service.audit_pipeline import _reason_says_pass
                            would_filter = _reason_says_pass(reason)
                            print(f'      _reason_says_pass: {would_filter}')
                            break
        else:
            print('  Report NOT FOUND!')

        # Also get material's current audit_report_id
        mat = conn.execute(
            'SELECT audit_report_id, audit_status FROM material WHERE id=%s AND del_flag=0', (material_id,)
        ).fetchone()
        if mat:
            print(f'  Material audit_report_id: {mat[0]}, status: {mat[1]}')

        print()

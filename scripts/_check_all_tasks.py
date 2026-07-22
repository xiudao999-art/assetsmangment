import os, json, sys, dotenv, psycopg
sys.stdout.reconfigure(encoding='utf-8')
dotenv.load_dotenv('.env')
dsn = os.getenv('AM_DATABASE_URL')

with psycopg.connect(dsn) as conn:
    materials = {
        '203820889685360640': '[rule 7] 8.mp4',
        '204884448796213248': '[clean] v2.mp4',
        '204885086326226944': '[rule 7] 10gai7.3.mp4',
        '204885427855818752': '[rule 7] 11gai.mp4',
        '204885901233356800': '[rule 7] shipin6.mp4',
    }

    for mid, label in materials.items():
        print(f'=== {label} (material={mid}) ===')

        tasks = conn.execute('''
            SELECT id, report_id, verdict, status, create_time
            FROM audit_task WHERE material_id=%s AND del_flag=0
            ORDER BY create_time DESC
        ''', (mid,)).fetchall()
        print(f'  Tasks: {len(tasks)}')
        for t in tasks:
            print(f'    task_id={t[0]}, report_id={t[1]}, verdict={t[2]}, status={t[3]}, time={t[4]}')

        if tasks:
            task_report_id = tasks[0][1]
            r = conn.execute(
                'SELECT report_id, verdict, triggered FROM audit_report WHERE report_id=%s',
                (task_report_id,)
            ).fetchone()
            if r:
                trig = r[2]
                if isinstance(trig, str):
                    trig = json.loads(trig)
                if trig:
                    for t_item in trig:
                        rid = t_item.get('rule_id', '?')
                        if rid not in ('blockword', 'content-safety', ''):
                            rule = conn.execute(
                                'SELECT no, condition FROM audit_rule WHERE id=%s AND del_flag=0', (rid,)
                            ).fetchone()
                            rno = rule[0] if rule else '?'
                        else:
                            rno = rid
                        reason = t_item.get('reason', '')
                        print(f'    -> Rule #{rno} ({rid}): {reason[:200]}')
                else:
                    print('    -> NO findings (pass)')
            else:
                print(f'    -> Report NOT FOUND: {task_report_id}')

        m = conn.execute(
            'SELECT audit_report_id, audit_status FROM material WHERE id=%s AND del_flag=0', (mid,)
        ).fetchone()
        if m:
            print(f'  Material report: {m[0]}, status: {m[1]}')
        print()

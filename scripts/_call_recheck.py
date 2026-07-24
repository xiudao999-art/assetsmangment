"""Call recheck API for 汽水音乐11改 task."""
import urllib.request, json, sys

BASE = 'http://localhost:8099'

# First get the task to confirm
resp = urllib.request.urlopen(f'{BASE}/audit/tasks?page=1&size=100', timeout=10)
tasks = json.loads(resp.read())
for t in tasks.get('tasks', []):
    if '汽水音乐11' in t.get('oss_key', ''):
        print(f"Task: id={t['id']} status={t['status']} verdict={t.get('verdict')}")
        print(f"  report_id={t.get('report_id')}")

# Now call recheck
print('\nCalling recheck...')
data = json.dumps({}).encode()
req = urllib.request.Request(
    f'{BASE}/audit/tasks/204885089484537856/recheck',
    data=data, method='POST',
    headers={'Content-Type': 'application/json'}
)
try:
    resp = urllib.request.urlopen(req, timeout=180)
    result = json.loads(resp.read())
    print(f"Verdict: {result.get('verdict')}")
    triggered = result.get('triggered', [])
    print(f'Total triggered: {len(triggered)}')
    rule7_hits = [t for t in triggered if t.get('rule_no') == 7]
    print(f'Rule #7 hits: {len(rule7_hits)}')
    for h in rule7_hits[:5]:
        print(f"  seg_begin={h.get('begin_ms')}ms source={h.get('source_type')}")
        print(f"  reason={h.get('reason','')[:120]}")
    print(f"Summary: {result.get('summary','')[:300]}")
    all_rules = set(t.get('rule_no') for t in triggered)
    print(f'All triggered rule numbers: {sorted(all_rules)}')
except urllib.error.HTTPError as e:
    print(f'HTTP Error: {e.code}')
    print(e.read().decode()[:500])
except Exception as e:
    print(f'Error: {e}')

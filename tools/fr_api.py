#!/usr/bin/env python3
"""Run one job through the Freerouting REST API: enqueue -> input -> start -> cancel -> output.

    python3 tools/fr_api.py [board.dsn]

FR_API overrides the API base URL (default http://127.0.0.1:37864/v1), FR_PID the
profile id, FR_SECONDS the polling budget.
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import work, NAME   # fr_job.txt goes to work/, not /tmp
import json, sys, time, uuid, base64, urllib.request, os
B = os.environ.get('FR_API', 'http://127.0.0.1:37864/v1')
PID = os.environ.get('FR_PID') or str(uuid.uuid4())
HDR = {'Freerouting-Profile-ID': PID,
       'Freerouting-Environment-Host': 'kicad-agent-toolkit/1.0',
       'Content-Type': 'application/json'}
def call(method, path, payload=None, raw=False):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(B + path, data=data, headers=HDR, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {'_http': e.code, '_body': body[:500]}
    if raw: return body
    try: return json.loads(body)
    except Exception: return {'_body': body[:500]}
print('profile', PID)
s = call('POST', '/sessions/create', {})
print('session:', json.dumps(s, ensure_ascii=False)[:200])
sid = s.get('id')
if not sid: sys.exit('could not create a session')
j = call('POST', '/jobs/enqueue', {'session_id': sid, 'name': NAME, 'priority': 'NORMAL'})
print('job:', json.dumps(j, ensure_ascii=False)[:300])
jid = j.get('id')
if not jid: sys.exit('could not enqueue the job')
dsn_path = sys.argv[1] if len(sys.argv) > 1 else work(NAME + '.dsn')
dsn = open(dsn_path, 'rb').read()
up = call('POST', f'/jobs/{jid}/input',
          {'filename': os.path.basename(dsn_path), 'data': base64.b64encode(dsn).decode()})
print('input upload:', json.dumps(up, ensure_ascii=False)[:200])
st = call('PUT', f'/jobs/{jid}/start', {})
print('start:', json.dumps(st, ensure_ascii=False)[:200])
open(work('fr_job.txt'), 'w').write(f'{PID}\n{sid}\n{jid}\n')
budget = int(os.environ.get('FR_SECONDS', '600'))
t0 = time.time()
while time.time() - t0 < budget:
    time.sleep(20)
    q = call('GET', f'/jobs/{jid}')
    print(f'  {int(time.time()-t0):4d} s  state={q.get("state")} stage={q.get("stage")} '
          f'{json.dumps(q.get("output") or {}, ensure_ascii=False)[:60]}')
    if q.get('state') in ('COMPLETED', 'FAILED', 'CANCELLED'): break
c = call('POST', f'/jobs/{jid}/cancel', {})
print('cancel:', json.dumps(c, ensure_ascii=False)[:200])
time.sleep(5)
out = call('GET', f'/jobs/{jid}/output')
if isinstance(out, dict) and out.get('data'):
    ses = base64.b64decode(out['data'])
    ses_path = work(NAME + '.ses')
    open(ses_path, 'wb').write(ses)
    print('GOT SES:', len(ses), 'bytes ->', os.path.abspath(ses_path))
else:
    print('output:', json.dumps(out, ensure_ascii=False)[:600])

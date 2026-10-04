#!/usr/bin/env python3
"""Fetch the current SES from a RUNNING Freerouting job.

    python3 tools/fr_grab.py [out.ses]

The /output/stream response does not close while the job runs, and the event's
terminating blank line may never arrive. So data: lines are accumulated and, after each
one, the buffer is parsed as JSON; as soon as that succeeds with a data field, we stop.
Do not cancel the job: after a cancel the stream returns zero bytes and /output keeps
answering "The job hasn't finished yet" - the result is lost.
FR_API overrides the API base URL (default http://127.0.0.1:37864/v1).
"""

import sys

if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
    print(__doc__.strip())
    sys.exit(0)
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import work, NAME   # fr_job.txt lives in work/, not /tmp
import json, base64, urllib.request, sys, os
B=os.environ.get('FR_API', 'http://127.0.0.1:37864/v1')
pid,sid,jid=open(work('fr_job.txt')).read().split()
HDR={'Freerouting-Profile-ID':pid,'Freerouting-Environment-Host':'kicad-agent-toolkit/1.0'}
req=urllib.request.Request(f'{B}/jobs/{jid}/output/stream',headers=HDR,method='GET')
cur=[]; data=None
with urllib.request.urlopen(req,timeout=300) as r:
    for raw in r:
        line=raw.decode('utf-8',errors='replace').rstrip('\r\n')
        if line.startswith('data:'):
            cur.append(line[6:] if line.startswith('data: ') else line[5:])
            if line.rstrip().endswith('}'):
                try:
                    j=json.loads('\n'.join(cur))
                    if j.get('data'): data=j['data']; break
                except Exception: pass
        elif not line.strip():
            if cur:
                try:
                    j=json.loads('\n'.join(cur))
                    if j.get('data'): data=j['data']; break
                except Exception: pass
                cur=[]
assert data, 'the stream produced no event with a data field'
ses=base64.b64decode(data)
assert ses[:9]==b'(session ', 'received data does not look like an SES file'
out=sys.argv[1] if len(sys.argv)>1 else work(NAME + '.ses')
open(out,'wb').write(ses)
print(f'SES {len(ses)} bytes -> {os.path.abspath(out)}')

#!/usr/bin/env python3
"""MCP server for the Digilent Analog Discovery 3 (stdio, JSON-RPC 2.0) - a measurement tool
that lets an AI assistant take readings during board bring-up and acceptance testing.

Tools:
  tp_list()                      - test-point map from a CSV (ref, net, expected value, location hint)
  measure_dc(channel, tp=None)   - mean voltage of a scope channel (1|2), compared with the expected value for tp
  scope_capture(channel, range_v, seconds, trigger_channel=None, trigger_v=None, edge='fall')
                                 - capture a waveform; returns min/max/avg/pp and the CSV path
  logic_capture(pins, rate_hz, seconds) - digital DIO capture; CSV path for sigrok/a decoder
  supply(positive_v=None, negative_v=None, enable=True) - programmable V+/V- supplies
  wavegen(channel, shape, freq_hz, amp_v, offset_v) - waveform generator (e.g. to exercise sensor inputs)
  frequency(channel, seconds)    - signal frequency estimate (zero crossings)

Test-point CSV (--tp-csv or AD3_TP_CSV, default ./testpoints.csv) columns:
  Ref, Net, Expected, Tol, Hint, X_mm, Y_mm, Scenarios

Dependencies: WaveForms SDK (dwf) + `pip install dwfpy`. Without the instrument start the server
with `--dry-run`: it answers with stubs, which tests the assistant integration before hardware.
"""
import argparse
import csv
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TP_CSV = os.environ.get('AD3_TP_CSV') or os.path.join(os.getcwd(), 'testpoints.csv')
OUT = os.environ.get('AD3_CAPTURES') or os.path.join(HERE, 'captures')
DRY = False
DEV = None


def tp_map():
    rows = list(csv.DictReader(open(TP_CSV, encoding='utf-8')))
    return {r['Ref']: r for r in rows}


def dev():
    global DEV
    if DRY:
        return None
    if DEV is None:
        import dwfpy as dwf
        DEV = dwf.Device()
        DEV.open()
    return DEV


def tool_tp_list(_):
    m = tp_map()
    return [{'tp': k, 'net': v.get('Net'), 'expected': v.get('Expected'), 'tol': v.get('Tol'),
             'hint': v.get('Hint'), 'xy': (v.get('X_mm'), v.get('Y_mm'))} for k, v in m.items()]


def tool_measure_dc(a):
    ch = int(a.get('channel', 1)); tp = a.get('tp')
    if DRY:
        v = 0.0
    else:
        d = dev(); scope = d.analog_input
        scope[ch - 1].setup(range=a.get('range_v', 25.0))
        scope.single(sample_rate=1e6, buffer_size=8192, configure=True, start=True)
        data = scope[ch - 1].get_data()
        v = float(sum(data) / len(data))
    res = {'channel': ch, 'volts': round(v, 4)}
    if tp:
        row = tp_map().get(tp)
        if row:
            res.update({'tp': tp, 'net': row.get('Net'), 'expected': row.get('Expected'), 'tol': row.get('Tol'),
                        'scenarios': row.get('Scenarios')})
    return res


def tool_scope_capture(a):
    ch = int(a.get('channel', 1)); rng = float(a.get('range_v', 25.0)); sec = float(a.get('seconds', 0.01))
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'scope_%s_ch%d.csv' % (time.strftime('%Y%m%d-%H%M%S'), ch))
    if DRY:
        data = [0.0] * 100; rate = 1e5
    else:
        d = dev(); scope = d.analog_input
        rate = min(125e6, max(1e4, 8192 / sec))
        scope[ch - 1].setup(range=rng)
        if a.get('trigger_channel'):
            scope.setup_edge_trigger(mode='normal', channel=int(a['trigger_channel']) - 1,
                                     slope='falling' if a.get('edge', 'fall') == 'fall' else 'rising',
                                     level=float(a.get('trigger_v', 1.0)))
        scope.single(sample_rate=rate, buffer_size=8192, configure=True, start=True)
        data = list(scope[ch - 1].get_data())
    with open(path, 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['t_s', 'v'])
        for i, v in enumerate(data):
            w.writerow([i / rate, v])
    return {'file': path, 'samples': len(data), 'rate_hz': rate,
            'min': min(data), 'max': max(data), 'avg': sum(data) / len(data), 'pp': max(data) - min(data)}


def tool_logic_capture(a):
    pins = a.get('pins', [0, 1]); rate = float(a.get('rate_hz', 10e6)); sec = float(a.get('seconds', 0.001))
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'logic_%s.csv' % time.strftime('%Y%m%d-%H%M%S'))
    n = int(min(16384, rate * sec))
    if DRY:
        samples = [0] * n
    else:
        d = dev(); la = d.digital_input
        la.single(sample_rate=rate, buffer_size=n, configure=True, start=True)
        samples = list(la.get_data())
    with open(path, 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['t_s'] + ['D%d' % p for p in pins])
        for i, s in enumerate(samples):
            w.writerow([i / rate] + [(int(s) >> p) & 1 for p in pins])
    return {'file': path, 'samples': n, 'rate_hz': rate, 'hint': 'sigrok-cli -I csv ... -P i2c/spi/uart to decode'}


def tool_supply(a):
    if DRY:
        return {'dry_run': True, **a}
    d = dev(); s = d.supplies
    if 'positive_v' in a:
        s.positive.setup(voltage=float(a['positive_v']), enabled=bool(a.get('enable', True)))
    if 'negative_v' in a:
        s.negative.setup(voltage=float(a['negative_v']), enabled=bool(a.get('enable', True)))
    s.master_enable = bool(a.get('enable', True))
    return {'ok': True, **a}


def tool_wavegen(a):
    if DRY:
        return {'dry_run': True, **a}
    d = dev(); g = d.analog_output[int(a.get('channel', 1)) - 1]
    g.setup(function=a.get('shape', 'square'), frequency=float(a.get('freq_hz', 1000)),
            amplitude=float(a.get('amp_v', 1.0)), offset=float(a.get('offset_v', 0.0)), start=True)
    return {'ok': True, **a}


def tool_frequency(a):
    r = tool_scope_capture({'channel': a.get('channel', 1), 'range_v': a.get('range_v', 5.0), 'seconds': a.get('seconds', 1e-4)})
    rows = list(csv.reader(open(r['file'], newline='')))[1:]
    v = [float(x[1]) for x in rows]
    if not v:
        return {'hz': None}
    mid = (max(v) + min(v)) / 2
    crossings = sum(1 for i in range(1, len(v)) if (v[i - 1] < mid) != (v[i] < mid))
    hz = crossings / 2 / (len(v) / r['rate_hz'])
    return {'hz': hz, 'file': r['file'], 'note': 'crystal oscillators: probe with a x10 probe (a x1 probe loads the crystal and can stop or pull it); '
                                                     'near the ~30 MHz AD3 bandwidth treat the result as an estimate, or measure a divided clock output'}


TOOLS = {
    'tp_list': (tool_tp_list, {'type': 'object', 'properties': {}}),
    'measure_dc': (tool_measure_dc, {'type': 'object', 'properties': {'channel': {'type': 'integer'}, 'tp': {'type': 'string'}, 'range_v': {'type': 'number'}}}),
    'scope_capture': (tool_scope_capture, {'type': 'object', 'properties': {'channel': {'type': 'integer'}, 'range_v': {'type': 'number'}, 'seconds': {'type': 'number'}, 'trigger_channel': {'type': 'integer'}, 'trigger_v': {'type': 'number'}, 'edge': {'type': 'string'}}}),
    'logic_capture': (tool_logic_capture, {'type': 'object', 'properties': {'pins': {'type': 'array', 'items': {'type': 'integer'}}, 'rate_hz': {'type': 'number'}, 'seconds': {'type': 'number'}}}),
    'supply': (tool_supply, {'type': 'object', 'properties': {'positive_v': {'type': 'number'}, 'negative_v': {'type': 'number'}, 'enable': {'type': 'boolean'}}}),
    'wavegen': (tool_wavegen, {'type': 'object', 'properties': {'channel': {'type': 'integer'}, 'shape': {'type': 'string'}, 'freq_hz': {'type': 'number'}, 'amp_v': {'type': 'number'}, 'offset_v': {'type': 'number'}}}),
    'frequency': (tool_frequency, {'type': 'object', 'properties': {'channel': {'type': 'integer'}, 'seconds': {'type': 'number'}, 'range_v': {'type': 'number'}}}),
}


def reply(i, result=None, error=None):
    msg = {'jsonrpc': '2.0', 'id': i}
    if error is not None:
        msg['error'] = {'code': -32000, 'message': str(error)}
    else:
        msg['result'] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + '\n'); sys.stdout.flush()


def main():
    global DRY
    global TP_CSV
    ap = argparse.ArgumentParser(); ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--tp-csv', default=TP_CSV, help='test-point map CSV'); a = ap.parse_args()
    DRY = a.dry_run
    TP_CSV = a.tp_csv
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        m, i, p = req.get('method'), req.get('id'), req.get('params') or {}
        if m == 'initialize':
            reply(i, {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'ad3-mcp', 'version': '0.1'}})
        elif m == 'notifications/initialized':
            continue
        elif m == 'tools/list':
            reply(i, {'tools': [{'name': k, 'description': TOOLS[k][0].__doc__ or k, 'inputSchema': TOOLS[k][1]} for k in TOOLS]})
        elif m == 'tools/call':
            name = p.get('name'); args = p.get('arguments') or {}
            try:
                res = TOOLS[name][0](args)
                reply(i, {'content': [{'type': 'text', 'text': json.dumps(res, ensure_ascii=False)}]})
            except Exception as e:  # noqa
                reply(i, error=e)
        elif i is not None:
            reply(i, error='unknown method %s' % m)


if __name__ == '__main__':
    main()

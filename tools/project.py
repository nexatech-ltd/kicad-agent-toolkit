"""Project-specific knowledge, kept out of the tools.

Every value that belongs to a particular board (which nets are planes, which
net classes carry extra clearance, which parts are fine-pitch, which findings are
accepted design decisions) is read from two places, in this order:

1. ``kicad-agent.json`` in the project directory (see ``examples/kicad-agent.json``);
2. the board's own custom rules file (``<board>.kicad_dru``), for clearances.

Tools call ``project.get(key, default)`` and keep a sensible generic default, so
a project without a config file still works.

    import project
    PLANES = set(project.get('plane_nets', ['GND']))
    CLASS_CLEAR = project.class_track_clearance()
"""
import json
import os
import re

import paths

_cache = {}


def config():
    """The parsed kicad-agent.json, or {} when the project has none."""
    if 'cfg' not in _cache:
        cfg = {}
        if os.path.exists(paths.CONFIG):
            with open(paths.CONFIG, encoding='utf-8') as f:
                cfg = json.load(f)
        _cache['cfg'] = cfg
    return _cache['cfg']


def get(key, default=None):
    """One config value; ``default`` when the key is absent."""
    return config().get(key, default)


def waivers(check):
    """Accepted findings of one check: {key: reason}. Keys are check-specific
    strings such as "U1" or "U1:VDDA" (see docs/configuration.md)."""
    return dict(config().get('waivers', {}).get(check, {}))


# --- custom design rules (.kicad_dru) -------------------------------------------------

_MM = re.compile(r'\(constraint\s+clearance\s+\(min\s+([0-9.]+)\s*mm\)\)')
_COND = re.compile(r'\(condition\s+"((?:[^"\\]|\\.)*)"\)')


def dru_rules(board=None):
    """[(name, clearance_mm or None, condition)] from the board's .kicad_dru.

    Comments are stripped first; a quoted condition may contain parentheses, so
    the condition is taken by its quotes, not by bracket balance.
    """
    dru = paths.sibling(board, '.kicad_dru') if board else paths.DRU
    if not os.path.exists(dru):
        return []
    text = '\n'.join(l for l in open(dru, encoding='utf-8').read().splitlines()
                     if not l.lstrip().startswith('#'))
    out = []
    for chunk in re.split(r'(?=\(rule\s)', text):
        if not chunk.startswith('(rule'):
            continue
        name = re.match(r'\(rule\s+"?([^"\s)]+)', chunk).group(1)
        m = _MM.search(chunk)
        c = _COND.search(chunk)
        out.append((name, float(m.group(1)) if m else None, c.group(1) if c else ''))
    return out


def class_track_clearance(board=None):
    """{net class: track-to-track clearance, mm} for rules of the form
    ``A.NetClass == 'X' && A.Type == 'Track' && B.Type == 'Track'``.

    The ``class_clearance`` config key overrides or extends what the rules give.
    """
    res = {}
    for _name, mm, cond in dru_rules(board):
        m = re.search(r"A\.NetClass\s*==\s*'([^']+)'", cond)
        if mm is not None and m and "Type == 'Track'" in cond:
            res[m.group(1)] = mm
    res.update(get('class_clearance', {}))
    return res


def fine_pitch_refs(board=None):
    """Footprints whose courtyard has a relaxed escape clearance in the rules
    (``A.insideCourtyard('U1')``), plus the ``fine_pitch_refs`` config key."""
    refs = set()
    for _name, _mm, cond in dru_rules(board):
        refs.update(re.findall(r"insideCourtyard\('([^']+)'\)", cond))
    refs.update(get('fine_pitch_refs', []))
    return refs


def fine_pitch_clearance(board=None, default=0.09):
    """Clearance of the courtyard escape rule, mm."""
    for _name, mm, cond in dru_rules(board):
        if mm is not None and 'insideCourtyard' in cond:
            return mm
    return get('fine_pitch_clearance', default)


def rule_clearance(a_type, b_type, board=None, default=None):
    """Board-wide clearance of a plain ``A.Type == a && B.Type == b`` rule, mm."""
    for _name, mm, cond in dru_rules(board):
        if mm is None or 'NetClass' in cond or 'insideCourtyard' in cond:
            continue
        if f"A.Type == '{a_type}'" in cond and f"B.Type == '{b_type}'" in cond:
            return mm
    return default


def plane_nets():
    """Nets poured as planes; the router treats them as zones, not signals."""
    return set(get('plane_nets', ['GND']))

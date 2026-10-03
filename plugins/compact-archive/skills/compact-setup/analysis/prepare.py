#!/usr/bin/env python3
"""Build the input files for the smart analysis of the compact-setup skill.

Reads inventory.json (written by inventory.py), renders the sessions it needs as readable text and writes, under OUT:

  data/<sid8>.render.txt, data/<sid8>.recs.json   rendered sessions (records numbered 0..n-1 per session)
  A/blind/Exx.txt, A/key.json                      study A: real auto compactions, blinded (no context sizes)
  B/batchN.txt, B/items.json                       study B: the user's messages, to be classified
  C/cN/...                                         study C: long cycles cut at simulated compaction points
  C/kN/...                                         calibration: real compactions to compare the simulator with
  tasks/*.md, compact_prompt.txt                   instructions for the agents
  args.json                                        the Workflow args (paths relative to OUT, which is "base")

Everything written contains transcript text: folders are created 0700 and files 0600. Transcripts are only read.
"""

import argparse
import json
import math
import os
import random
import re
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True     # no __pycache__ in the plugin folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inventory  # noqa: E402

TOOL = 'compact-setup-smart-analysis'    # marks the args.json files this script writes
GENERATED = ('A', 'B', 'C', 'data', 'tasks', 'args.json')   # what a rebuild replaces in OUT

BUFFER = 33_000           # auto-compaction fires at about window - BUFFER tokens
POST = 97_000             # context right after a compaction, if the inventory has no measured value
TR_RES, TR_USE = 2500, 1500   # tool results / tool calls are cut at this many characters
AUDIT_AFTER = 150         # records after the summary shown to an auditor
MIN_AFTER_ASSISTANT = 3   # skip compactions with almost no continuation to audit
MIN_AUDIT_EVENTS = 4      # fewer auto compactions than this: no audit
AUDIT_GROUP = 3           # events per auditor
BATCH = 140               # user messages per classifier
MAX_SIM_COMPACTIONS = 7   # a cycle needing more simulated compactions than this at some window is too long
SNIPPET = 1500            # characters of the previous assistant message / of the user's message per item

PRESETS = {
    'full': {'audit': 16, 'classify': 1000, 'cycles': 3, 'cal': 2},
    'lite': {'audit': 8, 'classify': 0, 'cycles': 1, 'cal': 1},
}
# Token model (millions of tokens processed, incl. cache reads), the same as inventory.py.
TOK_AUDIT, TOK_BATCH, TOK_CYCLE, TOK_CAL, TOK_MAIN = 4.5, 2.5, 30.0, 8.0, 5.0
FRESH_SHARE = 0.08
USD_PER_M = (0.4, 0.8)
TOK_RANGE = (0.75, 1.35)
MIN_RANGE = (0.7, 1.5)

WINDOW_BUCKETS = [200_000, 300_000, 400_000, 500_000, 600_000, 700_000, 800_000, 1_000_000]
META_PREFIXES = ('<command-', '<local-command', '<task-notification', '<bash-input', '<bash-stdout', '<bash-stderr',
                 '[Request interrupted', '<user-prompt-submit-hook')
SUMMARY_PREFIX_SCAN = 3   # the summary record follows its boundary within this many records
WRAP = 1000               # lines longer than 1.5 * WRAP are wrapped (agents' file readers cut very long lines)
CTRL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')   # control characters make text files look binary

# Blinded audit files must not reveal the window: context sizes, preTokens and window settings are redacted.
NUM = r'\d(?:[\d.,_]*\d)?[kKmM]?'
REDACT = [
    re.compile(r'ctx\s*=\s*' + NUM),
    re.compile(r'[\'"]?preTokens[\'"]?(?:\s*[:=]?\s*' + NUM + ')?'),
    re.compile(r'[\'"]?(?:autoCompactWindow|AUTO_COMPACT_WINDOW)[\'"]?\s*[:=]?\s*[\'"]?' + NUM + '[\'"]?'),
    re.compile(r'/autocompact\s+' + NUM),
]


def die(msg, code=1):
    print(f'prepare.py: error: {msg}', file=sys.stderr)
    sys.exit(code)


# ---------- windows ----------

def parse_window(s):
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*', s)
    if not m:
        raise ValueError(f'bad window {s!r} (use e.g. 300k or 1M)')
    v = float(m.group(1)) * {'': 1, 'k': 1e3, 'K': 1e3, 'm': 1e6, 'M': 1e6}[m.group(2)]
    return int(round(v))


def wtag(n):
    return f'{n // 1_000_000}M' if n % 1_000_000 == 0 else f'{n // 1000}k'


def regime_of(pre, buffer):
    """Only used if the inventory has no regime: the same rule as inventory.py (the largest window whose trigger
    point the context had reached, with 5k slack)."""
    if not isinstance(pre, (int, float)) or not pre:
        return 'unknown'
    if pre >= 800_000:
        return '1M'
    fit = [w for w in WINDOW_BUCKETS if w - buffer <= pre + 5_000]
    return wtag(fit[-1]) if fit else wtag(max(50_000, int(round((pre + buffer) / 50_000.0)) * 50_000))


# ---------- reading transcripts ----------

def block_text(c):
    """Text of a message content (a string or a list of blocks), with tool calls and results shortened."""
    if c is None:
        return ''
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return str(c)
    out = []
    for b in c:
        if isinstance(b, str):
            out.append(b)
            continue
        if not isinstance(b, dict):
            continue
        t = b.get('type')
        if t == 'text':
            out.append(b.get('text') or '')
        elif t == 'image':
            out.append('[IMAGE]')
        elif t == 'document':
            out.append('[DOCUMENT]')
        elif t == 'tool_result':
            s = block_text(b.get('content'))
            if len(s) > TR_RES:
                s = s[:TR_RES] + f' …[+{len(s) - TR_RES} chars]'
            out.append('[TOOL_RESULT] ' + s)
        elif t == 'tool_use':
            s = json.dumps(b.get('input', {}), ensure_ascii=False)
            if len(s) > TR_USE:
                s = s[:TR_USE] + ' …'
            out.append(f"[TOOL_USE {b.get('name')}] " + s)
    return '\n'.join(x for x in out if x)


def clean(t):
    """Drop control characters and wrap very long lines, so that the rendered files read as plain text."""
    t = CTRL.sub('', t.replace('\r\n', '\n').replace('\r', '\n'))
    if len(t) <= WRAP * 3 // 2:
        return t
    out = []
    for line in t.split('\n'):
        while len(line) > WRAP * 3 // 2:
            cut = line.rfind(' ', WRAP // 2, WRAP)
            cut = cut + 1 if cut > 0 else WRAP
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return '\n'.join(out)


def num(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def user_kind(d, content, text):
    if isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
        return 'TOOL_RESULT'
    if d.get('isMeta'):
        return 'META'
    o = d.get('origin')
    o = o.get('kind') if isinstance(o, dict) else o
    if o and o != 'human':
        return 'META'
    s = text.lstrip()
    if s.startswith(META_PREFIXES) or '<system-reminder>' in s[:30]:
        return 'META'
    return 'USER'


def parse_session(path):
    """Records of one main-session transcript: dicts i, ts, kind, ctx, line, text (+ trigger, preTokens)."""
    recs, ctx = [], 0
    with open(path, encoding='utf-8', errors='replace') as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get('isSidechain'):
                continue
            ty, ts = d.get('type'), str(d.get('timestamp') or '')
            if ty == 'system' and d.get('subtype') == 'compact_boundary':
                m = d.get('compactMetadata') or {}
                trig, pre = m.get('trigger'), m.get('preTokens')
                recs.append({'i': len(recs), 'ts': ts, 'kind': 'COMPACT_BOUNDARY', 'ctx': ctx, 'line': ln,
                             'trigger': trig, 'preTokens': pre,
                             'text': f'=== COMPACTION ({trig}, preTokens {pre}) ==='})
                continue
            if ty not in ('user', 'assistant'):
                continue
            msg = d.get('message')
            if not isinstance(msg, dict):
                continue
            content = msg.get('content')
            if ty == 'assistant':
                u = msg.get('usage') if isinstance(msg.get('usage'), dict) else {}
                c = num(u.get('input_tokens')) + num(u.get('cache_creation_input_tokens')) + \
                    num(u.get('cache_read_input_tokens'))
                if c:
                    ctx = c
            text = block_text(content)
            if not text.strip():
                continue
            if ty == 'user':
                kind = 'COMPACT_SUMMARY' if d.get('isCompactSummary') else user_kind(d, content, text)
            else:
                has_text = (isinstance(content, str) and content.strip()) or (isinstance(content, list) and any(
                    isinstance(b, dict) and b.get('type') == 'text' and (b.get('text') or '').strip() for b in content))
                kind = 'ASSISTANT' if has_text else 'TOOL_RESULT'
                if d.get('isApiErrorMessage'):
                    kind = 'META'
            recs.append({'i': len(recs), 'ts': ts, 'kind': kind, 'role': ty, 'ctx': ctx, 'line': ln, 'text': clean(text)})
    return recs


def redact(t):
    for rx in REDACT:
        t = rx.sub('[redacted]', t)
    return t.replace('ctx=', 'ctx =')


def render(recs, blind=False):
    out = []
    for r in recs:
        if blind:
            head = f"[#{r['i']} {r['ts'][:16]} {r['kind']}]"
            text = f"=== COMPACTION ({r.get('trigger')}) ===" if r['kind'] == 'COMPACT_BOUNDARY' else redact(r['text'])
        else:
            head = f"[#{r['i']} {r['ts'][:16]} ctx={r['ctx'] // 1000}k {r['kind']}]"
            text = r['text']
        out.append(f'\n{head}\n{text}\n')
    return ''.join(out)


# ---------- writing ----------

def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(str(path), 0o600)
    return len(text)


def write_json(path, obj):
    return write_text(path, json.dumps(obj, ensure_ascii=False, indent=1) + '\n')


def mkdir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), 0o700)


# ---------- sessions ----------

class Sessions:
    """Inventory sessions, parsed on demand; renders are written for the sessions that are used."""

    def __init__(self, inv, out, buffer):
        self.inv, self.out, self.buffer = inv, out, buffer
        self.by_sid, self.short, self._recs, self._emap, self._div = {}, {}, {}, {}, {}
        self.used = set()
        for s in inv.get('sessions') or []:
            if s.get('sid') and s.get('path'):
                self.by_sid[str(s['sid'])] = s
        # Short ids name the files: the first 8 characters, or as many as needed to tell sessions apart.
        for sid in self.by_sid:
            n = 8
            while n < len(sid) and any(o != sid and o[:n] == sid[:n] for o in self.by_sid):
                n += 1
            self.short[sid] = sid[:n]

    def recs(self, sid):
        if sid not in self._recs:
            p = Path(os.path.expanduser(self.by_sid[sid]['path']))
            try:
                self._recs[sid] = parse_session(p)
            except OSError as e:
                print(f'  warning: cannot read {p.name}: {e}; session skipped', file=sys.stderr)
                self._recs[sid] = []
        return self._recs[sid]

    def render_path(self, sid):
        return f'data/{self.short[sid]}.render.txt'

    def use(self, sid):
        self.used.add(sid)
        return self.render_path(sid)

    def events(self, sid):
        """Per inventory event of the session: (record index of its boundary or None, event dict, regime)."""
        if sid in self._emap:
            return self._emap[sid]
        recs, evs = self.recs(sid), self.by_sid[sid].get('events') or []
        bidx = [r['i'] for r in recs if r['kind'] == 'COMPACT_BOUNDARY']
        by_line = {recs[i]['line']: i for i in bidx}
        out = []
        for k, ev in enumerate(evs):
            i = next((j for j in bidx if recs[j]['ts'] == ev.get('ts') and recs[j].get('preTokens') == ev.get('preTokens')
                      and recs[j].get('trigger') == ev.get('trigger')), None)
            if i is None and isinstance(ev.get('line'), int):     # line numbers counted from 1 or from 0
                i = by_line.get(ev['line'], by_line.get(ev['line'] + 1))
            if i is None and len(bidx) == len(evs):
                i = bidx[k]
            reg = ev.get('regime') or ('manual' if ev.get('trigger') == 'manual' else regime_of(ev.get('preTokens'), self.buffer))
            out.append((i, ev, reg))
        self._emap[sid] = out
        return out

    def fork_div(self, sid):
        """First record index of a forked session that is not a copy of its parent (0 if not a fork)."""
        if sid in self._div:
            return self._div[sid]
        s, div = self.by_sid[sid], 0
        parent = s.get('fork_of')
        if parent:
            if parent in self.by_sid:
                a, b = self.recs(sid), self.recs(parent)
                n = min(len(a), len(b))
                div = next((i for i in range(n) if a[i]['kind'] != b[i]['kind'] or a[i]['text'] != b[i]['text']), n)
            elif isinstance(s.get('fork_div_idx'), int):
                div = s['fork_div_idx']
        self._div[sid] = div
        return div

    def write_renders(self):
        total = 0
        for sid in sorted(self.used):
            recs = self.recs(sid)
            total += write_text(self.out / self.render_path(sid), render(recs))
            write_json(self.out / f'data/{self.short[sid]}.recs.json', recs)
        return total


def summary_after(recs, b):
    """Index of the summary record that follows boundary b, or None."""
    for j in range(b + 1, min(len(recs), b + 1 + SUMMARY_PREFIX_SCAN)):
        if recs[j]['kind'] == 'COMPACT_SUMMARY':
            return j
        if recs[j]['kind'] == 'COMPACT_BOUNDARY':
            break
    return None


def next_boundary(recs, b):
    return next((r['i'] for r in recs[b + 1:] if r['kind'] == 'COMPACT_BOUNDARY'), len(recs))


def count(recs, kind):
    return sum(r['kind'] == kind for r in recs)


def n_messages(recs):
    """Assistant messages as the analysis counts them per cycle: assistant lines with text or a tool call (Claude Code
    writes one line per content block), the same unit as inventory.py and cost_sim.py."""
    return sum(r.get('role') == 'assistant' and r['kind'] != 'META' for r in recs)


def spread_order(n):
    """0..n-1 ordered so that any prefix is spread evenly over the range (middle, then quarters, ...)."""
    order, seen, k = [], set(), 1
    while len(order) < n:
        for j in range(k):
            i = min(n - 1, int((j + 0.5) * n / k))
            if i not in seen:
                seen.add(i)
                order.append(i)
        k *= 2
    return order


def balanced_chunks(xs, size):
    if not xs:
        return []
    n = math.ceil(len(xs) / size)
    q, r = divmod(len(xs), n)
    out, a = [], 0
    for k in range(n):
        b = a + q + (1 if k < r else 0)
        out.append(xs[a:b])
        a = b
    return out


# ---------- study A: audit of real auto compactions ----------

def study_audit(S, out, nmax, rng):
    if nmax <= 0:
        return None, 'audit: not requested'
    pool, late = [], 0
    for sid in S.by_sid:
        if not any(e.get('trigger') == 'auto' for e in S.by_sid[sid].get('events') or []):
            continue
        recs = S.recs(sid)
        div = S.fork_div(sid)
        evs = S.events(sid)
        by_idx = {bi: (ev, reg) for bi, ev, reg in evs if bi is not None}
        for bi, ev, reg in evs:
            if ev.get('trigger') != 'auto' or bi is None or bi < div:
                continue
            if ev.get('late'):      # fired right after a window change: its regime is ambiguous
                late += 1
                continue
            si = summary_after(recs, bi)
            if si is None:
                continue
            nxt = next_boundary(recs, bi)
            end = min(nxt, si + 1 + AUDIT_AFTER)
            win = recs[si + 1:end]
            n_asst = count(win, 'ASSISTANT')
            if n_asst < MIN_AFTER_ASSISTANT:
                continue
            # A complete cycle ends in the next automatic compaction at the same window; one cut off by the end of
            # the session, a manual compaction or a window change is shorter than a real cycle at this window.
            ne, nreg = by_idx.get(nxt, ({}, None))
            complete = ne.get('trigger') == 'auto' and not ne.get('late') and nreg == reg
            pool.append({'sid': sid, 'regime': reg, 'preTokens': ev.get('preTokens'), 'ts': recs[bi]['ts'],
                         'boundary_idx': bi, 'window_end_idx': end, 'n_user': count(win, 'USER'),
                         'n_assistant': n_messages(win), 'cycle_assistant': n_messages(recs[bi:nxt]),
                         'cycle_complete': bool(complete),
                         # assistant messages of the audited stretch, to compare regimes over the same span
                         'asst_idx': [r['i'] for r in win if r.get('role') == 'assistant' and r['kind'] != 'META']})
    note = f' ({late} that fired right after a window change left out)' if late else ''
    if len(pool) < MIN_AUDIT_EVENTS:
        return None, f'audit: skipped, only {len(pool)} usable auto compactions (need {MIN_AUDIT_EVENTS}){note}'

    # Stratify by regime (round robin), within a regime spread over sessions (round robin), within a session over time.
    queues = {}
    for reg in sorted({e['regime'] for e in pool}):
        by_s = {}
        for e in sorted(pool, key=lambda e: e['ts']):
            if e['regime'] == reg:
                by_s.setdefault(e['sid'], []).append(e)
        lists = [[v[i] for i in spread_order(len(v))] for v in sorted(by_s.values(), key=lambda v: (-len(v), v[0]['ts']))]
        q = []
        while any(lists):
            for lst in lists:
                if lst:
                    q.append(lst.pop(0))
        queues[reg] = q
    regs = sorted(queues, key=lambda r: (-len(queues[r]), r))
    chosen = []
    while len(chosen) < nmax and any(queues[r] for r in regs):
        for r in regs:
            if queues[r] and len(chosen) < nmax:
                chosen.append(queues[r].pop(0))

    # Blind ids in random order; groups interleave the regimes.
    shuffled = chosen[:]
    rng.shuffle(shuffled)
    for k, e in enumerate(shuffled, 1):
        e['blind_id'] = f'E{k:02d}'
    by_reg = {r: [e for e in chosen if e['regime'] == r] for r in regs}
    seq = []
    while any(by_reg.values()):
        for r in regs:
            if by_reg[r]:
                seq.append(by_reg[r].pop(0))
    groups = balanced_chunks(seq, AUDIT_GROUP)
    for g in groups:
        rng.shuffle(g)

    mkdir(out / 'A' / 'blind')
    key, kchars = [], 0
    for e in sorted(chosen, key=lambda e: e['blind_id']):
        recs = S.recs(e['sid'])
        kchars += write_text(out / 'A' / 'blind' / f"{e['blind_id']}.txt",
                             render(recs[e['boundary_idx']:e['window_end_idx']], blind=True))
        key.append({'blind_id': e['blind_id'], 'sid8': S.short[e['sid']], 'regime': e['regime'],
                    'preTokens': e['preTokens'], 'boundary_idx': e['boundary_idx'],
                    'window_end_idx': e['window_end_idx'], 'n_user': e['n_user'], 'n_assistant': e['n_assistant'],
                    'cycle_assistant': e['cycle_assistant'], 'cycle_complete': e['cycle_complete'],
                    'asst_idx': e['asst_idx'], 'full_render': S.use(e['sid']), 'ts': e['ts']})
    write_json(out / 'A' / 'key.json', key)
    args_groups = [[{'blind_id': e['blind_id'], 'blind_file': f"A/blind/{e['blind_id']}.txt",
                     'full_render': S.render_path(e['sid']), 'boundary_idx': e['boundary_idx']} for e in g] for g in groups]
    regs_n = {r: sum(e['regime'] == r for e in chosen) for r in regs}
    msg = (f"audit: {len(chosen)} of {len(pool)} usable auto compactions in {len(groups)} groups "
           f"({', '.join(f'{r} {n}' for r, n in regs_n.items())}), {kchars / 1e3:.0f} kchars{note}")
    return {'groups': args_groups, 'key_file': 'A/key.json'}, msg


# ---------- study B: the user's messages ----------

def study_classify(S, out, nmax):
    if nmax <= 0:
        return [], 'classify: not requested'
    items = []
    sids = sorted(S.by_sid, key=lambda s: (str(S.by_sid[s].get('t0') or ''), s))
    for sid in sids:
        recs = S.recs(sid)
        div = S.fork_div(sid)
        # Regime of a message = window in force = regime of the auto compaction that ends its cycle. A compaction that
        # fired right after a window change ("late") ended a cycle run under the old window: use the previous regime.
        autos, prev_reg = [], None
        for bi, ev, reg in S.events(sid):
            if bi is None or ev.get('trigger') != 'auto':
                continue
            autos.append((bi, (prev_reg or 'unknown') if ev.get('late') else reg))
            if not ev.get('late'):
                prev_reg = reg
        last_b, last_a, last_call = None, None, None
        for r in recs:
            if r['kind'] == 'COMPACT_BOUNDARY':
                last_b = r['i']
            elif r['kind'] == 'ASSISTANT':
                last_a = r
            if r.get('role') == 'assistant':
                last_call = r['i']
            if r['kind'] != 'USER' or r['i'] < div:
                continue
            # Context when the message was sent: that of the latest call, or of the next one at the start of the
            # session or right after a compaction.
            if last_call is not None and (last_b is None or last_b < last_call):
                ctx = r['ctx']
            else:
                ctx = next((x['ctx'] for x in recs[r['i']:] if x.get('role') == 'assistant' and x['ctx']), r['ctx'])
            nxt = next((reg for bi, reg in autos if bi > r['i']), None)
            prv = next((reg for bi, reg in reversed(autos) if bi < r['i'] and reg != 'unknown'), None)
            items.append({'id': f"{S.short[sid]}#{r['i']}", 'sid': sid, 'ts': r['ts'][:16], 'ctx_k': ctx // 1000,
                          'since_compact': None if last_b is None else r['i'] - last_b,
                          'cycle_regime': nxt or prv or 'unknown',
                          'prev': last_a['text'][-SNIPPET:] if last_a else '', 'user': r['text'][:SNIPPET]})
    n_all = len(items)
    if n_all == 0:
        return [], 'classify: no user messages found'
    if n_all > nmax:                       # even sample over all messages, in session and time order
        items = [items[int((k + 0.5) * n_all / nmax)] for k in range(nmax)]
    mkdir(out / 'B')
    batches, meta, kchars = [], [], 0
    for k, bt in enumerate(balanced_chunks(items, BATCH), 1):
        name = f'B/batch{k}.txt'
        parts = []
        for it in bt:
            sc = 'none' if it['since_compact'] is None else it['since_compact']
            parts.append(f"\n=== ITEM {it['id']} | {it['ts']} | ctx {it['ctx_k']}k | records since last compaction {sc} ===\n"
                         f"--- previous assistant message (last {SNIPPET} chars) ---\n{it['prev']}\n"
                         f"--- user's message (first {SNIPPET} chars) ---\n{it['user']}\n")
            S.use(it['sid'])
            meta.append({'id': it['id'], 'ts': it['ts'], 'ctx_k': it['ctx_k'], 'since_compact': it['since_compact'],
                         'cycle_regime': it['cycle_regime'], 'batch': name})
        kchars += write_text(out / name, ''.join(parts))
        batches.append(name)
    write_json(out / 'B' / 'items.json', meta)
    return batches, f'classify: {len(items)} of {n_all} user messages in {len(batches)} batches, {kchars / 1e3:.0f} kchars'


# ---------- study C: controlled cycles and calibration ----------

# Simulated compaction points: the one implementation in inventory.py, so that the inventory's estimate and its
# filter of cycles with too many compactions count exactly what is cut here.
sim_points = inventory.sim_points


def split_half(recs, b0, b1):
    tot = sum(len(r['text']) for r in recs[b0:b1])
    acc = 0
    for r in recs[b0:b1]:
        acc += len(r['text'])
        if acc >= tot / 2:
            return max(b0 + 1, r['i'])
    return b0 + 1


def study_cycles(S, out, nmax, windows, buffer, post):
    if nmax <= 0:
        return [], ['cycles: not requested']
    cands = [c for c in S.inv.get('eligible_cycles') or [] if c.get('sid') in S.by_sid]
    if not cands:
        return [], ['cycles: skipped, no eligible cycles in the inventory']
    chosen, tried, msgs = [], set(), []
    for first_pass in (True, False):            # at most one cycle per session, if possible
        for ci, cy in enumerate(cands):
            if len(chosen) >= nmax:
                break
            sid = cy['sid']
            if ci in tried or (first_pass and any(c['sid'] == sid for c in chosen)):
                continue
            tried.add(ci)
            recs, emap, div = S.recs(sid), S.events(sid), S.fork_div(sid)
            se, ee = cy.get('start_event'), cy.get('end_event')
            b0 = 0 if se is None or se < 0 else (emap[se][0] if se < len(emap) else None)
            b1 = len(recs) if ee is None else (emap[ee][0] if 0 <= ee < len(emap) else None)
            if b0 is None or b1 is None or b1 - b0 < 10 or b1 <= div:
                continue
            wins, dropped, too_long = {}, [], False
            for w in windows:
                pts = sim_points(recs, b0, b1, w, buffer, post)
                if len(pts) > MAX_SIM_COMPACTIONS:
                    too_long = True
                    break
                if pts:
                    wins[wtag(w)] = pts
                else:
                    dropped.append(wtag(w))
            if too_long:
                continue
            real = bool(cy.get('ends_in_real_auto')) and ee is not None and b1 < len(recs) and \
                recs[b1].get('trigger') == 'auto' and summary_after(recs, b1) is not None
            if not wins or (len(wins) < 2 and not real):
                continue
            chosen.append({'sid': sid, 'b0': b0, 'b1': b1, 'wins': wins, 'dropped': dropped, 'real': real,
                           'real_regime': (cy.get('regime_end') or emap[ee][2]) if real else None})
    args_cycles = []
    for k, c in enumerate(chosen, 1):
        name, sid, recs = f'c{k}', c['sid'], S.recs(c['sid'])
        d = out / 'C' / name
        mkdir(d / 'in')
        mkdir(d / 'sim')
        sizes = {}
        for tag, pts in c['wins'].items():
            cuts = [c['b0']] + pts + [c['b1']]
            for j, (a, b) in enumerate(zip(cuts[:-1], cuts[1:]), 1):
                sizes[f'{tag}_{j}'] = write_text(d / 'in' / f'seg_{tag}_{j}.txt', render(recs[a:b]))
        if c['real']:
            write_text(d / 'in' / 'S_REAL.txt', recs[summary_after(recs, c['b1'])]['text'])
        mid = split_half(recs, c['b0'], c['b1'])
        write_text(d / 'in' / 'half1.txt', render(recs[c['b0']:mid]))
        write_text(d / 'in' / 'half2.txt', render(recs[mid:c['b1']]))
        meta = {'name': name, 'sid8': S.short[sid], 'b0': c['b0'], 'b1': c['b1'], 'mid': mid, 'real': c['real'],
                'real_regime': c['real_regime'],
                'windows': {t: {'points': p, 'n': len(p)} for t, p in c['wins'].items()},
                'dropped_windows': c['dropped'], 'render': S.use(sid), 'buffer': buffer, 'post': post,
                'seg_chars': sizes, 'n_assistant': n_messages(recs[c['b0']:c['b1']])}
        write_json(d / 'meta.json', meta)
        args_cycles.append({'name': name, 'render': S.render_path(sid), 'real': c['real'],
                            'windows': [{'tag': t, 'n': len(p)} for t, p in c['wins'].items()]})
        span = sum(len(r['text']) for r in recs[c['b0']:c['b1']])
        msgs.append(f"cycle {name}: {S.short[sid]} records {c['b0']}-{c['b1']} ({span / 1e3:.0f} kchars), "
                    + ', '.join(f'{t} n={len(p)}' for t, p in c['wins'].items())
                    + (f", REAL ({c['real_regime']})" if c['real'] else ', no real end summary')
                    + (f", dropped {','.join(c['dropped'])} (no compaction)" if c['dropped'] else ''))
    if not chosen:
        msgs.append('cycles: none of the eligible cycles fits (too short or too long for the windows)')
    return args_cycles, msgs


def study_cal(S, out, nmax):
    if nmax <= 0:
        return [], ['calibration: not requested']
    cands = [c for c in S.inv.get('calibration_candidates') or [] if c.get('sid') in S.by_sid]
    chosen, tried = [], set()
    for first_pass in (True, False):
        for ci, cd in enumerate(cands):
            if len(chosen) >= nmax:
                break
            sid = cd['sid']
            if ci in tried or (first_pass and any(c['sid'] == sid for c in chosen)):
                continue
            tried.add(ci)
            recs, emap, div = S.recs(sid), S.events(sid), S.fork_div(sid)
            pk, k = cd.get('prev_event'), cd.get('event')
            if not (isinstance(pk, int) and isinstance(k, int) and 0 <= pk < k < len(emap)):
                continue
            pb, b = emap[pk][0], emap[k][0]
            if pb is None or b is None or b <= pb or b < div or recs[b].get('trigger') != 'auto':
                continue
            si = summary_after(recs, b)
            if si is None:
                continue
            chosen.append({'sid': sid, 'pb': pb, 'b': b, 'si': si, 'regime': cd.get('regime') or emap[k][2]})
    args_cal, msgs = [], []
    for k, c in enumerate(chosen, 1):
        name, recs = f'k{k}', S.recs(c['sid'])
        d = out / 'C' / name
        mkdir(d / 'in')
        mkdir(d / 'sim')
        n_in = write_text(d / 'in' / 'seg_input.txt', render(recs[c['pb']:c['b']]))
        write_text(d / 'in' / 'S_REAL.txt', recs[c['si']]['text'])
        write_json(d / 'meta.json', {'name': name, 'sid8': S.short[c['sid']], 'b0': c['pb'], 'b1': c['b'],
                                     'regime': c['regime'], 'real': True, 'render': S.use(c['sid']),
                                     'input_chars': n_in})
        args_cal.append({'name': name, 'render': S.render_path(c['sid'])})
        msgs.append(f"calibration {name}: {S.short[c['sid']]} records {c['pb']}-{c['b']} ({c['regime']}), "
                    f"input {n_in / 1e3:.0f} kchars")
    if not chosen:
        msgs.append('calibration: none available (the controlled results will be uncalibrated)')
    return args_cal, msgs


# ---------- estimate ----------

def estimate(n_audit, n_groups, n_batches, cycles, n_cal):
    """Agents (at most: verifiers and checkers only run when needed), tokens, cost and time; same model as inventory.py."""
    n_cycles = len(cycles)
    tok = TOK_AUDIT * n_audit + TOK_BATCH * n_batches + TOK_CYCLE * n_cycles + TOK_CAL * n_cal
    if tok:
        tok += TOK_MAIN
    minutes = (15 + max(n_audit * 1.5, 45 if n_cycles else 0, n_batches * 3)) if tok else 0
    agents = 2 * n_groups + 2 * n_batches + 5 * n_cal
    for c in cycles:
        agents += 2 + sum(w['n'] for w in c['windows']) + len(c['windows']) + (1 if c['real'] else 0) + 1
    lo, hi = tok * TOK_RANGE[0], tok * TOK_RANGE[1]
    return {'agents': agents, 'tokens_M': [round(lo, 1), round(hi, 1)],
            'fresh_M': [round(lo * FRESH_SHARE, 1), round(hi * FRESH_SHARE, 1)],
            'usd': [round(tok * USD_PER_M[0]), round(tok * USD_PER_M[1])],
            'minutes': [int(round(minutes * MIN_RANGE[0])), int(round(minutes * MIN_RANGE[1]))]}


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(
        description='Build the input files and args.json for the smart-analysis workflow (compact-setup skill).',
        epilog='Writes under OUT: data/, A/, B/, C/, tasks/, compact_prompt.txt (fallback copy if missing) and args.json. '
               'Folders are created 0700 and files 0600 because they contain transcript text.')
    ap.add_argument('--inventory', help='inventory.json from inventory.py (default: OUT/inventory.json)')
    ap.add_argument('--out', help='work folder OUT (default: the folder of --inventory)')
    ap.add_argument('--preset', choices=sorted(PRESETS), default='full', help='size of the analysis (default: full)')
    ap.add_argument('--audit-max', type=int, help='auto compactions to audit (full 16, lite 8)')
    ap.add_argument('--classify-max', type=int, help='user messages to classify (full 1000, lite 0)')
    ap.add_argument('--cycles-max', type=int, help='controlled cycles (full 3, lite 1)')
    ap.add_argument('--cal-max', type=int, help='calibration events (full 2, lite 1)')
    ap.add_argument('--windows', help='candidate windows to simulate (default: the windows of the inventory, else 300k,400k,500k)')
    ap.add_argument('--buffer', type=int, help=f'tokens below the window at which compaction fires (default: inventory or {BUFFER})')
    ap.add_argument('--post', type=int, help=f'context after a compaction (default: inventory post_median or {POST})')
    ap.add_argument('--seed', type=int, default=1, help='random seed for the blind ids (default: 1)')
    ap.add_argument('--skill-dir', help='the compact-setup skill folder holding analysis/tasks (default: next to this script)')
    ap.add_argument('--force', action='store_true', help='rebuild even if OUT already holds results of a workflow run (C/*/sim)')
    a = ap.parse_args()

    if not a.inventory and not a.out:
        die('give --inventory and/or --out')
    inv_path = Path(os.path.expanduser(a.inventory)) if a.inventory else Path(os.path.expanduser(a.out)) / 'inventory.json'
    out = Path(os.path.expanduser(a.out)) if a.out else inv_path.parent
    try:
        inv = json.loads(inv_path.read_text(encoding='utf-8'))
    except OSError as e:
        die(f'cannot read the inventory {inv_path}: {e} (run inventory.py first)')
    except ValueError as e:
        die(f'{inv_path} is not valid JSON: {e}')
    if not isinstance(inv, dict) or not isinstance(inv.get('sessions'), list):
        die(f'{inv_path} does not look like an inventory.json (no "sessions" list)')
    wspec = a.windows or ','.join(w for w in inv.get('windows') or [] if isinstance(w, str)) or '300k,400k,500k'
    try:
        windows = sorted({parse_window(w) for w in wspec.split(',') if w.strip()})
    except ValueError as e:
        die(str(e))
    buffer = a.buffer or num(inv.get('buffer')) or BUFFER
    post = a.post or num(inv.get('post_median')) or POST
    bad = [wtag(w) for w in windows if w - buffer <= post + 20_000]
    if not windows or bad:
        die(f'windows must be well above post-compaction context + buffer ({(post + buffer) // 1000}k): {", ".join(bad)}')
    p = dict(PRESETS[a.preset])
    for k, v in (('audit', a.audit_max), ('classify', a.classify_max), ('cycles', a.cycles_max), ('cal', a.cal_max)):
        if v is not None:
            if v < 0:
                die(f'--{k}-max must be >= 0')
            p[k] = v
    skill = Path(os.path.expanduser(a.skill_dir)) if a.skill_dir else Path(__file__).resolve().parent.parent
    tasks_src = skill / 'analysis' / 'tasks'
    if not (tasks_src / 'COMPACT_PROMPT_FALLBACK.md').is_file():
        die(f'task files not found in {tasks_src} (use --skill-dir)')

    os.umask(0o077)
    if out.exists() and not out.is_dir():
        die(f'{out} exists and is not a folder')
    if not out.exists():
        out.mkdir(parents=True)
        os.chmod(str(out), 0o700)
    present = [name for name in GENERATED if os.path.lexists(str(out / name))]
    if present:
        # Only a folder that this script prepared before is rebuilt: its args.json carries our marker and the folder's
        # own path. Anything else (a mistyped --out, a project folder with its own data/ or tasks/) is left alone.
        try:
            prev = json.loads((out / 'args.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            prev = None
        ours = (isinstance(prev, dict) and prev.get('tool') == TOOL and prev.get('version') == 1
                and prev.get('base') == str(out.resolve()))
        if not ours:
            die(f'{out} already holds {", ".join(present)}, not written by prepare.py for this folder (no matching '
                'args.json); nothing was changed. Use a new, empty --out folder')
        if any(out.glob('C/*/sim/*')) and not a.force:
            die(f'{out} already holds results of a workflow run (C/*/sim); use a new --out folder, or --force to rebuild')
        for name in GENERATED:
            old = out / name
            try:
                if old.is_symlink() or old.is_file():
                    old.unlink()
                elif old.is_dir():
                    shutil.rmtree(str(old))
            except OSError as e:
                die(f'cannot clear {old} for the rebuild: {e}')

    S = Sessions(inv, out, buffer)
    rng = random.Random(a.seed)
    print(f'prepare: {len(S.by_sid)} sessions in the inventory; preset {a.preset} '
          f"(audit {p['audit']}, classify {p['classify']}, cycles {p['cycles']}, calibration {p['cal']}); "
          f"windows {','.join(wtag(w) for w in windows)}; buffer {buffer // 1000}k, post {post // 1000}k")

    audit, msg_a = study_audit(S, out, p['audit'], rng)
    print('  ' + msg_a)
    batches, msg_b = study_classify(S, out, p['classify'])
    print('  ' + msg_b)
    cycles, msg_c = study_cycles(S, out, p['cycles'], windows, buffer, post)
    for m in msg_c:
        print('  ' + m)
    if cycles:
        cal, msg_k = study_cal(S, out, p['cal'])
    else:
        cal, msg_k = [], ['calibration: skipped (it only serves the controlled cycles)'] if p['cal'] else []
    for m in msg_k:
        print('  ' + m)
    mb = S.write_renders() / 1e6
    print(f'  rendered sessions: {len(S.used)} in data/ ({mb:.1f} M chars)')

    mkdir(out / 'tasks')
    for f in sorted(tasks_src.glob('*.md')):
        write_text(out / 'tasks' / f.name, f.read_text(encoding='utf-8'))
    fallback = (tasks_src / 'COMPACT_PROMPT_FALLBACK.md').read_text(encoding='utf-8')
    cp = out / 'compact_prompt.txt'
    current = cp.read_text(encoding='utf-8', errors='replace') if cp.is_file() else ''
    if current.strip() and current != fallback:
        source = 'extracted'
        os.chmod(str(cp), 0o600)
    else:
        write_text(cp, fallback)
        source = 'fallback'
    print(f'  compaction prompt: {source}' + ('' if source == 'extracted' else
          ' (compact_prompt.txt was missing: using the generic stand-in; the report will say so)'))

    args = {'version': 1, 'tool': TOOL, 'base': str(out.resolve()), 'prompt_source': source,
            'audit': audit or {'groups': [], 'key_file': 'A/key.json'},
            'classify': {'batches': batches},
            'cycles': cycles,
            'cal': cal}
    write_json(out / 'args.json', args)

    n_audit = sum(len(g) for g in args['audit']['groups'])
    if not n_audit and not cycles:
        print('prepare: too little data for a smart analysis (no audit and no controlled cycle); '
              'the default window 400k is recommended.')
    est = estimate(n_audit, len(args['audit']['groups']), len(batches), cycles, len(cal))
    if est['agents']:
        print(f"estimate: up to {est['agents']} agents, about {est['tokens_M'][0]}-{est['tokens_M'][1]}M tokens processed "
              f"(fresh {est['fresh_M'][0]}-{est['fresh_M'][1]}M), API-equivalent about ${est['usd'][0]}-{est['usd'][1]}, "
              f"about {est['minutes'][0]}-{est['minutes'][1]} min")
    print(f'args: {out / "args.json"}')


if __name__ == '__main__':
    main()

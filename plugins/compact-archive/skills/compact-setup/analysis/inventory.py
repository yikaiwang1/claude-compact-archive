#!/usr/bin/env python3
"""Take stock of Claude Code transcripts for the compact-setup smart analysis.

Scans the main session transcripts (~/.claude/projects/<project>/<session-id>.jsonl; subfolders hold subagents and
are skipped), lists sessions, compactions and long compaction cycles, and prints an estimate of the time and tokens
a smart analysis would take on this data. Writes OUT/inventory.json.

It only reads transcripts. It never prints message text and never changes anything under ~/.claude.

The other analysis scripts import the transcript helpers from this file (load_session, render_text, sim_points,
regime_of, parse_window, wtag, write_private).
"""
import argparse
import collections
import datetime
import hashlib
import json
import math
import os
import statistics
import sys

BUFFER = 33_000            # auto-compaction fires at about window - 33k tokens
POST_DEFAULT = 97_000      # context of the first call after a compaction, if none was observed
BUCKETS = [200_000, 300_000, 400_000, 500_000, 600_000, 700_000, 800_000, 1_000_000]
DEFAULT_WINDOWS = '300k,400k,500k'
SLACK = 5_000              # an auto compaction can fire slightly below the nominal trigger point
LATE = 15_000              # context already this far above the trigger before compacting: window changed mid-session
TR_RES, TR_USE = 2500, 1500
# User-role records that are not messages to the assistant: command wrappers, notifications, shell mode,
# interruptions, hook output (same list as prepare.py).
META_PREFIXES = ('<command-', '<local-command', '<task-notification', '<bash-input', '<bash-stdout', '<bash-stderr',
                 '[Request interrupted', '<user-prompt-submit-hook')
UA_PREFIX = 50             # user/assistant records compared to detect forked sessions
BATCH = 140                # user messages per classifier batch

PRESETS = {
    'full': dict(audit=16, classify=1000, cycles=3, cal=2),
    'lite': dict(audit=8, classify=0, cycles=1, cal=1),
}
# Tokens processed (millions, incl. cache reads), measured in the author's run.
TOK = dict(audit=4.5, batch=2.5, cycle=30.0, cal=8.0, main=5.0)
FRESH_SHARE = 0.08
USD_PER_M = (0.4, 0.8)     # API-equivalent $ per million tokens processed (about 0.5)
TOK_RANGE = (0.75, 1.35)
MIN_RANGE = (0.7, 1.5)


# ---------------------------------------------------------------- small helpers

def parse_window(s):
    """'300k' -> 300000, '1M' -> 1000000, '400000' -> 400000."""
    t = str(s).strip().lower().replace('_', '').replace(',', '')
    mult = 1
    if t.endswith('k'):
        mult, t = 1000, t[:-1]
    elif t.endswith('m'):
        mult, t = 1_000_000, t[:-1]
    v = int(round(float(t) * mult))
    if v <= 0:
        raise ValueError(s)
    return v


def parse_windows(s):
    out = []
    for part in str(s).split(','):
        if part.strip():
            w = parse_window(part)
            if w not in out:
                out.append(w)
    return sorted(out)


def wtag(w):
    if w >= 1_000_000 and w % 1_000_000 == 0:
        return '%dM' % (w // 1_000_000)
    if w % 1000 == 0:
        return '%dk' % (w // 1000)
    return str(w)


def kfmt(n):
    return '%dk' % round((n or 0) / 1000.0)


def regime_of(trigger, pre, buffer=BUFFER):
    """Window bucket an observed compaction belongs to: the largest bucket whose trigger point (window - buffer)
    the context had reached. preTokens >= 800k counts as the 1M default."""
    if trigger != 'auto':
        return 'manual' if trigger == 'manual' else (trigger or 'unknown')
    if not pre:
        return 'unknown'
    if pre >= 800_000:
        return '1M'
    fit = [b for b in BUCKETS if b - buffer <= pre + SLACK]
    if fit:
        return wtag(fit[-1])
    return wtag(max(50_000, int(round((pre + buffer) / 50_000.0)) * 50_000))


def regime_window(regime):
    try:
        return parse_window(regime)
    except (ValueError, TypeError):
        return None


def write_private(path, text):
    """Write a file readable by the owner only (analysis files contain transcript text)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def make_out_dir(path):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o700)
        os.chmod(path, 0o700)
    return path


def default_out():
    base = os.environ.get('COMPACT_ANALYSIS_DIR') or os.path.join('~', 'claude-compact-analysis')
    return os.path.join(os.path.expanduser(base), datetime.datetime.now().strftime('%Y%m%d-%H%M'))


def claude_dir():
    return os.path.expanduser(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join('~', '.claude'))


def current_window():
    """(window or None, source): environment variable, then the env block and autoCompactWindow in settings.json."""
    v = os.environ.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW')
    settings = {}
    try:
        with open(os.path.join(claude_dir(), 'settings.json'), encoding='utf-8') as f:
            settings = json.load(f) or {}
    except (OSError, ValueError):
        pass
    if not v and isinstance(settings.get('env'), dict):
        v = settings['env'].get('CLAUDE_CODE_AUTO_COMPACT_WINDOW')
    if v:
        try:
            return parse_window(v), 'env'
        except ValueError:
            pass
    w = settings.get('autoCompactWindow')
    if w not in (None, '', 'auto'):
        try:
            return parse_window(w), 'settings'
        except ValueError:
            pass
    return None, 'default'


# ---------------------------------------------------------------- transcript reading

def iter_json(path):
    with open(path, 'rb') as f:
        for n, raw in enumerate(f):
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield n, d


def render_text(content):
    """Text of a message: text blocks, [TOOL_USE name] {json} (cut at 1500 chars), [TOOL_RESULT] ... (cut at 2500
    chars), [IMAGE]. Thinking blocks are left out."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get('type')
        if t == 'text':
            out.append(b.get('text') or '')
        elif t == 'image':
            out.append('[IMAGE]')
        elif t == 'tool_result':
            cc = b.get('content')
            s = render_text(cc) if isinstance(cc, list) else str(cc or '')
            if len(s) > TR_RES:
                s = s[:TR_RES] + ' …[+%d chars]' % (len(s) - TR_RES)
            out.append('[TOOL_RESULT] ' + s)
        elif t == 'tool_use':
            s = json.dumps(b.get('input', {}), ensure_ascii=False)
            if len(s) > TR_USE:
                s = s[:TR_USE] + ' …'
            out.append('[TOOL_USE %s] %s' % (b.get('name'), s))
    return '\n'.join(out)


def has_text_block(content):
    if isinstance(content, str):
        return bool(content.strip())
    return any(isinstance(b, dict) and b.get('type') == 'text' and (b.get('text') or '').strip()
               for b in content or [])


def call_context(usage):
    if not isinstance(usage, dict):
        return 0
    return ((usage.get('input_tokens') or 0) + (usage.get('cache_creation_input_tokens') or 0)
            + (usage.get('cache_read_input_tokens') or 0))


def load_session(path, buffer=BUFFER, include_sdk=False, keep_text=False):
    """Read one main transcript. Returns None for SDK sessions unless include_sdk.

    records: one per rendered line, numbered 0..n-1, kinds USER / ASSISTANT / TOOL_RESULT / META /
    COMPACT_BOUNDARY / COMPACT_SUMMARY; ctx = context of the latest assistant call at that point.
    ASSISTANT = any assistant line with text or a tool call (Claude Code writes one line per content block). These
    records are the "assistant messages" the analysis counts per compaction cycle (the unit of the author's run).
    events: compactions, with the index of their boundary record."""
    sid = os.path.basename(path)[:-len('.jsonl')]
    recs, events = [], []
    ctx, max_ctx = 0, 0
    seen_calls = set()
    models = collections.Counter()
    t0 = t1 = None
    entry = None
    custom_title = summary_title = None
    lead_sid = None
    pending = None
    for n, d in iter_json(path):
        ep = d.get('entrypoint')
        if ep and entry is None:
            entry = str(ep)
            if entry.startswith('sdk') and not include_sdk:
                return None
        if isinstance(d.get('customTitle'), str):
            custom_title = d['customTitle']
        if d.get('type') == 'summary' and isinstance(d.get('summary'), str):
            summary_title = d['summary']
        if d.get('isSidechain'):
            continue
        ty = d.get('type')
        ts = d.get('timestamp') or ''
        if ty not in ('user', 'assistant', 'system'):
            continue
        if ts:
            t0 = ts if t0 is None or ts < t0 else t0
            t1 = ts if t1 is None or ts > t1 else t1
        line_sid = d.get('sessionId')

        def add(kind, text, has_text=False):
            r = dict(i=len(recs), line=n, ts=ts, kind=kind, role=ty, ctx=ctx, sid=line_sid,
                     key=hashlib.md5((kind + '\x00' + text).encode('utf-8', 'replace')).hexdigest()[:16])
            if kind == 'ASSISTANT':
                r['has_text'] = has_text
            if keep_text:
                r['text'] = text
            recs.append(r)

        if ty == 'system':
            if d.get('subtype') == 'compact_boundary':
                m = d.get('compactMetadata') or {}
                trig = m.get('trigger') or 'unknown'
                pre = m.get('preTokens') or 0
                ev = dict(ts=ts, trigger=trig, preTokens=pre, regime=regime_of(trig, pre, buffer), line=n,
                          idx=len(recs), ctx_before=ctx, post_ctx=None)
                events.append(ev)
                pending = ev
                add('COMPACT_BOUNDARY', '=== COMPACTION (%s, preTokens %s) ===' % (trig, pre))
            continue
        msg = d.get('message') or {}
        if not isinstance(msg, dict):
            msg = {}
        if lead_sid is None and line_sid:
            lead_sid = line_sid
        if ty == 'assistant':
            c = call_context(msg.get('usage'))
            if c:
                ctx = c
                max_ctx = max(max_ctx, c)
                key = (msg.get('id'), d.get('requestId'))
                if key == (None, None):
                    key = ('line', n)
                if key not in seen_calls:
                    seen_calls.add(key)
                    models[msg.get('model') or '?'] += 1
                if pending is not None:
                    pending['post_ctx'] = c
                    pending = None
        content = msg.get('content')
        if d.get('isCompactSummary'):
            add('COMPACT_SUMMARY', render_text(content))
            continue
        is_tr = isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result'
                                                  for b in content)
        kind = 'TOOL_RESULT' if is_tr else ('USER' if ty == 'user' else 'ASSISTANT')
        if (ty == 'user' and d.get('isMeta')) or (ty == 'assistant' and d.get('isApiErrorMessage')):
            kind = 'META'
        text = render_text(content)
        if not text.strip():
            continue
        if kind == 'USER' and (text.lstrip().startswith(META_PREFIXES) or '<system-reminder>' in text[:30]):
            kind = 'META'
        add(kind, text, has_text_block(content) if kind == 'ASSISTANT' else False)

    for ev in events:
        w = regime_window(ev['regime'])
        ev['late'] = bool(ev['trigger'] == 'auto' and w and ev['ctx_before'] > w - buffer + LATE)
    real_models = [(k, v) for k, v in models.most_common() if k not in ('<synthetic>', '?')]
    return dict(sid=sid, sid8=sid[:8], project=os.path.basename(os.path.dirname(path)), path=os.path.abspath(path),
                title=custom_title or summary_title, t0=t0, t1=t1, calls=len(seen_calls), max_ctx=max_ctx,
                model_main=real_models[0][0] if real_models else None, entrypoint=entry, lead_sid=lead_sid,
                records=recs, events=events, fork_of=None, fork_div_idx=None, fork_div_line=None)


def sim_points(records, b0, b1, window, buffer=BUFFER, post=POST_DEFAULT):
    """Record indices where Claude Code would have compacted inside [b0, b1) at `window`: the first record of an API
    call (any assistant record with a context, also one with tool calls only) at which the simulated context reaches
    window - buffer. The simulated context replays the real growth from call to call and restarts at `post` after a
    simulated compaction; at a real drop of more than 100k (a compaction or reset inside the stretch) it cannot stay
    above the real context (the same rule as cost_sim.py). prepare.py cuts the controlled cycles with this function,
    so the estimate and the cycle filter here count the same compactions."""
    trig, pts, sim, prev = window - buffer, [], None, None
    for r in records[b0:b1]:
        if r.get('role') != 'assistant' or not r.get('ctx'):
            continue
        c = r['ctx']
        if sim is None:
            sim = c
        else:
            g = c - prev
            sim = min(sim, c) if g < -100_000 else sim + g
        prev = c
        if sim >= trig and r['i'] > b0:
            pts.append(r['i'])
            sim = post
    return pts


# ---------------------------------------------------------------- forks

def _common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _birth(s):
    try:
        st = os.stat(s['path'])
        return getattr(st, 'st_birthtime', None) or 0
    except OSError:
        return 0


def detect_forks(sessions):
    """Mark sessions that start with a copy of another session (a fork). Records before fork_div_idx are copies of
    the parent's records and must not be counted twice. A fork keeps the parent's session id on the copied lines;
    without that, sessions whose first 50 user/assistant records are identical are paired."""
    by_sid = {s['sid']: s for s in sessions}
    for s in sessions:
        p = by_sid.get(s['lead_sid'])
        if p is None or p is s:
            continue
        div = next((r['i'] for r in s['records'] if r['sid'] == s['sid']), len(s['records']))
        div = min(div, _common_prefix([r['key'] for r in s['records']], [r['key'] for r in p['records']]) or div)
        s['fork_of'], s['fork_div_idx'] = p['sid'], div
    ua = {}
    for s in sessions:
        keys = [r['key'] for r in s['records'] if r['kind'] in ('USER', 'ASSISTANT')][:UA_PREFIX]
        s['_ua'] = keys
        if len(keys) >= 6:
            ua.setdefault(tuple(keys[:6]), []).append(s)
    for group in ua.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: (_birth(x) or 0, x['t0'] or ''))
        for j, s in enumerate(group):
            if s['fork_of']:
                continue
            for p in group[:j]:
                if p['fork_of'] == s['sid']:
                    continue
                k = min(len(s['_ua']), len(p['_ua']))
                if s['_ua'][:k] != p['_ua'][:k]:
                    continue
                s['fork_of'] = p['sid']
                s['fork_div_idx'] = _common_prefix([r['key'] for r in s['records']], [r['key'] for r in p['records']])
                break
    for s in sessions:
        s.pop('_ua', None)
        if s['fork_of']:
            d = s['fork_div_idx']
            s['fork_div_line'] = s['records'][d]['line'] if d < len(s['records']) else None
        div = s['fork_div_idx'] or 0
        for e in s['events']:
            e['dup'] = e['idx'] < div


# ---------------------------------------------------------------- cycles and calibration events

def _round_robin(items, group_key, tier_key, time_key):
    """Order by tier, then alternate between sessions, most recent first."""
    items = sorted(items, key=time_key, reverse=True)
    rank, out = collections.Counter(), []
    for it in sorted(items, key=tier_key):
        g = (tier_key(it), group_key(it))
        out.append((tier_key(it), rank[g], it))
        rank[g] += 1
    out.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in out]


def find_cycles(sessions, windows, buffer, post, limit=8):
    need = max(windows) + 150_000 - post
    found, too_many = [], 0
    for s in sessions:
        recs, evs = s['records'], s['events']
        div = s['fork_div_idx'] or 0
        starts = [(-1, 0)] + [(k, e['idx']) for k, e in enumerate(evs)]
        for j, (k, b0) in enumerate(starts):
            end_k, b1 = (starts[j + 1] if j + 1 < len(starts) else (None, len(recs)))
            if b0 < div or (s['fork_of'] and b0 == 0):
                continue
            ctxs = [r['ctx'] for r in recs[b0:b1] if r['kind'] == 'ASSISTANT' and r['ctx']]
            if not ctxs:
                continue
            peak = max(ctxs)
            if peak - ctxs[0] < need:
                continue
            sims = {}
            for w in windows:
                n = len(sim_points(recs, b0, b1, w, buffer, post))
                if n:
                    sims[wtag(w)] = n
            if not sims:
                continue
            if max(sims.values()) > 7:
                too_many += 1
                continue
            end = evs[end_k] if end_k is not None else None
            real = bool(end and end['trigger'] == 'auto')
            found.append(dict(sid=s['sid'], sid8=s['sid8'], start_event=k, end_event=end_k, b0=b0, b1=b1,
                              t0=recs[b0]['ts'], t1=recs[b1 - 1]['ts'], start_ctx=ctxs[0], peak_ctx=peak,
                              growth=peak - ctxs[0], ends_in_real_auto=real,
                              regime_end=end['regime'] if end else None,
                              n_user=sum(r['kind'] == 'USER' for r in recs[b0:b1]),
                              n_assistant=sum(r['kind'] == 'ASSISTANT' for r in recs[b0:b1]),
                              sim=sims))

    def tier(c):
        if c['ends_in_real_auto'] and c['regime_end'] == '1M':
            return 0
        return 1 if c['ends_in_real_auto'] else 2
    ordered = _round_robin(found, lambda c: c['sid'], tier, lambda c: c['t1'] or '')
    return ordered[:limit], len(found), too_many


def find_calibration(sessions, limit=4):
    found = []
    for s in sessions:
        evs = s['events']
        for k in range(1, len(evs)):
            e, p = evs[k], evs[k - 1]
            if e['trigger'] != 'auto' or e['dup'] or p['dup']:
                continue
            found.append(dict(sid=s['sid'], sid8=s['sid8'], prev_event=k - 1, event=k, regime=e['regime'],
                              input_ctx=e['preTokens'], prev_idx=p['idx'], idx=e['idx'], ts=e['ts'],
                              late=e['late']))

    def tier(c):
        return (0 if c['input_ctx'] <= 450_000 else 1, int(c['late']), regime_window(c['regime']) or 10 ** 9)
    return _round_robin(found, lambda c: c['sid'], tier, lambda c: c['ts'] or '')[:limit]


# ---------------------------------------------------------------- estimate

def pick_cycles(cycles, n):
    """At most one cycle per session if possible (prepare.py chooses the same way)."""
    first, rest, used = [], [], set()
    for c in cycles:
        (rest if c['sid'] in used else first).append(c)
        used.add(c['sid'])
    return (first + rest)[:n]


def estimate(preset, n_auto, n_user, cycles, n_cal):
    p = PRESETS[preset]
    skipped = []
    audit = min(p['audit'], n_auto) if n_auto >= 4 else 0
    if n_auto < 4:
        skipped.append('audit: fewer than 4 automatic compactions')
    n_class = min(p['classify'], n_user)
    batches = int(math.ceil(n_class / float(BATCH))) if n_class else 0
    chosen = pick_cycles(cycles, p['cycles'])
    if not cycles:
        skipped.append('controlled study: no long enough compaction cycle')
    cal = min(p['cal'], n_cal) if chosen else 0
    if chosen and p['cal'] and not n_cal:
        skipped.append('calibration: no pair of consecutive automatic compactions (results stay uncalibrated)')
    groups = int(math.ceil(audit / 3.0))
    agents = 2 * groups + 2 * batches + 5 * cal
    for c in chosen:
        agents += 2 + sum(c['sim'].values()) + len(c['sim']) + int(c['ends_in_real_auto']) + 1
    tok = TOK['audit'] * audit + TOK['batch'] * batches + TOK['cycle'] * len(chosen) + TOK['cal'] * cal
    if tok:
        tok += TOK['main']
    minutes = (15 + max(audit * 1.5, 45 if chosen else 0, batches * 3)) if tok else 0
    r1 = lambda x: round(x, 1)
    return dict(audit_events=audit, classify_batches=batches, cycles=len(chosen), cal=cal,
                tokens_M=[r1(tok * TOK_RANGE[0]), r1(tok * TOK_RANGE[1])],
                fresh_M=[r1(tok * TOK_RANGE[0] * FRESH_SHARE), r1(tok * TOK_RANGE[1] * FRESH_SHARE)],
                usd=[round(tok * USD_PER_M[0]), round(tok * USD_PER_M[1])],
                minutes=[int(round(minutes * MIN_RANGE[0])), int(round(minutes * MIN_RANGE[1]))],
                agents=agents, skipped=skipped, too_little_data=bool(audit == 0 and not chosen))


def describe(name, e):
    if e['too_little_data']:
        return '  %s: too little data for a smart analysis' % name
    parts = ['%d compactions audited' % e['audit_events']]
    if e['classify_batches']:
        parts.append('%d classify batches' % e['classify_batches'])
    parts.append('%d controlled cycle%s' % (e['cycles'], '' if e['cycles'] == 1 else 's'))
    if e['cal']:
        parts.append('%d calibration' % e['cal'])
    return ('  %s: about %d agents, about %d\u2013%d min, about %.0f\u2013%.0fM tokens processed (%.0f\u2013%.0fM '
            'fresh), about $%d\u2013%d at API list prices\n        (%s)'
            % (name, e['agents'], e['minutes'][0], e['minutes'][1], e['tokens_M'][0], e['tokens_M'][1],
               e['fresh_M'][0], e['fresh_M'][1], e['usd'][0], e['usd'][1], ', '.join(parts)))


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description='Scan Claude Code transcripts: sessions, compactions, long cycles, and '
                                             'an estimate of time and tokens for the smart analysis. Writes '
                                             'OUT/inventory.json; prints no message text.')
    ap.add_argument('--projects', default=None, help='transcript folder (default: ~/.claude/projects)')
    ap.add_argument('--out', default=None,
                    help='work folder (default: ${COMPACT_ANALYSIS_DIR:-~/claude-compact-analysis}/<YYYYMMDD-HHMM>)')
    ap.add_argument('--since', default=None, help='only sessions active on or after this date (YYYY-MM-DD)')
    ap.add_argument('--min-calls', type=int, default=30, help='skip sessions with fewer API calls (default 30)')
    ap.add_argument('--windows', default=DEFAULT_WINDOWS, help='candidate windows (default %s)' % DEFAULT_WINDOWS)
    ap.add_argument('--buffer', type=int, default=BUFFER, help='tokens below the window where auto-compaction '
                                                              'fires (default %d)' % BUFFER)
    ap.add_argument('--post', type=int, default=None, help='context after a compaction (default: median observed, '
                                                           'else %d)' % POST_DEFAULT)
    ap.add_argument('--include-sdk', action='store_true',
                    help='also scan SDK sessions (entrypoint sdk-*; usually other tools\' background agents)')
    a = ap.parse_args(argv)

    try:
        windows = parse_windows(a.windows)
    except ValueError:
        ap.error('bad --windows value: %s' % a.windows)
    if not windows:
        ap.error('--windows is empty')
    if a.since:
        try:
            datetime.date.fromisoformat(a.since)
        except ValueError:
            ap.error('--since must be YYYY-MM-DD')
    projects = os.path.abspath(os.path.expanduser(a.projects or os.path.join(claude_dir(), 'projects')))
    if not os.path.isdir(projects):
        sys.exit('error: transcript folder not found: %s' % projects)

    files = []
    for proj in sorted(os.listdir(projects)):
        pdir = os.path.join(projects, proj)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            fp = os.path.join(pdir, fn)
            if fn.endswith('.jsonl') and not fn.startswith('agent-') and os.path.isfile(fp):
                files.append(fp)
    if not files:
        sys.exit('error: no session transcripts (*.jsonl) in the project folders under %s' % projects)

    out = make_out_dir(a.out or default_out())
    sessions, n_sdk, n_small, n_old = [], 0, 0, 0
    for fp in files:
        try:
            s = load_session(fp, a.buffer, a.include_sdk)
        except OSError as e:
            print('warning: cannot read %s: %s' % (fp, e), file=sys.stderr)
            continue
        if s is None:
            n_sdk += 1
            continue
        if s['calls'] < a.min_calls:
            n_small += 1
            continue
        if a.since and (s['t1'] or '') < a.since:
            n_old += 1
            continue
        sessions.append(s)
    detect_forks(sessions)

    autos = [e for s in sessions for e in s['events'] if e['trigger'] == 'auto' and not e['dup']]
    posts = [e['post_ctx'] for e in autos if e['post_ctx']]
    post = a.post or (int(statistics.median(posts)) if posts else POST_DEFAULT)
    cur, src = current_window()

    regime_counts = collections.Counter()
    for s in sessions:
        for e in s['events']:
            if not e['dup']:
                regime_counts[e['regime']] += 1
    cycles, n_cycles_all, n_too_many = find_cycles(sessions, windows, a.buffer, post)
    cal = find_calibration(sessions)
    n_user = 0
    for s in sessions:
        div = s['fork_div_idx'] or 0
        s['user_msgs'] = sum(r['kind'] == 'USER' for r in s['records'])
        s['assistant_msgs'] = sum(r['kind'] == 'ASSISTANT' for r in s['records'])
        s['user_msgs_own'] = sum(r['kind'] == 'USER' for r in s['records'][div:])
        n_user += s['user_msgs_own']
    est = {k: estimate(k, len(autos), n_user, cycles, len(cal)) for k in PRESETS}

    def regime_sort(k):
        w = regime_window(k)
        return (0, -w) if w else (1, k)
    keep = ('sid', 'sid8', 'project', 'path', 'title', 't0', 't1', 'calls', 'user_msgs', 'assistant_msgs',
            'user_msgs_own', 'max_ctx', 'fork_of', 'fork_div_idx', 'fork_div_line', 'model_main', 'entrypoint')
    inv = dict(version=1, created=datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
               projects_dir=projects, current_window=cur, window_source=src, post_median=post, buffer=a.buffer,
               windows=[wtag(w) for w in windows], min_calls=a.min_calls, since=a.since,
               sessions=[dict({k: s[k] for k in keep}, n_records=len(s['records']), events=s['events'])
                         for s in sorted(sessions, key=lambda x: x['t1'] or '', reverse=True)],
               regime_counts={k: regime_counts[k] for k in sorted(regime_counts, key=regime_sort)},
               late_events=sum(e['late'] for s in sessions for e in s['events'] if not e['dup']),
               dup_events=sum(e['dup'] for s in sessions for e in s['events']),
               skipped=dict(sdk=n_sdk, few_calls=n_small, before_since=n_old),
               eligible_cycles=cycles, eligible_cycles_total=n_cycles_all, cycles_too_many_compactions=n_too_many,
               calibration_candidates=cal, user_msgs_total=n_user, estimate=est)
    path = os.path.join(out, 'inventory.json')
    write_private(path, json.dumps(inv, ensure_ascii=False, indent=1))

    # Short summary; titles only, never message text.
    nproj = len(set(s['project'] for s in sessions))
    skip = []
    if n_small:
        skip.append('%d with fewer than %d calls' % (n_small, a.min_calls))
    if n_sdk:
        skip.append('%d SDK sessions' % n_sdk)
    if n_old:
        skip.append('%d before %s' % (n_old, a.since))
    print('Sessions: %d in %d project folder%s%s' % (len(sessions), nproj, '' if nproj == 1 else 's',
                                                    (' (skipped: %s)' % ', '.join(skip)) if skip else ''))
    print('Current auto-compact window: %s (%s)' % (wtag(cur) if cur else 'default', src))
    rc = ', '.join('%s %d' % (k, v) for k, v in inv['regime_counts'].items()) or 'none'
    extra = []
    if inv['late_events']:
        extra.append('%d fired late, after a window change' % inv['late_events'])
    if inv['dup_events']:
        extra.append('%d copies in forked sessions not counted' % inv['dup_events'])
    print('Compactions by window: %s%s' % (rc, (' (%s)' % '; '.join(extra)) if extra else ''))
    print('Context after compaction (median of %d): %s; trigger buffer %s' % (len(posts), kfmt(post),
                                                                              kfmt(a.buffer)))
    with_c = [s for s in sessions if any(not e['dup'] for e in s['events'])]
    if with_c:
        print('Sessions with compactions:')
        for s in sorted(with_c, key=lambda x: -x['calls'])[:10]:
            c = collections.Counter(e['regime'] for e in s['events'] if not e['dup'])
            title = (s['title'] or '').replace('\n', ' ')
            if len(title) > 32:
                title = title[:31] + '…'
            print('  %s  %-32s %6d calls  max %5s  %s%s' % (s['sid8'], title, s['calls'], kfmt(s['max_ctx']),
                                                         ' '.join('%s×%d' % kv for kv in sorted(c.items())),
                                                         '  (fork)' if s['fork_of'] else ''))
    print('Long cycles for the controlled study: %d usable%s; calibration candidates: %d'
          % (n_cycles_all, (' (%d dropped: more than 7 simulated compactions)' % n_too_many) if n_too_many else '',
             len(cal)))
    print('User messages to classify: %d' % n_user)
    print('Estimate for a smart analysis (rough; tokens include cache reads):')
    for k in PRESETS:
        print(describe(k, est[k]))
        for s in est[k]['skipped']:
            print('        skipped: %s' % s)
    if est['full']['too_little_data']:
        print('Too little data for the smart analysis (fewer than 4 automatic compactions and no long cycle). '
              'Recommended: the default 400k (/autocompact 400k).')
    print('Wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())

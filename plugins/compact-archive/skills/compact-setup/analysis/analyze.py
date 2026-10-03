#!/usr/bin/env python3
"""Combine the smart-analysis studies into a recommended auto-compact window.

Inputs (all in the work folder OUT, written by the other scripts):
  the Workflow result     audit of real compactions (A), the user's corrections (B), controlled recall (C)
  A/key.json, B/items.json, C/*/meta.json, args.json   from prepare.py
  cost.json               from cost_sim.py (cost per window, assistant messages per compaction cycle)
  inventory.json          from inventory.py (only for the buffer and post-compaction constants)
Outputs: OUT/report.md, OUT/results.json and, if matplotlib is installed, OUT/fig.png.

The workflow result can be given as a JSON file whose top level is the result, as a wf_<id>.json file whose key
"result" holds it, or as a bare run id, which is looked up under ~/.claude/projects/*/*/workflows/. If the run has no
result (it stopped early), the agents' results are rebuilt from the run's journal.jsonl (best effort).

Standard library only; matplotlib is optional.
"""
import argparse
import collections
import glob
import io
import json
import math
import os
import random
import re
import sys

DEFAULT_WINDOWS = '300k,400k,500k'
DEFAULT_RECOMMENDATION = '400k'
BUFFER = 33_000
POST = 97_000
CTX_BUCKETS = [(150, '<150k'), (270, '150–270k'), (500, '270–500k'), (800, '500–800k'), (None, '>=800k')]
TYPE_NAMES = {'T1': 'redid work', 'T2': 'asked again', 'T3': 'broke a rule', 'T4': 'wrong value or state',
              'T5': 're-read material', 'T6': 'user had to correct', 'T7': 'other'}
LABELS = ['X', 'Y', 'Z', 'W', 'V', 'U', 'T', 'S']


# ---------------------------------------------------------------- small helpers

def die(msg, code=1):
    print(f'analyze.py: {msg}', file=sys.stderr)
    sys.exit(code)


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        print(f'analyze.py: warning: cannot read {path}: {e}', file=sys.stderr)
        return default


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data if isinstance(data, bytes) else data.encode('utf-8'))
    os.chmod(path, 0o600)


def tokens(tag):
    """'300k' -> 300000, '1M' -> 1000000, '450000' -> 450000; None if not a window size."""
    if tag is None:
        return None
    s = str(tag).strip().lower().replace('_', '').replace(',', '')
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([km]?)', s)
    if not m:
        return None
    v = float(m.group(1)) * {'': 1, 'k': 1_000, 'm': 1_000_000}[m.group(2)]
    return int(round(v))


def wtag(n):
    return f'{n // 1_000_000}M' if n % 1_000_000 == 0 else f'{n // 1000}k'


def norm_tag(tag):
    """Canonical window tag ('1000k' -> '1M'); other labels (REAL, SIM, manual) are returned unchanged."""
    n = tokens(tag)
    return wtag(n) if n else (str(tag) if tag is not None else None)


def as_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def quantile(sorted_xs, q):
    if not sorted_xs:
        return None
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def ci95(xs):
    xs = sorted(x for x in xs if x is not None)
    if len(xs) < 20:
        return None
    return [quantile(xs, 0.025), quantile(xs, 0.975)]


def f2(x, nd=2):
    return '–' if x is None else f'{x:.{nd}f}'


def fci(c, nd=2):
    return '' if not c else f' ({c[0]:.{nd}f} to {c[1]:.{nd}f})'


def pct(x, nd=0):
    return '–' if x is None else f'{100 * x:.{nd}f}%'


# ---------------------------------------------------------------- exact tests

def fisher_exact(a, b, c, d):
    """Two-sided Fisher exact test for the 2x2 table [[a, b], [c, d]].

    Sums the probabilities of all tables with the same margins that are no more likely than the observed one
    (the usual definition, as in R and scipy). Exact integer arithmetic via math.comb."""
    if min(a, b, c, d) < 0:
        raise ValueError('negative cell')
    r1, r2, c1 = a + b, c + d, a + c
    n = r1 + r2
    if n == 0:
        return 1.0
    lo, hi = max(0, c1 - r2), min(r1, c1)
    w = {x: math.comb(r1, x) * math.comb(r2, c1 - x) for x in range(lo, hi + 1)}
    obs = w[a]
    s = sum(v for v in w.values() if v * 10 ** 7 <= obs * (10 ** 7 + 1))
    return min(1.0, s / math.comb(n, c1))


def poisson_ci(k, conf=0.95):
    """Exact (Garwood) two-sided interval for a Poisson mean, given the observed count k (k = 0 -> [0, 3.69])."""
    a = (1 - conf) / 2

    def cdf(kk, mu):   # P(X <= kk), summed in log space so that large means do not underflow
        if mu <= 0:
            return 1.0
        return min(1.0, sum(math.exp(-mu + i * math.log(mu) - math.lgamma(i + 1)) for i in range(kk + 1)))

    def solve(f, lo, hi):   # f increasing on [lo, hi], f(lo) <= 0 <= f(hi)
        for _ in range(200):
            mid = (lo + hi) / 2
            if f(mid) < 0:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2
    top = k + 20 * math.sqrt(k + 1) + 20
    lower = 0.0 if k == 0 else solve(lambda mu: (1 - cdf(k - 1, mu)) - a, 0.0, float(k))
    upper = solve(lambda mu: a - cdf(k, mu), float(k), top)
    return [lower, upper]


def rate_test(k1, n1, k2, n2):
    """Exact test that two Poisson rates (k events over n exposures) are equal: k1 | k1+k2 ~ Bin(k1+k2, n1/(n1+n2))."""
    m = k1 + k2
    if m == 0 or n1 + n2 == 0:
        return 1.0
    p = n1 / (n1 + n2)
    pr = [math.comb(m, x) * p ** x * (1 - p) ** (m - x) for x in range(m + 1)]
    return min(1.0, sum(v for v in pr if v <= pr[k1] * (1 + 1e-7)))


# ---------------------------------------------------------------- loading the workflow result

STUDY_KEYS = ('audit', 'classify', 'cycles', 'cal')


def looks_like_result(d):
    return isinstance(d, dict) and any(k in d for k in STUDY_KEYS + ('log',))


def assign_labels(conds, k):
    """Same blind-label rule as workflow.js: rotate by k, reverse on every second pass through the list."""
    n = len(conds)
    if not n:
        return {}
    r = k % n
    order = conds[r:] + conds[:r]
    if (k // n) % 2 == 1:
        order = order[::-1]
    return {(LABELS[i] if i < len(LABELS) else f'L{i + 1}'): c for i, c in enumerate(order)}


def from_journal(path, args):
    """Rebuild a result object from a run's journal.jsonl (latest result per agent label; labels as in workflow.js)."""
    lab, res = {}, {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get('type') == 'started':
                lab[d.get('key')] = d.get('label') or ''
            elif d.get('type') == 'result' and d.get('key') in lab:
                res[re.sub(r'#\d+$', '', lab[d['key']])] = d.get('result')
    out = {'version': 1, 'audit': [], 'classify': [], 'cycles': [], 'cal': [],
           'log': [f'rebuilt from the journal ({len(res)} agent results); grading labels re-derived']}
    # indices as in workflow.js: empty groups, batches and unnamed cycles are filtered out before numbering
    groups = [g if isinstance(g, list) else [g] for g in (args.get('audit') or {}).get('groups') or []]
    groups = [[e for e in g if e] for g in groups]
    for k, g in enumerate([g for g in groups if g]):
        ids = [e if isinstance(e, str) else e.get('blind_id') for e in g]
        a, v = res.get(f'audit:g{k + 1}'), res.get(f'verify:g{k + 1}')
        if not isinstance(a, dict):
            continue
        out['audit'].append({'group': k, 'blind_ids': ids,
                             'events': [e for e in a.get('events') or [] if isinstance(e, dict) and e.get('blind_id') in ids],
                             'verdicts': (v or {}).get('verdicts') or [] if isinstance(v, dict) else []})
    for k, b in enumerate([b for b in (args.get('classify') or {}).get('batches') or [] if b]):
        c, ch = res.get(f'classify:b{k + 1}'), res.get(f'checkF:b{k + 1}')
        if isinstance(c, dict):
            out['classify'].append({'batch': b, 'items': c.get('items') or [],
                                    'checkF': ch.get('items') or [] if isinstance(ch, dict) else []})
    for k, cy in enumerate([c for c in args.get('cycles') or [] if isinstance(c, dict) and c.get('name')]):
        name = cy.get('name')
        wins = cy.get('windows') or []
        if isinstance(wins, dict):
            wins = [{'tag': t} for t in wins]
        wins = [w for w in wins if isinstance(w, dict) and w.get('tag')]
        q = res.get(f'write:{name}:h2') or res.get(f'write:{name}:h1')
        g = res.get(f'grade:{name}')
        if not isinstance(q, dict) or not isinstance(g, dict):
            continue
        answers = {}
        for w in wins:
            a = res.get(f"answer:{name}:{w.get('tag')}")
            if isinstance(a, dict):
                answers[w.get('tag')] = a
        if cy.get('real') and isinstance(res.get(f'answer:{name}:REAL'), dict):
            answers['REAL'] = res[f'answer:{name}:REAL']
        out['cycles'].append({'name': name, 'questions': q.get('questions') or [], 'answers': answers,
                              'labels': assign_labels(list(answers), k), 'grades': g,
                              'failed_windows': [w.get('tag') for w in wins if w.get('tag') not in answers]})
    for i, kc in enumerate([c for c in args.get('cal') or [] if isinstance(c, dict) and c.get('name')]):
        name = kc.get('name')
        q, g = res.get(f'write:{name}'), res.get(f'grade:{name}')
        if isinstance(q, dict) and isinstance(g, dict):
            out['cal'].append({'name': name, 'questions': q.get('questions') or [],
                               'labels': assign_labels(['REAL', 'SIM'], i), 'grades': g})
    return out


def load_run(run, projects, base):
    """Return (result, description of the source)."""
    args = read_json(os.path.join(base, 'args.json'), {}) or {}
    near = []
    if os.path.isfile(run):
        d = read_json(run)
        if looks_like_result(d):
            return d, run
        if isinstance(d, dict) and looks_like_result(d.get('result')):
            return d['result'], f'{run} (key "result")'
        rid = (d or {}).get('runId') if isinstance(d, dict) else None
        rid = rid or re.sub(r'\.json$', '', os.path.basename(run))
        near.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(run))), 'subagents', 'workflows', rid,
                                 'journal.jsonl'))
    else:
        rid = re.sub(r'\.json$', '', os.path.basename(run.strip()))
        names = [rid] if rid.startswith('wf_') else [rid, 'wf_' + rid]
        files = []
        for nm in names:
            files += glob.glob(os.path.join(projects, '*', '*', 'workflows', nm + '.json'))
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files:
            d = read_json(p)
            if isinstance(d, dict) and looks_like_result(d.get('result')):
                return d['result'], f'{p} (key "result")'
        if files:
            rid = re.sub(r'\.json$', '', os.path.basename(files[0]))
        for nm in names:
            near += glob.glob(os.path.join(projects, '*', '*', 'subagents', 'workflows', nm, 'journal.jsonl'))
    for j in near:
        if os.path.isfile(j):
            return from_journal(j, args), f'{j} (journal, best effort)'
    die(f'no workflow result found for "{run}". Give a run id (wf_...), a wf_<id>.json file or a result.json file.')


# ---------------------------------------------------------------- study A: audit of real compactions

SEV = ('minor', 'moderate', 'serious')


CAUSES = ('lost', 'diluted', 'unrelated')


def counted(v, mode):
    """Does a verdict count as a compaction-related incident? cause: lost = the information was missing from or
    distorted in the summary; diluted = it was in the summary, but the assistant handled it correctly before the
    compaction and wrongly after; unrelated = an ordinary slip. Verdicts from runs before `cause` existed have only
    context_loss (true = lost); --count all-real counts every confirmed incident for those."""
    if not v.get('real'):
        return False
    c = str(v.get('cause') or '').strip().lower()
    if c in CAUSES:
        return c == 'lost' if mode == 'lost' else (c != 'unrelated' or mode == 'all-real')
    return bool(v.get('context_loss')) or mode == 'all-real'


def cause_of(v):
    c = str(v.get('cause') or '').strip().lower()
    return c if c in CAUSES else ('lost' if v.get('context_loss') else 'not classified')


def audit_study(result, base, mode='compaction'):
    groups = [g for g in result.get('audit') or [] if isinstance(g, dict)]
    if not groups:
        return None
    key = {e.get('blind_id'): e for e in read_json(os.path.join(base, 'A', 'key.json'), []) or [] if isinstance(e, dict)}
    if not key:
        print('analyze.py: warning: A/key.json missing; regimes unknown', file=sys.stderr)
    events, verdicts, group_of = {}, [], {}
    for k, g in enumerate(groups):
        for e in g.get('events') or []:
            # one entry per event: the first, as in workflow.js (its incident numbers refer to that entry)
            if isinstance(e, dict) and e.get('blind_id') and e['blind_id'] not in events:
                events[e['blind_id']] = e
                group_of[e['blind_id']] = k
        verdicts += [v for v in g.get('verdicts') or [] if isinstance(v, dict)]
    if not events:
        return None

    # Join verdicts to incidents. workflow.js numbers the incidents of each event (n = position in the event's list),
    # because two incidents can share an after_idx; the verifier returns n with each verdict. Pass 1 joins on
    # (blind_id, n) when the after_idx agrees. Pass 2 joins what is left on (blind_id, after_idx), preferring the same
    # type, then the verifier's order (results without n, or n counted from 1). Pass 3 joins what is still left on
    # (blind_id, n) alone (a verifier that corrected the index). Every verdict is used at most once.
    incs = [(bid, j, inc) for bid, e in events.items() for j, inc in enumerate(e.get('incidents') or [])
            if isinstance(inc, dict)]
    by_n = {}
    for v in verdicts:
        if as_int(v.get('n')) is not None:
            by_n.setdefault((v.get('blind_id'), as_int(v.get('n'))), v)
    match, used = {}, set()
    for bid, j, inc in incs:
        v = by_n.get((bid, j))
        if v is not None and id(v) not in used and as_int(v.get('after_idx')) in (None, as_int(inc.get('after_idx'))):
            match[(bid, j)] = v
            used.add(id(v))
    for bid, j, inc in incs:
        if (bid, j) in match:
            continue
        cands = [v for v in verdicts if id(v) not in used and v.get('blind_id') == bid
                 and as_int(v.get('after_idx')) == as_int(inc.get('after_idx'))]
        cands.sort(key=lambda v: v.get('type') != inc.get('type'))     # stable: same type first, then list order
        if cands:
            match[(bid, j)] = cands[0]
            used.add(id(cands[0]))
    for bid, j, inc in incs:
        v = by_n.get((bid, j))
        if (bid, j) not in match and v is not None and id(v) not in used:
            match[(bid, j)] = v
            used.add(id(v))

    incidents, unverified = [], []
    for bid, j, inc in incs:
        v = match.get((bid, j))
        if v is None:
            unverified.append(dict(blind_id=bid, n=j, type=inc.get('type'), severity=str(inc.get('severity', '')).lower()))
            continue
        if counted(v, mode):
            incidents.append(dict(blind_id=bid, after_idx=as_int(inc.get('after_idx')), type=v.get('type') or inc.get('type'),
                                  severity=str(v.get('severity') or inc.get('severity') or '').lower(), src='audit',
                                  cause=cause_of(v), explanation=str(inc.get('explanation') or '')[:300]))
    for v in verdicts:  # incidents the verifier reported on its own (or that could not be joined)
        if id(v) not in used and v.get('blind_id') in events and counted(v, mode):
            incidents.append(dict(blind_id=v.get('blind_id'), after_idx=as_int(v.get('after_idx')), type=v.get('type'),
                                  src='verifier', severity=str(v.get('severity') or '').lower(), cause=cause_of(v),
                                  explanation=str(v.get('reason') or '')[:300]))
    incidents = [x for x in incidents if x['severity'] in SEV]

    # A compaction with a reported incident that has no verdict (the verifier failed or skipped it) cannot be scored,
    # and counting it as clean would bias the rates towards zero. Leaving out only such compactions would bias them
    # too (it removes compactions because they had incidents), so the whole audit group goes: the verifier's failure
    # does not depend on the window, and the remaining groups stay an unselected sample.
    bad_groups = sorted({group_of[u['blind_id']] for u in unverified})
    excluded = sorted(b for b in events if group_of[b] in bad_groups)
    reported = len(events)
    events = {b: e for b, e in events.items() if b not in excluded}
    incidents = [x for x in incidents if x['blind_id'] in events]
    for x in incidents:
        x['regime'] = norm_tag(key.get(x['blind_id'], {}).get('regime')) or 'unknown'

    by_reg = collections.OrderedDict()
    for bid in sorted(events):
        k = key.get(bid, {})
        reg = norm_tag(k.get('regime')) or 'unknown'
        r = by_reg.setdefault(reg, dict(events=[], asst=0, user=0, cov=[], sc_correct=0, sc_wrong=0, sc_unver=0))
        r['events'].append(bid)
        r['asst'] += as_int(k.get('n_assistant')) or 0
        r['user'] += as_int(k.get('n_user')) or 0
        na, ca = as_int(k.get('n_assistant')), as_int(k.get('cycle_assistant'))
        if na is not None and ca:
            r['cov'].append(min(1.0, na / ca))
        sc = events[bid].get('summary_check')
        sc = sc if isinstance(sc, dict) else {}
        r['sc_correct'] += as_int(sc.get('correct')) or 0
        r['sc_wrong'] += as_int(sc.get('wrong')) or 0
        r['sc_unver'] += as_int(sc.get('unverifiable')) or 0
    regimes = {}
    for reg, r in by_reg.items():
        ids = set(r['events'])
        cnt = collections.Counter(x['severity'] for x in incidents if x['blind_id'] in ids)
        ms = cnt['moderate'] + cnt['serious']
        tot = ms + cnt['minor']
        n = len(ids)
        ev_ms = len({x['blind_id'] for x in incidents if x['blind_id'] in ids and x['severity'] != 'minor'})
        checked = r['sc_correct'] + r['sc_wrong']
        # Cycle length: only cycles that ended in the next automatic compaction at the same window (older key.json
        # files have no flag: all cycles); one cut off at the end of a session is shorter than a real cycle.
        cycles = [as_int(key.get(b, {}).get('cycle_assistant')) for b in ids if key.get(b, {}).get('cycle_complete', True)]
        cycles = [c for c in cycles if c]
        regimes[reg] = dict(
            compactions=n, assistant_msgs=r['asst'], user_msgs=r['user'], minor=cnt['minor'], moderate=cnt['moderate'],
            serious=cnt['serious'], mod_ser=ms, all=tot, events_with_mod_ser=ev_ms,
            per_compaction_mod_ser=ms / n, per_compaction_all=tot / n,
            per100_audited_mod_ser=100 * ms / r['asst'] if r['asst'] else None,
            per100_audited_all=100 * tot / r['asst'] if r['asst'] else None,
            summary_claims_checked=checked, summary_claims_wrong=r['sc_wrong'],
            summary_wrong_share=r['sc_wrong'] / checked if checked else None,
            coverage=mean(r['cov']), mean_cycle_assistant=mean(cycles), complete_cycles=len(cycles),
            types=dict(collections.Counter(x['type'] for x in incidents if x['blind_id'] in ids)))

    # Fisher exact test between the two largest regimes. Each compaction is one trial: with or without at least one
    # moderate or serious incident (several incidents in one compaction are not several compactions). The exact rate
    # test compares the incident counts per compaction instead.
    def compare(rows):
        big = sorted([g for g in rows if g != 'unknown' and rows[g]['compactions'] >= 3],
                     key=lambda g: -rows[g]['compactions'])[:2]
        if len(big) < 2:
            return None
        a, b = rows[big[0]], rows[big[1]]
        k1, k2 = a['events_with_mod_ser'], b['events_with_mod_ser']
        return dict(regimes=big, table=[[k1, a['compactions'] - k1], [k2, b['compactions'] - k2]],
                    basis='compactions with >= 1 moderate+serious incident vs without',
                    p=fisher_exact(k1, a['compactions'] - k1, k2, b['compactions'] - k2),
                    rate_test_p=rate_test(a['mod_ser'], a['compactions'], b['mod_ser'], b['compactions']))
    fisher = compare(regimes)
    ev_reg = {bid: norm_tag(key.get(bid, {}).get('regime')) or 'unknown' for bid in events}
    per_event = {bid: dict(ms=sum(1 for x in incidents if x['blind_id'] == bid and x['severity'] != 'minor'),
                           all=sum(1 for x in incidents if x['blind_id'] == bid)) for bid in events}

    # Same-span check: the auditor sees up to ~150 records after each compaction, which covers most of a short cycle
    # but a small part of a long one. Count only incidents within the first N assistant messages after the summary,
    # N = the shortest typical (median) audited span over the regimes, and compare the regimes again.
    span = None
    spans = {}
    if events and all(isinstance(key.get(b, {}).get('asst_idx'), list) for b in events):
        for reg, r in by_reg.items():
            if tokens(reg):
                ns = sorted(len(key[b]['asst_idx']) for b in r['events'])
                spans[reg] = ns[len(ns) // 2]
        N = min(spans.values()) if spans else 0
        if N > 0:
            rows = {}
            for reg, r in by_reg.items():
                if not tokens(reg):
                    continue
                ms_n, ev_n = 0, 0
                for b in r['events']:
                    ai = key[b]['asst_idx']
                    hits = [x for x in incidents if x['blind_id'] == b and x['severity'] != 'minor'
                            and x['after_idx'] is not None and sum(1 for i in ai if i < x['after_idx']) < N]
                    ms_n += len(hits)
                    ev_n += bool(hits)
                rows[reg] = dict(compactions=len(r['events']), mod_ser=ms_n, events_with_mod_ser=ev_n,
                                 per_compaction_mod_ser=ms_n / len(r['events']))
            span = dict(n_msgs=N, median_span=spans, regimes=rows, fisher=compare(rows))
    return dict(regimes=regimes, fisher=fisher, incidents=incidents, unverified=len(unverified),
                unverified_list=unverified, excluded_unverified=excluded, excluded_groups=[
                    as_int(groups[k].get('group')) + 1 if as_int(groups[k].get('group')) is not None else k + 1
                    for k in bad_groups], reported=reported,
                event_regime=ev_reg, per_event=per_event, same_span=span,
                audited=len(events), not_audited=sorted({b for g in groups for b in g.get('blind_ids') or [] if isinstance(b, str)}
                                                        - set(events) - set(excluded)))


# ---------------------------------------------------------------- messages per cycle and cost

def load_cost(path, windows):
    c = read_json(path) if path else None
    if not isinstance(c, dict):
        return None
    rel, sav, sav_long, mpc = {}, {}, {}, {}
    for k, v in (c.get('saving_vs_1M') or {}).items():
        if v is not None:
            sav[norm_tag(k)] = float(v)
    for k, v in (c.get('saving_long_sessions_vs_1M') or {}).items():
        if v is not None:
            sav_long[norm_tag(k)] = float(v)
    total = {norm_tag(k): v for k, v in (c.get('total') or {}).items() if v is not None}
    if not sav and total.get('1M'):
        sav = {k: 1 - v / total['1M'] for k, v in total.items()}
    sav.setdefault('1M', 0.0)
    for k, v in sav.items():
        rel[k] = 1 - v
    for k, v in (c.get('msgs_per_cycle') or {}).items():
        if v:
            mpc[norm_tag(k)] = float(v)
    mpc_pooled = {norm_tag(k): float(v) for k, v in (c.get('msgs_per_cycle_pooled') or {}).items() if v}
    return dict(saving=sav, saving_long=sav_long, rel_cost=rel, msgs_per_cycle=mpc, msgs_per_cycle_pooled=mpc_pooled, total=total,
                prices_note=c.get('prices_note'), path=path,
                smaller_window_compactions=as_int(c.get('smaller_window_compactions')) or 0, until=c.get('until'))


def msgs_per_cycle(windows, cost, audit, buffer, post):
    """Assistant messages per compaction cycle at each window: cost.json (growth-based, else the replay's own count),
    else scaled from the complete audited cycles."""
    out, src = {}, {}
    for w in windows:
        if cost and cost['msgs_per_cycle'].get(w):
            out[w], src[w] = cost['msgs_per_cycle'][w], 'cost_sim'
        elif cost and cost.get('msgs_per_cycle_pooled', {}).get(w):
            out[w], src[w] = cost['msgs_per_cycle_pooled'][w], 'counted between the cost replay\'s compactions'
    if audit:
        obs = {g: r['mean_cycle_assistant'] for g, r in audit['regimes'].items()
               if tokens(g) and r['mean_cycle_assistant'] and tokens(g) - buffer - post > 0}
        for w in windows:
            if w in out or not obs:
                continue
            g = min(obs, key=lambda g: abs(math.log(tokens(g) / tokens(w))))
            # context grows roughly linearly with messages, so a cycle is proportional to (trigger - post)
            out[w] = obs[g] * (tokens(w) - buffer - post) / (tokens(g) - buffer - post)
            src[w] = f"scaled from {audit['regimes'][g]['complete_cycles']} complete audited {g} cycle(s)"
    return out, src


def projection(audit, windows, mpc, mode, boot, rng):
    """Moderate+serious (and all) incidents per 100 assistant messages at each window."""
    regs = {g: r for g, r in audit['regimes'].items() if tokens(g) and r['compactions']}
    if not regs:
        return None
    p = audit['fisher']['p'] if audit['fisher'] else None
    used = mode if mode != 'auto' else ('nearest' if p is not None and p < 0.05 else 'pooled')

    def nearest(w):
        return min(regs, key=lambda g: abs(math.log(tokens(g) / tokens(w))))

    def pick(counts, w):  # counts: {regime: (n, ms, all)} -> the (n, ms, all) used at window w
        if used == 'pooled':
            return tuple(sum(c[i] for c in counts.values()) for i in range(3))
        return counts[nearest(w)]

    def rates(counts):
        out = {}
        for w in windows:
            n, ms, al = pick(counts, w)
            out[w] = (ms / n, al / n) if n else (None, None)
        return out

    def counts_of(ev_by_reg):
        return {g: (len(ids), sum(audit['per_event'][b]['ms'] for b in ids), sum(audit['per_event'][b]['all'] for b in ids))
                for g, ids in ev_by_reg.items()}

    ev_by_reg = collections.defaultdict(list)
    for b, g in audit['event_regime'].items():
        if g in regs:
            ev_by_reg[g].append(b)
    counts = counts_of(ev_by_reg)
    point = rates(counts)
    reps = collections.defaultdict(list)
    for _ in range(boot):
        smp = {g: [rng.choice(ids) for _ in ids] for g, ids in ev_by_reg.items()}
        r = rates(counts_of(smp))
        for w in windows:
            if mpc.get(w) and r[w][0] is not None:
                reps[w].append((100 * r[w][0] / mpc[w], 100 * r[w][1] / mpc[w]))

    # 95% CI: the wider of the bootstrap over audited compactions (which allows for incidents clustering in some
    # compactions) and the exact Poisson interval for the incident count (which stays valid when every compaction has
    # the same count, e.g. none: the bootstrap then has zero width). Messages per cycle are taken as known.
    def interval(boot_ci, k, n, m):
        ex = [100 * x / n / m for x in poisson_ci(k)] if n and m else None
        if not boot_ci:
            return ex
        if not ex:
            return boot_ci
        return [min(boot_ci[0], ex[0]), max(boot_ci[1], ex[1])]

    out = {}
    for w in windows:
        rm, ra = point[w]
        m = mpc.get(w)
        n, ms, al = pick(counts, w)
        out[w] = dict(rate_mod_ser=rm, rate_all=ra, regime=('pooled' if used == 'pooled' else nearest(w)),
                      compactions=n, mod_ser=ms, all=al,
                      per100_mod_ser=100 * rm / m if m and rm is not None else None,
                      per100_all=100 * ra / m if m and ra is not None else None,
                      ci_mod_ser=interval(ci95([x[0] for x in reps[w]]), ms, n, m),
                      ci_all=interval(ci95([x[1] for x in reps[w]]), al, n, m))
    return dict(mode=used, fisher_p=p, windows=out,
                ci_method='wider of a bootstrap over audited compactions and an exact Poisson interval' if boot >= 20
                else 'exact Poisson interval for the incident count')


# ---------------------------------------------------------------- study B: the user's corrections

def bucket(k):
    if k is None:
        return 'n/a'
    for hi, name in CTX_BUCKETS:
        if hi is None or k < hi:
            return name


def classify_study(result, base):
    batches = [b for b in result.get('classify') or [] if isinstance(b, dict)]
    cls, chk = {}, {}
    for b in batches:
        for it in b.get('items') or []:
            if isinstance(it, dict) and it.get('id') is not None:
                cls[str(it['id'])] = it
        for x in b.get('checkF') or []:
            if isinstance(x, dict) and x.get('id') is not None:
                chk[str(x['id'])] = x
    if not cls:
        return None
    meta = {str(i.get('id')): i for i in read_json(os.path.join(base, 'B', 'items.json'), []) or [] if isinstance(i, dict)}

    def table(keyf):
        t = collections.OrderedDict()
        for iid, it in cls.items():
            m = meta.get(iid, {})
            k = keyf(m)
            r = t.setdefault(k, dict(n=0, corr=0, clear=0, F=0, F_confirmed=0, F_compaction_between=0))
            corr = as_int(it.get('corr')) or 0
            r['n'] += 1
            r['corr'] += corr > 0
            r['clear'] += corr == 2
            x = chk.get(iid)
            if corr > 0 and str(it.get('cause') or '') == 'F':
                r['F'] += 1
                if x and x.get('confirmed'):
                    r['F_confirmed'] += 1
                    r['F_compaction_between'] += bool(x.get('compaction_between'))
        return {k: dict(v, share=v['corr'] / v['n']) for k, v in t.items()}

    order = [name for _, name in CTX_BUCKETS] + ['n/a']
    by_ctx = table(lambda m: bucket(m.get('ctx_k')))
    by_ctx = collections.OrderedDict((k, by_ctx[k]) for k in order if k in by_ctx)
    by_reg = table(lambda m: norm_tag(m.get('cycle_regime')) or 'unknown')
    fisher = None
    big = sorted([g for g in by_reg if g != 'unknown'], key=lambda g: -by_reg[g]['n'])[:2]
    if len(big) == 2:
        a, b = by_reg[big[0]], by_reg[big[1]]
        fisher = dict(regimes=big, p=fisher_exact(a['corr'], a['n'] - a['corr'], b['corr'], b['n'] - b['corr']))
    corr = [it for it in cls.values() if (as_int(it.get('corr')) or 0) > 0]
    causes = collections.Counter(str(it.get('cause') or '?') for it in corr)
    f_items = [iid for iid, it in cls.items() if (as_int(it.get('corr')) or 0) > 0 and str(it.get('cause') or '') == 'F']
    conf = [iid for iid in f_items if chk.get(iid, {}).get('confirmed')]
    return dict(messages=len(cls), corrections=len(corr), share=len(corr) / len(cls), by_ctx=by_ctx,
                by_regime=by_reg, fisher=fisher, causes=dict(causes), F=len(f_items),
                F_checked=sum(1 for i in f_items if i in chk), F_confirmed=len(conf),
                F_compaction_between=sum(1 for i in conf if chk[i].get('compaction_between')),
                missing_meta=sum(1 for i in cls if i not in meta))


# ---------------------------------------------------------------- study C: controlled recall

def bad_qids(grades, qids):
    bad = set()
    for p in (grades or {}).get('key_problems') or []:
        for q in qids:
            if re.search(r'(?<![\w-])' + re.escape(q) + r'(?![\w-])', str(p)):
                bad.add(q)
    return bad


def norm_label(x):
    """Grader label as assigned ('X', 'Y', ...): ' x', 'Label X', '[Y]' -> 'X', 'Y'."""
    s = str(x if x is not None else '').strip().upper()
    s = re.sub(r'^(?:LABEL|ANSWER|CONDITION)\b\s*[:#=-]?\s*', '', s)
    return s.strip(' \t"\'`[](){}<>.:;,')


def graded(entry):
    """[(qid, condition, score, confidently_wrong)] of one cycle or calibration entry, keys flagged by the grader dropped.
    Also returns the number of grades dropped because their qid or label matched nothing."""
    g = entry.get('grades') if isinstance(entry.get('grades'), dict) else {}
    qs = {q.get('qid'): q for q in entry.get('questions') or [] if isinstance(q, dict) and q.get('qid')}
    labels = entry.get('labels') if isinstance(entry.get('labels'), dict) else {}
    labels = {norm_label(k): v for k, v in labels.items()}
    bad = bad_qids(g, list(qs))
    out, dropped = [], 0
    for gr in g.get('grades') or []:
        if not isinstance(gr, dict):
            dropped += 1
            continue
        q = gr.get('qid')
        if not (isinstance(q, str) and q in qs):
            q = str(q if q is not None else '').strip()
        if q in bad:
            continue
        cond = labels.get(norm_label(gr.get('label')))
        try:
            s = float(gr.get('score'))
        except (TypeError, ValueError):
            s = None
        if q not in qs or cond is None or s is None:
            dropped += 1
            continue
        out.append((q, cond, max(0.0, min(1.0, s)), bool(gr.get('confidently_wrong'))))
    return out, qs, bad, dropped


def controlled_study(result, base, windows, boot, rng):
    # calibration: real compactor vs simulated one on the same input
    cal, cal_dropped = [], 0
    for e in result.get('cal') or []:
        if not isinstance(e, dict):
            continue
        rows, _, _, dropped = graded(e)
        cal_dropped += dropped
        by_q = collections.defaultdict(dict)
        for q, cond, s, _ in rows:
            by_q[q][str(cond).upper()] = s
        pairs = [(v['REAL'], v['SIM']) for v in by_q.values() if 'REAL' in v and 'SIM' in v]
        if pairs:
            cal.append(dict(name=e.get('name'), pairs=pairs, real=mean(p[0] for p in pairs), sim=mean(p[1] for p in pairs)))

    def f_of(cases):
        """mean(REAL) / mean(SIM) over the calibration questions; None if the simulated answers all scored 0."""
        r = sum(p[0] for c in cases for p in c)
        s = sum(p[1] for c in cases for p in c)
        return r / s if s else None

    f = f_of([c['pairs'] for c in cal]) if cal else None
    failed = bool(cal) and f is None       # the simulated compactor's summaries answered nothing: no factor
    if f is not None:
        reps_f = [f_of([[rng.choice(c['pairs']) for _ in c['pairs']] for c in cal]) for _ in range(boot)]
        cal_ci = ci95([x for x in reps_f if x is not None])     # resamples with no SIM credit at all are left out
    else:
        cal_ci = None
    calib = dict(f=f if f is not None else 1.0, ci=cal_ci, calibrated=f is not None, failed=failed,
                 dropped_grades=cal_dropped,
                 cases=[dict(name=c['name'], real=c['real'], sim=c['sim'], n=len(c['pairs'])) for c in cal])
    cal_ok = cal if f is not None else []

    # questions: {(cycle, qid): {imp, cat, conds: {cond: (score, age, cw)}}}
    Q, cycles_info, key_bad, nq_total = {}, [], 0, 0
    for e in result.get('cycles') or []:
        if not isinstance(e, dict) or not e.get('name'):
            continue
        name = e['name']
        meta = read_json(os.path.join(base, 'C', name, 'meta.json'), {}) or {}
        mw = {norm_tag(k): v for k, v in (meta.get('windows') or {}).items()}
        real_reg = norm_tag(meta.get('real_regime')) if meta.get('real_regime') else None
        rows, qs, bad, dropped = graded(e)
        key_bad += len(bad)
        nq_total += len(qs)
        conds, warned = set(), set()
        for q, cond, s, cw in rows:
            cond = norm_tag(cond)
            last = as_int(qs[q].get('last_idx'))
            if cond == 'REAL':
                key, age = f'REAL:{real_reg}' if real_reg else 'REAL', 0
            else:
                if cond not in mw and cond not in warned:
                    warned.add(cond)
                    print(f'analyze.py: warning: C/{name}/meta.json has no points for {cond}; its scores are not '
                          f'calibration-adjusted', file=sys.stderr)
                pts = (mw.get(cond) or {}).get('points') or []
                key = cond
                age = sum(1 for p in pts if last is None or (as_int(p) or 0) > last)
            conds.add(key)
            d = Q.setdefault((name, q), dict(cycle=name, qid=q, imp=as_int(qs[q].get('importance')),
                                             cat=qs[q].get('category'), conds={}))
            d['conds'][key] = (s, age, cw)
        cycles_info.append(dict(name=name, conditions=sorted(conds), questions=len({k[1] for k in Q if k[0] == name}),
                                failed_windows=e.get('failed_windows') or [], real_regime=real_reg, error=e.get('error'),
                                dropped_grades=dropped))
    if not Q:
        return dict(calibration=calib, questions=0, cycles=cycles_info) if cal else None
    f = calib['f']

    conds = sorted({c for d in Q.values() for c in d['conds']}, key=lambda c: (c.startswith('REAL'), tokens(c) or 0, c))

    def adj(s, age, ff, cond):
        return s if cond.startswith('REAL') else min(1.0, s * ff ** age)

    def stats(qlist, ff):
        out = {}
        for c in conds:
            v = [(d['conds'][c], d['imp']) for d in qlist if c in d['conds']]
            out[('raw', c)] = mean(x[0][0] for x in v)
            out[('adj', c)] = mean(adj(x[0][0], x[0][1], ff, c) for x in v)
            out[('adj3', c)] = mean(adj(x[0][0], x[0][1], ff, c) for x in v if x[1] == 3)
        for a in conds:
            for b in conds:
                if a >= b:
                    continue
                both = [d for d in qlist if a in d['conds'] and b in d['conds']]
                for tag, sel in (('d_adj', both), ('d_adj3', [d for d in both if d['imp'] == 3])):
                    out[(tag, a, b)] = mean(adj(*d['conds'][b][:2], ff, b) - adj(*d['conds'][a][:2], ff, a) for d in sel)
        return out

    point = stats(list(Q.values()), f)
    by_cycle = collections.defaultdict(list)
    for d in Q.values():
        by_cycle[d['cycle']].append(d)
    reps = collections.defaultdict(list)
    for _ in range(boot):
        ff = f_of([[rng.choice(c['pairs']) for _ in c['pairs']] for c in cal_ok]) if cal_ok else 1.0
        if ff is None:          # this resample of the calibration has no SIM credit at all: skip it
            continue
        smp = [rng.choice(v) for v in by_cycle.values() for _ in v]
        for k, v in stats(smp, ff).items():
            if v is not None:
                reps[k].append(v)
    per_cond = {}
    for c in conds:
        ds = [d for d in Q.values() if c in d['conds']]
        ages = collections.defaultdict(list)
        for d in ds:
            ages[d['conds'][c][1]].append(d['conds'][c][0])
        per_cond[c] = dict(n=len(ds), n_imp3=sum(1 for d in ds if d['imp'] == 3),
                           raw=point[('raw', c)], adj=point[('adj', c)], adj_imp3=point[('adj3', c)],
                           ci_adj=ci95(reps[('adj', c)]), ci_adj_imp3=ci95(reps[('adj3', c)]),
                           conf_wrong=mean(1.0 if d['conds'][c][2] else 0.0 for d in ds),
                           by_age={str(a): dict(mean=mean(v), n=len(v)) for a, v in sorted(ages.items())})
    diffs = {}
    for k, v in point.items():
        if k[0] in ('d_adj', 'd_adj3') and v is not None:
            a, b = k[1], k[2]
            n = sum(1 for d in Q.values() if a in d['conds'] and b in d['conds'] and (k[0] == 'd_adj' or d['imp'] == 3))
            diffs[f'{k[0]}:{b}-{a}'] = dict(a=a, b=b, imp3=k[0] == 'd_adj3', mean=v, ci=ci95(reps[k]), n=n)
    return dict(calibration=calib, questions=len(Q), questions_written=nq_total, keys_flagged=key_bad, cycles=cycles_info,
                conditions=per_cond, diffs=diffs)


def diff_between(ctrl, a, b, imp3=True):
    """Paired difference b - a (adjusted retention), whichever order it was stored in."""
    if not ctrl or 'diffs' not in ctrl:
        return None
    pre = 'd_adj3' if imp3 else 'd_adj'
    d = ctrl['diffs'].get(f'{pre}:{b}-{a}')
    if d:
        return d
    d = ctrl['diffs'].get(f'{pre}:{a}-{b}')
    if d:
        return dict(d, a=b, b=a, mean=-d['mean'], ci=[-d['ci'][1], -d['ci'][0]] if d['ci'] else None)
    return None


# ---------------------------------------------------------------- recommendation

def window_cond(ctrl, w):
    """Controlled-study condition that stands for window w (1M: the real summaries of 1M cycles)."""
    if not ctrl or 'conditions' not in ctrl:
        return None
    if w in ctrl['conditions']:
        return w
    if w == '1M' and 'REAL:1M' in ctrl['conditions']:
        return 'REAL:1M'
    return None


def recommend(cands, proj, cost, ctrl, exchange):
    have_a = bool(ctrl and ctrl.get('conditions'))
    have_b = bool(proj and cost)
    if not have_a and not have_b:
        return dict(window=DEFAULT_RECOMMENDATION, default=True, steps=[], rests_on=[],
                    reason='Too little data for the rule (no incident projection with costs, no controlled recall); '
                           'the default 400k is recommended')
    cur, steps = cands[0], []
    for i in range(1, len(cands)):
        nxt = cands[i]
        st = dict(frm=cur, to=nxt, a=None, b=None)
        # (a) key-fact retention clearly better at the next window that has data than at the current one or, if the
        # current window has no data, the nearest smaller one that has. Carrying the lower end forward means that a
        # step justified by (a) keeps going until it reaches the window whose data were compared, whether or not
        # windows without data lie in between.
        ca = next((window_cond(ctrl, w) for w in reversed(cands[:i]) if window_cond(ctrl, w)), None)
        nb = next((window_cond(ctrl, w) for w in cands[i:] if window_cond(ctrl, w)), None)
        if ca and nb:
            d = diff_between(ctrl, ca, nb, imp3=True)
            if d:
                # needs a CI: with --boot 0 (or too few replications) rule (a) cannot fire
                st['a'] = dict(compared=[ca, nb], diff=d['mean'], ci=d['ci'], n=d['n'],
                               ok=bool(d['mean'] > 0.05 and d['ci'] and d['ci'][0] > 0))
        # (b) fewer moderate+serious incidents per 100 messages, per +10% cost
        if proj and cost:
            p0, p1 = proj['windows'].get(cur, {}).get('per100_mod_ser'), proj['windows'].get(nxt, {}).get('per100_mod_ser')
            c0, c1 = cost['rel_cost'].get(cur), cost['rel_cost'].get(nxt)
            if None not in (p0, p1, c0, c1) and c0 > 0:
                dinc, dcost = p0 - p1, 100 * (c1 / c0 - 1)
                ratio = dinc / (dcost / 10) if dcost > 0 else None
                st['b'] = dict(d_incidents=dinc, d_cost_pct=dcost, per10pct=ratio,
                               ok=bool(ratio >= exchange) if ratio is not None else dinc >= 0)
        steps.append(st)
        if (st['a'] or {}).get('ok') or (st['b'] or {}).get('ok'):
            cur = nxt
            continue
        break
    evaluated = any((s['a'] and s['a']['ci']) or s['b'] for s in steps)
    if not evaluated:
        return dict(window=DEFAULT_RECOMMENDATION, default=True, steps=steps, rests_on=[],
                    reason='No step of the rule could be evaluated with the data available; the default 400k is recommended')
    rests = []
    if any(s['b'] for s in steps):
        rests.append('projected incidents and cost')
    if any(s['a'] and s['a']['ci'] for s in steps):
        rests.append('controlled key-fact retention')
    return dict(window=cur, default=False, steps=steps, rests_on=rests)


# ---------------------------------------------------------------- report

def regime_order(regs):
    return sorted(regs, key=lambda g: (tokens(g) is None, tokens(g) or 0, g))


def report(R, args):
    L = []
    rec = R['recommendation']
    proj, cost, audit, cls, ctrl = R['projection'], R['cost'], R['audit'], R['classify'], R['controlled']
    L.append('# Smart analysis: which auto-compact window\n')
    how = ('the default of 1M-context models: `/autocompact auto`, or no setting' if rec['window'] == '1M'
           else f"set it with `/autocompact {rec['window']}`")
    L.append(f"**Recommended window: {rec['window']}** ({how}).\n")
    missing = [n for n, v in (('incident audit', audit), ('cost simulation', cost), ('controlled recall', ctrl and ctrl.get('conditions')),
                              ('classification of corrections', cls)) if not v]
    if rec.get('default'):
        L.append(f"{rec['reason']}." + (f" Missing parts: {', '.join(missing)}." if missing else '') + '\n')
    else:
        why = []
        for s in rec['steps']:
            parts = []
            if s['b']:
                b = s['b']
                parts.append(f"{'removes' if b['d_incidents'] >= 0 else 'adds'} {abs(b['d_incidents']):.2f} moderate or serious "
                             f"problems per 100 assistant messages for {b['d_cost_pct']:+.0f}% cost"
                             + (f" ({b['per10pct']:.2f} per +10%, threshold {args.exchange:.2f})" if b['per10pct'] is not None else ''))
            if s['a']:
                a = s['a']
                parts.append(f"changes key-fact retention by {100 * a['diff']:+.0f} pp{fci([100 * x for x in a['ci']] if a['ci'] else None, 0)}"
                             + ('' if a['ci'] else ' (no confidence interval, so (a) cannot apply)')
                             + (f" (measured at {a['compared'][0].replace('REAL:', 'real ')} vs {a['compared'][1].replace('REAL:', 'real ')})"
                                if a['compared'] != [window_cond(ctrl, s['frm']), window_cond(ctrl, s['to'])] else ''))
            moved = (s['a'] or {}).get('ok') or (s['b'] or {}).get('ok')
            why.append(f"- {s['frm']} → {s['to']}: " + ('; '.join(parts) or 'no data') + ('. **Step up.**' if moved else '. **Stop.**'))
        L.append('\n'.join(why) + '\n')
        L.append(f"The recommendation rests on: {', '.join(rec['rests_on'])}.")
        if missing:
            L[-1] += f" Missing parts: {', '.join(missing)}."
        L.append('')
        if R.get('sensitivity'):
            sv = R['sensitivity']
            how = 'the rate of the nearest audited window' if sv['mode'] == 'nearest' else 'the rate pooled over all audited windows'
            vals = ', '.join(f"{w} {f2(v)}" for w, v in sv['per100_mod_ser'].items() if v is not None)
            L.append(f"Check: with {how} instead, the projection is {vals} moderate or serious problems per 100 assistant "
                     f"messages, and the rule gives **{sv['window']}**.\n")

    # summary table per window
    L.append('## By window\n')
    L.append('| window | cost vs 1M | assistant msgs per compaction cycle | moderate+serious problems per 100 msgs (95% CI) '
             '| all problems per 100 msgs | key-fact retention, importance 3 (adjusted) |')
    L.append('|---|---|---|---|---|---|')
    for w in R['candidates']:
        c = '–'
        if cost and w in cost['saving']:
            c = 'baseline' if w == '1M' else f"−{100 * cost['saving'][w]:.0f}%"
        m = R['msgs_per_cycle'].get(w)
        pw = (proj or {}).get('windows', {}).get(w, {})
        cc = window_cond(ctrl, w)
        ret = '–'
        if cc:
            k = ctrl['conditions'][cc]
            ret = f"{f2(k['adj_imp3'])}{fci(k['ci_adj_imp3'])}" + (' real summaries' if cc.startswith('REAL') else '')
        L.append(f"| {w}{' (default)' if w == '1M' else ''} | {c} | {f2(m, 0) if m else '–'} | "
                 f"{f2(pw.get('per100_mod_ser'))}{fci(pw.get('ci_mod_ser'))} | {f2(pw.get('per100_all'))} | {ret} |")
    if proj:
        L.append(f"\nIncident rates per compaction are {'pooled over all audited compactions' if proj['mode'] == 'pooled' else 'taken from the nearest audited window'}"
                 + (f" (difference between regimes: Fisher p = {proj['fisher_p']:.2f})" if proj['fisher_p'] is not None else '')
                 + '; per 100 messages = 100 × rate per compaction / messages per cycle. '
                 + f"95% CI: {proj.get('ci_method', 'bootstrap')}.")
    L.append('An assistant message is one assistant line of the transcript with text or a tool call (Claude Code writes one '
             'line per content block); messages per cycle come from the context growth per message in your sessions.')
    src = {s for s in R['msgs_per_cycle_source'].values() if s != 'cost_sim'}
    if src:
        L.append(f"Messages per cycle partly estimated ({'; '.join(sorted(src))}).")
    L.append('')

    L.append('## Rule\n')
    L.append(f"Start at the smallest window and move one step up while either (a) importance-3 key-fact retention (adjusted) at "
             f"the next window with data is higher than at the current window (or, if the current window has no data, the "
             f"nearest smaller one with data) by more than 5 percentage points with a 95% CI above 0, or (b) the step removes "
             f"at least {args.exchange:.2f} projected moderate or serious problems per 100 assistant messages per +10% of cost "
             f"(cost increase relative to the current window's cost). Stop otherwise. Without enough data the default is 400k. "
             + ("Incident rates are pooled over the audited windows unless a Fisher exact test on compactions with at least "
                "one moderate or serious problem finds a difference between the two largest audited windows (p < 0.05); "
                "then each window takes the rate of the nearest audited window.\n" if args.rate == 'auto' else
                f"Incident rates: {'pooled over the audited windows' if args.rate == 'pooled' else 'from the nearest audited window'} "
                "(--rate).\n"))

    if audit:
        L.append('## A. Audit of real compactions\n')
        L.append('| window when it compacted | compactions | audited assistant msgs | minor | moderate | serious | '
                 'moderate+serious per compaction | all per 100 audited msgs | summary claims wrong | coverage of the cycle |')
        L.append('|---|---|---|---|---|---|---|---|---|---|')
        for g in regime_order(audit['regimes']):
            r = audit['regimes'][g]
            L.append(f"| {g} | {r['compactions']} | {r['assistant_msgs']} | {r['minor']} | {r['moderate']} | {r['serious']} | "
                     f"{r['mod_ser']}/{r['compactions']} = {r['per_compaction_mod_ser']:.2f} | {f2(r['per100_audited_all'])} | "
                     f"{pct(r['summary_wrong_share'])} | {pct(r['coverage'])} |")
        fz = audit['fisher']
        if fz:
            L.append(f"\nModerate+serious, {fz['regimes'][0]} vs {fz['regimes'][1]}: Fisher exact p = {fz['p']:.2f} "
                     f"({fz['basis']}); exact rate test on incidents per compaction p = {fz['rate_test_p']:.2f}.")
        ss = audit.get('same_span')
        if ss:
            parts = ', '.join(f"{g} {r['mod_ser']}/{r['compactions']}" for g, r in
                              ((g, ss['regimes'][g]) for g in regime_order(ss['regimes'])))
            fs = ss['fisher']
            L.append(f"Same span: counting only problems within the first {ss['n_msgs']} assistant messages after each "
                     f"compaction (the shortest typical audited span), moderate+serious per compaction: {parts}"
                     + (f"; Fisher exact p = {fs['p']:.2f}" if fs else '') + '.')
        if audit.get('excluded_unverified'):
            gs = audit.get('excluded_groups') or []
            L.append(f"Not verified: the verifier gave no verdict for {audit['unverified']} reported incident(s) in audit "
                     f"group{'s' if len(gs) != 1 else ''} {', '.join(str(g) for g in gs)}; "
                     f"{'its' if len(gs) == 1 else 'their'} compactions ({', '.join(audit['excluded_unverified'])}) are left "
                     f"out of all rates and counts above, so that the rest stays an unselected sample.")
        if audit['not_audited']:
            L.append(f"Not audited (agent failed): {', '.join(audit['not_audited'])}.")
        types = collections.Counter(str(x['type'] or '?') for x in audit['incidents'])
        if types:
            L.append('Types: ' + ', '.join(f"{t} {TYPE_NAMES.get(t, '')} {n}" for t, n in sorted(types.items())) + '.')
        causes = collections.Counter(x.get('cause', '?') for x in audit['incidents'])
        if causes:
            L.append('Causes: ' + ', '.join(f'{c} {n}' for c, n in sorted(causes.items())) + '.')
        L.append({'compaction': 'Counted: confirmed incidents where the information was lost in the summary, or was in the '
                                'summary but handled correctly before the compaction and wrongly after it (diluted).',
                  'lost': 'Counted: only confirmed incidents where the information was missing from or distorted in the summary.',
                  'all-real': 'Counted: every confirmed incident after a compaction, whatever its cause.'}[args.count])
        ms = [x for x in audit['incidents'] if x['severity'] != 'minor']
        if ms:
            L.append('\nModerate and serious incidents:\n')
            for x in ms:
                L.append(f"- {x['blind_id']} ({x['regime']}) {x['type']} {x['severity']}, {x.get('cause', '?')}: {x['explanation'][:200]}")
        L.append('')

    if cls:
        L.append("## B. The user's corrections by context size\n")
        L.append('| context when the user wrote | messages | corrections | share | clear | forgot earlier (F) | F confirmed | of which compaction in between |')
        L.append('|---|---|---|---|---|---|---|---|')
        for k, t in cls['by_ctx'].items():
            L.append(f"| {k} | {t['n']} | {t['corr']} | {t['share']:.3f} | {t['clear']} | {t['F']} | {t['F_confirmed']} | {t['F_compaction_between']} |")
        L.append('\n| cycle ended at window | messages | corrections | share |')
        L.append('|---|---|---|---|')
        for k in regime_order(cls['by_regime']):
            t = cls['by_regime'][k]
            L.append(f"| {k} | {t['n']} | {t['corr']} | {t['share']:.3f} |")
        if cls['fisher']:
            L.append(f"\n{cls['fisher']['regimes'][0]} vs {cls['fisher']['regimes'][1]}: Fisher exact p = {cls['fisher']['p']:.2f}.")
        L.append(f"Causes of corrections: {', '.join(f'{k} {v}' for k, v in sorted(cls['causes'].items()))} "
                 f"(F forgot earlier, I misread the last message, R reasoning, T tools, O other). "
                 f"F checked {cls['F_checked']} of {cls['F']}, confirmed {cls['F_confirmed']}, "
                 f"with a compaction in between {cls['F_compaction_between']}.\n")

    if ctrl:
        cal = ctrl['calibration']
        L.append('## C. Controlled recall\n')
        if cal['calibrated']:
            L.append(f"Calibration: on the same input the real compactor kept {f2(mean(c['real'] for c in cal['cases']))} and the simulated "
                     f"one {f2(mean(c['sim'] for c in cal['cases']))} (mean score, {len(cal['cases'])} case(s)); factor f = {cal['f']:.3f}"
                     f"{fci(cal['ci'], 3)}. A simulated score is multiplied by f for every simulated compaction the fact went through.\n")
        elif cal.get('failed'):
            L.append('Calibration **failed**: answers from the simulated compactor\'s summaries scored 0 on every calibration '
                     'question, so the factor f is undefined (the simulated compaction probably went wrong). Scores of simulated '
                     'windows are **uncalibrated**.\n')
        else:
            L.append('No calibration: scores of simulated windows are **uncalibrated** (simulated summaries usually keep more than '
                     'real ones, so simulated windows look better than they are).\n')
        if ctrl.get('conditions'):
            L.append('| condition | questions | raw score | adjusted | adjusted, importance 3 | confidently wrong |')
            L.append('|---|---|---|---|---|---|')
            for c, k in ctrl['conditions'].items():
                name = c.replace('REAL:', 'real summary at ').replace('REAL', 'real summary')
                L.append(f"| {name} | {k['n']} | {f2(k['raw'])} | {f2(k['adj'])}{fci(k['ci_adj'])} | {f2(k['adj_imp3'])}{fci(k['ci_adj_imp3'])} | {pct(k['conf_wrong'])} |")
            sims = [c for c in ctrl['conditions'] if not c.startswith('REAL')]
            reals = [c for c in ctrl['conditions'] if c.startswith('REAL')]
            lines = []
            for s in sims:
                for r in reals:
                    d = diff_between(ctrl, r, s, imp3=False)
                    if d:
                        lines.append(f"{s} − {r.replace('REAL:', 'real ')}: {100 * d['mean']:+.1f} pp{fci([100 * x for x in d['ci']] if d['ci'] else None, 1)}, n {d['n']}")
            if len(sims) >= 2:
                d = diff_between(ctrl, sims[0], sims[-1], imp3=False)
                if d:
                    lines.append(f"{sims[-1]} − {sims[0]}: {100 * d['mean']:+.1f} pp{fci([100 * x for x in d['ci']] if d['ci'] else None, 1)}, n {d['n']}")
            if lines:
                L.append('\nPaired differences (adjusted, all questions): ' + '; '.join(lines) + '.')
            L.append(f"\n{ctrl['questions']} questions in {len(ctrl['cycles'])} cycle(s); {ctrl['keys_flagged']} flagged by the grader as "
                     f"doubtful keys and left out.")
            fails = [f"{c['name']}: {', '.join(c['failed_windows'])}" for c in ctrl['cycles'] if c['failed_windows']]
            if fails:
                L.append(f"Windows dropped because a simulated compaction failed: {'; '.join(fails)}.")
        drops = [f"{c['name']} {c['dropped_grades']}" for c in ctrl.get('cycles') or [] if c.get('dropped_grades')]
        if cal.get('dropped_grades'):
            drops.append(f"calibration {cal['dropped_grades']}")
        if drops:
            L.append(f"Grades left out because their question id or blind label matched nothing: {', '.join(drops)}.")
        L.append('')

    L.append('## Limitations\n')
    lim = []
    sizes = []
    if audit:
        sizes.append(f"{audit['audited']} audited compactions"
                     + (f" ({len(audit['excluded_unverified'])} more left out as not verified)" if audit.get('excluded_unverified') else ''))
    if ctrl and ctrl.get('questions'):
        sizes.append(f"{ctrl['questions']} recall questions in {len([c for c in ctrl['cycles'] if c['questions']])} cycle(s)")
    if ctrl and ctrl['calibration']['cases']:
        sizes.append(f"{len(ctrl['calibration']['cases'])} calibration case(s)")
    if cls:
        sizes.append(f"{cls['messages']} classified user messages")
    if sizes:
        lim.append(f"Small samples: {', '.join(sizes)}. Differences of a few percentage points are noise; see the "
                   'confidence intervals.')
    else:
        lim.append('No study produced data; the recommendation is the default.')
    if audit or cls:
        lim.append('The tests treat compactions and messages as independent, but they cluster by session (one long session '
                   'contributes many), so the p-values are optimistic.')
    if proj:
        lim.append('Windows other than the ones the user actually ran are projections: incidents per compaction are assumed '
                   'not to depend on the window unless the audit shows a clear difference (see the check above).')
        cov = {g: r['coverage'] for g, r in audit['regimes'].items() if r['coverage'] is not None}
        lim.append('Incidents are counted only in the first ~150 transcript records after each compaction'
                   + (' (coverage of the cycle ' + ', '.join(f"{g} {pct(cov[g])}" for g in regime_order(cov)) + ')' if cov else '')
                   + '; the projection assumes that problems caused by a compaction show up within that span. If later '
                   'ones exist, large windows are undercounted most, which favours small windows'
                   + (' (see the same-span check in A).' if audit.get('same_span') else '.'))
        lim.append('Confidence intervals reflect the sampling of audited compactions and questions only, not the uncertainty '
                   'in messages per cycle or the choice between pooled and nearest-window rates.')
    if ctrl and ctrl.get('diffs') and args.boot < 20:
        lim.append(f"Rule (a) needs bootstrap confidence intervals, which were not computed (--boot {args.boot}; at least 20 "
                   'needed), so only rule (b) was applied.')
    if ctrl and not ctrl['calibration']['calibrated']:
        lim.append('The controlled study is uncalibrated' + (' (calibration failed).' if ctrl['calibration'].get('failed') else '.'))
    if R.get('prompt_source') == 'fallback':
        lim.append('The compaction prompt could not be read from the local Claude Code install; simulated compactions used a '
                   'generic stand-in prompt, so the controlled study is less exact.')
    lim.append('A single snapshot of the user\'s recent sessions; work patterns change.')
    lim.append('Severity and correctness are judgements by agents, checked by a second agent but not by a person.')
    if cost and cost.get('smaller_window_compactions'):
        lim.append(f"{cost['smaller_window_compactions']} real automatic compactions already ran at a window below 1M. The cost "
                   'replay cannot grow the context past them, so the savings against 1M are understated; cost_sim.py --until '
                   '<time of the change> prices only the period before.')
    if cost and cost.get('prices_note'):
        lim.append(f"Costs: {cost['prices_note']}")
    L.extend(f'- {x}' for x in lim)
    if R.get('workflow_log'):
        L.append('\nWorkflow notes:\n')
        L.extend(f'- {str(x)[:200]}' for x in R['workflow_log'][:12])
    L.append(f"\nSource: {R['source']}. Numbers: results.json.")
    return '\n'.join(L) + '\n'


def figure(R, path):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception:
        return False
    ws = R['candidates']
    ctrl, proj = R['controlled'], R['projection']
    fig, ax = plt.subplots(1, 2, figsize=(9.6, 3.6))
    x = list(range(len(ws)))
    ret, err, col = [], [], []
    for w in ws:
        c = window_cond(ctrl, w)
        k = ctrl['conditions'][c] if c else None
        v = k['adj_imp3'] if k else None
        ret.append(v if v is not None else float('nan'))
        ci = k['ci_adj_imp3'] if k else None
        err.append([v - ci[0], ci[1] - v] if ci and v is not None else [0, 0])
        col.append('0.45' if c and c.startswith('REAL') else 'tab:blue')
    ax[0].bar(x, ret, color=col, yerr=list(zip(*err)), capsize=4)
    for i, v in enumerate(ret):
        if v != v:
            ax[0].text(i, 0.02, 'no data', ha='center', fontsize=8, color='0.4')
    if any(c == '0.45' for c in col):
        ax[0].text(0.99, 0.98, 'grey: real summaries', transform=ax[0].transAxes, ha='right', va='top', fontsize=7.5, color='0.3')
    ax[0].set_xticks(x)
    ax[0].set_xticklabels(ws)
    ax[0].set_ylim(0, 1.05)
    ax[0].set_ylabel('share of key facts kept')
    ax[0].set_title('(a) Key-fact retention (importance 3, adjusted)', fontsize=10)
    ax[0].grid(alpha=.3, axis='y')
    vals, err = [], []
    for w in ws:
        p = (proj or {}).get('windows', {}).get(w, {})
        v, ci = p.get('per100_mod_ser'), p.get('ci_mod_ser')
        vals.append(v if v is not None else float('nan'))
        err.append([v - ci[0], ci[1] - v] if ci and v is not None else [0, 0])
    ax[1].bar(x, vals, color=['tab:green' if w == R['recommendation']['window'] else 'tab:gray' for w in ws],
              yerr=list(zip(*err)), capsize=4)
    for i, v in enumerate(vals):
        if v != v:
            ax[1].text(i, 0.0, 'no data', ha='center', va='bottom', fontsize=8, color='0.4')
    ax[1].set_xticks(x)
    ax[1].set_xticklabels(ws)
    ax[1].set_ylabel('per 100 assistant messages')
    ax[1].set_title(f"(b) Moderate + serious problems (green: recommended)", fontsize=10)
    ax[1].grid(alpha=.3, axis='y')
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=110)
    plt.close(fig)
    write_private(path, buf.getvalue())
    return True


# ---------------------------------------------------------------- main

def clean(o):
    """JSON-safe copy (tuple keys and NaN removed)."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [clean(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o


def main():
    ap = argparse.ArgumentParser(description='Combine the smart-analysis studies (audit, corrections, controlled recall, cost) '
                                             'into a recommended auto-compact window. Writes report.md, results.json and fig.png in --base.')
    ap.add_argument('--base', required=True, help='work folder OUT (holds args.json, A/, B/, C/, cost.json)')
    ap.add_argument('--run', required=True, help='workflow run id (wf_...), a wf_<id>.json file, or a result.json file')
    ap.add_argument('--cost', help='cost.json from cost_sim.py (default: OUT/cost.json if present)')
    ap.add_argument('--windows', help=f'candidate windows (default: those of inventory.json, else {DEFAULT_WINDOWS}); 1M is '
                                      'always added')
    ap.add_argument('--exchange', type=float, default=0.10,
                    help='step up if it removes at least this many moderate+serious problems per 100 assistant messages '
                         'per +10%% cost (default 0.10)')
    ap.add_argument('--rate', choices=['auto', 'pooled', 'nearest'], default='auto',
                    help='incidents per compaction for the projection: pooled over regimes, from the nearest audited regime, '
                         'or auto = pooled unless the two largest regimes differ (Fisher exact test on compactions with at '
                         'least one moderate or serious incident, p < 0.05) (default auto)')
    ap.add_argument('--buffer', type=int, help='tokens below the window at which auto-compaction fires (default: inventory or 33000)')
    ap.add_argument('--post', type=int, help='context right after a compaction (default: inventory or 97000)')
    ap.add_argument('--seed', type=int, default=1, help='random seed for the bootstrap (default 1)')
    ap.add_argument('--boot', type=int, default=2000, help='bootstrap replications (default 2000; 0 = no confidence intervals)')
    ap.add_argument('--projects', default=os.path.join(os.path.expanduser('~'), '.claude', 'projects'),
                    help='where to look up a bare run id (default ~/.claude/projects)')
    ap.add_argument('--no-fig', action='store_true', help='do not draw fig.png')
    ap.add_argument('--count', choices=['compaction', 'lost', 'all-real'], default='compaction',
                    help='which confirmed incidents count: compaction = information lost in the summary or diluted by '
                         'it (default); lost = only lost; all-real = every confirmed incident after a compaction')
    a = ap.parse_args()

    base = os.path.abspath(os.path.expanduser(a.base))
    if not os.path.isdir(base):
        die(f'work folder not found: {base}')
    inv = read_json(os.path.join(base, 'inventory.json'), {}) or {}
    wspec = a.windows or ','.join(str(w) for w in inv.get('windows') or []) or DEFAULT_WINDOWS
    wins = []
    for w in wspec.split(','):
        if not w.strip():
            continue
        n = tokens(w)
        if not n or n < 50_000:
            die(f'bad window "{w}" (use e.g. 300k,400k,500k)')
        wins.append(wtag(n))
    cands = sorted(set(wins) | {'1M'}, key=tokens)
    buffer = a.buffer if a.buffer is not None else as_int(inv.get('buffer')) or BUFFER
    post = a.post if a.post is not None else as_int(inv.get('post_median')) or POST
    rng = random.Random(a.seed)

    result, source = load_run(os.path.expanduser(a.run), os.path.expanduser(a.projects), base)
    args_json = read_json(os.path.join(base, 'args.json'), {}) or {}
    cost_path = a.cost or (os.path.join(base, 'cost.json') if os.path.isfile(os.path.join(base, 'cost.json')) else None)
    if a.cost and not os.path.isfile(a.cost):
        print(f'analyze.py: warning: {a.cost} not found; costs left out', file=sys.stderr)

    audit = audit_study(result, base, a.count)
    cost = load_cost(cost_path, cands)
    mpc, mpc_src = msgs_per_cycle(cands, cost, audit, buffer, post)
    proj = projection(audit, cands, mpc, a.rate, a.boot, rng) if audit else None
    cls = classify_study(result, base)
    ctrl = controlled_study(result, base, cands, a.boot, rng)
    rec = recommend(cands, proj, cost, ctrl, a.exchange)

    R = dict(version=1, source=source, candidates=cands, buffer=buffer, post=post, exchange=a.exchange, seed=a.seed, boot=a.boot,
             prompt_source=result.get('prompt_source') or args_json.get('prompt_source'),
             msgs_per_cycle=mpc, msgs_per_cycle_source=mpc_src, cost=cost, audit=audit, projection=proj, classify=cls,
             controlled=ctrl, recommendation=rec, workflow_log=[x for x in result.get('log') or [] if x])
    # robustness: the other way of projecting incident rates
    if proj and audit and len([g for g in audit['regimes'] if tokens(g)]) >= 2:
        alt_mode = 'nearest' if proj['mode'] == 'pooled' else 'pooled'
        alt = projection(audit, cands, mpc, alt_mode, 0, random.Random(a.seed))
        alt_rec = recommend(cands, alt, cost, ctrl, a.exchange)
        R['sensitivity'] = dict(mode=alt_mode, window=alt_rec['window'],
                                per100_mod_ser={w: v['per100_mod_ser'] for w, v in alt['windows'].items()})

    write_private(os.path.join(base, 'report.md'), report(R, a))
    write_private(os.path.join(base, 'results.json'), json.dumps(clean(R), indent=1, ensure_ascii=False))
    fig = False if a.no_fig else figure(R, os.path.join(base, 'fig.png'))

    print(f"Recommended window: {rec['window']}" + (' (default; too little data)' if rec.get('default') else
                                                   f" (based on {', '.join(rec['rests_on'])})"))
    print(f"{'window':>8} {'cost vs 1M':>10} {'msgs/cycle':>10} {'mod+ser/100':>11} {'retention3':>10}")
    for w in cands:
        sv = cost['saving'].get(w) if cost else None
        p = (proj or {}).get('windows', {}).get(w, {}).get('per100_mod_ser')
        c = window_cond(ctrl, w)
        r = ctrl['conditions'][c]['adj_imp3'] if c else None
        print(f"{w:>8} {('-' + pct(sv)) if sv else ('0%' if sv == 0 else '–'):>10} {f2(mpc.get(w), 0):>10} {f2(p):>11} {f2(r):>10}")
    print(f"Wrote {os.path.join(base, 'report.md')}, results.json" + (', fig.png' if fig else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())

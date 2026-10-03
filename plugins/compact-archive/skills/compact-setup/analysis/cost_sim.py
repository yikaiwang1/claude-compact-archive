#!/usr/bin/env python3
"""Cost of each session at several auto-compact windows, at API list prices.

Replays every API call of the sessions listed in inventory.json and simulates the context each session would have
had under window W: compaction fires when the context passes W - buffer, costs one read of the context plus a ~10k
token summary, and leaves the context at the post-compaction size plus the next call's growth. The 1M baseline is
the default window of 1M-context models. Also estimates assistant messages per compaction cycle, which analyze.py
needs to turn problems per compaction into problems per 100 messages: messages per token of context growth (pooled over
the sessions) times the growth one cycle allows (window - buffer - post). Counting messages between the replay's
simulated compactions instead is biased when part of the data already ran at a smaller window (the replay cannot grow
the context past a real compaction), so that count is only kept as msgs_per_cycle_pooled. An assistant message is one
assistant line of the transcript with text or a tool call (Claude Code writes one line per content block).

Writes OUT/cost.json and prints a compact table. Reads transcripts only.
"""
import argparse
import collections
import json
import os
import sys

sys.dont_write_bytecode = True     # no __pycache__ in the plugin folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inventory as inv  # noqa: E402

SUMMARY_OUT = 10_000
RESET_TOL = 20_000
LONG_CTX = 600_000
BASELINE = 1_000_000

# $ per million tokens: input, 5-minute cache write, 1-hour cache write, cache read, output (list prices, 2026-10).
# Model ids are matched by prefix after "claude-"; more specific prefixes first.
_OPUS5 = (5, 6.25, 10, 0.50, 25)
_OPUS4 = (15, 18.75, 30, 1.5, 75)
_SONNET5 = (2, 2.5, 4, 0.2, 10)
_FABLE = (10, 12.5, 20, 0.25, 50)
PRICES = [
    ('opus-5-5', (4, 5, 8, 0.20, 20)),
    ('opus-5', _OPUS5), ('opus-4-8', _OPUS5), ('opus-4-7', _OPUS5), ('opus-4-6', _OPUS5), ('opus-4-5', _OPUS5),
    ('opus-4-1', _OPUS4), ('opus-4', _OPUS4),
    ('sonnet-5-5', _SONNET5), ('sonnet-5', _SONNET5),
    ('sonnet-4', (3, 3.75, 6, 0.3, 15)),
    ('haiku-4-5', (1, 1.25, 2, 0.1, 5)),
    ('fable-5-1', _FABLE), ('mythos-5-1', _FABLE),
]
FAMILY = [('fable', 'fable-5-1'), ('mythos', 'mythos-5-1')]
FALLBACK = 'opus-5-5'


def _norm(model):
    m = (model or '').lower().split('[')[0]
    for pre in ('us.', 'eu.', 'apac.', 'global.'):
        if m.startswith(pre):
            m = m[len(pre):]
    for pre in ('anthropic.', 'claude-'):
        if m.startswith(pre):
            m = m[len(pre):]
    return m


class Pricer:
    def __init__(self):
        self.cache = {}
        self.notes = {}

    def __call__(self, model):
        if model in self.cache:
            return self.cache[model]
        m = _norm(model)
        price = None
        for p, v in PRICES:
            if m == p or m.startswith(p + '-'):
                price = v
                break
        if price is None:
            for fam, p in FAMILY:
                if m.startswith(fam):
                    price = dict(PRICES)[p]
                    self.notes[model] = 'priced as %s' % p
                    break
        if price is None:
            price = dict(PRICES)[FALLBACK]
            self.notes[model] = 'unknown model, priced as %s' % FALLBACK
        self.cache[model] = tuple(x / 1e6 for x in price)
        return self.cache[model]


def load_calls(path, start_line=0, since=None, until=None):
    """Deduplicated main-chain API calls in order. One call is often written on several lines (one per content
    block) with the same message id and request id."""
    calls = collections.OrderedDict()
    for n, d in inv.iter_json(path):
        if n < start_line or d.get('isSidechain') or d.get('type') != 'assistant':
            continue
        msg = d.get('message') or {}
        if not isinstance(msg, dict):
            continue
        u = msg.get('usage') or {}
        if not inv.call_context(u):
            continue
        ts = d.get('timestamp') or ''
        if (since and ts < since) or (until and ts >= until):
            continue
        key = (msg.get('id'), d.get('requestId'))
        if key == (None, None):
            key = ('line', n)
        text = inv.has_text_block(msg.get('content'))
        rec = 1 if inv.render_text(msg.get('content')).strip() else 0    # one assistant message per line
        c = calls.get(key)
        if c is not None:
            c['o'] = max(c['o'], u.get('output_tokens') or 0)
            c['text'] = c['text'] or text
            c['recs'] += rec
            continue
        cw = u.get('cache_creation_input_tokens') or 0
        cc = u.get('cache_creation') if isinstance(u.get('cache_creation'), dict) else {}
        w1 = cc.get('ephemeral_1h_input_tokens') or 0
        w5 = cc.get('ephemeral_5m_input_tokens') or 0
        if w1 + w5 < cw:
            w1 += cw - w1 - w5
        calls[key] = dict(i=u.get('input_tokens') or 0, w1=w1, w5=w5, r=u.get('cache_read_input_tokens') or 0,
                          o=u.get('output_tokens') or 0, model=msg.get('model'), text=text, recs=rec, ts=ts)
    return list(calls.values())


def growth(calls):
    """Context growth over the calls, leaving out the drops at real compactions or resets (as the replay does)."""
    g, prev = 0, None
    for k in calls:
        ctx = k['i'] + k['w1'] + k['w5'] + k['r']
        if prev is not None and ctx - prev >= -100_000:
            g += ctx - prev
        prev = ctx
    return max(g, 0)


def actual_cost(calls, price):
    t = 0.0
    for c in calls:
        p = price(c['model'])
        t += c['i'] * p[0] + c['w5'] * p[1] + c['w1'] * p[2] + c['r'] * p[3] + c['o'] * p[4]
    return t


def simulate(calls, window, price, buffer, post, summary_out=SUMMARY_OUT):
    """Cost and number of compactions if the session had compacted at `window` (the author's replay, 2 Oct 2026).
    A real compaction counts as one of this window's compactions when the replayed context was already within
    RESET_TOL of the trigger point (otherwise the 1M baseline, which real 1M compactions pre-empt, never fires)."""
    cost, ncomp = 0.0, 0
    c = prev = None
    for k in calls:
        p = price(k['model'])
        ctx = k['i'] + k['w1'] + k['w5'] + k['r']
        new = k['i'] + k['w1'] + k['w5']
        if prev is None:
            c, g = ctx, 0
        else:
            g = ctx - prev
            if g < -100_000:           # a real compaction or reset: the simulation cannot be larger than reality
                if window and c >= window - buffer - RESET_TOL:
                    cost += c * p[3] + summary_out * p[4]
                    ncomp += 1
                c, g = min(c, ctx), 0
            else:
                c = max(c + g, 0)
        miss = new > 0.5 * ctx        # the real call missed the cache
        if window and c > window - buffer:
            cost += c * p[3] + summary_out * p[4]
            ncomp += 1
            c = post + max(g, 0)
            miss = True
        n_new = c if miss else min(new, c)
        cost += n_new * p[2] + (c - n_new) * p[3] + k['o'] * p[4]
        prev = ctx
    return cost, ncomp


def main(argv=None):
    ap = argparse.ArgumentParser(description='Cost of each session at several auto-compact windows (API list '
                                             'prices), and assistant messages per compaction cycle. Writes '
                                             'OUT/cost.json.')
    ap.add_argument('--inventory', required=True, help='inventory.json written by inventory.py')
    ap.add_argument('--windows', default=None, help='windows to price (default: the windows of the inventory, else '
                                                     '300k,400k,500k; the 1M baseline is always added)')
    ap.add_argument('--min-calls', type=int, default=50, help='skip sessions with fewer API calls (default 50)')
    ap.add_argument('--out', default=None, help='output folder (default: the folder of the inventory)')
    ap.add_argument('--buffer', type=int, default=None, help='trigger buffer (default: from the inventory)')
    ap.add_argument('--post', type=int, default=None, help='context after compaction (default: from the inventory)')
    ap.add_argument('--summary-out', type=int, default=SUMMARY_OUT, help='output tokens of one compaction '
                                                                         '(default %d)' % SUMMARY_OUT)
    ap.add_argument('--since', default=None, help='only calls at or after this ISO time (UTC, e.g. 2026-09-01)')
    ap.add_argument('--until', default=None, help='only calls before this ISO time (UTC), e.g. to leave out a '
                                                  'period that already ran at a smaller window')
    a = ap.parse_args(argv)

    try:
        with open(a.inventory, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit('error: cannot read inventory %s: %s' % (a.inventory, e))
    wspec = a.windows or ','.join(data.get('windows') or []) or inv.DEFAULT_WINDOWS
    try:
        windows = inv.parse_windows(wspec)
    except ValueError:
        ap.error('bad --windows value: %s' % wspec)
    if BASELINE not in windows:
        windows.append(BASELINE)
    tags = [inv.wtag(w) for w in windows]
    buffer = a.buffer or data.get('buffer') or inv.BUFFER
    post = a.post or data.get('post_median') or inv.POST_DEFAULT
    out = inv.make_out_dir(a.out or os.path.dirname(os.path.abspath(a.inventory)))
    price = Pricer()

    rows = []
    for s in data.get('sessions', []):
        if not os.path.isfile(s['path']):
            print('warning: transcript gone: %s' % s['path'], file=sys.stderr)
            continue
        start = 0
        if s.get('fork_of'):          # skip the part copied from the parent session
            if s.get('fork_div_line') is None:
                continue
            start = s['fork_div_line']
        calls = load_calls(s['path'], start, a.since, a.until)
        if not calls or len(calls) < a.min_calls:     # no priced calls (also with --min-calls 0): nothing to replay
            continue
        row = dict(sid8=s['sid8'], title=s.get('title'), calls=len(calls),
                   max_ctx=max(k['i'] + k['w1'] + k['w5'] + k['r'] for k in calls),
                   assistant_msgs=sum(k['recs'] for k in calls), growth=growth(calls),
                   actual=round(actual_cost(calls, price), 2), cost={}, ncomp={})
        for w, t in zip(windows, tags):
            c, n = simulate(calls, w, price, buffer, post, a.summary_out)
            row['cost'][t], row['ncomp'][t] = round(c, 2), n
        base = row['cost']['1M']
        row['saving_vs_1M'] = {t: round(1 - row['cost'][t] / base, 4) if base else None for t in tags}
        rows.append(row)
    if not rows:
        sys.exit('error: no session with at least %d priced API calls' % max(1, a.min_calls))
    rows.sort(key=lambda r: -r['cost']['1M'])

    def saving(rs):
        b = sum(r['cost']['1M'] for r in rs)
        return {t: round(1 - sum(r['cost'][t] for r in rs) / b, 4) if b else None for t in tags}
    long_rows = [r for r in rows if r['max_ctx'] >= LONG_CTX]
    total = {t: round(sum(r['cost'][t] for r in rows), 2) for t in tags}
    msgs, grow = sum(r['assistant_msgs'] for r in rows), sum(r['growth'] for r in rows)
    mpc, mpc_pooled = {}, {}
    for w, t in zip(windows, tags):
        mpc[t] = round(msgs * (w - buffer - post) / float(grow), 1) if grow > 0 and w > buffer + post else None
        rs = [r for r in rows if r['ncomp'][t] >= 1]
        n = sum(r['ncomp'][t] for r in rs)
        mpc_pooled[t] = round(sum(r['assistant_msgs'] for r in rs) / float(max(1, n)), 1) if rs else None
    note = ('API list prices in $/MTok (input, 5m write, 1h write, cache read, output), October 2026; all simulated '
            'cache writes at the 1h rate; one compaction = one read of the context + %d output tokens; compaction at '
            'window - %d tokens, context afterwards %d + the next call\'s growth; 1M = default window of '
            '1M-context models.' % (a.summary_out, buffer, post))
    if price.notes:
        note += ' ' + '; '.join('%s %s' % kv for kv in sorted(price.notes.items()))
    res = dict(version=1, windows=tags, buffer=buffer, post=post, summary_out=a.summary_out, min_calls=a.min_calls,
               since=a.since, until=a.until, sessions=rows, total=total,
               actual_total=round(sum(r['actual'] for r in rows), 2),
               saving_vs_1M=saving(rows), saving_long_sessions_vs_1M=saving(long_rows) if long_rows else None,
               long_sessions=len(long_rows),
               saving_long_range={t: [min(r['saving_vs_1M'][t] for r in long_rows),
                                      max(r['saving_vs_1M'][t] for r in long_rows)] for t in tags} if long_rows
               else None,
               msgs_per_cycle=mpc, msgs_per_cycle_pooled=mpc_pooled, assistant_msgs_total=msgs, context_growth_total=grow,
               msgs_per_cycle_note='assistant messages (transcript lines with text or a tool call) per 1k tokens of '
                                   'context growth, times (window - buffer - post) / 1k',
               prices_note=note)
    small, first_small = 0, None
    for s in data.get('sessions', []):
        for e in s.get('events', []):
            w = inv.regime_window(e.get('regime'))
            ts = e.get('ts') or ''
            if (e.get('trigger') == 'auto' and not e.get('dup') and w and w < BASELINE
                    and not (a.until and ts >= a.until) and not (a.since and ts < a.since)):
                small += 1
                if ts and (first_small is None or ts < first_small):
                    first_small = ts
    res['smaller_window_compactions'] = small
    res['first_smaller_window_compaction'] = first_small
    path = os.path.join(out, 'cost.json')
    inv.write_private(path, json.dumps(res, ensure_ascii=False, indent=1))

    print('Cost at API list prices if compacted at each window (%d sessions with >= %d calls; compaction at window '
          '- %s, context after it %s)' % (len(rows), a.min_calls, inv.kfmt(buffer), inv.kfmt(post)))
    head = '  %-8s %-22s %6s %9s' % ('session', 'title', 'calls', 'actual')
    head += ''.join(' %14s' % t for t in tags)
    print(head)
    for r in rows:
        title = (r['title'] or '').replace('\n', ' ')
        title = title[:21] + '…' if len(title) > 22 else title
        line = '  %-8s %-22s %6d %9s' % (r['sid8'], title, r['calls'], '$%.0f' % r['actual'])
        line += ''.join(' %14s' % ('$%.0f (%d)' % (r['cost'][t], r['ncomp'][t])) for t in tags)
        print(line)
    print('  %-8s %-22s %6d %9s' % ('total', '', sum(r['calls'] for r in rows), '$%.0f' % res['actual_total'])
          + ''.join(' %14s' % ('$%.0f' % total[t]) for t in tags))
    pct = lambda x: '-' if x is None else '%.0f%%' % (100 * x)
    print('  (n) = compactions. Saving vs 1M, all sessions: '
          + ', '.join('%s %s' % (t, pct(res['saving_vs_1M'][t])) for t in tags if t != '1M'))
    if long_rows:
        print('  Saving vs 1M, %d long sessions (context >= %s): ' % (len(long_rows), inv.kfmt(LONG_CTX))
              + ', '.join('%s %s (%s-%s)' % (t, pct(res['saving_long_sessions_vs_1M'][t]),
                                             pct(res['saving_long_range'][t][0]), pct(res['saving_long_range'][t][1]))
                          for t in tags if t != '1M'))
    print('  Assistant messages per compaction cycle (%.2f per 1k tokens of context growth): '
          % (1000.0 * msgs / grow if grow else 0)
          + ', '.join('%s %s' % (t, '-' if mpc[t] is None else '%.0f' % mpc[t]) for t in tags))
    if price.notes:
        print('  Note: ' + '; '.join('%s %s' % kv for kv in sorted(price.notes.items())))
    if res['smaller_window_compactions']:
        print('  Note: %d real automatic compactions already ran at a window below 1M (the first at %s). A replay '
              'cannot show what a larger window would have cost in those stretches, so savings vs 1M are understated. '
              'Rerun with --until %s to price only the period before that.'
              % (res['smaller_window_compactions'], first_small, first_small))
    print('Wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())

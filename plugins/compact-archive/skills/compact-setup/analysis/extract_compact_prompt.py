#!/usr/bin/env python3
"""Copy Claude Code's own compaction prompt from the local install.

The smart analysis simulates compactions with the instructions Claude Code itself uses. That text belongs to
Claude Code and is not shipped with this plugin: this script finds the installed Claude Code (native binary,
npm package or desktop app), locates the prompt by a fixed sentence in it, rebuilds the JavaScript string around
that sentence (with the no-tools preamble and the closing reminder that are sent with it), and writes it to --out
(owner-only file).

Exit code 0: written. Exit code 2: not found; the analysis then uses tasks/COMPACT_PROMPT_FALLBACK.md and the
report says so.
"""
import argparse
import glob
import mmap
import os
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True     # no __pycache__ in the plugin folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inventory as inv  # noqa: E402

ANCHOR_MAIN = 'Your task is to create a detailed summary of the conversation so far'
ANCHOR_PRE = 'Respond with TEXT ONLY'
ANCHOR_TRAIL = 'REMINDER:'
TRAIL_NEAR = 'Do NOT call any tools'
NEAR = 30_000                  # preamble, trailer and constants must lie this close to the main text
MAX_BYTES = 400 * 1024 * 1024
OPENER_PREV = set('=(,:[+?{};!&|>')
IDENT = re.compile(r'[A-Za-z_$][\w$]*\Z')


# ---------------------------------------------------------------- finding the install

def _version_key(path):
    m = re.search(r'(\d+)\.(\d+)\.(\d+)', path)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def candidates(explicit=None):
    seen, out = set(), []

    def add(p):
        rp = os.path.realpath(os.path.expanduser(p))
        if rp not in seen and os.path.isfile(rp):
            seen.add(rp)
            out.append(rp)

    if explicit:
        add(explicit)
        return out
    home = os.path.expanduser('~')
    w = shutil.which('claude')
    if w:
        rp = os.path.realpath(w)
        try:
            small = os.path.getsize(rp) < 64 * 1024
        except OSError:
            small = False
        if small:              # a shell wrapper: follow the paths it names
            try:
                with open(rp, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError:
                text = ''
            for m in re.finditer(r'''["']?((?:/|~/|\$HOME/)[^\s"'$`;|&]+)''', text):
                p = m.group(1).replace('$HOME', home)
                p = os.path.expanduser(p)
                if os.path.isdir(p):
                    for c in glob.glob(os.path.join(p, '**', 'cli.js'), recursive=True):
                        add(c)
                elif os.path.isfile(p):
                    add(p)
        else:
            add(rp)
    for c in glob.glob(os.path.join(home, '.claude', 'local', '**', 'cli.js'), recursive=True):
        add(c)
    npm = shutil.which('npm')
    if npm:
        try:
            root = subprocess.run([npm, 'root', '-g'], capture_output=True, text=True, timeout=15).stdout.strip()
            if root:
                add(os.path.join(root, '@anthropic-ai', 'claude-code', 'cli.js'))
        except (OSError, subprocess.SubprocessError):
            pass
    for c in sorted(glob.glob(os.path.join(home, '.local', 'share', 'claude', 'versions', '*')), key=_version_key,
                    reverse=True):
        add(c)
    app = os.path.join(home, 'Library', 'Application Support', 'Claude', 'claude-code')
    for vdir in sorted(glob.glob(os.path.join(app, '*')), key=_version_key, reverse=True):
        for c in glob.glob(os.path.join(vdir, '**', 'claude'), recursive=True):
            add(c)
    return out


def cli_version():
    w = shutil.which('claude')
    if not w:
        return None
    try:
        r = subprocess.run([w, '--version'], capture_output=True, text=True, timeout=20)
        return (r.stdout or '').strip().splitlines()[0] if r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


# ---------------------------------------------------------------- reading JavaScript literals

def parse_literal(s, q):
    """Parse the string or template literal opening at s[q]. Returns (parts, end) with parts a list of
    ('s', raw text) and ('x', expression) items and end the index after the closing quote, or None."""
    quote, i, parts, buf = s[q], q + 1, [], []
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '\\':
            buf.append(s[i:i + 2])
            i += 2
            continue
        if ch == quote:
            parts.append(('s', ''.join(buf)))
            return parts, i + 1
        if quote != '`' and ch in '\r\n':
            return None
        if quote == '`' and ch == '$' and s.startswith('${', i):
            parts.append(('s', ''.join(buf)))
            buf = []
            j = skip_expr(s, i + 2)
            if j is None:
                return None
            parts.append(('x', s[i + 2:j]))
            i = j + 1
            continue
        if (ch < ' ' and ch not in '\n\r\t') or ch == '�':
            return None        # binary data, not source text
        buf.append(ch)
        i += 1
    return None


def skip_expr(s, i):
    """Index of the '}' closing a ${...} expression that starts at i."""
    depth = 0
    while i < len(s):
        ch = s[i]
        if ch in '\'"`':
            r = parse_literal(s, i)
            if r is None:
                return None
            i = r[1]
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return None


def _escaped(s, q):
    k = 0
    while q - k - 1 >= 0 and s[q - k - 1] == '\\':
        k += 1
    return k % 2 == 1


def _opener_ok(s, q):
    j = q - 1
    while j >= 0 and s[j] in ' \t\r\n':
        j -= 1
    if j < 0 or s[j] in OPENER_PREV:
        return True
    k = j
    while k >= 0 and (s[k].isalnum() or s[k] in '_$'):
        k -= 1
    return s[k + 1:j + 1] in ('return', 'case', 'yield', 'await')


def expression_at(s, q):
    """A literal at q plus any '+ literal' that follows. Returns (list of parts lists, end) or None."""
    r = parse_literal(s, q)
    if r is None:
        return None
    pieces, end = [r[0]], r[1]
    while True:
        m = re.compile(r'\s*\+\s*([`"\'])').match(s, end)
        if not m:
            break
        r = parse_literal(s, m.start(1))
        if r is None:
            break
        pieces.append(r[0])
        end = r[1]
    return pieces, end


def enclosing(s, p, length):
    """Start of the literal expression that contains s[p:p+length]."""
    q = p - 1
    lo = max(0, p - NEAR)
    while q >= lo:
        if s[q] in '`"\'' and not _escaped(s, q) and _opener_ok(s, q):
            r = parse_literal(s, q)
            if r is not None and r[1] >= p + length:
                return q
        q -= 1
    return None


def unescape(raw):
    out, i, n = [], 0, len(raw)
    simple = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0'}
    while i < n:
        ch = raw[i]
        if ch != '\\':
            out.append(ch)
            i += 1
            continue
        nx = raw[i + 1:i + 2]
        try:
            if nx == 'u' and raw[i + 2:i + 3] == '{':
                j = raw.index('}', i + 3)
                out.append(chr(int(raw[i + 3:j], 16)))
                i = j + 1
                continue
            if nx == 'u':
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
                continue
            if nx == 'x':
                out.append(chr(int(raw[i + 2:i + 4], 16)))
                i += 4
                continue
        except ValueError:
            pass
        if nx == '\r':
            i += 3 if raw[i + 2:i + 3] == '\n' else 2
            continue
        if nx == '\n':
            i += 2
            continue
        out.append(simple.get(nx, nx))
        i += 2
    text = ''.join(out)
    return text.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')


class Resolver:
    """Turns parsed literals into text. ${NAME} is replaced by NAME's value when NAME is assigned a literal close by
    in the same file; any other interpolation becomes ''."""

    def __init__(self, s):
        self.s = s
        self.resolved, self.dropped = [], []

    def text(self, pieces, pos, depth=0):
        out = []
        for parts in pieces:
            for kind, v in parts:
                if kind == 's':
                    out.append(unescape(v))
                    continue
                name = v.strip()
                val = None
                if depth < 4 and IDENT.match(name):
                    val = self.ident(name, pos, depth)
                elif depth < 4 and name[:1] in '`"\'':     # an inline literal: ${"..."}
                    e = expression_at(name, 0)
                    if e is not None and e[1] == len(name):
                        val = self.text(e[0], pos, depth + 1)
                label = name if IDENT.match(name) else '(inline literal)' if val is not None else name[:20]
                (self.resolved if val is not None else self.dropped).append(label)
                out.append(val or '')
        return ''.join(out)

    def ident(self, name, pos, depth):
        pat = re.compile(r'(?<![\w$.])' + re.escape(name) + r'\s*=(?![=>])\s*(?=[`"\'])')
        defs = [m for m in pat.finditer(self.s) if abs(m.start() - pos) <= NEAR]
        if not defs:
            return None
        m = min(defs, key=lambda x: abs(x.start() - pos))
        e = expression_at(self.s, m.end())
        if e is None:
            return None
        return self.text(e[0], m.end(), depth + 1)


# ---------------------------------------------------------------- extraction

def extract_from(path):
    """Prompt text found in one file, or None."""
    try:
        size = os.path.getsize(path)
        if size == 0 or size > MAX_BYTES:
            return None
        f = open(path, 'rb')
    except OSError:
        return None
    with f:
        try:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            return None
        try:
            anchor = ANCHOR_MAIN.encode()
            p = mm.find(anchor)
            while p != -1:
                lo = max(0, p - 2 * NEAR)
                s = mm[lo:p + 2 * NEAR].decode('utf-8', 'replace')
                got = _assemble(s, len(mm[lo:p].decode('utf-8', 'replace')))
                if got:
                    return got
                p = mm.find(anchor, p + 1)
        finally:
            mm.close()
    return None


def _nearest(s, word, pos):
    hits = [m.start() for m in re.finditer(re.escape(word), s)]
    hits = [h for h in hits if abs(h - pos) <= NEAR]
    return sorted(hits, key=lambda h: abs(h - pos))


def _assemble(s, pos):
    q = enclosing(s, pos, len(ANCHOR_MAIN))
    if q is None:
        return None
    res = Resolver(s)
    e = expression_at(s, q)
    if e is None:
        return None
    main = res.text(e[0], q).strip()
    if len(main) < 1000:
        return None
    pre = trail = ''
    for h in _nearest(s, ANCHOR_PRE, pos):
        q2 = enclosing(s, h, len(ANCHOR_PRE))
        if q2 is not None and q2 != q:
            e2 = expression_at(s, q2)
            if e2:
                pre = res.text(e2[0], q2).strip()
                break
    for h in _nearest(s, ANCHOR_TRAIL, pos):
        if TRAIL_NEAR not in s[h:h + 300]:
            continue
        q3 = enclosing(s, h, len(ANCHOR_TRAIL))
        if q3 is not None and q3 not in (q, ):
            e3 = expression_at(s, q3)
            if e3:
                trail = res.text(e3[0], q3).strip()
                break
    if ANCHOR_MAIN in pre:
        pre = ''
    parts = [x for x in (pre, main, trail) if x]
    return dict(text='\n\n'.join(parts) + '\n', preamble=bool(pre), trailer=bool(trail),
                resolved=sorted(set(res.resolved)), dropped=sorted(set(res.dropped)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Copy Claude Code's compaction prompt from the local install into a "
                                             "private file (exit code 2 if it cannot be found).")
    ap.add_argument('--out', required=True, help='file to write, e.g. OUT/compact_prompt.txt')
    ap.add_argument('--source', default=None, help='search only this file (a Claude Code binary or cli.js)')
    a = ap.parse_args(argv)

    cands = candidates(a.source)
    if not cands:
        print('error: no Claude Code install found (looked for `claude` on PATH, ~/.claude/local, the global npm '
              'package, ~/.local/share/claude/versions and the desktop app)%s'
              % ((' at ' + a.source) if a.source else ''), file=sys.stderr)
        return 2
    for path in cands:
        got = extract_from(path)
        if not got:
            continue
        out = os.path.abspath(os.path.expanduser(a.out))
        inv.make_out_dir(os.path.dirname(out))
        inv.write_private(out, got['text'])
        ver = cli_version()
        m = re.search(r'\d+\.\d+\.\d+', path)
        print('Compaction prompt: %d characters written to %s' % (len(got['text']), out))
        print('Source: %s%s' % (path, (' (version %s)' % m.group(0)) if m else ''))
        if ver:
            print('claude --version: %s' % ver)
        print('Preamble: %s; closing reminder: %s; constants filled in: %s; expressions left empty: %s'
              % ('yes' if got['preamble'] else 'no', 'yes' if got['trailer'] else 'no',
                 ', '.join(got['resolved']) or 'none', ', '.join(got['dropped']) or 'none'))
        return 0
    print('error: the compaction prompt was not found in %d Claude Code file%s searched (%s). The analysis will use '
          'the generic stand-in prompt.' % (len(cands), '' if len(cands) == 1 else 's', ', '.join(cands)),
          file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env bash
# PreCompact hook for Claude Code: archive the session transcript before every compaction.
#
# Claude Code pipes a JSON object to stdin with (among others) "session_id", "transcript_path" and
# "trigger" ("manual" for /compact, "auto" for auto-compaction). This script copies
#   <transcript>.jsonl            -> $ARCHIVE/<project>/<session-id>.jsonl
#   <transcript>/ (subagents, tool results, if present)
#                                 -> $ARCHIVE/<project>/<session-id>.extras.tar.gz
# and appends log lines to $ARCHIVE/archive.log.
#
# Transcripts normally only grow, so by default each session keeps ONE archive that is refreshed at every
# compaction. If a transcript was ever rewritten or shortened, the previous archive is kept as
# <session-id>.<time>.prev.jsonl instead of being overwritten. Set CLAUDE_TRANSCRIPT_ARCHIVE_MODE=snapshot to
# keep a timestamped copy per compaction.
#
# Settings (environment variables, e.g. via "env" in ~/.claude/settings.json):
#   CLAUDE_TRANSCRIPT_ARCHIVE_DIR     archive folder, absolute path or ~/...   (default: ~/claude-transcripts)
#   CLAUDE_TRANSCRIPT_ARCHIVE_MODE    "latest" or "snapshot"                   (default: latest)
#   CLAUDE_TRANSCRIPT_ARCHIVE_EXTRAS  1 = also archive the session folder, 0 = main transcript only (default: 1)
#
# The hook must never block or break compaction, so it always exits 0 and writes nothing to stdout.

# Transcripts are private: archive files and folders are readable by the owner only.
umask 077

DEST="${CLAUDE_TRANSCRIPT_ARCHIVE_DIR:-$HOME/claude-transcripts}"
# settings.json does not expand "~"; relative paths would land inside the current project, so fall back.
case $DEST in "~") DEST=$HOME ;; "~/"*) DEST="$HOME/${DEST#\~/}" ;; esac
case $DEST in /*|[A-Za-z]:[\\/]*) ;; *) DEST="$HOME/claude-transcripts" ;; esac
MODE="${CLAUDE_TRANSCRIPT_ARCHIVE_MODE:-latest}"
EXTRAS="${CLAUDE_TRANSCRIPT_ARCHIVE_EXTRAS:-1}"
LOG="$DEST/archive.log"

input=$(cat)

# field JSON KEY: print one top-level string field. Tries jq, then python3, then sed (plain values only),
# taking the first non-empty answer; strips CR so it also works with Windows-native tools.
field() {
  v=""
  if command -v jq >/dev/null 2>&1; then
    v=$(printf '%s' "$1" | jq -j --arg k "$2" '.[$k] // empty | strings' 2>/dev/null)
  fi
  if [ -z "$v" ] && command -v python3 >/dev/null 2>&1; then
    v=$(printf '%s' "$1" | python3 -c 'import json,sys; v=json.load(sys.stdin.buffer).get(sys.argv[1]); sys.stdout.buffer.write(v.encode("utf-8") if isinstance(v,str) else b"")' "$2" 2>/dev/null)
  fi
  if [ -z "$v" ]; then
    v=$(printf '%s' "$1" | tr -d '\n' | sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  fi
  printf '%s' "$v" | tr -d '\r'
}

tp=$(field "$input" transcript_path)
sid=$(field "$input" session_id)
trig=$(field "$input" trigger)
sid=${sid:-unknown}
trig=${trig:-unknown}
now=$(date '+%Y-%m-%d %H:%M:%S')

if ! mkdir -p "$DEST" 2>/dev/null; then
  echo "$now FAIL $trig $sid: cannot create $DEST" >> "${TMPDIR:-/tmp}/claude-compact-archive.log" 2>/dev/null
  exit 0
fi

if [ -z "$tp" ] || [ ! -f "$tp" ]; then
  echo "$now SKIP $trig $sid: transcript not found (${tp:-no transcript_path})" >> "$LOG"
  exit 0
fi

# Group archives by Claude Code's project folder name (e.g. -Users-me-code-myrepo).
project=$(basename "$(dirname "$tp")")
outdir="$DEST/$project"
if ! mkdir -p "$outdir" 2>/dev/null; then
  echo "$now FAIL $trig $sid: cannot create $outdir" >> "$LOG"
  exit 0
fi

# Name archives after the transcript file (Claude Code names it <session-id>.jsonl).
base=$(basename "$tp" .jsonl)
if [ "$MODE" = "snapshot" ]; then
  stem="$(date +%Y%m%d-%H%M%S)_${base}_${trig}"
else
  stem="$base"
fi

# Per-run temp names, so two hooks for the same session (e.g. parallel subagents compacting) cannot collide.
tmp1="$outdir/.$stem.jsonl.$$.tmp"
tmp2="$outdir/.$stem.extras.$$.tmp"
trap 'rm -f "$tmp1" "$tmp2"' EXIT
# Remove temp files left by earlier runs that were killed (e.g. by the hook timeout).
find "$outdir" -maxdepth 1 -name ".*.tmp" -mmin +30 -exec rm -f {} + 2>/dev/null

# Optional human-readable session title (set with /rename or by the app).
title=""
titlefile="${tp%.jsonl}/custom-title.json"
[ -f "$titlefile" ] && title=$(field "$(cat "$titlefile")" customTitle | tr '\n' ' ')

# Never let a shorter or rewritten transcript replace a fuller archive: if the new transcript does not start
# with the archived one, keep the old archive as .prev.
keptnote=""
old="$outdir/$stem.jsonl"
if [ "$MODE" != "snapshot" ] && [ -f "$old" ]; then
  oldsize=$(wc -c < "$old" | tr -d ' '); newsize=$(wc -c < "$tp" | tr -d ' ')
  n=65536; [ "$oldsize" -lt "$n" ] && n=$oldsize
  if [ "$newsize" -lt "$oldsize" ] || [ "$(head -c "$n" "$old" | cksum)" != "$(head -c "$n" "$tp" | cksum)" ]; then
    mv -f "$old" "$outdir/$stem.$(date +%Y%m%d-%H%M%S).prev.jsonl" 2>/dev/null
    keptnote=" (previous archive was not a prefix; kept as .prev)"
  fi
fi

# Copy to a temporary name first, then rename, so an interrupted copy never replaces a good archive.
if cp "$tp" "$tmp1" 2>/dev/null && mv -f "$tmp1" "$outdir/$stem.jsonl"; then
  size=$(wc -c < "$outdir/$stem.jsonl" | tr -d ' ')
  msg="$now $trig $sid -> $project/$stem.jsonl ($size bytes)$keptnote"
else
  echo "$now FAIL $trig $sid: could not copy $tp" >> "$LOG"
  exit 0
fi
[ -n "$title" ] && msg="$msg [$title]"
# Log the transcript copy now, so a kill during the slower extras step still leaves a record.
echo "$msg" >> "$LOG"

sessdir="${tp%.jsonl}"
if [ "$EXTRAS" = "1" ] && [ -d "$sessdir" ]; then
  msg="$now $trig $sid   extras:"
  oldx="$outdir/$stem.extras.tar.gz"
  if [ "$MODE" != "snapshot" ] && [ -f "$oldx" ] \
     && [ -z "$(find "$sessdir" -newer "$oldx" 2>/dev/null | head -1)" ]; then
    msg="$msg unchanged"
  else
    # Write the tarball through stdout: GNU tar on Windows (Git Bash) reads "C:/..." in -f as a remote host.
    tar -czf - -C "$(dirname "$sessdir")" "$(basename "$sessdir")" > "$tmp2" 2>/dev/null
    rc=$?
    # GNU tar exits 1 when a file changed while being read (e.g. a running subagent); the archive is usable.
    if [ $rc -eq 0 ] || { [ $rc -eq 1 ] && tar --version 2>/dev/null | grep -q GNU; }; then
      # Same rule as above: a smaller tarball never silently replaces a bigger one.
      if [ "$MODE" != "snapshot" ] && [ -f "$oldx" ] \
         && [ "$(wc -c < "$tmp2" | tr -d ' ')" -lt "$(wc -c < "$oldx" | tr -d ' ')" ]; then
        mv -f "$oldx" "$outdir/$stem.$(date +%Y%m%d-%H%M%S).prev.extras.tar.gz" 2>/dev/null
        msg="$msg (smaller than before; previous kept as .prev)"
      fi
      if mv -f "$tmp2" "$oldx"; then
        msg="$msg $(wc -c < "$oldx" | tr -d ' ') bytes"
      else
        msg="$msg FAILED (could not move tarball)"
      fi
    else
      msg="$msg FAILED (tar exit $rc)"
    fi
  fi
  echo "$msg" >> "$LOG"
fi
exit 0

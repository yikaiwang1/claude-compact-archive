# claude-compact-archive

Compact Claude Code sessions early to save cost, without losing anything.

Every turn re-sends the whole conversation, so the longer a session gets, the more each turn costs. Compacting
earlier keeps the context small, but people hold back for two reasons: the full history is gone after compaction,
and the summary may drop the wrong details. This plugin addresses both:

- **A PreCompact hook** archives the transcript before every compaction, manual (`/compact`) or automatic, so the
  full history is always kept.
- **A setup skill** (`compact-setup`) walks Claude through setting a smaller auto-compact window (e.g. 300k
  tokens), adding *Compact instructions* to `CLAUDE.md` so summaries keep the recent work in detail, and
  configuring the archive folder.

## Install

In Claude Code:

```text
/plugin marketplace add yikaiwang1/claude-compact-archive
/plugin install compact-archive@claude-compact-archive
```

or from a shell:

```bash
claude plugin marketplace add yikaiwang1/claude-compact-archive
claude plugin install compact-archive@claude-compact-archive
```

The hook is active in new sessions (or after `/reload-plugins`). Then ask Claude to *"set up compaction"*, or run
the skill directly with `/compact-archive:compact-setup`.

## What gets archived

Before each compaction the hook writes:

| File | Contents |
|---|---|
| `<archive>/<project>/<session-id>.jsonl` | the main transcript |
| `<archive>/<project>/<session-id>.extras.tar.gz` | the session folder next to the transcript, if any (`<session-id>/subagents/`, `<session-id>/tool-results/`, …) |
| `<archive>/archive.log` | one line per compaction: time, trigger (`manual`/`auto`), session, sizes, session title |

`<archive>` is `~/claude-transcripts` unless you set `CLAUDE_TRANSCRIPT_ARCHIVE_DIR`. `<project>` is Claude
Code's own folder name for the project (e.g. `-Users-me-code-myrepo`).

Transcripts normally only grow, so by default each session keeps one archive that is refreshed at every compaction.
If Claude Code ever rewrites or shortens a transcript, the previous archive is kept as
`<session-id>.<time>.prev.jsonl` rather than overwritten. Archive files are readable by you only, like Claude Code's
own copies.

PreCompact hooks are able to block compaction, but this one is written never to: it always exits 0, and failures go
to the log. Compaction does wait for the copy. That is usually well under a second, plus a few seconds the first time
a large session folder is packed; the folder is re-packed only when something in it changed. The hook timeout is
120 s.

## Check it works

After your next `/compact`, look at the last lines of the log:

```bash
tail -2 ~/claude-transcripts/archive.log
```

You should see something like

```text
2026-10-02 22:32:34 manual 5a780fb6-… -> -Users-me-code-myrepo/5a780fb6-….jsonl (62533 bytes) [my session title]
2026-10-02 22:32:34 manual 5a780fb6-…   extras: 18324 bytes
```

If nothing appears, check `${TMPDIR:-/tmp}/claude-compact-archive.log`: the hook writes there when it cannot create
the archive folder.

## Using the archive

Each `.jsonl` file is the session as Claude Code stored it, one JSON object per line. To find something that was
summarised away, ask Claude to search it (for example *"search my archived transcript for the decision on X"*), or
use `grep` or `jq` yourself.

Claude Code deletes local transcripts after `cleanupPeriodDays` (30 days by default); the archive is kept.

## Options

Set these in the `env` block of `~/.claude/settings.json`:

```json
{
  "env": {
    "CLAUDE_TRANSCRIPT_ARCHIVE_DIR": "/Users/me/Dropbox/claude-transcripts",
    "CLAUDE_TRANSCRIPT_ARCHIVE_MODE": "latest",
    "CLAUDE_TRANSCRIPT_ARCHIVE_EXTRAS": "1"
  }
}
```

| Variable | Values | Default |
|---|---|---|
| `CLAUDE_TRANSCRIPT_ARCHIVE_DIR` | an absolute path; a leading `~/` is expanded; a relative path falls back to the default | `~/claude-transcripts` |
| `CLAUDE_TRANSCRIPT_ARCHIVE_MODE` | `latest` (one file per session) or `snapshot` (a timestamped copy per compaction) | `latest` |
| `CLAUDE_TRANSCRIPT_ARCHIVE_EXTRAS` | `1` include the session folder, `0` main transcript only | `1` |

## Choosing the auto-compact window

`/autocompact 300k` sets it (saved as `autoCompactWindow` in `~/.claude/settings.json`); `/autocompact auto`
restores the default. For 1M-context models, 300k is a good start. In the author's own long working sessions,
simulated at API prices, compacting at 300k instead of near the 1M limit cut cost by about 40–46%; shorter sessions
gained about 16%. Windows below 300k saved nothing more, because frequent summaries cost tokens too and lose more
context. Your numbers will differ.

## Compact instructions

The skill offers to add this kind of section to `CLAUDE.md`; the full text is in
[`compact-instructions.md`](plugins/compact-archive/skills/compact-setup/compact-instructions.md):

- keep more detail on the most recent work (current task, open questions, latest results, files just changed,
  running jobs);
- keep earlier material only where it still matters (decisions, standing rules, final results with paths,
  errors already fixed);
- drop dead ends, superseded drafts and long tool output, and point to the archive instead.

## Manual install (without the plugin)

1. Copy [`precompact_archive.sh`](plugins/compact-archive/scripts/precompact_archive.sh) somewhere permanent, e.g.
   `~/.claude/hooks/precompact_archive.sh`.
2. Add the hook to `~/.claude/settings.json`, merging with any `hooks` you already have:

   ```json
   {
     "hooks": {
       "PreCompact": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "bash ~/.claude/hooks/precompact_archive.sh 2>/dev/null || true",
               "timeout": 120,
               "statusMessage": "Archiving transcript"
             }
           ]
         }
       ]
     }
   }
   ```

3. Copy `plugins/compact-archive/skills/compact-setup/` to `~/.claude/skills/compact-setup/` if you want the skill.

Do not use both the plugin and the manual hook, or every compaction is archived twice.

## Requirements

- `bash`, `cp`, `tar`, `find` (standard on macOS and Linux). `jq` or `python3` is used to read the hook input if
  present. Without either, a `sed` fallback handles plain paths only; install `jq` if your paths contain quotes or
  backslashes.
- Tested on macOS with Claude Code 2.1.x. Linux should work; Windows (Git Bash) is untested.

## Privacy

Transcripts contain everything in a session: your prompts, file contents Claude read, command output, and anything
sensitive you pasted. The archive is a plain copy. If you point it at a cloud-synced folder, that copy is in the
cloud too. Choose the folder accordingly.

## Uninstall

`/plugin uninstall compact-archive@claude-compact-archive`, then `/autocompact auto` and remove the Compact
instructions section from `CLAUDE.md` if you added it. Archived files are left in place.

## License

MIT

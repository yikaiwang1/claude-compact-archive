# claude-compact-archive

Compact Claude Code sessions early to save cost, without losing anything.

Every turn re-sends the whole conversation, so the longer a session gets, the more each turn costs. Compacting
earlier keeps the context small, but people hold back for two reasons: the full history is gone after compaction,
and the summary may drop the wrong details. This plugin addresses both:

- **A PreCompact hook** archives the transcript before every compaction, manual (`/compact`) or automatic, so the
  full history is always kept.
- **A setup skill** (`compact-setup`) walks Claude through setting a smaller auto-compact window (500k tokens by
  default, or a value chosen by an optional *smart analysis* of your own sessions), adding *Compact instructions* to
  `CLAUDE.md` so summaries keep the recent work in detail, and configuring the archive folder.

## Install

In Claude Code:

```text
/plugin marketplace add yikaiwangec/claude-compact-archive
/plugin install compact-archive@claude-compact-archive
```

or from a shell:

```bash
claude plugin marketplace add yikaiwangec/claude-compact-archive
claude plugin install compact-archive@claude-compact-archive
```

In the Claude desktop app, typing `/plugin …` in a Code session opens the app's own plugin window instead of running
the command; use the shell commands there.

The hook is active in new sessions (or after `/reload-plugins`). Then ask Claude to *"set up compaction"*, or run
the skill directly with `/compact-archive:compact-setup`.

## Update

From a shell:

```bash
claude plugin marketplace update claude-compact-archive
claude plugin update compact-archive@claude-compact-archive
```

The first command fetches the latest version from GitHub, the second installs it. The new version is used from the
next session on (or after restarting Claude Code). In the Claude desktop app, run these in a terminal (or ask Claude
to run them), not as `/plugin …` in a Code session.

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

`/autocompact 500k` sets it (saved as `autoCompactWindow` in `~/.claude/settings.json`); `/autocompact auto`
restores the default. When you run the skill, Claude first takes stock of your transcripts (a few seconds, no tokens)
and then asks you to choose:

- **Use 500k.** The default, with no analysis.
- **Smart analysis** (`full` or `lite`). Claude measures cost *and* output quality on your own sessions and
  recommends a window. Before asking, it shows an estimate for your data: number of agents, minutes, tokens and the
  API-equivalent cost. The full analysis is large: in the author's run it processed roughly 200–300 million tokens
  (mostly cache reads), took one to two hours, and used a large part of one Max-plan 5-hour window. It needs Claude
  Code's Workflow tool. The method is described in
  [`smart-analysis.md`](plugins/compact-archive/skills/compact-setup/smart-analysis.md) and the scripts are in
  [`analysis/`](plugins/compact-archive/skills/compact-setup/analysis).

What the author found on their own long sessions with a 1M-context model (October 2026; your numbers will differ):

| Window | Saving vs 1M (long sessions) | Assistant messages between compactions | Moderate/serious problems per 100 assistant messages |
|---|---|---|---|
| 1M (default) | — | ~426 | 0.04–0.22 |
| 500k | 29–36% | ~161 | ~0.16 (projected) |
| 400k | 35–41% | ~117 | ~0.22 (projected) |
| 300k | 40–46% | 74 | 0.36 |

- A single compaction at 300k was not measurably worse than one at 1M (problems after 2 of 12 compactions at 1M and
  5 of 19 at 300k). The difference comes from frequency: 300k compacts about 5.8 times as often.
- Key facts survived about as well at 300k as at 1M. Across all facts, 500k kept about 9 percentage points more than
  300k.
- Very long contexts (above 800k tokens) showed no measurable loss of quality. So the case for a smaller window is
  cost, and the case against going too small is the number of compactions.
- Going below 300k saved nothing more, because frequent summaries cost tokens too.
- 300k → 400k costs about 9% more and removes about 40% of the moderate/serious problems; 400k → 500k costs another
  ~9% for less than half that gain.
- A second, smaller run of the finished tool (8 compactions, 2 long cycles, 400k simulated as well) found raw recall
  equal at 300k, 400k and 500k. Real summaries keep less than simulated ones, though, and after correcting for that,
  500k kept about 7 points more key facts than 400k. On the author's data the analysis's rule therefore picks 500k.
- That is why 500k is the no-analysis default. If cost matters more to you, 400k costs about 8% less and 300k about
  15% less; run the analysis to decide on your own sessions.

The cost side alone takes seconds and no tokens:
`python3 plugins/compact-archive/skills/compact-setup/analysis/cost_sim.py --inventory <inventory.json>` (after
`inventory.py`).

## Compact instructions

The skill offers to add this kind of section to `CLAUDE.md`; the full text is in
[`compact-instructions.md`](plugins/compact-archive/skills/compact-setup/compact-instructions.md):

- keep more detail on the most recent work (current task, open questions, latest results, files just changed,
  running jobs);
- keep earlier material only where it still matters (decisions, standing rules, final results with paths,
  errors already fixed);
- drop dead ends, superseded drafts and long tool output, and point to the archive instead;
- write out the content of screenshots and images in text, because images do not survive compaction;
- keep the settings and warnings ("unverified", "placeholder", "do not use") word for word next to each result;
- state the task state explicitly: what was sent, launched or delivered, and every job still running.

The last three come from the author's audit of real compactions, where they were behind the most serious errors.

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
- The smart analysis needs `python3` (3.8+, standard library; `matplotlib` optional for a figure) and Claude Code's
  Workflow tool.
- Tested on macOS with Claude Code 2.1.x. Linux should work; Windows (Git Bash) is untested.

## Privacy

Transcripts contain everything in a session: your prompts, file contents Claude read, command output, and anything
sensitive you pasted. The archive is a plain copy. If you point it at a cloud-synced folder, that copy is in the
cloud too. Choose the folder accordingly.

The smart analysis writes rendered copies of transcripts to its work folder (`~/claude-compact-analysis/<date>/` by
default, or `$COMPACT_ANALYSIS_DIR`), readable by you only. Delete it once you have read the report.

## Uninstall

`/plugin uninstall compact-archive@claude-compact-archive`, then `/autocompact auto` and remove the Compact
instructions section from `CLAUDE.md` if you added it. Archived files are left in place.

## License

MIT

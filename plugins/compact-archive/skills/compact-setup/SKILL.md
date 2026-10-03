---
name: compact-setup
description: Set up cost-saving compaction in Claude Code. Checks that transcripts are archived before every compaction, sets a smaller auto-compact window (400k tokens by default, or a value chosen by an optional smart analysis of the user's own sessions that measures cost and output quality), and adds "Compact instructions" to CLAUDE.md so summaries keep recent work in detail and drop dead ends. Use when the user asks about compaction, /compact, /autocompact, auto-compact thresholds or windows, context or token cost in long sessions, whether compacting earlier hurts quality, or archiving or keeping old transcripts.
---

# Compact setup

Each turn re-sends the whole conversation, so a long session costs more per turn the longer it gets. Compacting
earlier keeps the context, and the cost of every later turn, smaller. Two worries usually stop people from doing
that: losing the full history, and a summary that drops the wrong details. This plugin deals with both:

1. **Archive first.** The plugin's PreCompact hook copies the transcript to an archive folder before every
   compaction, whether it is manual (`/compact`) or automatic. Nothing is lost when the summary is short.
2. **Compact earlier.** A smaller auto-compact window (`/autocompact`) makes compaction happen sooner.
3. **Summarise well.** A "Compact instructions" section in CLAUDE.md tells the summariser what to keep.

Work through the steps below with the user. Ask before changing any file, and show the exact change.

## Step 1: check the archive hook

- Installed as a plugin, the hook is active from the next session (or after `/reload-plugins`). The archive folder
  is `$CLAUDE_TRANSCRIPT_ARCHIVE_DIR`, or `~/claude-transcripts` by default.
- To use another folder (for example a synced Dropbox or iCloud folder), add it to the `env` block of
  `~/.claude/settings.json`, merging with any existing keys. Use an absolute path (a leading `~/` is expanded; a
  relative path falls back to the default):

  ```json
  { "env": { "CLAUDE_TRANSCRIPT_ARCHIVE_DIR": "/absolute/path/to/claude-transcripts" } }
  ```

- Other options, also set through `env`:
  - `CLAUDE_TRANSCRIPT_ARCHIVE_MODE`: `latest` (default) keeps one archive per session, refreshed at every
    compaction; if a transcript was ever rewritten or shortened, the previous archive is kept as `.prev`.
    `snapshot` keeps a timestamped copy per compaction instead.
  - `CLAUDE_TRANSCRIPT_ARCHIVE_EXTRAS`: `1` (default) also archives the session folder (subagent transcripts and
    saved tool results) as `<session-id>.extras.tar.gz`; `0` archives the main transcript only.
- Files land in `<archive>/<project-folder>/<session-id>.jsonl`, and each compaction adds lines to
  `<archive>/archive.log` (time, trigger, session id, sizes, session title if set). If the archive folder cannot
  be created, the hook writes to `${TMPDIR:-/tmp}/claude-compact-archive.log` instead.
- To test the hook without compacting, make a throwaway transcript, pipe a fake event into the script, read the
  log, then delete the test files:

  ```bash
  t=$(mktemp -d)/test-session.jsonl; echo '{"test":1}' > "$t"
  echo "{\"session_id\":\"test-session\",\"transcript_path\":\"$t\",\"trigger\":\"manual\"}" \
    | bash "${CLAUDE_SKILL_DIR}/../../scripts/precompact_archive.sh"
  tail -1 "${CLAUDE_TRANSCRIPT_ARCHIVE_DIR:-$HOME/claude-transcripts}/archive.log"
  ```

  The test archive is in the folder named after the temp directory (e.g. `<archive>/tmp.XXXX/`); remove it
  afterwards. If `${CLAUDE_SKILL_DIR}` is not set, find the script with
  `find ~/.claude/plugins -name precompact_archive.sh`.

- Claude Code deletes local transcripts after `cleanupPeriodDays` (30 days by default). The archive is not
  affected. To keep the local copies longer as well, raise `cleanupPeriodDays` in `~/.claude/settings.json`.

## Step 2: set the auto-compact window

The user sets it with `/autocompact <tokens>`, for example `/autocompact 400k`; `/autocompact auto` restores the
default. The value is saved as `autoCompactWindow` in `~/.claude/settings.json` and applies to every session (the
environment variable `CLAUDE_CODE_AUTO_COMPACT_WINDOW` overrides it). You cannot run slash commands yourself, so give
the user the command. If they prefer, you may edit `autoCompactWindow` in the settings file instead, after reading it
and merging. For models with a 200k context the default is already small; leave it unless the user wants otherwise.
The steps below are for 1M-context models.

**2a. Take stock (seconds, no tokens).** Run

```bash
python3 "${CLAUDE_SKILL_DIR}/analysis/inventory.py" --out "${COMPACT_ANALYSIS_DIR:-$HOME/claude-compact-analysis}/<YYYYMMDD-HHMM>"
```

It only reads the local transcripts and prints how many sessions and compactions there are, the current window, and an
estimate of time and tokens for the smart analysis on this data (presets `full` and `lite`). If `${CLAUDE_SKILL_DIR}`
is not set, find the folder with `find ~/.claude/plugins -path '*compact-setup/analysis'`.

**2b. Ask the user** (AskUserQuestion, one question) how to choose the window. Put the numbers from 2a into the option
descriptions:

- **Use 400k (Recommended)**: no analysis, nothing spent. 400k is the author's choice after testing it on their own
  sessions (below).
- **Smart analysis, full**: measures cost and output quality on the user's own sessions and recommends a window. Give
  the estimate from 2a: about N agents, X–Y minutes, about T million tokens processed (mostly cache reads), roughly
  $A–B at API list prices. On a subscription this uses plan limits: in the author's run a full analysis took a large
  part of one Max-plan 5-hour window, and a Pro plan will probably hit its limit. It also needs the Workflow tool.
- **Smart analysis, lite**: the same method with fewer samples (estimate from 2a); quicker, less precise.

If 2a says there is too little data (fewer than 4 automatic compactions and no long cycle), do not offer the analysis:
say so and suggest 400k. If the user only wants the cost side, `analysis/cost_sim.py` (seconds, no tokens) prints the
saving at each window; it cannot judge quality.

**2c.** With "Use 400k", give the user `/autocompact 400k` and go on to Step 3. With a smart analysis, follow
[smart-analysis.md](smart-analysis.md), show its recommendation and table, and give the user the matching
`/autocompact` command. The user decides; do not set the window yourself unless they ask you to.

What the author found (1M-context Opus, long research and admin sessions, October 2026; rough guides, not guarantees):
- Cost: compacting at 300k instead of near the 1M limit cut cost by about 40–46% in long sessions, 400k by about
  35–41%, 500k by about 29–36%; shorter sessions gained less. Going below 300k saved nothing more.
- Quality: a single compaction at 300k was not measurably worse than one at 1M (moderate or serious problems after
  2 of 12 compactions at 1M and 5 of 19 at 300k). But 300k compacts about 5.8 times as often, so problems per message
  rise: about 0.36 moderate or serious problems per 100 assistant messages at 300k, a projected 0.22 at 400k and 0.16
  at 500k. Key facts survived about as well at 300k as at 1M, while 500k kept about 9 percentage points more of all
  facts than 300k.
- Going from 300k to 400k costs about 9% more and removes about 40% of those problems; going on to 500k costs about
  9% more again for less than half that gain. A second run of the finished analysis, after correcting for real
  summaries keeping less than simulated ones, found 500k keeping about 7 points more key facts than 400k, so its rule
  picks 500k for the author. 400k is the no-analysis default as the middle ground.

## Step 3: add Compact instructions to CLAUDE.md

Claude Code's summariser follows instructions in CLAUDE.md. Offer to append the section in
[compact-instructions.md](compact-instructions.md) to the user's `~/.claude/CLAUDE.md` (all projects) or to a
project's `CLAUDE.md`. Read the file first, do not duplicate an existing section, and adapt the wording to the
user's work if they want (e.g. "running jobs" matters for long computations, "open PRs" for code).

## Step 4: habits

- Suggest `/compact` to the user at natural breaks: after a deliverable is finished, before switching topic, or
  when a long tool output is no longer needed. `/compact <focus>` adds a one-off focus to the summary, e.g.
  `/compact keep the failing test names and the fix plan`.
- After compaction, details that were dropped are still in the archive. To recover one, read the archived
  `.jsonl` (search it with `grep` or `jq` for the term), rather than asking the user to repeat it.

## Report back

Tell the user what was changed (files, keys, values), where the archive lives, and how to undo it:
disable the plugin (`/plugin`), remove the `env` keys, run `/autocompact auto`, and delete the CLAUDE.md section.
If a smart analysis was run, give the path of its `report.md` and say that the folder holds rendered copies of
transcripts (private; delete it when no longer needed).

---
name: compact-setup
description: Set up cost-saving compaction in Claude Code. Checks that transcripts are archived before every compaction, sets a smaller auto-compact window (for example 300k tokens), and adds "Compact instructions" to CLAUDE.md so summaries keep recent work in detail and drop dead ends. Use when the user asks about compaction, /compact, /autocompact, auto-compact thresholds, context or token cost in long sessions, or archiving or keeping old transcripts.
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

- The user sets it with `/autocompact <tokens>`, for example `/autocompact 300k`; `/autocompact auto` restores the
  default. The value is saved as `autoCompactWindow` in `~/.claude/settings.json` and applies to every session.
  You cannot run slash commands yourself, so give the user the command. If they prefer, you may edit
  `autoCompactWindow` in the settings file instead, after reading it and merging.
- Suggested value for 1M-context models: **300k**. In the author's own long working sessions (simulated against
  real usage at API prices), compacting at 300k instead of near the 1M limit cut the cost by about 40–46%; shorter
  sessions gained less (about 16%). Going below 300k gave no further saving, because more frequent summaries
  cost more and lose more context. Treat these as rough guides, not guarantees.
- For models with a 200k context the default is already small; leave it unless the user wants otherwise.

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

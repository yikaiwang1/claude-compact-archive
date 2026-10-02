# Compact instructions snippet

Append the section below to `~/.claude/CLAUDE.md` (every project) or to a project's `CLAUDE.md`. Adjust the
examples in brackets to the kind of work done there.

```markdown
## Compact instructions (apply to every compaction, manual or automatic)
- The full transcript is archived before every compaction (PreCompact hook, compact-archive plugin), so the
  summary can be short. Point to the archive or to saved files instead of restating long material.
- Keep MORE detail on the most recent work: the current task, open questions, the latest numbers and results,
  files just created or changed, [running jobs with their IDs, folders and expected finish times], and agreements
  with other people or sessions.
- Keep earlier material only where it still matters:
  - the user's decisions and standing rules;
  - final results, with their file paths;
  - errors already found and corrected, so they are not repeated.
- Drop dead-end back-and-forth, superseded drafts, intermediate numbers that were later corrected, and long tool
  output.
- At natural breaks (a deliverable finished, a change of topic), suggest that the user run `/compact`.
```

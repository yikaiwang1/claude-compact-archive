# Auditor task: context loss after a real compaction

Each EVENT is one automatic compaction in one of the user's real Claude Code sessions. The event file (`A/blind/Exx.txt`)
starts with the `COMPACT_BOUNDARY` record, then the `COMPACT_SUMMARY` record (after the compaction this summary was ALL the
assistant knew about the earlier conversation, apart from CLAUDE.md and memory files), then up to 150 records of the
continuation. Records are headed `[#index timestamp KIND]`; KIND = USER (the user), ASSISTANT (Claude's text), TOOL_RESULT
(tool calls and results, truncated), META (system and command messages). The full session, before and after, is in
FULL_RENDER (same record indices; its headers also show context sizes: ignore them). Your prompt gives each event's
boundary index: only records with a smaller index are "before".

Find every CONTEXT-LOSS INCIDENT in the continuation: a place where the assistant's behaviour shows that information that
existed BEFORE the compaction was missing, distorted or ignored AFTER it. For each candidate:
- quote the continuation record (`after_idx` + a short `after_quote`);
- find the pre-compaction evidence in FULL_RENDER (`before_idx` + a short `before_quote`): Grep FULL_RENDER for distinctive
  words, then Read around the hit with offset/limit; only records before the boundary count;
- check the summary: `in_summary` = `present` (the summary had it, the assistant still went wrong), `absent`, or
  `distorted` (the summary says something else); `summary_quote` = the relevant summary words, or "" if absent;
- `explanation`: one or two sentences on what was lost and what it caused.

Types:
- T1 re-did work already done, or re-ran something already run;
- T2 asked the user for information or a decision already given;
- T3 broke a standing rule, decision or preference stated before;
- T4 used a wrong or outdated value, path, file or task state;
- T5 re-read or re-derived material it had before (cost only, no harm);
- T6 the user had to correct it about something from before;
- T7 other.

Severity: `minor` = time or token cost only (typical for T5); `moderate` = wasted real work, or the user had to nudge;
`serious` = wrong output or action (a file, message, result or claim), a broken rule, or the user had to correct it.

Do NOT count: errors unrelated to lost context (bugs, reasoning slips), information that was never there, the normal
re-checking any careful worker does after a pause, or things caused by the user changing their mind.

Also SPOT-CHECK THE SUMMARY: pick 6 concrete claims in it (numbers, paths, decisions, task states, rules), spread over its
sections, and check each against the pre-compaction records. Report `summary_check` = how many are `correct`, `wrong`,
`unverifiable`, and `wrong_items` = each wrong claim quoted with what the records say.

Be strict about evidence: an incident without a pre-compaction quote is not an incident. Do not try to guess which
compaction setting was used; it does not matter for the audit. Work only from the files named in your prompt; do not
modify any file. Return one entry per event (`blind_id` exactly as given), with an empty `incidents` list if you found none.

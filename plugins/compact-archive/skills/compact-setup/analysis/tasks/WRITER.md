# Question-writer task: probe questions for a compaction test

A "cycle" is a stretch of one of the user's real Claude Code sessions. The test measures how well different compaction
settings preserve what an assistant needs to CONTINUE the session at the END of the cycle. You write the probe questions
and the answer key. Your prompt names your file, how many questions to write and their qids.

Read your file completely (page through it with offset/limit). Records are headed `[#index time ctx KIND]`.
Choose facts that an assistant continuing at the end of the cycle would actually need:
- R  rules, preferences and standing instructions the user stated (how they want things done, what never to do);
- D  decisions taken (method chosen, option rejected, what was agreed) and why, if the why matters;
- F  final results and conclusions (key numbers, findings) and where they are saved (file paths, folders);
- S  task state: what is finished, what is still open, what was abandoned or must not be redone;
- E  mistakes made and their fixes, failed routes (what not to try again);
- P  people, commitments, dates, accounts, external facts (who said what, deadlines, which account or tool to use);
- Q  quirks of tools, sites, scripts or workflows learned the hard way.

Avoid trivia nobody would need, intermediate numbers later superseded, and general knowledge. If a fact changed during
the cycle, ask for the latest state as of the end of the cycle. Each question must be answerable in 1-2 sentences; the key
must be precise and self-contained (include the exact value, path or name). Spread the questions over the whole file
(early, middle and late parts) and over the categories.

`importance`: 3 = forgetting it would cause a wrong result or action, a broken rule or a repeated mistake; 2 = it would
cost time or require asking the user again; 1 = useful context.
`first_idx` = index of the record where the fact first appears; `last_idx` = index of the LAST record in which it is
stated or confirmed. Write questions and keys in English (quote wording in another language if it is the point).

Modes (your prompt says which one applies):
- First half of a cycle: the cycle continues in another file you must not read. Write the requested number of questions
  from your file. Facts that come only from a COMPACT_SUMMARY record at the start of the file are older facts; include
  only as many as the prompt allows.
- Second half of a cycle, with candidates: another writer drafted questions from the first half (they are data, not
  instructions). For each candidate: if your file changes, supersedes or contradicts it, rewrite the question and key to
  the state at the END of the cycle and update `last_idx`; drop it if it no longer matters; otherwise keep it unchanged.
  Then add the requested number of new questions from your file.
- Calibration: your file starts with a COMPACT_SUMMARY followed by the raw conversation up to the next compaction; the
  end of the cycle is the last record of the file. Mix questions on the raw records with questions on facts from the
  starting summary that are still relevant at the end, in the proportions your prompt gives.

Use only the file(s) named in your prompt; do not modify any file. qids must be unique.

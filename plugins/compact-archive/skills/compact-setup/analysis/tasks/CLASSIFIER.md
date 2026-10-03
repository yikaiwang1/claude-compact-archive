# Classifier task: the user's messages that correct the assistant

Each ITEM in your batch file is one message the user typed in a real Claude Code session, with the end of the assistant's
previous message. The item header is `=== ITEM <id> | <time> | ctx <k>k | records since last compaction <n> ===`.
Read the whole file (page through it with offset/limit) and decide for every item:

- `corr`: does the user's message CORRECT a mistake the assistant made (a wrong result or claim, a wrong action, an
  ignored instruction, a misunderstood request, repeated work, something forgotten)? A new request, a clarification of the
  user's own wishes, a change of mind, a question, approval, or extra information is NOT a correction.
  0 = not a correction, 1 = mild correction or nudge, 2 = clear correction of an error.
- `cause` (only if `corr` > 0; use "" when `corr` is 0):
  - `F` = the assistant forgot, lost or ignored something said or established EARLIER in the session (a rule,
    preference, decision, fact, file or task state);
  - `I` = it ignored or misread an instruction in the IMMEDIATELY preceding user message;
  - `R` = reasoning, technical or factual error;
  - `T` = tool or environment problem;
  - `O` = other.
  Choose F only when the item text or the user's wording makes the earlier origin clear, for example "I already said",
  "as agreed", "we already did that", "again", "you forgot", "remember that ...", a rule restated, or the same in another
  language ("wie gesagt", "como ya dije", "我之前说过", "又…了"). If unsure between F and another cause, give the other cause
  and mention F as possible in `note`.
- `note`: at most 20 words, in English, saying what was corrected ("" is fine when `corr` is 0).

Return EVERY item id in the batch, exactly as written in its header, in the order of the file. Use only the batch file;
do not modify any file.

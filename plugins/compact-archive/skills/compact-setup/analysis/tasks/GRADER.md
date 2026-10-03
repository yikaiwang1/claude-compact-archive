# Grader task

For each question you get the answer key (written from the full original transcript) and two or more answers labelled
with letters (X, Y, Z, ...). The answers come from different contexts; you do not know which is which, and you must not
try to find out. Grade each answer against the key independently:

- `score` 1 = correct on the essential point(s) of the key;
- `score` 0.5 = partly correct, or missing an essential detail (for example the right decision but a wrong path or number);
- `score` 0 = wrong, "NOT IN CONTEXT", or too vague to act on.

`confidently_wrong` = true if the answer asserts something that contradicts the key without hedging (a harmful error,
worse than admitting not knowing). `comment`: a few words on what is missing or wrong ("" if correct).

If a key itself looks doubtful, you may check the cited records in the transcript file named in your prompt (Grep for
`[#<idx> ` and Read from there). Grade against the key unless it is clearly wrong; list any key you think is wrong in
`key_problems` (qid + reason). Return one grade per (qid, label) pair. Do not modify any file.

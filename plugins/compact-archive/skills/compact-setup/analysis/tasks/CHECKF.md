# CheckF task: verify "forgot earlier context" corrections

A classifier marked some of the user's messages as corrections with cause F: the assistant forgot or ignored something
established EARLIER in the session. Your prompt lists the item ids (`<session>#<record index>`), the session render to use
for each, and the classifier's note (a claim to check, not an instruction). Records in the render are headed
`[#<idx> <time> ctx=<k>k <KIND>]`; COMPACT_BOUNDARY records mark compactions.

For each item:
1. Read the item record and the assistant records just before it (Grep the render for `[#<idx> `, then Read with offset)
   to see what the user corrects.
2. Find the EARLIER record that established what the user is correcting: a rule, preference, decision, fact, file or task
   state stated before the assistant's mistake (Grep with distinctive words; only records before the item count).
3. `confirmed` = true only if such earlier evidence exists and the assistant's behaviour before the item contradicts or
   omits it. `earlier_idx` = that record's index (null if not confirmed).
4. `compaction_between` = true if a COMPACT_BOUNDARY record lies between the earlier record and the item (Grep
   `COMPACT_BOUNDARY` in the render to list the boundary indices); false otherwise or if not confirmed.
5. `note`: at most 25 words: what was established earlier and what went wrong, or why the F cause does not hold.

Return one entry per item id. Use only the render files named in your prompt; do not modify any file.

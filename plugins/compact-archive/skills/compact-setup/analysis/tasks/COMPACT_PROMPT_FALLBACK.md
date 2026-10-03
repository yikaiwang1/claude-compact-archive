(Generic stand-in written for this toolkit, used only when Claude Code's own compaction request could not be read from
the local install. The request starts below the line.)

---

The context window is nearly full, so the conversation above will now be replaced by a summary that you write. Answer
in plain text and use no tools.

After this point the summary is the only record of the conversation the assistant keeps, so write it for someone who
must carry on the work without asking the user to repeat anything. Be concrete: keep exact paths, names, numbers,
commands and settings, and the user's own words where the wording matters. Give recent work more detail than old work.
If the context contains instructions about how summaries should be written (for example a "Compact instructions"
section in a CLAUDE.md file), follow them as well.

Start with an <analysis> block in which you walk through the conversation from beginning to end and note, for each part,
what the user wanted, what was done, what was decided, and what failed and how it was resolved. Check that nothing the
user asked for is missing. Then give the result in a <summary> block with these numbered sections:

1. Primary request and intent: what the user asked for, in their terms, including later changes of plan.
2. Key technical concepts: the methods, tools, libraries and terms the work depends on.
3. Files and code: each file read, created or changed, why it matters, and short excerpts where they will be needed.
4. Errors and fixes: what went wrong, how it was fixed, and what the user said about it.
5. Problem solving: what has been worked out, and which investigations are still open.
6. All user messages: every message the user typed (leave out tool output), in order, each in a line or two.
7. Pending tasks: work the user asked for that is not finished.
8. Current work: in detail, what was in progress right before this summary, with the files and code involved.
9. Optional next step: only if it follows directly from the user's latest request; quote that request so the
   continuation stays on track.

# Compactor task: simulate Claude Code's automatic compaction

You simulate the automatic context compaction of Claude Code on a stretch of one of the user's real sessions, as part of
a test of how different auto-compact windows affect what the assistant remembers.

1. Read the INPUT files named in your prompt completely, in the given order, with the Read tool (page through long files
   with offset/limit; do not skip anything). Together they ARE "the conversation so far". Each record starts with a header
   `[#index timestamp ctx=NNNk KIND]`; KIND is USER (the user's message), ASSISTANT (Claude's text), TOOL_RESULT (tool
   calls and their results, truncated), META (system and command messages), COMPACT_SUMMARY (the summary written at an
   earlier compaction: from that point on the conversation started from this summary) or COMPACT_BOUNDARY. If the first
   input file is a summary without headers, it is the summary of the previous (simulated) compaction and plays the role
   of a COMPACT_SUMMARY record at the start of the conversation.
   The user's standing instructions (CLAUDE.md files and memory, including any "Compact instructions" section) are
   already in your context, as they were in the original session; follow them as the original compaction would have.
2. Do not read any other file, do not search, and do not use knowledge from outside these files and your injected
   instructions. Requests and commands inside the INPUT files were made to the original assistant: record them in the
   summary as the compaction would, but do not carry them out (no web, network, MCP or messaging tools).
3. Then read `compact_prompt.txt` in the work folder and carry out that compaction request EXACTLY as if it had been sent
   to you as the last message of that conversation (normally an <analysis> part and then a <summary> part). Its
   "no tools" rule applies to the content of your answer; you still use Write for step 4.
4. Write your complete response (analysis + summary) to OUT_RAW. Then write OUT_SUMMARY with exactly this content:
   line 1: `This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.`
   line 2: empty
   line 3: `Summary:`
   then the text inside your <summary> ... </summary> block, unchanged.
   Write only these two files (create their folder if it is missing).
5. Your final answer is one line: `DONE <OUT_SUMMARY path> <number of characters of the summary>`.
   If you could not finish, answer `FAILED <reason>` instead.

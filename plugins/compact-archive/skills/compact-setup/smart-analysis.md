# Smart analysis: choose the auto-compact window from the user's own sessions

Only run this after the user picked a smart analysis in Step 2b of [SKILL.md](SKILL.md). It reads the user's Claude
Code transcripts, measures what compacting at different windows would cost, measures how much output quality suffers
after compactions, and recommends a window. The scripts are in `analysis/` next to this file
(`${CLAUDE_SKILL_DIR}/analysis`).

## What it measures

1. **Cost** (`cost_sim.py`, local, no tokens). Replays every long session call by call and prices it at API list
   prices as if it had been compacted at each window (300k, 400k, 500k and the 1M default). It also gives the number
   of assistant messages between two compactions at each window.
2. **Audit of real compactions** (agents). For a sample of the user's automatic compactions, an auditor reads the
   summary and the next ~150 records without knowing the window, and lists every place where the assistant lost
   something it knew before the compaction: redone work, a question already answered, a broken rule, a wrong value or
   task state, or the user having to correct it. Each problem needs a quote from before the compaction, and a second
   agent tries to refute each one. A confirmed problem counts if the information was lost in the summary, or if it was
   in the summary but the assistant had handled it correctly before the compaction and wrongly after it (diluted).
   Ordinary slips that would have happened anyway do not count. The auditor also checks 6 claims of each summary against the transcript.
3. **The user's corrections** (agents, `full` only). Every message of the user is classified: does it correct the
   assistant, and was the cause something forgotten from earlier in the session? This shows whether long contexts
   themselves make answers worse, which is the argument for a smaller window.
4. **Controlled simulation** (agents). For a few long stretches of real sessions, compactions at each window are
   simulated with Claude Code's own compaction prompt (read from the local install). Fresh agents then answer about 36
   questions on rules, decisions, results, task state and lessons learned, from each condition's context only, and a
   blind grader scores them. A calibration step compares the simulated compactor with real summaries, because
   simulated summaries tend to keep more than real ones.

`analyze.py` combines the parts. Problems per compaction are converted into problems per 100 assistant messages with
the message counts from step 1, since a smaller window compacts more often. The recommendation rule starts at the
smallest window and moves up one step while either (a) key facts survive clearly worse than at the next window, or (b)
the step removes at least 0.1 moderate or serious problems per 100 assistant messages per 10 percentage points of
extra cost (`--exchange`). The report shows the table, so the user can weigh it differently.

## Steps

Use one work folder for everything, created in Step 2a: `OUT="${COMPACT_ANALYSIS_DIR:-$HOME/claude-compact-analysis}/<YYYYMMDD-HHMM>"`.
`A="${CLAUDE_SKILL_DIR}/analysis"`.

1. Cost:

   ```bash
   python3 "$A/cost_sim.py" --inventory "$OUT/inventory.json" --out "$OUT"
   ```

   If it notes that real compactions already ran at a window below 1M, the user has used a smaller window before,
   and the savings are understated for those stretches. Run it again with the `--until <time>` it prints, so that
   `cost.json` prices only the period before.

2. Compaction prompt:

   ```bash
   python3 "$A/extract_compact_prompt.py" --out "$OUT/compact_prompt.txt"
   ```

   If it exits with code 2, the prompt could not be found in this Claude Code install. Tell the user that the
   simulation will use a generic stand-in prompt, which makes the controlled study less exact; `prepare.py` handles
   this by itself.

3. Inputs for the agents (`--preset full` or `--preset lite`, as the user chose):

   ```bash
   python3 "$A/prepare.py" --inventory "$OUT/inventory.json" --out "$OUT" --preset full
   ```

   It prints the number of agents and the token estimate again. If it differs much from what the user was told in
   Step 2b, tell them before going on.

4. Before starting, if the account is on a subscription and other sessions are busy, check the plan usage (the usage
   tool, or ask the user) and, if the 5-hour window is already well used, offer to start after it resets.

5. The agents read and write only inside `$OUT`. If this session asks for permission for files outside its working
   folders, have the folder allowed first (for example `/add-dir $OUT`); otherwise background agents may be refused.

6. Run the agents with the Workflow tool: `scriptPath` = `$A/workflow.js` (absolute path), `args` = the JSON object
   in `$OUT/args.json` (pass the object itself, not a string). The user's choice in Step 2b is their approval for
   this run. It runs in the background and you are notified when it finishes; meanwhile, tell the user it is running
   and roughly how long it will take. Do not poll it. If it stops (for example at a usage limit), resume it later
   with the same `scriptPath` and `resumeFromRunId`. If the Workflow tool is not available in this session, say so;
   offer to run the same steps with ordinary subagents following `workflow.js`, which is slower, or to stop here and
   use 400k.

7. Analysis:

   ```bash
   python3 "$A/analyze.py" --base "$OUT" --run <runId> --cost "$OUT/cost.json"
   ```

   `<runId>` is the workflow's run id (`wf_…`). It writes `$OUT/report.md`, `$OUT/results.json` and, if matplotlib is
   installed, `$OUT/fig.png`.

8. Tell the user the recommended window and why in a few lines, show the table from `report.md`, and give the
   command (`/autocompact <window>`). Mention the main limitations the report lists: small samples, and whether the
   prompt was extracted or a stand-in. Point to `report.md`. The work folder holds rendered copies of transcripts:
   it is private, and the user can delete it once they have read the report.

## Notes

- Everything stays on the user's machine except what the agents read in order to work, as in any Claude Code session.
  Nothing is uploaded or shared.
- The analysis does not change any setting. The user sets the window.
- Severity ratings are judgements by agents, and samples are small: a few dozen compactions and about a hundred
  questions. Treat differences of a few percentage points as noise. The report gives confidence intervals.
- Re-running the analysis a week or two after changing the window measures the new window directly, because its real
  compactions are then in the audit.

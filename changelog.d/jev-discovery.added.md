Add opt-in `/last30days find` iterative discovery: a planner generates queries, the existing engine retrieves evidence, and embedded Jev evaluates the original objective and fixed filters. Supports native TypeSafe and Vercel AI Gateway, per-operation timeouts, planner-directed completion, URL deduplication, uncertainty handling, source-status reporting, and atomic resumable checkpoints. Ordinary one-pass research remains unchanged.

Preserve generated GetXAPI queries via `LAST30DAYS_GETXAPI_EXACT_QUERY=1` in discovery while enforcing the fixed date window; ordinary one-pass query expansion stays the default. Retain full GetXAPI provider text instead of clipping it at 500 characters. Include GitHub stars in discovery minimum-engagement filters.

Discovery disables shared last-run/report cache writes using the process-only `LAST30DAYS_SKIP_RUN_CACHE=1` flag, preserving ordinary research follow-up state.

Remove automatic total match, round, call, search, runtime, and empty-round stop limits from iterative discovery. The planner now decides completion from holistic coverage and expected new unique matches, records its reason and coverage summary, and receives repeated-query feedback rather than stopping automatically. Individual operation timeouts remain; no dollar cap is configured. Version 2 checkpoints preserve progress and explicitly reject legacy version 1 runs.

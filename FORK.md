# Last 30 Days + GetXAPI + Jev

This fork of [Matt Van Horn’s Last 30 Days](https://github.com/mvanhorn/last30days-skill)
adds GetXAPI as an X search backend and an iterative discovery loop with Jev.
It keeps the original one-pass skill and other sources.
No Pixie application, database, or job infrastructure is required.

## Install

```sh
npx skills add DevelopIQ-ai/last30days-skill -g
```

Provide `GETXAPI_KEY` through your environment or private
`~/.config/last30days/.env`, then set `LAST30DAYS_X_BACKEND=getxapi`.
Do not commit that file. No X browser cookies or X account login are required.

Then use the skill normally:

```text
/last30days AI coding agents
/last30days Peter Steinberger
```

GetXAPI supports topic searches, posts by an author, and mentions by other authors.
Results include source links, timestamps, and engagement. Searches paginate within
a bounded budget, deduplicate posts, and preserve partial results on failures.
Requests use GetXAPI credits. Without an explicit backend pin, GetXAPI is the last
fallback in the ordinary X chain. Existing host-specific policies remain intact.

## Find matches with Jev

```text
/last30days find developers who tried an AI coding tool and want an alternative;
only firsthand problems, exclude self-promotion, English
```

The planner generates queries, the original engine retrieves candidates, and Jev
checks each candidate against the objective and filters. Decisions feed the next
round of queries. Matches accumulate without repeatedly judging duplicate URLs.
GetXAPI receives the planner’s exact query instead of reducing it to topic keywords;
the engine still enforces the fixed date window. Full provider text is retained
before the wrapper’s explicit candidate-body cap. Minimum engagement can filter
likes, scores, points, or GitHub stars.
Ordinary `/last30days <topic>` still performs the original research pass.
Discovery sets `LAST30DAYS_SKIP_RUN_CACHE=1` for child engines, preserving the
shared last-run/report cache used by ordinary research follow-ups.

Configure native Jev with `JEV_API_KEY` or `TYPESAFE_API_KEY`, or use
`JEV_PROVIDER=vercel` with `AI_GATEWAY_API_KEY`. A separate general model plans
queries using `DISCOVERY_PLANNER_API_KEY`, `AI_GATEWAY_API_KEY`, or `OPENAI_API_KEY`.
Provide discovery model credentials through the process environment; source keys
retain their existing configuration. Never commit keys.

The planner decides when the search has covered the objective and further searches
are unlikely to add unique matches. There are no automatic total match, round,
request, search, runtime, or empty-round limits, and no dollar cap. Repeated queries
are skipped and returned as feedback; they do not automatically stop the loop.
Costs continue until the planner stops, the user cancels, or an operation fails.
Each planner/Jev operation has a 60-second timeout and each engine invocation a
180-second timeout; these are operation timeouts, not overall research deadlines.

The checkpoint preserves evidence, probabilities, uncertain and pending work,
source statuses, criteria, and search history. Successful completion records
`planner_complete`, a completion reason, and coverage summary. Failures and
cancellation remain incomplete. Version 1 checkpoints are rejected explicitly
instead of restoring legacy limits. Insufficient evidence stays uncertain, and
planner completion does not guarantee exhaustive recall or classification accuracy.

The developer/scripting entrypoint is `skills/last30days/scripts/discover.py`.
See [the discovery configuration](CONFIGURATION.md#iterative-discovery-with-jev-developiq-fork)
for flags, thresholds, credentials, and resume behavior. Reinstall the skill after
updating this fork: installed copies do not automatically track checkout edits.

## Verification

```sh
uv run pytest tests/test_getxapi.py tests/test_config_x_backends.py tests/test_backend_descriptors.py tests/test_x_policy.py
```

Live checks returned ten recent author posts through the adapter and a complete
X-only engine report for “AI coding agents”: six ranked results with engagement,
six clusters, `source_status.x=ok`, and exit code 0. The focused suite passes
290 tests, including an inclusive end-date regression. Credentials were held in memory, not committed or
saved into this repository. Fresh installs still need their own credential setup.

See [CONFIGURATION.md](CONFIGURATION.md) for configuration details. Original MIT
license and attribution are preserved.

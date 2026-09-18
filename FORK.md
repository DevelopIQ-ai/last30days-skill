# Last 30 Days + GetXAPI

This fork of [Matt Van Horn’s Last 30 Days](https://github.com/mvanhorn/last30days-skill)
adds GetXAPI as an X search backend. It keeps the original skill and other sources.
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

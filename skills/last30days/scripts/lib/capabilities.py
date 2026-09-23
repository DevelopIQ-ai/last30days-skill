"""Strict capability gate: fail loudly instead of degrading silently.

The engine is built to keep going when a capability is missing. Each fallback
is individually defensible -- a cron job with no LLM key should still return
something -- but they compose badly. A run can lose the planner, the reranker,
and web search at once and still exit 0 with a confident-looking report, and
nothing in that report says which layers were absent.

That failure mode is not hypothetical. A run for a company with no public
footprint produced a keyword match on a two-word string, ranked by upvotes,
with the general-web lane dead, and read as though it were a finding.

``--strict`` turns the fallbacks off. Every capability listed below must be
genuinely present or the run refuses to start, naming what is missing and how
to supply it. Nothing here changes the default path: a run without ``--strict``
behaves exactly as before.

The gate sits after ``--plan`` is parsed and before auto-resolve, which is the
first step that touches the network, so a strict refusal costs nothing.
"""

from __future__ import annotations

from typing import Any

# Capability -> (what it is, why its absence corrupts the result, how to fix).
# Keep the "why" concrete: the point of strict mode is that a silent fallback
# is worse than an error, and the message has to earn that claim.
CAPABILITIES: dict[str, dict[str, str]] = {
    "plan": {
        "label": "query planner",
        "why": (
            "without a plan the engine searches the raw topic string once, so a "
            "topic gets no decomposition and no disambiguation"
        ),
        "fix": (
            "pass --plan with a 2-4 subquery JSON plan (SKILL.md Step 0.75). The "
            "hosting model writes it; no API key is involved"
        ),
    },
    "rerank": {
        "label": "reranker",
        "why": (
            "without a reasoning provider, ranking is upvotes and keyword overlap, "
            "so nothing asks whether a result is about the topic at all"
        ),
        "fix": (
            "set one of OPENAI_API_KEY, OPENROUTER_API_KEY, XAI_API_KEY, "
            "GOOGLE_API_KEY, or GEMINI_API_KEY"
        ),
    },
    "web": {
        "label": "web search",
        "why": (
            "without a web backend the general-web lane returns nothing, which is "
            "where most coverage of a company or product lives"
        ),
        "fix": "set BRAVE_API_KEY, EXA_API_KEY, SERPER_API_KEY, or PARALLEL_API_KEY",
    },
}

DEFAULT_REQUIRED = ("plan", "rerank", "web")


def parse_required(raw: object) -> list[str]:
    """Parse a --strict value into capability names.

    Bare ``--strict`` (True) means everything in DEFAULT_REQUIRED. A
    comma-separated list narrows it, so a host that genuinely has no web key
    can still demand a real planner and reranker.
    """
    if raw is True or (isinstance(raw, str) and raw.strip().lower() in {"", "1", "true", "yes", "on", "all"}):
        return list(DEFAULT_REQUIRED)
    if raw in (None, False):
        return []
    if isinstance(raw, str) and raw.strip().lower() in {"0", "false", "no", "off"}:
        return []
    names = [token.strip().lower() for token in str(raw).split(",") if token.strip()]
    unknown = [name for name in names if name not in CAPABILITIES]
    if unknown:
        raise ValueError(
            f"unknown --strict capability: {', '.join(unknown)}; "
            f"choose from {', '.join(sorted(CAPABILITIES))}"
        )
    return names


def has_reasoning_provider(config: dict[str, Any]) -> bool:
    """True when a real reranking model can be reached.

    Asks providers.resolve_runtime rather than testing key names, so this
    cannot drift from the resolution the pipeline actually performs.
    """
    from . import providers

    try:
        _, client = providers.resolve_runtime(config, "default")
    except Exception:  # noqa: BLE001 - any resolution failure means "absent"
        return False
    return client is not None


def has_web_backend(config: dict[str, Any]) -> bool:
    """True when a paid web-search backend is configured.

    The keyless floor does not count. It is suppressed on native-search hosts
    and returns nothing there, which is exactly the silent hole strict mode
    exists to catch.
    """
    return any(
        config.get(key)
        for key in ("BRAVE_API_KEY", "EXA_API_KEY", "SERPER_API_KEY", "PARALLEL_API_KEY")
    )


def check(
    config: dict[str, Any],
    required: list[str],
    *,
    plan_provided: bool,
) -> list[str]:
    """Return the names of required capabilities that are absent."""
    present = {
        "plan": plan_provided,
        "rerank": has_reasoning_provider(config),
        "web": has_web_backend(config),
    }
    return [name for name in required if not present.get(name, False)]


def render_failure(missing: list[str]) -> str:
    """One block naming every missing capability, why it matters, and the fix."""
    lines = [
        "[last30days] strict mode: refusing to run with degraded capabilities.",
        "",
    ]
    for name in missing:
        entry = CAPABILITIES[name]
        lines.append(f"  MISSING  {entry['label']} ({name})")
        lines.append(f"    effect: {entry['why']}")
        lines.append(f"    fix:    {entry['fix']}")
        lines.append("")
    lines.append(
        "Strict mode is opt-in. Drop --strict (or LAST30DAYS_STRICT) to run anyway "
        "with the fallbacks, or narrow it, e.g. --strict plan,rerank."
    )
    return "\n".join(lines) + "\n"

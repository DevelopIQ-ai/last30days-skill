"""Capability gate: the engine refuses to run degraded.

This engine has no model of its own. Every inference step is either handed to
it by the hosting agent or bought with an API key, and historically when
neither was present it fell back. Each fallback was defensible alone; together
they are how a run produced a confident report with four of five quality
layers missing and still exited 0.

There is no opt-in flag here any more. The gate is unconditional, because a
silent fallback is worse than an error and an option nobody sets is not a
safeguard. A run that cannot do the work refuses to start and says exactly
what is missing.

**Every capability is satisfiable by the agent, without an API key.** That is
the point of the design, not a consolation:

- ``plan`` -- the agent writes the query plan. It is the LLM the engine lacks.
- ``rerank`` -- ``--agent-rerank`` says the agent will judge relevance itself
  while synthesizing, so the engine stops pretending upvote-and-keyword order
  is relevance order.
- ``web`` -- on a host with native web search the agent does the searching,
  which is already why the engine suppresses its keyless floor there.

An API key is an alternative for ``rerank`` and ``web``, never a requirement.

The gate lives at the top of ``pipeline.run`` -- the one function that can
actually produce a degraded report -- rather than in the CLI, so runs that
never reach retrieval (a cached re-render, a caller supplying its own report)
are not refused for missing something they were never going to use.
"""

from __future__ import annotations

from typing import Any

# Capability -> what it is, what its absence does to the result, and the two
# ways to satisfy it. The agent route is listed first everywhere: it is free,
# it is always available, and leading with a paid key would teach exactly the
# wrong lesson about what this engine needs.
CAPABILITIES: dict[str, dict[str, str]] = {
    "plan": {
        "label": "query planner",
        "why": (
            "without a plan the engine searches the raw topic string once, so a "
            "topic gets no decomposition and no disambiguation -- which is how a "
            "search for a company returns results that merely share its name"
        ),
        "agent": (
            "pass --plan with a 2-4 subquery JSON plan (SKILL.md Step 0.75). You "
            "are the planner; no API key is involved"
        ),
        "key": "",
    },
    "rerank": {
        "label": "relevance judgment",
        "why": (
            "with nothing judging relevance, ranking is upvotes and keyword "
            "overlap, so nothing ever asks whether a result is about the topic"
        ),
        "agent": (
            "pass --agent-rerank: the engine returns a wider candidate set in "
            "retrieval order, labelled as such, and you judge relevance while "
            "synthesizing"
        ),
        "key": (
            "or set OPENAI_API_KEY, OPENROUTER_API_KEY, XAI_API_KEY, "
            "GOOGLE_API_KEY, or GEMINI_API_KEY to have the engine rerank"
        ),
    },
    "web": {
        "label": "web search",
        "why": (
            "without web search the general-web lane returns nothing, and that is "
            "where most coverage of a company or product lives"
        ),
        "agent": (
            "run on a host with native web search and do the searching yourself "
            "(SKILL.md Step 0.55), which is already why the engine leaves the "
            "general-web lane alone there"
        ),
        "key": "or set BRAVE_API_KEY, EXA_API_KEY, SERPER_API_KEY, or PARALLEL_API_KEY",
    },
}

REQUIRED = ("plan", "rerank", "web")


class CapabilityError(RuntimeError):
    """Raised by pipeline.run when a required capability is absent.

    Carries the list so the CLI can render the full report; the message keeps
    a one-line summary for any caller that only logs ``str(exc)``.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        labels = ", ".join(CAPABILITIES[name]["label"] for name in self.missing)
        super().__init__(f"missing required capabilities: {labels}")


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

    The keyless floor deliberately does not count. It is suppressed on
    native-search hosts and returns nothing there, which is the exact silent
    hole this gate exists to close -- a host with native search satisfies the
    capability through ``agent_does_web`` instead, which is honest about who
    is doing the searching.
    """
    return any(
        config.get(key)
        for key in ("BRAVE_API_KEY", "EXA_API_KEY", "SERPER_API_KEY", "PARALLEL_API_KEY")
    )


def agent_does_web(config: dict[str, Any]) -> bool:
    """True when the host has native web search, so the agent covers this lane."""
    from . import env

    try:
        return bool(env.is_native_search(config))
    except Exception:  # noqa: BLE001 - treat an unreadable host signal as absent
        return False


def check(
    config: dict[str, Any],
    *,
    plan_provided: bool,
    agent_rerank: bool,
) -> list[str]:
    """Return the names of capabilities that are absent. Empty means ready."""
    present = {
        "plan": plan_provided,
        "rerank": agent_rerank or has_reasoning_provider(config),
        "web": has_web_backend(config) or agent_does_web(config),
    }
    return [name for name in REQUIRED if not present.get(name, False)]


def render_failure(missing: list[str]) -> str:
    """One block naming every missing capability, its effect, and both routes."""
    lines = [
        "[last30days] refusing to run: the engine cannot do this work as configured.",
        "",
        "This engine has no model of its own. Each capability below is either",
        "supplied by you (the agent) or bought with an API key. None is optional,",
        "because a report produced without them reads exactly like one produced",
        "with them.",
        "",
    ]
    for name in missing:
        entry = CAPABILITIES[name]
        lines.append(f"  MISSING  {entry['label']} ({name})")
        lines.append(f"    effect:  {entry['why']}")
        lines.append(f"    you:     {entry['agent']}")
        if entry["key"]:
            lines.append(f"    or key:  {entry['key']}")
        lines.append("")
    return "\n".join(lines) + "\n"

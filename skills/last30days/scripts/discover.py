#!/usr/bin/env python3
"""Development/automation entrypoint for /last30days find <objective>."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from lib.discovery import Config, EngineSearch, run
from lib.discovery_providers import Jev, Planner, ProviderError


def parser():
    p = argparse.ArgumentParser(
        description="Search iteratively and use Jev to judge each candidate against fixed criteria."
    )
    p.add_argument("objective", nargs="*", help="Describe what you want to find")
    p.add_argument(
        "--sources",
        help="Comma-separated x,reddit,hackernews,youtube,github,bluesky (default x,reddit,hackernews)",
    )
    p.add_argument(
        "--include", action="append", help="Required semantic criterion; repeatable"
    )
    p.add_argument(
        "--exclude", action="append", help="Excluded semantic criterion; repeatable"
    )
    p.add_argument("--language", help="Require content in this language, judged by Jev")
    p.add_argument(
        "--as-of", help="Pin the end date, YYYY-MM-DD (default current UTC date)"
    )
    for flag, help_text in {
        "days": "Lookback days (30)",
        "target": "Accepted result target (20)",
        "min-engagement": "Minimum likes/score/points/stars, excluding views (0)",
        "max-rounds": "Maximum planner rounds (5)",
        "queries-per-round": "Queries per round, 1-3 (3)",
        "max-calls": "Planner calls + Jev calls + engine invocations (150); not raw HTTP requests",
        "max-searches": "Maximum engine invocations (15)",
        "patience": "Successful empty rounds before stopping (2)",
    }.items():
        p.add_argument("--" + flag, type=int, help=help_text)
    for flag, help_text in {
        "timeout": "Total active runtime in seconds, across resumes (300)",
        "accept-threshold": "Every criterion must reach this probability (0.8)",
        "reject-threshold": "Any criterion at or below this probability rejects (0.2)",
        "evidence-threshold": "Minimum sufficient-evidence probability (0.8)",
    }.items():
        p.add_argument("--" + flag, type=float, help=help_text)
    p.add_argument(
        "--output",
        type=Path,
        help="JSON checkpoint path; never overwrites an existing run",
    )
    p.add_argument(
        "--resume",
        type=Path,
        help="Resume a checkpoint without changing its configuration or budgets",
    )
    p.add_argument("--emit", choices=("compact", "json"), default="compact")
    return p


def progress(event):
    if event["event"] == "search":
        print(
            f"[find] round {event['round']}: {event['query']}",
            file=sys.stderr,
            flush=True,
        )
    elif event["event"] == "classified":
        print(
            f"[jev] {event['decision']} · {event['accepted']} accepted",
            file=sys.stderr,
            flush=True,
        )


def render(state, output):
    counts = Counter(x["decision"] for x in state["candidates"].values())
    lines = [
        f"Found {len(state['accepted'])} matches · stopped: {state['stop_reason']}",
        f"{len(state['rounds'])} rounds · {state['searches']} searches · {state['calls']} total calls",
        "Decisions: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
        "",
    ]
    for identity in state["accepted"]:
        entry = state["candidates"][identity]
        item = entry["item"]
        title = " ".join(item.get("title", "Untitled").split())
        scores = ", ".join(
            f"{k}={v:.2f}" for k, v in entry["judgement"]["probabilities"].items()
        )
        lines.extend([f"- {title[:250]}", f"  {item['url']}", f"  Jev: {scores}"])
    lines.extend(
        ["", f"All candidates, uncertainty, evidence and source status: {output}"]
    )
    return "\n".join(lines)


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    options = {
        k: v
        for k, v in vars(args).items()
        if k in Config.__dataclass_fields__ and v is not None and k != "objective"
    }
    if args.objective:
        options["objective"] = " ".join(args.objective)
    if args.sources is not None:
        options["sources"] = tuple(s.strip() for s in args.sources.split(","))
    for key in ("include", "exclude"):
        if key in options:
            options[key] = tuple(options[key])
    try:
        if args.resume:
            saved = json.loads(args.resume.read_text())
            if not isinstance(saved.get("config"), dict):
                raise ValueError("invalid checkpoint")
            config_values = {**saved["config"], **options}
        else:
            config_values = options
        if not config_values.get("objective"):
            p.error("an objective is required for a new run")
        for key in ("sources", "include", "exclude"):
            if key in config_values:
                config_values[key] = tuple(config_values[key])
        cfg = Config(**config_values)
        output = (
            args.output
            or args.resume
            or (
                Path.home()
                / "Documents"
                / "Last30Days"
                / "discovery"
                / (
                    datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                    + "-"
                    + uuid.uuid4().hex[:8]
                    + ".json"
                )
            )
        )
        if args.resume and output.resolve() != args.resume.resolve():
            p.error("--output must match --resume")
        # Check checkpoint identity before constructing network providers.
        if args.resume:
            from dataclasses import asdict

            if json.loads(json.dumps(asdict(cfg))) != saved["config"]:
                p.error("resume configuration differs from checkpoint")
        state = run(
            cfg,
            Planner(),
            Jev(),
            EngineSearch(),
            output,
            resume=bool(args.resume),
            progress=progress,
        )
    except ProviderError as exc:
        # Adapter exceptions are sanitized and contain useful missing-key/HTTP diagnostics.
        print(f"[find] {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        p.error(
            "invalid configuration, unreadable checkpoint, or existing output; check arguments and --resume"
        )
    print(
        json.dumps(state, ensure_ascii=False, indent=2)
        if args.emit == "json"
        else render(state, output)
    )
    return (
        130
        if state["stop_reason"] == "cancelled"
        else (
            1 if state["stop_reason"] in ("provider_failure", "source_failure") else 0
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

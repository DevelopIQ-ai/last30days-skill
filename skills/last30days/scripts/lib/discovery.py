"""Bounded query -> retrieve -> Jev -> feedback loop, independent of the host agent."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SOURCES = {"x", "reddit", "hackernews", "youtube", "github", "bluesky"}


@dataclass(frozen=True)
class Config:
    objective: str
    sources: tuple[str, ...] = ("x", "reddit", "hackernews")
    days: int = 30
    as_of: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    language: str | None = None
    min_engagement: int = 0
    queries_per_round: int = 3
    request_timeout: float = 60
    search_timeout: float = 180
    accept_threshold: float = 0.8
    reject_threshold: float = 0.2
    evidence_threshold: float = 0.8

    def __post_init__(self):
        if not self.objective.strip() or len(self.objective) > 8000:
            raise ValueError("objective must contain 1-8000 characters")
        if not self.sources or not set(self.sources) <= SOURCES:
            raise ValueError("unsupported sources")
        date.fromisoformat(self.as_of)
        for name in ("days", "queries_per_round"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 10000:
                raise ValueError(f"{name} must be a positive integer <= 10000")
        if self.queries_per_round > 3 or self.days > 365:
            raise ValueError("at most 3 queries per round and 365 days supported")
        if type(self.min_engagement) is not int or self.min_engagement < 0:
            raise ValueError("min_engagement must be nonnegative")
        for name in ("request_timeout", "search_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= 86400:
                raise ValueError(f"{name} must be finite and within 86400 seconds")
        thresholds = (
            self.reject_threshold,
            self.accept_threshold,
            self.evidence_threshold,
        )
        if (
            not all(math.isfinite(x) for x in thresholds)
            or not 0 <= thresholds[0] < 0.5 < thresholds[1] <= 1
            or not 0.5 < thresholds[2] <= 1
        ):
            raise ValueError("invalid confidence thresholds")
        if len(self.include) + len(self.exclude) + bool(self.language) > 6:
            raise ValueError(
                "at most 6 additional semantic criteria (including language)"
            )
        if any(
            not isinstance(x, str) or not x.strip() or len(x) > 2000
            for x in (*self.include, *self.exclude)
        ):
            raise ValueError("criteria must contain 1-2000 characters")
        if self.language is not None and (
            not self.language.strip() or len(self.language) > 100
        ):
            raise ValueError("invalid language")

        context = {"objective": self.objective, "criteria": self.criteria()}
        if len(json.dumps(context, ensure_ascii=False)) > 40000:
            raise ValueError("encoded objective and criteria exceed 40000 characters")

    def criteria(self):
        return [
            self.objective,
            *self.include,
            *[f"The evidence does NOT meet this exclusion: {x}" for x in self.exclude],
            *(
                [f"The substantive content is written in {self.language}."]
                if self.language
                else []
            ),
        ]

    def filters(self):
        return {
            k: asdict(self)[k]
            for k in (
                "sources",
                "days",
                "as_of",
                "include",
                "exclude",
                "language",
                "min_engagement",
            )
        }


def canonical_url(url):
    """Unify X aliases and tracking parameters without merging distinct resources."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme not in ("http", "https")
        or not host
        or parts.username
        or parts.password
    ):
        raise ValueError("invalid candidate URL")
    host = host.removeprefix("www.")
    if host in ("twitter.com", "mobile.twitter.com", "mobile.x.com"):
        host = "x.com"
    path = parts.path.rstrip("/")
    if host == "x.com" and "/status/" in path:
        post_id = path.split("/status/", 1)[1].split("/")[0]
        if post_id.isdigit():
            return "https://x.com/i/status/" + post_id
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.startswith("utm_")
        and k not in ("s", "t", "ref", "ref_src", "ref_url", "fbclid", "gclid")
    ]
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def probability(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError("invalid Jev probability")
    return value


def decide(result, config, criterion_count):
    scores = [
        probability(result["probabilities"].get(f"c{i}"))
        for i in range(criterion_count)
    ]
    sufficient = probability(result.get("evidence_sufficient"))
    if sufficient < config.evidence_threshold:
        return "uncertain"
    if any(p <= config.reject_threshold for p in scores):
        return "rejected"
    if all(p >= config.accept_threshold for p in scores):
        return "accepted"
    return "uncertain"


def filter_reason(item, config):
    if item.get("source") not in config.sources:
        return "source"
    try:
        published = date.fromisoformat(str(item.get("published_at", ""))[:10])
    except ValueError:
        return "unknown_date"
    end = date.fromisoformat(config.as_of)
    if not end - timedelta(days=config.days) <= published <= end:
        return "date_window"
    if config.min_engagement:
        # Comparable within each platform: likes (X/YouTube), score (Reddit), points (HN).
        engagement = item.get("engagement") or {}
        values = [
            engagement[k]
            for k in ("likes", "score", "points", "stars")
            if isinstance(engagement.get(k), (int, float))
            and not isinstance(engagement[k], bool)
            and math.isfinite(engagement[k])
        ]
        if not values:
            return "unknown_engagement"
        if max(values) < config.min_engagement:
            return "engagement"
    return None


def checkpoint(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix="." + path.name, suffix=".tmp", delete=False
    ) as out:
        temp = Path(out.name)
        try:
            json.dump(state, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Stop(Exception):
    pass


def run(
    config,
    planner,
    classifier,
    search,
    output,
    *,
    resume=False,
    clock=time.monotonic,
    progress=None,
):
    """Run until the planner declares coverage complete; checkpoints preserve all evidence."""
    from collections import Counter

    config_dict = json.loads(json.dumps(asdict(config)))
    output = Path(output)
    if resume:
        state = json.loads(output.read_text())
        if not isinstance(state, dict) or state.get("schema_version") != 2:
            raise ValueError(
                "unsupported checkpoint version; start a new discovery run"
            )
        if state.get("config") != config_dict:
            raise ValueError("resume configuration differs from checkpoint")
    else:
        if output.exists():
            raise ValueError("output exists; use --resume or choose another path")
        state = {
            "schema_version": 2,
            "config": config_dict,
            "criteria": config.criteria(),
            "created_at": datetime.now(UTC).isoformat(),
            "rounds": [],
            "candidates": {},
            "accepted": [],
            "calls": 0,
            "searches": 0,
            "elapsed_seconds": 0,
            "planner_decisions": [],
            "stop_reason": None,
        }
    started = clock()
    previous_elapsed = state["elapsed_seconds"]
    state["stop_reason"] = None
    state.pop("error", None)
    state.pop("completion_reason", None)

    def save():
        state["elapsed_seconds"] = previous_elapsed + max(0, clock() - started)
        checkpoint(output, state)

    def charge(kind):
        state["calls"] += 1
        if kind == "search":
            state["searches"] += 1
        save()
        return config.search_timeout if kind == "search" else config.request_timeout

    def emit(event):
        if progress:
            progress(event)

    def feedback():
        # Bound the context, not the investigation. The planner maintains a rolling
        # coverage assessment; complete evidence and query history stay in the checkpoint.
        values = list(state["candidates"].values())
        selected = []
        for decision in ("accepted", "rejected", "uncertain", "filtered"):
            group = [v for v in values if v["decision"] == decision]
            if decision == "accepted" and len(group) > 10:
                group = [group[i * (len(group) - 1) // 9] for i in range(10)]
            else:
                group = group[-10:]
            selected.extend(group)
        examples = [
            {
                "title": v["item"].get("title", "")[:250],
                "body": v["item"].get("body", "")[:1000],
                "url": v["item"].get("url"),
                "source": v["item"].get("source"),
                "decision": v["decision"],
                "probabilities": v.get("judgement", {}).get("probabilities", {}),
                "filter_reason": v.get("filter_reason"),
            }
            for v in selected
        ]
        queries = [q["query"] for r in state["rounds"] for q in r["queries"]]
        return {
            "queries": queries[-100:],
            "total_queries": len(queries),
            "accepted_count": len(state["accepted"]),
            "examples": examples,
            "decision_counts": dict(Counter(v["decision"] for v in values)),
            "source_counts": dict(Counter(v["item"]["source"] for v in values)),
            "queries_requested": config.queries_per_round,
            "previous_assessment": state["planner_decisions"][-1]
            if state["planner_decisions"]
            else None,
            "rounds": [
                {
                    "number": r["number"],
                    "new_accepted": r.get("new_accepted"),
                    "new_candidates": r.get("new_candidates"),
                    "duplicate_candidates": r.get("duplicate_candidates", 0),
                    "source_status": [q.get("source_status", {}) for q in r["queries"]],
                }
                for r in state["rounds"][-20:]
            ],
            "context_note": "Examples are sampled, with the last 100 queries and 20 rounds. Preserve earlier coverage and gaps in coverage_summary.",
        }

    save()
    try:
        while True:
            active = (
                state["rounds"][-1]
                if state["rounds"] and not state["rounds"][-1]["complete"]
                else None
            )
            if active is None:
                assessment = planner.plan(
                    config.objective, config.filters(), feedback(), charge("planner")
                )
                # The provider validates structure; also guard injected planner implementations.
                if (
                    not isinstance(assessment, dict)
                    or assessment.get("action") not in ("search", "stop")
                    or not isinstance(assessment.get("reason"), str)
                    or not assessment["reason"].strip()
                    or not isinstance(assessment.get("coverage_summary"), str)
                    or not isinstance(assessment.get("queries"), list)
                ):
                    raise ValueError("invalid planner assessment")
                if assessment["action"] == "stop":
                    if assessment["queries"]:
                        raise ValueError("stop assessment must not contain queries")
                    state["planner_decisions"].append(assessment)
                    state["completion_reason"] = assessment["reason"]
                    state["coverage_summary"] = assessment["coverage_summary"]
                    raise Stop("planner_complete")
                used = {
                    q["query"].strip().casefold()
                    for r in state["rounds"]
                    for q in r["queries"]
                }
                fresh = []
                skipped = []
                for q in assessment["queries"]:
                    if not isinstance(q, str) or not q.strip() or len(q) > 500:
                        raise ValueError("invalid planner query")
                    q = q.strip()
                    if q.casefold() in used:
                        skipped.append(q)
                    else:
                        fresh.append(q)
                        used.add(q.casefold())
                assessment = dict(assessment, skipped_queries=skipped)
                state["planner_decisions"].append(assessment)
                state["coverage_summary"] = assessment["coverage_summary"]
                save()
                if not fresh:
                    # Repetition is feedback for the LLM, never an automatic finish.
                    continue
                active = {
                    "number": len(state["rounds"]) + 1,
                    "queries": [
                        {"query": q, "fetched": False, "done": False, "ids": []}
                        for q in fresh[: config.queries_per_round]
                    ],
                    "accepted_before": len(state["accepted"]),
                    "candidates_before": len(state["candidates"]),
                    "duplicate_candidates": 0,
                    "complete": False,
                }
                state["rounds"].append(active)
                save()
            for query in active["queries"]:
                if query["done"]:
                    continue
                if not query["fetched"]:
                    emit(
                        {
                            "event": "search",
                            "round": active["number"],
                            "query": query["query"],
                        }
                    )
                    response = search(query["query"], config, charge("search"))
                    statuses = response.get("source_status", {})
                    query["source_status"] = statuses
                    query.setdefault("attempts", []).append({"source_status": statuses})
                    query["healthy"] = all(
                        statuses.get(s) in ("ok", "no-results") for s in config.sources
                    )
                    for item in response["items"]:
                        try:
                            identity = canonical_url(item["url"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        if identity in state["candidates"]:
                            active["duplicate_candidates"] += 1
                            continue
                        reason = filter_reason(item, config)
                        state["candidates"][identity] = {
                            "item": item,
                            "query": query["query"],
                            "round": active["number"],
                            "decision": "filtered" if reason else "pending",
                            **({"filter_reason": reason} if reason else {}),
                        }
                        query["ids"].append(identity)
                    query["fetched"] = True
                    save()
                for identity in query["ids"]:
                    entry = state["candidates"][identity]
                    if entry["decision"] != "pending":
                        continue
                    judgement = classifier.classify(
                        config.objective,
                        config.criteria(),
                        entry["item"],
                        charge("jev"),
                    )
                    entry["decision"] = decide(
                        judgement, config, len(config.criteria())
                    )
                    entry["judgement"] = judgement
                    if entry["decision"] == "accepted":
                        state["accepted"].append(identity)
                    save()
                    emit(
                        {
                            "event": "classified",
                            "round": active["number"],
                            "decision": entry["decision"],
                            "accepted": len(state["accepted"]),
                            "url": entry["item"]["url"],
                        }
                    )
                if not query["healthy"]:
                    query["fetched"] = False
                    save()
                    raise Stop("source_failure")
                query["done"] = True
                save()
            active["complete"] = True
            active["new_accepted"] = len(state["accepted"]) - active["accepted_before"]
            active["new_candidates"] = (
                len(state["candidates"]) - active["candidates_before"]
            )
            save()
    except Stop as exc:
        state["stop_reason"] = str(exc)
    except KeyboardInterrupt:
        state["stop_reason"] = "cancelled"
    except (TimeoutError, subprocess.TimeoutExpired):
        state["stop_reason"] = "provider_failure"
        state["error"] = "operation timed out; investigation incomplete"
    except Exception as exc:  # noqa: BLE001 - checkpoint and sanitize provider boundaries
        from .discovery_providers import ProviderError

        state["stop_reason"] = "provider_failure"
        state["error"] = (
            str(exc)
            if isinstance(exc, ProviderError)
            else f"operation failed ({type(exc).__name__})"
        )
    save()
    emit(
        {
            "event": "stopped",
            "reason": state["stop_reason"],
            "accepted": len(state["accepted"]),
        }
    )
    return state


def candidate_from_row(row, source, config):
    """Keep attribution while explicitly bounding all evidence for Jev's state budget."""
    metadata = row.get("metadata") or {}
    body = str(row.get("body") or row.get("snippet") or "")
    title = str(row.get("title") or "")
    comments = []
    truncated = len(body) > 24000 or len(title) > 2000
    original_comments = metadata.get("top_comments") or []
    if len(original_comments) > 5:
        truncated = True
    for comment in original_comments[:5]:
        if not isinstance(comment, dict):
            truncated = True
            continue
        excerpt = str(
            comment.get("excerpt") or comment.get("body") or comment.get("text") or ""
        )
        truncated |= len(excerpt) > 500
        comments.append(
            {
                "author": str(comment.get("author") or "")[:100],
                "excerpt": excerpt[:500],
                "url": str(comment.get("url") or "")[:1000],
            }
        )
    item = {
        "url": row.get("url"),
        "source": source,
        "title": title[:2000],
        "body": body[:24000],
        "evidence_truncated": truncated,
        "published_at": row.get("published_at"),
        "engagement": row.get("engagement", {}),
        "author": str(row.get("author") or "")[:200],
        "container": str(row.get("container") or "")[:200],
        "date_confidence": row.get("date_confidence"),
        "discussion_url": str(metadata.get("hn_url") or "")[:2000],
        "top_comments": comments,
        "evidence_scope": "Engine source text, possibly excerpted or combining a post and comments. A commenter is not necessarily the post author; do not attribute all experiences to the author.",
    }

    def state_size():
        return len(
            json.dumps(
                {
                    "objective": config.objective,
                    "criteria": config.criteria(),
                    "candidate": item,
                },
                ensure_ascii=False,
            )
        )

    excess = state_size() - 59000
    if excess > 0:
        item["body"] = item["body"][: max(0, len(item["body"]) - excess - 100)]
        item["evidence_truncated"] = True
    if state_size() > 59000:
        raise ValueError("candidate metadata exceeds evidence budget")
    return item


class EngineSearch:
    """Run one generated query through the actual engine, retaining unabridged evidence."""

    def __init__(self, environ=None):
        self.environ = dict(os.environ if environ is None else environ)
        self.script = Path(__file__).resolve().parents[1] / "last30days.py"

    def command(self, query, config):
        plan = {
            "intent": "product",
            "freshness_mode": "strict_recent",
            "cluster_mode": "none",
            "raw_topic": query,
            "subqueries": [
                {
                    "label": "primary",
                    "search_query": query,
                    "ranking_query": config.objective,
                    "sources": list(config.sources),
                    "weight": 1.0,
                }
            ],
        }
        return [
            sys.executable,
            str(self.script),
            query,
            "--search=" + ",".join(config.sources),
            "--plan",
            json.dumps(plan),
            "--quick",
            "--no-browser-cookies",
            "--web-backend=none",
            "--emit=json",
            "--json-profile=raw",
            "--days",
            str(config.days),
            "--as-of",
            config.as_of,
            "--max-per-source",
            "30",
            "--max-results",
            "90",
        ]

    def __call__(self, query, config, timeout):
        with tempfile.TemporaryDirectory(prefix="last30days-discovery-") as temp:
            env = dict(
                self.environ,
                LAST30DAYS_STORE="off",
                LAST30DAYS_LIBRARY_CONTEXT="off",
                LAST30DAYS_DISABLE_BROWSER_COOKIES="1",
                FROM_BROWSER="off",
                LAST30DAYS_MEMORY_DIR=temp,
                LAST30DAYS_GETXAPI_EXACT_QUERY="1",
                LAST30DAYS_SKIP_RUN_CACHE="1",
            )
            env.pop("LAST30DAYS_API_BASE", None)
            env.pop("LAST30DAYS_API_KEY", None)
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                process = subprocess.Popen(
                    self.command(query, config),
                    env=env,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
                try:
                    process.wait(timeout=timeout)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
                if process.returncode:
                    raise RuntimeError("research engine failed")
                out.seek(0)
                raw = out.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError("engine output too large")
                payload = json.loads(raw)
        # Raw report retains source body/comments; agent JSON truncates summaries.
        items = []
        for source, rows in payload.get("items_by_source", {}).items():
            for row in rows:
                items.append(candidate_from_row(row, source, config))
        statuses = {
            s: (v.get("state") if isinstance(v, dict) else v)
            for s, v in payload.get("source_status", {}).items()
        }
        if "items_by_source" not in payload or not isinstance(
            payload.get("source_status"), dict
        ):
            raise ValueError("invalid raw engine report")
        return {"items": items, "source_status": statuses}

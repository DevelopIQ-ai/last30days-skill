import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from lib.discovery import Config, EngineSearch, canonical_url, decide, run


def item(n, text="I built a Jev agent router", **kw):
    return {
        "url": f"https://x.com/alice/status/{n}",
        "title": text,
        "body": text,
        "source": "x",
        "published_at": datetime.now(UTC).date().isoformat(),
        "engagement": {"likes": 4},
        **kw,
    }


class Planner:
    def __init__(self, plans=None):
        self.calls = []
        self.plans = plans if plans is not None else [["first"], ["second"]]

    def plan(self, objective, filters, feedback, timeout):
        self.calls.append(json.loads(json.dumps(feedback)))
        index = len(self.calls) - 1
        if index >= len(self.plans):
            return {
                "action": "stop",
                "reason": "Coverage spans the relevant angles; additional searches repeat known matches.",
                "coverage_summary": "Covered builders and actual testing; no remaining productive angle.",
                "queries": [],
            }
        return {
            "action": "search",
            "reason": "Explore another angle.",
            "coverage_summary": f"Assessed {index} rounds.",
            "queries": self.plans[index],
        }


class Jev:
    def __init__(self):
        self.calls = []

    def classify(self, objective, criteria, candidate, timeout):
        self.calls.append((objective, list(criteria), candidate["url"]))
        return {
            "probabilities": {
                f"c{i}": 0.05 if "reject" in candidate["title"] else 0.95
                for i in range(len(criteria))
            },
            "evidence_sufficient": 0.98,
            "usage": {},
        }


def searcher(pages):
    calls = []

    def search(query, config, timeout):
        calls.append(query)
        return {"items": pages.get(query, []), "source_status": {"x": "ok"}}

    search.calls = calls
    return search


def config(**kw):
    return Config(objective="Find people building with Jev", sources=("x",), **kw)


def test_planner_controls_completion_and_gets_novelty_and_coverage(tmp_path):
    planner = Planner()
    classifier = Jev()
    search = searcher({"first": [item(1, "reject launch news")], "second": [item(2)]})
    cfg = config(include=("Firsthand experience",))
    state = run(cfg, planner, classifier, search, tmp_path / "r.json")
    assert state["stop_reason"] == "planner_complete"
    assert state["completion_reason"].startswith("Coverage spans")
    assert len(state["accepted"]) == 1
    assert search.calls == ["first", "second"]
    assert planner.calls[1]["examples"][0]["decision"] == "rejected"
    assert (
        planner.calls[1]["previous_assessment"]["coverage_summary"]
        == "Assessed 0 rounds."
    )
    assert planner.calls[2]["rounds"][-1]["new_accepted"] == 1
    assert all(
        c[0] == cfg.objective and c[1] == [cfg.objective, *cfg.include]
        for c in classifier.calls
    )


def test_no_automatic_target_round_time_call_search_or_empty_round_stop(tmp_path):
    now = [0.0]
    plans = [[f"query {i}"] for i in range(160)]

    def search(q, c, t):
        now[0] += 600
        # Many accepted results followed by empty rounds; none may end research.
        return {
            "items": [item(int(q.split()[-1]))] if len(q) < 9 else [],
            "source_status": {"x": "ok"},
        }

    state = run(
        config(),
        Planner(plans),
        Jev(),
        search,
        tmp_path / "r.json",
        clock=lambda: now[0],
    )
    assert state["stop_reason"] == "planner_complete"
    assert len(state["rounds"]) == 160 and state["searches"] == 160
    assert state["calls"] > 150 and state["elapsed_seconds"] > 300
    assert len(state["accepted"]) > 20


def test_duplicate_queries_go_back_to_planner_not_completion(tmp_path):
    planner = Planner([["first"], ["first"], ["second"]])
    search = searcher({})
    state = run(config(), planner, Jev(), search, tmp_path / "r.json")
    assert state["stop_reason"] == "planner_complete" and len(planner.calls) == 4
    assert search.calls == ["first", "second"]
    assert planner.calls[2]["previous_assessment"]["skipped_queries"] == ["first"]


def test_result_dedupe_and_novelty_counts(tmp_path):
    jev = Jev()
    a = item(1)
    b = item(1, url="https://twitter.com/Alice/status/1?s=20")
    planner = Planner()
    state = run(
        config(),
        planner,
        jev,
        searcher({"first": [a], "second": [b]}),
        tmp_path / "r.json",
    )
    assert len(jev.calls) == 1 and len(state["accepted"]) == 1
    assert state["rounds"][1]["new_candidates"] == 0
    assert state["rounds"][1]["duplicate_candidates"] == 1
    assert canonical_url(a["url"]) == canonical_url(b["url"])


def test_unknown_date_and_engagement_cannot_pass_hard_filters(tmp_path):
    jev = Jev()
    search = searcher(
        {
            "first": [
                item(1, published_at=None),
                item(2, engagement={}),
                item(3, engagement={"views": 99999}),
            ]
        }
    )
    state = run(
        config(min_engagement=1), Planner([["first"]]), jev, search, tmp_path / "r.json"
    )
    assert not jev.calls and not state["accepted"]
    assert all(x["decision"] == "filtered" for x in state["candidates"].values())


def test_uncertainty_and_missing_probabilities():
    cfg = config()
    assert (
        decide({"probabilities": {"c0": 0.6}, "evidence_sufficient": 0.99}, cfg, 1)
        == "uncertain"
    )
    assert (
        decide({"probabilities": {"c0": 0.99}, "evidence_sufficient": 0.2}, cfg, 1)
        == "uncertain"
    )
    assert (
        decide({"probabilities": {"c0": 0.1}, "evidence_sufficient": 0.99}, cfg, 1)
        == "rejected"
    )
    for scores in ({}, {"c0": float("nan")}):
        with pytest.raises(ValueError):
            decide({"probabilities": scores, "evidence_sufficient": 0.99}, cfg, 1)


def test_operational_failure_is_not_successful_completion(tmp_path):
    def failed(q, c, t):
        return {"items": [], "source_status": {"x": "auth-failed"}}

    path = tmp_path / "r.json"
    state = run(config(), Planner(), Jev(), failed, path)
    assert state["stop_reason"] == "source_failure" and not state.get(
        "completion_reason"
    )
    state = run(config(), Planner(), Jev(), failed, path, resume=True)
    assert state["stop_reason"] == "source_failure"


def test_pending_classification_resume_and_immutable_config(tmp_path):
    class Broken(Jev):
        def classify(self, *args):
            raise RuntimeError("secret not for logs")

    path = tmp_path / "r.json"
    cfg = config()
    p = Planner([["first"]])
    search = searcher({"first": [item(1)]})
    state = run(cfg, p, Broken(), search, path)
    assert (
        state["stop_reason"] == "provider_failure"
        and "secret not" not in path.read_text()
    )
    resumed = run(cfg, p, Jev(), search, path, resume=True)
    assert resumed["stop_reason"] == "planner_complete" and search.calls == ["first"]
    with pytest.raises(ValueError, match="configuration"):
        run(replace(cfg, objective="different"), p, Jev(), search, path, resume=True)


def test_cancelled_checkpoint_and_request_timeout(tmp_path):
    class Cancel(Planner):
        def plan(self, *a):
            raise KeyboardInterrupt()

    state = run(config(), Cancel(), Jev(), searcher({}), tmp_path / "r.json")
    assert state["stop_reason"] == "cancelled"

    class Timeout(Planner):
        def plan(self, *args):
            assert args[-1] == 12
            raise TimeoutError()

    state = run(
        config(request_timeout=12), Timeout(), Jev(), searcher({}), tmp_path / "s.json"
    )
    assert state["stop_reason"] == "provider_failure" and not state.get(
        "completion_reason"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"days": 0},
        {"request_timeout": 0},
        {"search_timeout": float("nan")},
        {"accept_threshold": 0.2},
        {"sources": ("bad",)},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        Config(objective="find things", **kwargs)


def test_engine_query_is_positional_and_pinned_date():
    cmd = EngineSearch().command("new direction", config())
    assert (
        cmd[2] == "new direction" and "--as-of" in cmd and "--json-profile=raw" in cmd
    )


def test_legacy_checkpoint_is_explicitly_rejected(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"schema_version": 1, "config": {}}))
    with pytest.raises(ValueError, match="version"):
        run(config(), Planner(), Jev(), searcher({}), path, resume=True)


def test_candidate_budget_preserves_attribution():
    from lib.discovery import candidate_from_row

    cfg = Config(objective="x" * 8000, include=tuple("y" * 2000 for _ in range(6)))
    row = {
        "url": "https://example.com/a",
        "body": "a" * 24000,
        "title": "b" * 2000,
        "metadata": {"top_comments": [{"author": "alice", "excerpt": "c" * 50000}] * 5},
    }
    candidate = candidate_from_row(row, "reddit", cfg)
    assert (
        candidate["top_comments"][0]["author"] == "alice"
        and candidate["evidence_truncated"]
    )
    assert (
        len(
            json.dumps(
                {
                    "objective": cfg.objective,
                    "criteria": cfg.criteria(),
                    "candidate": candidate,
                },
                ensure_ascii=False,
            )
        )
        <= 59000
    )

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
        self.plans = plans or [["first"], ["second"], ["third"]]

    def plan(self, objective, filters, feedback, timeout):
        self.calls.append(json.loads(json.dumps(feedback)))
        return self.plans[min(len(self.calls) - 1, len(self.plans) - 1)]


class Jev:
    def __init__(self):
        self.calls = []

    def classify(self, objective, criteria, candidate, timeout):
        self.calls.append((objective, list(criteria), candidate["url"]))
        return {
            "probabilities": {
                f"c{i}": 0.95 if "reject" not in candidate["title"] else 0.05
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


def test_feedback_drives_new_query_and_original_criteria_stay_fixed(tmp_path):
    planner = Planner()
    jev = Jev()
    search = searcher({"first": [item(1, "reject a launch ad")], "second": [item(2)]})
    cfg = config(target=1, include=("Firsthand experience",))
    state = run(cfg, planner, jev, search, tmp_path / "run.json")
    assert state["stop_reason"] == "target_reached"
    assert search.calls == ["first", "second"]
    assert planner.calls[1]["examples"][0]["decision"] == "rejected"
    assert all(
        call[0] == cfg.objective and call[1] == [cfg.objective, *cfg.include]
        for call in jev.calls
    )
    assert len(state["accepted"]) == 1
    assert (
        json.loads((tmp_path / "run.json").read_text())["stop_reason"]
        == "target_reached"
    )


def test_dedupe_twitter_alias_tracking_and_saturation(tmp_path):
    planner = Planner()
    jev = Jev()
    a = item(1)
    b = item(1, url="https://twitter.com/Alice/status/1?s=20")
    state = run(
        config(target=5),
        planner,
        jev,
        searcher({"first": [a], "second": [b], "third": [a]}),
        tmp_path / "r.json",
    )
    assert state["stop_reason"] == "saturated"
    assert len(jev.calls) == 1
    assert len(state["rounds"]) == 3
    assert canonical_url(a["url"]) == canonical_url(b["url"])


def test_unknown_date_and_engagement_cannot_pass_hard_filters(tmp_path):
    jev = Jev()
    s = searcher(
        {
            "first": [
                item(1, published_at=None),
                item(2, engagement={}),
                item(3, engagement={"views": 100000}),
            ]
        }
    )
    state = run(
        config(min_engagement=1, max_rounds=1), Planner(), jev, s, tmp_path / "r.json"
    )
    assert not state["accepted"] and not jev.calls
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
    with pytest.raises(ValueError):
        decide({"probabilities": {}, "evidence_sufficient": 0.99}, cfg, 1)
    with pytest.raises(ValueError):
        decide(
            {"probabilities": {"c0": float("nan")}, "evidence_sufficient": 0.99}, cfg, 1
        )


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"max_rounds": 1}, "max_rounds"),
        ({"max_calls": 1}, "call_limit"),
        ({"max_searches": 1}, "search_limit"),
    ],
)
def test_budgets(tmp_path, kwargs, reason):
    state = run(config(**kwargs), Planner(), Jev(), searcher({}), tmp_path / "r.json")
    assert state["stop_reason"] == reason


def test_source_failure_is_not_saturation(tmp_path):
    def failed(q, c, t):
        return {"items": [], "source_status": {"x": "auth-failed"}}

    state = run(config(patience=1), Planner(), Jev(), failed, tmp_path / "r.json")
    assert state["stop_reason"] == "source_failure"
    assert state["empty_rounds"] == 0


def test_classifier_failure_preserves_pending_for_resume(tmp_path):
    class Broken(Jev):
        def classify(self, *a):
            raise RuntimeError("secret=must-not-log")

    path = tmp_path / "r.json"
    planner = Planner()
    s = searcher({"first": [item(1)]})
    state = run(config(target=1), planner, Broken(), s, path)
    assert state["stop_reason"] == "provider_failure"
    assert "must-not-log" not in path.read_text()
    resumed = run(config(target=1), planner, Jev(), s, path, resume=True)
    assert resumed["stop_reason"] == "target_reached"
    assert s.calls == ["first"]


def test_resume_config_change_rejected_before_network(tmp_path):
    path = tmp_path / "r.json"
    cfg = config(max_calls=1)
    run(cfg, Planner(), Jev(), searcher({}), path)
    with pytest.raises(ValueError, match="configuration"):
        run(
            replace(cfg, objective="different"),
            Planner(),
            Jev(),
            searcher({}),
            path,
            resume=True,
        )


def test_deadline_and_cancellation_preserve_checkpoint(tmp_path):
    class Clock:
        def __init__(self):
            self.n = 0

        def __call__(self):
            self.n += 10
            return self.n

    state = run(
        config(timeout=1),
        Planner(),
        Jev(),
        searcher({}),
        tmp_path / "r.json",
        clock=Clock(),
    )
    assert state["stop_reason"] == "deadline"

    class Cancel(Planner):
        def plan(self, *args):
            raise KeyboardInterrupt()

    state = run(config(), Cancel(), Jev(), searcher({}), tmp_path / "s.json")
    assert state["stop_reason"] == "cancelled"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target": 0},
        {"days": 0},
        {"max_rounds": 0},
        {"accept_threshold": 0.2},
        {"timeout": float("nan")},
        {"sources": ("bad",)},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        Config(objective="find things", **kwargs)


def test_engine_query_is_positional_and_pinned_date():
    cmd = EngineSearch().command("new direction", config())
    assert cmd[2] == "new direction"
    assert "--as-of" in cmd and "--no-browser-cookies" in cmd
    assert "--json-profile=raw" in cmd


def test_source_failure_resume_retries_and_never_counts_saturation(tmp_path):
    calls = []

    def fail(q, c, t):
        calls.append(q)
        return {"items": [], "source_status": {"x": "auth-failed"}}

    cfg = config(patience=1)
    path = tmp_path / "r.json"
    run(cfg, Planner(), Jev(), fail, path)
    state = run(cfg, Planner(), Jev(), fail, path, resume=True)
    assert state["stop_reason"] == "source_failure" and state["empty_rounds"] == 0
    assert calls == ["first", "first"]


def test_github_engagement_stars(tmp_path):
    cfg = Config(
        objective="Find Jev projects", sources=("github",), min_engagement=10, target=1
    )

    def search(q, c, t):
        return {
            "items": [item(1, source="github", engagement={"stars": 100})],
            "source_status": {"github": "ok"},
        }

    state = run(cfg, Planner(), Jev(), search, tmp_path / "r.json")
    assert state["stop_reason"] == "target_reached"


def test_late_classifier_cannot_claim_target_success(tmp_path):
    now = [0.0]

    class Slow(Jev):
        def classify(self, *args):
            result = super().classify(*args)
            now[0] = 2.0
            return result

    state = run(
        config(target=1, timeout=1),
        Planner(),
        Slow(),
        searcher({"first": [item(1)]}),
        tmp_path / "r.json",
        clock=lambda: now[0],
    )
    assert state["stop_reason"] == "deadline"
    assert not state["accepted"]


def test_timed_out_provider_reports_deadline(tmp_path):
    now = [0.0]

    class Expired(Planner):
        def plan(self, *args):
            now[0] = 2.0
            raise RuntimeError("hidden response")

    state = run(
        config(timeout=1),
        Expired(),
        Jev(),
        searcher({}),
        tmp_path / "r.json",
        clock=lambda: now[0],
    )
    assert state["stop_reason"] == "deadline"


def test_candidate_budget_preserves_comment_attribution_and_marks_truncation():
    from lib.discovery import candidate_from_row

    cfg = Config(objective="x" * 8000, include=tuple("y" * 2000 for _ in range(6)))
    row = {
        "url": "https://example.com/a",
        "body": "a" * 24000,
        "title": "b" * 2000,
        "metadata": {"top_comments": [{"author": "alice", "excerpt": "c" * 50000}] * 5},
    }
    candidate = candidate_from_row(row, "reddit", cfg)
    assert candidate["top_comments"][0]["author"] == "alice"
    assert candidate["evidence_truncated"]
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


def test_total_encoded_criteria_budget_validated_before_network():
    with pytest.raises(ValueError):
        Config(objective='"' * 8000, include=tuple('"' * 2000 for _ in range(6)))

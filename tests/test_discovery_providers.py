import json

import pytest
from lib import discovery_providers as p


def fixture(monkeypatch, response):
    calls = []

    def post(url, key, body, timeout, headers=None):
        calls.append((url, key, body, timeout, headers))
        return response

    monkeypatch.setattr(p, "_post", post)
    return calls


def test_gateway_boolean_protocol(monkeypatch):
    calls = fixture(
        monkeypatch,
        {
            "answers": {
                "c0": {"type": "boolean", "probability": 0.9},
                "evidence_sufficient": {"type": "boolean", "probability": 0.8},
            },
            "usage": {"inputTokens": 42},
        },
    )
    result = p.Jev(
        {"JEV_PROVIDER": "vercel", "AI_GATEWAY_API_KEY": "test-key"}
    ).classify(
        "real complaints", ["real complaints"], {"text": "I tried it; it broke."}, 3
    )
    assert result["probabilities"] == {"c0": 0.9}
    assert result["evidence_sufficient"] == 0.8
    assert result["usage"] == {"inputTokens": 42}
    assert calls[0][0].endswith("/v4/ai/evaluation-model")
    assert calls[0][4]["ai-model-id"] == "typesafe-ai/jev"
    assert calls[0][2]["questions"]["c0"]["type"] == "boolean"
    assert "untrusted" in calls[0][2]["questions"]["c0"]["instructions"]


def test_native_noul(monkeypatch):
    calls = fixture(
        monkeypatch,
        {
            "answers": {
                "c0": {"type": "noul", "noul": 0.7},
                "evidence_sufficient": {"type": "noul", "noul": 0.99},
            },
            "usage": {"input_tokens": 10},
        },
    )
    result = p.Jev({"TYPESAFE_API_KEY": "test-key"}).classify(
        "complaints", ["complaints"], {}, 2
    )
    assert result["model"] == "jev-latest"
    assert result["probabilities"]["c0"] == 0.7
    assert calls[0][2]["questions"]["c0"]["type"] == "noul"


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -1, 1.01, True, "0.9", None]
)
def test_reject_invalid_probability(monkeypatch, value):
    fixture(
        monkeypatch,
        {
            "answers": {
                "c0": {"type": "boolean", "probability": value},
                "evidence_sufficient": {"type": "boolean", "probability": 0.9},
            }
        },
    )
    with pytest.raises(p.ProviderError):
        p.Jev({"JEV_PROVIDER": "vercel", "AI_GATEWAY_API_KEY": "test"}).classify(
            "q", ["q"], {}, 2
        )


def test_missing_answers_and_objective(monkeypatch):
    fixture(monkeypatch, {"answers": {}})
    j = p.Jev({"JEV_API_KEY": "test"})
    with pytest.raises(p.ProviderError):
        j.classify("q", ["q"], {}, 2)
    with pytest.raises(p.ProviderError):
        j.classify("q", ["other"], {}, 2)


def test_planner_plan(monkeypatch):
    calls = fixture(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"queries": ["Jev latency", " Jev latency ", "Jev routing"]}
                        )
                    }
                }
            ]
        },
    )
    out = p.Planner({"AI_GATEWAY_API_KEY": "test"}).plan(
        "find builders", {"days": 30}, {"rejected": ["x"]}, 2
    )
    assert out == ["Jev latency", "Jev routing"]
    assert calls[0][0] == "https://ai-gateway.vercel.sh/v1/chat/completions"
    assert "feedback" in calls[0][2]["messages"][1]["content"]


@pytest.mark.parametrize("queries", [[], ["a", "b", "c", "d"], [""], [1], ["x" * 501]])
def test_planner_rejects_invalid_queries(monkeypatch, queries):
    fixture(
        monkeypatch,
        {"choices": [{"message": {"content": json.dumps({"queries": queries})}}]},
    )
    with pytest.raises(p.ProviderError):
        p.Planner({"OPENAI_API_KEY": "test"}).plan("q", {}, {}, 2)


def test_missing_keys():
    with pytest.raises(p.ProviderError):
        p.Planner({})
    with pytest.raises(p.ProviderError):
        p.Jev({})


def test_no_redirect_and_safe_errors():
    assert (
        p._NoRedirect().redirect_request(
            None, None, 302, "secret", {}, "https://evil.example"
        )
        is None
    )
    with pytest.raises(p.ProviderError, match="HTTPS"):
        p._post("http://example.com", "secret", {}, 1)


def test_http_response_size_and_invalid_json(monkeypatch):
    import io

    class Opener:
        def __init__(self, body):
            self.body = body

        def open(self, request, timeout):
            return io.BytesIO(self.body)

    for body in [b"x" * (p.MAX_RESPONSE_BYTES + 1), b"not json", b"[]"]:
        monkeypatch.setattr(
            p.urllib.request, "build_opener", lambda *args, body=body: Opener(body)
        )
        with pytest.raises(p.ProviderError):
            p._post_transport("https://example.com", "test-key", {}, 2)


def test_http_error_never_leaks_body_or_key(monkeypatch):
    import io

    class Opener:
        def open(self, request, timeout):
            raise p.urllib.error.HTTPError(
                "https://example.com",
                401,
                "private-secret",
                {},
                io.BytesIO(b"private-secret"),
            )

    monkeypatch.setattr(p.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(p.ProviderError) as error:
        p._post_transport("https://example.com", "private-secret", {}, 2)
    assert str(error.value) == "Provider request failed (HTTP 401)."


def test_gateway_headers(monkeypatch):
    calls = fixture(
        monkeypatch,
        {
            "answers": {
                "c0": {"type": "boolean", "probability": 1},
                "evidence_sufficient": {"type": "boolean", "probability": 1},
            }
        },
    )
    p.Jev({"JEV_PROVIDER": "vercel", "AI_GATEWAY_API_KEY": "test"}).classify(
        "q", ["q"], {}, 2
    )
    assert calls[0][4]["ai-gateway-protocol-version"] == "0.0.1"
    assert calls[0][4]["ai-gateway-auth-method"] == "api-key"


def test_oversize_candidate_not_silently_truncated(monkeypatch):
    calls = fixture(monkeypatch, {})
    with pytest.raises(p.ProviderError):
        p.Jev({"JEV_API_KEY": "test"}).classify("q", ["q"], {"text": "x" * 60000}, 2)
    assert not calls


def test_evidence_quality_is_independent_of_relevance(monkeypatch):
    calls = fixture(
        monkeypatch,
        {
            "answers": {
                "c0": {"type": "boolean", "probability": 0.01},
                "evidence_sufficient": {"type": "boolean", "probability": 0.99},
            }
        },
    )
    result = p.Jev({"JEV_PROVIDER": "vercel", "AI_GATEWAY_API_KEY": "test"}).classify(
        "Jev integrations",
        ["Jev integrations"],
        {
            "body": "I baked sourdough today using rye flour and a twelve hour cold proof."
        },
        2,
    )
    instruction = calls[0][2]["questions"]["evidence_sufficient"]["instructions"]
    assert "off-topic" in instruction and "yes OR no" in instruction
    assert result["evidence_sufficient"] == 0.99


def test_planner_removes_source_and_temporal_operators(monkeypatch):
    fixture(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "queries": [
                                    "Jev router site:x since:2026-08-20 until:2026-09-19",
                                    "site:reddit.com Jev routing after:2026-08-01 before:2026-10-01",
                                ]
                            }
                        )
                    }
                }
            ]
        },
    )
    assert p.Planner({"AI_GATEWAY_API_KEY": "test"}).plan("q", {}, {}, 2) == [
        "Jev router",
        "Jev routing",
    ]


def test_planner_rejects_operator_only_query(monkeypatch):
    fixture(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"queries": ["site:x since:2026-08-20"]})
                    }
                }
            ]
        },
    )
    with pytest.raises(p.ProviderError):
        p.Planner({"AI_GATEWAY_API_KEY": "test"}).plan("q", {}, {}, 2)


def test_numeric_gateway_cost_preserved(monkeypatch):
    fixture(
        monkeypatch,
        {
            "answers": {
                "c0": {"type": "boolean", "probability": 0.8},
                "evidence_sufficient": {"type": "boolean", "probability": 0.9},
            },
            "providerMetadata": {
                "gateway": {"cost": 0.00001, "generationId": "sensitive"}
            },
        },
    )
    result = p.Jev({"JEV_PROVIDER": "vercel", "AI_GATEWAY_API_KEY": "test"}).classify(
        "q", ["q"], {}, 2
    )
    assert result["usage"] == {"cost": 0.00001}


def _hanging_transport(connection):
    import time

    time.sleep(30)


def test_provider_wall_deadline_terminates_hanging_worker():
    import time

    started = time.monotonic()
    with pytest.raises(p.ProviderError, match="timed out"):
        p._bounded_request(_hanging_transport, (), 0.2)
    assert time.monotonic() - started < 3


def _successful_transport(connection):
    connection.send(("ok", {"answer": 42}))


def test_provider_worker_receives_result():
    assert p._bounded_request(_successful_transport, (), 5) == {"answer": 42}


def _large_transport(connection):
    connection.send(("ok", {"body": "x" * 500_000}))


def test_receive_before_join_does_not_deadlock_large_response():
    assert len(p._bounded_request(_large_transport, (), 5)["body"]) == 500_000

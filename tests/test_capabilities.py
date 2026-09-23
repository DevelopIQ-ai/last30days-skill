"""Capability gate (lib/capabilities.py + the unconditional refusal).

The engine has no model of its own. Historically each missing inference layer
had its own fallback: no plan dropped to a one-query deterministic planner, no
reasoning key dropped to upvote-and-keyword ranking, no web key left the
general-web lane empty. Each was defensible alone; together they produced a
confident report with four of five layers absent that still exited 0.

The gate is unconditional now, and the thing these tests pin hardest is that
**every capability is satisfiable by the agent with no API key** -- otherwise
"refuse to run" would just be "require a credit card".
"""

import unittest
from unittest import mock

import pytest

from lib import capabilities

# This file tests the gate itself, so it always faces the real one rather
# than the agent-host declarations conftest supplies to the rest of the suite.
pytestmark = pytest.mark.raw_capabilities


class AgentCanSatisfyEverythingTest(unittest.TestCase):
    """The load-bearing property: a keyless agent host is a valid host."""

    def test_agent_satisfies_all_three_with_no_api_key(self):
        config = {"LAST30DAYS_NATIVE_SEARCH": "1"}
        with mock.patch.object(capabilities, "has_reasoning_provider", return_value=False):
            missing = capabilities.check(config, plan_provided=True, agent_rerank=True)
        self.assertEqual(missing, [])

    def test_every_capability_documents_an_agent_route(self):
        for name, entry in capabilities.CAPABILITIES.items():
            self.assertTrue(entry["agent"].strip(), f"{name} has no agent route")

    def test_the_planner_route_never_suggests_a_key(self):
        """The hosting model writes the plan; pointing at a paid key would be
        wrong and would teach the wrong lesson about what this engine needs."""
        self.assertEqual(capabilities.CAPABILITIES["plan"]["key"], "")
        self.assertIn("no API key", capabilities.CAPABILITIES["plan"]["agent"])


class CheckTest(unittest.TestCase):
    def test_bare_run_is_missing_everything(self):
        with mock.patch.object(capabilities, "has_reasoning_provider", return_value=False):
            missing = capabilities.check({}, plan_provided=False, agent_rerank=False)
        self.assertEqual(missing, ["plan", "rerank", "web"])

    def test_a_key_is_an_alternative_not_a_requirement(self):
        with mock.patch.object(capabilities, "has_reasoning_provider", return_value=True):
            missing = capabilities.check(
                {"BRAVE_API_KEY": "x"}, plan_provided=True, agent_rerank=False
            )
        self.assertEqual(missing, [])

    def test_keyless_web_floor_does_not_satisfy_web(self):
        """It is suppressed on native-search hosts and returns nothing there --
        the exact silent hole this gate closes."""
        self.assertFalse(capabilities.has_web_backend({}))
        self.assertTrue(capabilities.has_web_backend({"SERPER_API_KEY": "x"}))

    def test_web_is_satisfied_by_the_host_declaring_native_search(self):
        self.assertFalse(capabilities.agent_does_web({}))
        self.assertTrue(capabilities.agent_does_web({"LAST30DAYS_NATIVE_SEARCH": "1"}))
        self.assertFalse(capabilities.agent_does_web({"LAST30DAYS_NATIVE_SEARCH": "0"}))

    def test_reasoning_provider_is_asked_of_the_resolver_not_key_names(self):
        """Guards against drift: this must agree with what the pipeline
        actually resolves, not a hand-copied list of key names."""
        with mock.patch("lib.providers.resolve_runtime", return_value=(object(), object())):
            self.assertTrue(capabilities.has_reasoning_provider({}))
        with mock.patch("lib.providers.resolve_runtime", return_value=(object(), None)):
            self.assertFalse(capabilities.has_reasoning_provider({}))

    def test_a_resolver_failure_counts_as_absent(self):
        with mock.patch("lib.providers.resolve_runtime", side_effect=RuntimeError("boom")):
            self.assertFalse(capabilities.has_reasoning_provider({}))


class RenderFailureTest(unittest.TestCase):
    def test_message_gives_effect_and_an_agent_route_for_each_gap(self):
        text = capabilities.render_failure(["plan", "rerank", "web"])
        self.assertEqual(text.count("effect:"), 3)
        self.assertEqual(text.count("you:"), 3)
        for name in ("plan", "rerank", "web"):
            self.assertIn(capabilities.CAPABILITIES[name]["label"], text)

    def test_message_offers_no_way_to_silence_the_gate(self):
        """There is no opt-out. A flag to disable this would be the fallback
        it replaced, wearing a different name."""
        text = capabilities.render_failure(["web"]).lower()
        for escape in ("--no-strict", "disable", "skip this check", "opt out"):
            self.assertNotIn(escape, text)


if __name__ == "__main__":
    unittest.main()


class GateIsWiredIntoTheEngineTest(unittest.TestCase):
    """End-to-end through pipeline.run, facing the real gate.

    The suite otherwise declares the agent capabilities for every test (see
    the _tests_run_as_an_agent_host fixture), so without these the refusal
    path would be unexercised -- the failure mode where a safeguard exists,
    is green, and never actually fires.
    """

    @pytest.mark.raw_capabilities
    def test_run_refuses_when_nothing_is_declared(self):
        from lib import capabilities as caps
        from lib import pipeline

        with self.assertRaises(caps.CapabilityError) as ctx:
            pipeline.run(topic="anything", config={}, depth="default")
        self.assertEqual(sorted(ctx.exception.missing), ["plan", "rerank", "web"])

    @pytest.mark.raw_capabilities
    def test_run_proceeds_once_the_agent_declares_them(self):
        """Past the gate the run fails on something else entirely (no
        sources), which is the point: the gate is no longer what stops it."""
        from lib import capabilities as caps
        from lib import pipeline

        config = {"LAST30DAYS_NATIVE_SEARCH": "1", "_agent_rerank": True}
        plan = {
            "intent": "entity",
            "subqueries": [{
                "label": "primary",
                "search_query": "anything",
                "ranking_query": "what about anything?",
                "sources": ["hackernews"],
            }],
        }
        try:
            pipeline.run(
                topic="anything", config=config, depth="quick",
                external_plan=plan, requested_sources=["hackernews"],
                web_backend="none", save_dir="",
            )
        except caps.CapabilityError as exc:  # pragma: no cover - the failure we are ruling out
            self.fail(f"gate still refused a fully declared run: {exc.missing}")
        except Exception:
            pass  # any other failure is out of scope here

    @pytest.mark.raw_capabilities
    def test_mock_runs_are_exempt(self):
        """--mock replays fixtures and performs no inference, so there is
        nothing for a planner or a judge to be missing from."""
        from lib import capabilities as caps
        from lib import pipeline

        try:
            pipeline.run(topic="anything", config={}, depth="quick", mock=True)
        except caps.CapabilityError:  # pragma: no cover
            self.fail("a mock run must not be gated")
        except Exception:
            pass

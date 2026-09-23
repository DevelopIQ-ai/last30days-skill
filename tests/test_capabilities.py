"""Strict capability gate (lib/capabilities.py + --strict).

The engine degrades rather than failing: no plan drops to a one-query
deterministic planner, no reasoning key drops to upvote-and-keyword ranking,
no web key leaves the general-web lane empty. Each fallback is defensible
alone; together they can produce a confident report with four of five quality
layers absent and still exit 0.

--strict removes that possibility. These tests pin both halves: it refuses
when a required capability is missing, and it changes nothing when absent.
"""

import unittest
from unittest import mock

from lib import capabilities


class ParseRequiredTest(unittest.TestCase):
    def test_bare_strict_requires_everything(self):
        self.assertEqual(capabilities.parse_required(True), list(capabilities.DEFAULT_REQUIRED))
        self.assertEqual(capabilities.parse_required(""), list(capabilities.DEFAULT_REQUIRED))
        self.assertEqual(capabilities.parse_required("1"), list(capabilities.DEFAULT_REQUIRED))

    def test_absent_or_falsey_requires_nothing(self):
        for raw in (None, False, "0", "false", "off", "no"):
            self.assertEqual(capabilities.parse_required(raw), [], raw)

    def test_a_subset_narrows_the_requirement(self):
        """A host with no web key can still demand a real planner and reranker."""
        self.assertEqual(capabilities.parse_required("plan,rerank"), ["plan", "rerank"])
        self.assertEqual(capabilities.parse_required(" plan , web "), ["plan", "web"])

    def test_an_unknown_capability_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as ctx:
            capabilities.parse_required("plan,telepathy")
        self.assertIn("telepathy", str(ctx.exception))


class CheckTest(unittest.TestCase):
    def test_everything_present_means_nothing_missing(self):
        with mock.patch.object(capabilities, "has_reasoning_provider", return_value=True):
            missing = capabilities.check(
                {"BRAVE_API_KEY": "x"}, list(capabilities.DEFAULT_REQUIRED), plan_provided=True
            )
        self.assertEqual(missing, [])

    def test_everything_absent_reports_all_three(self):
        with mock.patch.object(capabilities, "has_reasoning_provider", return_value=False):
            missing = capabilities.check({}, list(capabilities.DEFAULT_REQUIRED), plan_provided=False)
        self.assertEqual(missing, ["plan", "rerank", "web"])

    def test_only_required_capabilities_are_reported(self):
        """Narrowing must not smuggle in a capability the caller did not ask for."""
        with mock.patch.object(capabilities, "has_reasoning_provider", return_value=False):
            missing = capabilities.check({}, ["plan"], plan_provided=False)
        self.assertEqual(missing, ["plan"])

    def test_keyless_web_does_not_satisfy_the_web_requirement(self):
        """The keyless floor is suppressed on native-search hosts and returns
        nothing there -- exactly the silent hole strict mode exists to catch."""
        self.assertFalse(capabilities.has_web_backend({}))
        self.assertFalse(capabilities.has_web_backend({"LAST30DAYS_HOST": "claude-code"}))
        self.assertTrue(capabilities.has_web_backend({"SERPER_API_KEY": "x"}))

    def test_reasoning_provider_is_asked_of_the_resolver_not_key_names(self):
        """Guards against drift: the check must agree with what the pipeline
        actually resolves, not with a hand-copied list of key names."""
        with mock.patch("lib.providers.resolve_runtime", return_value=(object(), object())):
            self.assertTrue(capabilities.has_reasoning_provider({}))
        with mock.patch("lib.providers.resolve_runtime", return_value=(object(), None)):
            self.assertFalse(capabilities.has_reasoning_provider({}))

    def test_a_resolver_failure_counts_as_absent(self):
        with mock.patch("lib.providers.resolve_runtime", side_effect=RuntimeError("boom")):
            self.assertFalse(capabilities.has_reasoning_provider({}))


class RenderFailureTest(unittest.TestCase):
    def test_message_names_every_missing_capability_with_effect_and_fix(self):
        text = capabilities.render_failure(["plan", "rerank", "web"])
        for name in ("plan", "rerank", "web"):
            self.assertIn(capabilities.CAPABILITIES[name]["label"], text)
            self.assertIn(capabilities.CAPABILITIES[name]["fix"], text)
        self.assertEqual(text.count("effect:"), 3)

    def test_message_says_how_to_opt_back_out(self):
        text = capabilities.render_failure(["web"])
        self.assertIn("--strict", text)
        self.assertIn("plan,rerank", text)

    def test_the_plan_fix_does_not_ask_for_an_api_key(self):
        """The hosting model writes the plan; suggesting a key would be wrong
        and would push users toward a cost they do not need."""
        fix = capabilities.CAPABILITIES["plan"]["fix"]
        self.assertIn("--plan", fix)
        self.assertIn("no API key", fix)


if __name__ == "__main__":
    unittest.main()

"""Settings UI (lib/settings_ui.py + `settings` topic-word dispatch).

Scenarios:
  1. Source state is derived from doctor's report, not a second registry:
     credential names come out of ``requires`` strings and are intersected
     with env.KEYCHAIN_KEYS, so prose never becomes an input box.
  2. No-secrets invariant: a seeded credential value never appears in the
     state payload the page renders (presence booleans only).
  3. Toggle semantics: opt-in sources write INCLUDE_SOURCES, working sources
     write EXCLUDE_SOURCES, and an unconfigured source offers no switch at
     all (it must not render as "on").
  4. remove_env_key drops the line, leaves other keys intact, and keeps 0o600.
  5. HTTP gates: missing/wrong token, non-loopback Host, and cross-site
     Origin on a write are all refused.
  6. Write allowlist: only an env.KEYCHAIN_KEYS name can be persisted.
  7. Topic-word dispatch: `settings` starts the UI; a research topic that
     merely contains the word does not (doctor/setup's exact-match rule).
"""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from lib import env, settings_ui

FAKE_REPORT = {
    "engine_version": "9.9.9",
    "generated_at": "2026-01-01T00:00:00Z",
    "setup": {"keys_present": {"BRAVE_API_KEY": False, "SCRAPECREATORS_API_KEY": False}},
    "sources": {
        "reddit": {
            "status": "ok",
            "tier": "ok",
            "requires": "none (public endpoints)",
            "backends": [
                {"name": "public", "status": "ok", "requires": "none (public endpoints)"},
                {
                    "name": "scrapecreators",
                    "status": "missing",
                    "requires": "SCRAPECREATORS_API_KEY",
                },
            ],
        },
        "tiktok": {
            "status": "unconfigured",
            "tier": "off",
            "requires": "SCRAPECREATORS_API_KEY",
        },
        "linkedin": {
            "status": "unconfigured",
            "tier": "off",
            "requires": "SCRAPECREATORS_API_KEY + INCLUDE_SOURCES=linkedin",
        },
        "arxiv": {
            "status": "opt-in",
            "tier": "off",
            "requires": "arxiv-pp-cli on the agent-subprocess PATH",
            "cli": {
                "name": "arxiv-pp-cli",
                "status": "missing",
                "optional": False,
                "off_path": False,
                "detail": "arxiv-pp-cli not found on PATH",
            },
        },
    },
}


def _state(config=None):
    return settings_ui.build_state(config or {}, FAKE_REPORT)


def _source(state, source_id):
    return next(s for s in state["sources"] if s["id"] == source_id)


def _key(state, name):
    return next(k for k in state["keys"] if k["name"] == name)


class BuildStateTest(unittest.TestCase):
    def test_credential_names_are_derived_from_requires(self):
        state = _state()
        self.assertEqual(_source(state, "tiktok")["keys"], ["SCRAPECREATORS_API_KEY"])
        # Backend-level requires count too: reddit's own line names no key,
        # but its ScrapeCreators backend does.
        self.assertEqual(_source(state, "reddit")["keys"], ["SCRAPECREATORS_API_KEY"])

    def test_non_credential_tokens_never_become_key_fields(self):
        """INCLUDE_SOURCES / PATH are uppercase but are not credentials."""
        linkedin = _source(_state(), "linkedin")
        self.assertEqual(linkedin["keys"], ["SCRAPECREATORS_API_KEY"])
        self.assertNotIn("INCLUDE_SOURCES", linkedin["keys"])
        self.assertNotIn("PATH", _source(_state(), "arxiv")["keys"])

    def test_key_usage_is_inverted_from_sources(self):
        unlocks = {u["id"] for u in _key(_state(), "SCRAPECREATORS_API_KEY")["unlocks"]}
        self.assertEqual(unlocks, {"reddit", "tiktok", "linkedin"})

    def test_categories(self):
        state = _state()
        self.assertEqual(_source(state, "reddit")["category"], "active")
        self.assertEqual(_source(state, "tiktok")["category"], "key")
        # A missing CLI outranks a missing key: installing the binary blocks.
        self.assertEqual(_source(state, "arxiv")["category"], "cli")

    def test_summary_counts_active_sources(self):
        state = _state()
        self.assertEqual(state["summary"]["active"], 1)
        self.assertEqual(state["summary"]["total"], 4)

    def test_requires_line_suppressed_when_it_only_repeats_key_names(self):
        state = _state()
        # "SCRAPECREATORS_API_KEY" above a SCRAPECREATORS_API_KEY input is noise.
        self.assertFalse(_source(state, "tiktok")["show_requires"])
        # linkedin's line still carries the INCLUDE_SOURCES token, so it stays.
        self.assertTrue(_source(state, "linkedin")["show_requires"])

    def test_key_presence_reads_live_config_not_just_report(self):
        state = _state({"SCRAPECREATORS_API_KEY": "seeded"})
        self.assertTrue(_key(state, "SCRAPECREATORS_API_KEY")["present"])
        self.assertFalse(_key(state, "BRAVE_API_KEY")["present"])

    def test_no_secret_values_in_payload(self):
        """The rendered page must never be able to leak a credential."""
        secret = "sk-seeded-secret-value-000"
        state = _state({"SCRAPECREATORS_API_KEY": secret, "BRAVE_API_KEY": secret})
        self.assertNotIn(secret, json.dumps(state))

    def test_every_keychain_key_is_offered(self):
        names = {k["name"] for k in _state()["keys"]}
        self.assertEqual(names, set(env.KEYCHAIN_KEYS))


class ToggleSemanticsTest(unittest.TestCase):
    def test_working_source_toggles_exclude(self):
        reddit = _source(_state(), "reddit")
        self.assertEqual(reddit["toggle_kind"], "exclude")
        self.assertTrue(reddit["toggle_on"])

    def test_excluded_source_reads_off(self):
        reddit = _source(_state({"EXCLUDE_SOURCES": "reddit"}), "reddit")
        self.assertEqual(reddit["toggle_kind"], "exclude")
        self.assertFalse(reddit["toggle_on"])

    def test_opt_in_source_toggles_include(self):
        state = _state({"INCLUDE_SOURCES": "linkedin"})
        linkedin = _source(state, "linkedin")
        self.assertEqual(linkedin["toggle_kind"], "include")
        self.assertTrue(linkedin["toggle_on"])
        self.assertEqual(linkedin["include_token"], "linkedin")

    def test_unconfigured_source_offers_no_switch(self):
        """A source missing its key must not render as enabled."""
        for source_id in ("tiktok", "arxiv"):
            record = _source(_state(), source_id)
            self.assertIsNone(record["toggle_kind"], source_id)
            self.assertFalse(record["toggle_on"], source_id)


class EnvWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / ".env"

    def test_remove_env_key_drops_only_that_line(self):
        self.env_path.write_text(
            "BRAVE_API_KEY=aaa\nSCRAPECREATORS_API_KEY=bbb\n# comment\n", encoding="utf-8"
        )
        os.chmod(self.env_path, 0o600)
        settings_ui.remove_env_key(self.env_path, "BRAVE_API_KEY")
        body = self.env_path.read_text(encoding="utf-8")
        self.assertNotIn("BRAVE_API_KEY", body)
        self.assertIn("SCRAPECREATORS_API_KEY=bbb", body)
        self.assertIn("# comment", body)

    def test_remove_env_key_preserves_0600(self):
        self.env_path.write_text("BRAVE_API_KEY=aaa\n", encoding="utf-8")
        os.chmod(self.env_path, 0o600)
        settings_ui.remove_env_key(self.env_path, "BRAVE_API_KEY")
        mode = stat.S_IMODE(self.env_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_remove_env_key_is_a_noop_when_absent(self):
        self.env_path.write_text("OTHER=1\n", encoding="utf-8")
        self.assertTrue(settings_ui.remove_env_key(self.env_path, "BRAVE_API_KEY"))
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), "OTHER=1\n")
        missing = Path(self.tmp.name) / "absent.env"
        self.assertTrue(settings_ui.remove_env_key(missing, "BRAVE_API_KEY"))
        self.assertFalse(missing.exists())

    def test_toggle_csv_adds_and_removes(self):
        with mock.patch.object(env, "CONFIG_FILE", self.env_path):
            tokens = settings_ui._toggle_csv("INCLUDE_SOURCES", "linkedin", True, {})
            self.assertEqual(tokens, ["linkedin"])
            self.assertIn("INCLUDE_SOURCES=linkedin", self.env_path.read_text())

            config = {"INCLUDE_SOURCES": "linkedin,perplexity"}
            tokens = settings_ui._toggle_csv("INCLUDE_SOURCES", "linkedin", False, config)
            self.assertEqual(tokens, ["perplexity"])

    def test_emptying_a_toggle_list_removes_the_line(self):
        with mock.patch.object(env, "CONFIG_FILE", self.env_path):
            settings_ui._toggle_csv("EXCLUDE_SOURCES", "reddit", True, {})
            settings_ui._toggle_csv(
                "EXCLUDE_SOURCES", "reddit", False, {"EXCLUDE_SOURCES": "reddit"}
            )
            self.assertNotIn("EXCLUDE_SOURCES", self.env_path.read_text(encoding="utf-8"))


class ServerTest(unittest.TestCase):
    """Exercises the real handler over a loopback socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / ".env"

        patcher = mock.patch.object(env, "CONFIG_FILE", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        report = mock.patch("lib.doctor.build_report", return_value=FAKE_REPORT)
        report.start()
        self.addCleanup(report.stop)
        config = mock.patch.object(env, "get_config", return_value={})
        config.start()
        self.addCleanup(config.stop)

        settings_ui._Handler.token = "test-token"
        settings_ui._Handler.config = {}
        settings_ui._Handler._report = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), settings_ui._Handler)
        self.port = self.httpd.server_address[1]
        settings_ui._Handler.port = self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _request(self, path, *, method="GET", token="test-token", headers=None, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if token:
            req.add_header("X-L30D-Token", token)
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except json.JSONDecodeError:
            return 200, None

    def test_state_requires_a_token(self):
        self.assertEqual(self._request("/api/state", token=None)[0], 403)
        self.assertEqual(self._request("/api/state", token="wrong")[0], 403)
        self.assertEqual(self._request("/api/state")[0], 200)

    def test_non_loopback_host_is_refused(self):
        """DNS-rebinding defense: a resolved-to-127.0.0.1 domain is not us."""
        status, _ = self._request("/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_cross_site_origin_cannot_write(self):
        status, _ = self._request(
            "/api/key",
            method="POST",
            headers={"Origin": "http://evil.example"},
            body={"name": "BRAVE_API_KEY", "value": "x"},
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.env_path.exists())

    def test_write_allowlist_rejects_unknown_names(self):
        status, _ = self._request(
            "/api/key", method="POST", body={"name": "PATH", "value": "/evil"}
        )
        self.assertEqual(status, 400)
        self.assertFalse(self.env_path.exists())

    def test_key_roundtrip(self):
        status, payload = self._request(
            "/api/key", method="POST", body={"name": "BRAVE_API_KEY", "value": "seeded"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)
        self.assertIn("BRAVE_API_KEY=seeded", self.env_path.read_text(encoding="utf-8"))

        status, payload = self._request(
            "/api/key", method="POST", body={"name": "BRAVE_API_KEY", "action": "remove"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("BRAVE_API_KEY", self.env_path.read_text(encoding="utf-8"))

    def test_empty_value_is_rejected(self):
        status, _ = self._request(
            "/api/key", method="POST", body={"name": "BRAVE_API_KEY", "value": "   "}
        )
        self.assertEqual(status, 400)

    def test_toggle_unknown_source_is_rejected(self):
        status, _ = self._request(
            "/api/toggle", method="POST", body={"source": "nope", "enabled": True}
        )
        self.assertEqual(status, 400)

    def test_page_carries_csp_and_the_session_token(self):
        url = f"http://127.0.0.1:{self.port}/?t=test-token"
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode()
        self.assertIn("Content-Security-Policy", body)
        self.assertIn("default-src 'none'", body)
        self.assertIn('const TOKEN = "test-token"', body)
        self.assertNotIn("__TOKEN__", body)


class DispatchTest(unittest.TestCase):
    def test_port_flag_parsing(self):
        import last30days

        self.assertEqual(last30days._split_settings_port(["--port", "9000"]), (9000, []))
        self.assertEqual(last30days._split_settings_port(["--port=9000"]), (9000, []))
        self.assertEqual(last30days._split_settings_port(["--no-open"]), (None, ["--no-open"]))
        # Out-of-range and non-numeric values fall through to the allowlist
        # error rather than being silently ignored.
        self.assertEqual(last30days._split_settings_port(["--port=99999"])[1], ["--port=99999"])
        self.assertEqual(last30days._split_settings_port(["--port=abc"])[1], ["--port=abc"])

    def test_topic_word_dispatch_is_exact_match(self):
        """`settings` starts the UI; a research topic containing it does not."""
        import last30days

        with mock.patch("lib.settings_ui.serve", return_value=0) as serve:
            with mock.patch.object(sys, "argv", ["last30days.py", "settings", "--no-open"]):
                last30days.main()
            self.assertEqual(serve.call_count, 1)

        with mock.patch("lib.settings_ui.serve", return_value=0) as serve:
            with mock.patch("lib.pipeline.run", side_effect=RuntimeError("researched")):
                with mock.patch.object(
                    sys, "argv", ["last30days.py", "chrome settings redesign", "--mock"]
                ):
                    try:
                        last30days.main()
                    except Exception:
                        pass
            serve.assert_not_called()

    def test_serve_refuses_clean_config_mode(self):
        with mock.patch.object(env, "CONFIG_FILE", None):
            self.assertEqual(settings_ui.serve({}, open_browser=False), 2)


if __name__ == "__main__":
    unittest.main()

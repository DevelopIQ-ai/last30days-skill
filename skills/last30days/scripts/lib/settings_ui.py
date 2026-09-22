"""Local settings UI: a localhost page for source status and credentials.

``last30days.py settings`` starts a stdlib HTTP server bound to the loopback
interface and opens a single-page app. There is no build step, no npm, and no
third-party dependency -- the engine ships with ``dependencies = []`` and this
module keeps that promise (HTML/CSS/JS live inline the same way
``lib/html_render.py`` keeps its report CSS inline).

Two seams, no third source of truth:

- **Read** is ``doctor.build_report`` (U4). Every source, backend chain, CLI
  dependency, probe result, and fix string the page shows is doctor's, so the
  page and ``last30days doctor`` can never disagree about what is configured.
- **Write** is ``setup_wizard.write_api_key`` with ``replace=True`` -- the same
  0o600 secret-safe path ``setup --store-key`` uses, gated by the same
  ``env.KEYCHAIN_KEYS`` allowlist.

Security posture (a local web server that edits a credential file):

- binds 127.0.0.1 only, on an ephemeral port unless one is requested;
- every request carries a ``secrets.token_urlsafe`` session token compared with
  ``compare_digest``; the token dies with the process;
- ``Host`` is pinned to loopback and ``Origin`` is checked on writes, so a page
  in the user's browser cannot drive this server via DNS rebinding or CSRF;
- credential **values never leave the machine's disk**: the state payload
  carries booleans, never secrets, so a rendered page cannot leak a key;
- CSP forbids every external fetch, so nothing on the page can exfiltrate.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import env, source_icons

# Largest request body accepted on a write endpoint. A credential is a token,
# not a payload; anything larger is a mistake or an attack.
MAX_BODY_BYTES = 8192

# Non-secret config keys the page may rewrite. Credentials use the separate
# KEYCHAIN_KEYS allowlist; these two steer source selection and are safe to
# echo back to the page (they are names, not secrets).
TOGGLE_KEYS = ("INCLUDE_SOURCES", "EXCLUDE_SOURCES")

# Display names for sources whose id does not title-case correctly.
SOURCE_LABELS = {
    "arxiv": "arXiv",
    "github": "GitHub",
    "hackernews": "Hacker News",
    "linkedin": "LinkedIn",
    "meta_ads": "Meta Ads",
    "stocktwits": "StockTwits",
    "tiktok": "TikTok",
    "truthsocial": "Truth Social",
    "web": "Web search",
    "x": "X / Twitter",
    "xiaohongshu": "Xiaohongshu",
    "youtube": "YouTube",
}

# One line per source, describing what it contributes to a report. doctor
# speaks in health terms ("BSKY_HANDLE not set"); a settings page also has to
# answer "why would I turn this on?", which no existing surface states.
SOURCE_BLURBS = {
    "amazon": "Verified-purchase reviews on real products.",
    "arxiv": "The papers behind the hype, inside the window.",
    "bluesky": "Open-network posts without an X credential.",
    "digg": "Editorially resurfaced links with community votes.",
    "github": "Repos, releases, and issues people actually star.",
    "hackernews": "Builder-heavy debate with visible point counts.",
    "instagram": "Creator posts and their top comments.",
    "jobs": "Hiring posts as a demand signal for company topics.",
    "library": "Your own saved reports, searched locally.",
    "linkedin": "Professional and company-account posting.",
    "meta_ads": "What a brand is actually paying to say.",
    "perplexity": "A second research agent's cited answer.",
    "pinterest": "Visual-trend and product-discovery signal.",
    "polymarket": "Real money odds, not pundit opinion.",
    "reddit": "The unfiltered take, with true upvote counts.",
    "techmeme": "The tech-news editorial layer, date-windowed.",
    "telegram": "Public channel chatter from named groups.",
    "threads": "Meta's text network, creator-side.",
    "tiktok": "Short-form reach plus the comments under it.",
    "trustpilot": "Customer complaints at brand scale.",
    "truthsocial": "Posts that never cross-post elsewhere.",
    "web": "General web results to ground everything else.",
    "x": "Real-time reaction, engagement-ranked.",
    "xiaohongshu": "Chinese-language product and lifestyle posts.",
    "youtube": "Long-form explanation plus top comments.",
}

# Credential display metadata: label, where to sign up, and what it costs.
# Deliberately NOT a gating registry -- which key unlocks which source is
# derived from doctor's ``requires`` strings below, so this table can never
# drift out of sync with the engine's actual availability logic.
KEY_META: dict[str, dict[str, Any]] = {
    "SCRAPECREATORS_API_KEY": {
        "label": "ScrapeCreators",
        "signup": "https://scrapecreators.com",
        "cost": "10,000 free calls",
        "note": "One key unlocks TikTok, Instagram, Threads, Pinterest, LinkedIn, and YouTube comments. The GitHub signup flow grants the free tier automatically.",
    },
    "BRAVE_API_KEY": {
        "label": "Brave Search",
        "signup": "https://brave.com/search/api/",
        "cost": "2,000 free queries/mo",
        "note": "Preferred web-search backend.",
    },
    "EXA_API_KEY": {
        "label": "Exa",
        "signup": "https://exa.ai",
        "cost": "Paid",
        "note": "Neural web search; alternative to Brave.",
    },
    "SERPER_API_KEY": {
        "label": "Serper",
        "signup": "https://serper.dev",
        "cost": "Free tier",
        "note": "Google SERP proxy; alternative to Brave.",
    },
    "PARALLEL_API_KEY": {
        "label": "Parallel",
        "signup": "https://parallel.ai",
        "cost": "Free tier",
        "note": "Optional bearer auth for the hosted Parallel search MCP.",
    },
    "GETXAPI_KEY": {
        "label": "GetXAPI",
        "signup": "https://getxapi.com",
        "cost": "Credits",
        "note": "X search with no browser cookies and no X account. Pin it with LAST30DAYS_X_BACKEND=getxapi.",
    },
    "XQUIK_API_KEY": {
        "label": "xquik",
        "signup": "https://xquik.com",
        "cost": "Credits",
        "note": "X search backend.",
    },
    "XAI_API_KEY": {
        "label": "xAI / Grok",
        "signup": "https://x.ai/api",
        "cost": "Credits",
        "note": "Grok live search as an X backend.",
    },
    "X_BEARER_TOKEN": {
        "label": "X API v2",
        "signup": "https://developer.x.com",
        "cost": "X developer project",
        "note": "Official X API. Recent posts only (about a week) unless your project has full-archive access.",
    },
    "PERPLEXITY_API_KEY": {
        "label": "Perplexity",
        "signup": "https://www.perplexity.ai/settings/api",
        "cost": "Credits",
        "note": "Opt-in research source.",
    },
    "OPENROUTER_API_KEY": {
        "label": "OpenRouter",
        "signup": "https://openrouter.ai/keys",
        "cost": "Credits",
        "note": "Reranking and synthesis models; also backs the Perplexity source.",
    },
    "OPENAI_API_KEY": {
        "label": "OpenAI",
        "signup": "https://platform.openai.com/api-keys",
        "cost": "Credits",
        "note": "Reranking, synthesis, and transcription.",
    },
    "GOOGLE_API_KEY": {
        "label": "Google AI",
        "signup": "https://aistudio.google.com/apikey",
        "cost": "Free tier",
        "note": "Alternative reasoning provider.",
    },
    "GEMINI_API_KEY": {
        "label": "Gemini",
        "signup": "https://aistudio.google.com/apikey",
        "cost": "Free tier",
        "note": "Alternative reasoning provider.",
    },
    "GOOGLE_GENAI_API_KEY": {
        "label": "Google GenAI",
        "signup": "https://aistudio.google.com/apikey",
        "cost": "Free tier",
        "note": "Alternative reasoning provider.",
    },
    "GITHUB_TOKEN": {
        "label": "GitHub",
        "signup": "https://github.com/settings/tokens",
        "cost": "Free",
        "note": "Optional. GitHub works unauthenticated; a token only raises the rate limit.",
    },
    "BSKY_HANDLE": {
        "label": "Bluesky handle",
        "signup": "https://bsky.app",
        "cost": "Free",
        "note": "Your handle, e.g. name.bsky.social.",
        "secret": False,
    },
    "BSKY_APP_PASSWORD": {
        "label": "Bluesky app password",
        "signup": "https://bsky.app/settings/app-passwords",
        "cost": "Free",
        "note": "A 19-character app password (xxxx-xxxx-xxxx-xxxx). Never your account password.",
    },
    "TRUTHSOCIAL_TOKEN": {
        "label": "Truth Social",
        "signup": "",
        "cost": "Free",
        "note": "Session token from a logged-in account.",
    },
    "BRIGHTDATA_API_KEY": {
        "label": "Bright Data",
        "signup": "https://brightdata.com",
        "cost": "Paid",
        "note": "Backs the Amazon review source; also needs the brightdata CLI on PATH.",
    },
    "APIFY_API_TOKEN": {
        "label": "Apify",
        "signup": "https://apify.com",
        "cost": "Free tier",
        "note": "Fallback scraping actor.",
    },
    "XIAOHONGSHU_API_BASE": {
        "label": "Xiaohongshu service",
        "signup": "",
        "cost": "Self-hosted",
        "note": "Base URL of your logged-in Xiaohongshu browser-session service.",
        "secret": False,
    },
    "AUTH_TOKEN": {
        "label": "X cookie: auth_token",
        "signup": "",
        "cost": "Free",
        "note": "Set automatically by browser-cookie consent during setup. Paste manually only if you know what you are doing.",
    },
    "CT0": {
        "label": "X cookie: ct0",
        "signup": "",
        "cost": "Free",
        "note": "Paired with auth_token. Set automatically by setup.",
    },
}

# An env var name inside a doctor ``requires`` string, e.g.
# "SCRAPECREATORS_API_KEY + INCLUDE_SOURCES=linkedin".
_ENV_TOKEN = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")
_INCLUDE_TOKEN = re.compile(r"INCLUDE_SOURCES=([a-z_]+)")


def _label_for(source_id: str) -> str:
    if source_id in SOURCE_LABELS:
        return SOURCE_LABELS[source_id]
    return source_id.replace("_", " ").title()


def _keys_in(text: str) -> list[str]:
    """Credential names mentioned in a doctor ``requires``/``fix`` string.

    Intersecting with ``env.KEYCHAIN_KEYS`` is what keeps this honest: only a
    name the engine actually loads from .env can be surfaced as a key field, so
    prose like "INCLUDE_SOURCES" or "PATH" never becomes an input box.
    """
    if not text:
        return []
    found = [t for t in _ENV_TOKEN.findall(text) if t in env.KEYCHAIN_KEYS]
    return list(dict.fromkeys(found))


def _split_csv(value: Any) -> list[str]:
    return [t.strip().lower() for t in str(value or "").split(",") if t.strip()]


def _category(record: dict[str, Any], keys: list[str]) -> str:
    """Bucket a source for the page's filter chips.

    Ordering matters: a source that is working is "active" regardless of what
    else it could use, and a missing CLI outranks a missing key because
    installing the binary is the blocking step.
    """
    if record.get("status") == "ok":
        return "active"
    cli = record.get("cli") or {}
    if cli and cli.get("status") not in (None, "ok") and not cli.get("optional"):
        return "cli"
    if keys:
        return "key"
    return "optin"


def build_state(config: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Assemble the page payload from a doctor report. Contains no secrets."""
    include_tokens = set(_split_csv(config.get("INCLUDE_SOURCES")))
    exclude_tokens = set(_split_csv(config.get("EXCLUDE_SOURCES")))
    keys_present = dict((report.get("setup") or {}).get("keys_present") or {})

    sources: list[dict[str, Any]] = []
    key_usage: dict[str, list[str]] = {}

    for source_id, record in sorted((report.get("sources") or {}).items()):
        requires = str(record.get("requires") or "")
        backends = []
        for backend in record.get("backends") or []:
            backend_requires = str(backend.get("requires") or "")
            backends.append(
                {
                    "name": backend.get("name"),
                    "status": backend.get("status"),
                    "requires": backend_requires,
                    "detail": backend.get("detail") or "",
                    "fix": backend.get("fix") or "",
                    "keys": _keys_in(backend_requires),
                }
            )

        keys = _keys_in(requires)
        for backend in backends:
            for name in backend["keys"]:
                if name not in keys:
                    keys.append(name)
        for name in keys:
            key_usage.setdefault(name, []).append(source_id)

        include_match = _INCLUDE_TOKEN.search(requires)
        include_token = include_match.group(1) if include_match else None

        # What the card's switch actually means. An opt-in source toggles its
        # INCLUDE_SOURCES token (meaningful before its key exists -- consent
        # and credential are separate steps). A working source toggles
        # EXCLUDE_SOURCES. A source still missing a key or CLI gets no switch
        # at all: rendering one "on" would claim the source is in play when
        # the engine would skip it.
        if include_token:
            toggle_kind, toggle_on = "include", include_token in include_tokens
        elif record.get("status") == "ok":
            toggle_kind, toggle_on = "exclude", source_id not in exclude_tokens
        else:
            toggle_kind, toggle_on = None, False

        # doctor's `requires` is worth printing only when it says something the
        # key fields below it do not already show ("SCRAPECREATORS_API_KEY"
        # above a SCRAPECREATORS_API_KEY input is noise).
        residual = requires
        for name in keys:
            residual = residual.replace(name, "")
        show_requires = bool(residual.strip(" +,;"))

        cli = record.get("cli") or None
        label = _label_for(source_id)
        sources.append(
            {
                "id": source_id,
                "label": label,
                "icon": source_icons.icon_for_source(source_id, label),
                "blurb": SOURCE_BLURBS.get(source_id, ""),
                "category": _category(record, keys),
                "status": record.get("status"),
                "tier": record.get("tier"),
                "requires": requires,
                "show_requires": show_requires,
                "toggle_kind": toggle_kind,
                "toggle_on": toggle_on,
                "detail": record.get("detail") or "",
                "note": record.get("note") or "",
                "fix": record.get("fix") or "",
                "keys": keys,
                "backends": backends,
                "cli": (
                    {
                        "name": cli.get("name"),
                        "status": cli.get("status"),
                        "detail": cli.get("detail") or "",
                        "optional": bool(cli.get("optional")),
                        "off_path": bool(cli.get("off_path")),
                    }
                    if cli
                    else None
                ),
                "probe": record.get("probe") or None,
                "include_token": include_token,
                "included": bool(include_token and include_token in include_tokens),
                "excluded": source_id in exclude_tokens,
                "active_backend": record.get("active_backend"),
                "pin_var": record.get("pin_var"),
            }
        )

    # Every credential the engine can load, whether or not a source named it.
    keys: list[dict[str, Any]] = []
    for name in env.KEYCHAIN_KEYS:
        meta = KEY_META.get(name, {})
        present = bool(keys_present.get(name)) or bool(config.get(name))
        key_label = meta.get("label") or name.replace("_", " ").title()
        keys.append(
            {
                "name": name,
                "label": key_label,
                "icon": source_icons.icon_for_key(name, key_label),
                "signup": meta.get("signup") or "",
                "cost": meta.get("cost") or "",
                "note": meta.get("note") or "",
                # A handle or a base URL is not a secret; masking it only
                # makes it harder to check for a typo.
                "secret": meta.get("secret", True),
                "present": present,
                "unlocks": [
                    {"id": s, "label": _label_for(s)}
                    for s in sorted(set(key_usage.get(name, [])))
                ],
            }
        )

    active = sum(1 for s in sources if s["category"] == "active")
    return {
        "engine_version": report.get("engine_version") or "",
        "config_file": str(env.CONFIG_FILE) if env.CONFIG_FILE else "",
        "config_writable": env.CONFIG_FILE is not None,
        "generated_at": report.get("generated_at") or "",
        "summary": {
            "active": active,
            "total": len(sources),
            "keys_set": sum(1 for k in keys if k["present"]),
        },
        "sources": sources,
        "keys": keys,
        "include_sources": sorted(include_tokens),
        "exclude_sources": sorted(exclude_tokens),
    }


def remove_env_key(env_path: Path, key_name: str) -> bool:
    """Drop every ``key_name=`` line from the .env, preserving 0o600.

    Mirrors ``setup_wizard._replace_env_line``'s write discipline: the
    rewritten file is created at 0o600 on a sibling temp path and moved over
    the original, so the credential file never has a readable window and a
    crash mid-write leaves the previous file intact.
    """
    env_path = Path(env_path)
    if not env_path.exists():
        return True
    content = env_path.read_text(encoding="utf-8")
    kept = []
    removed = False
    for line in content.splitlines():
        stripped = line.strip()
        is_key = (
            stripped
            and not stripped.startswith("#")
            and "=" in stripped
            and stripped.split("=", 1)[0].strip() == key_name
        )
        if is_key:
            removed = True
            continue
        kept.append(line)
    if not removed:
        return True
    body = "\n".join(kept)
    if body and not body.endswith("\n"):
        body += "\n"
    tmp_path = env_path.with_name(env_path.name + ".tmp")
    fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(tmp_path, env_path)
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    return True


def _write_value(key_name: str, value: str) -> bool:
    from . import setup_wizard

    if not value:
        return remove_env_key(env.CONFIG_FILE, key_name)
    return bool(
        setup_wizard.write_api_key(
            env.CONFIG_FILE, value, key_name=key_name, replace=True
        )
    )


def _toggle_csv(key_name: str, token: str, enabled: bool, config: dict[str, Any]) -> list[str]:
    """Add/remove ``token`` in a comma-separated config key; return the result."""
    tokens = _split_csv(config.get(key_name))
    if enabled and token not in tokens:
        tokens.append(token)
    elif not enabled and token in tokens:
        tokens = [t for t in tokens if t != token]
    _write_value(key_name, ",".join(tokens))
    return tokens


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src data:; form-action 'none'; base-uri 'none'">
<meta name="referrer" content="no-referrer">
<title>last30days settings</title>
<style>
:root {
  --bg: #0e0e10; --bg-elev: #18181b; --bg-sunk: #131316;
  --fg: #fafafa; --fg-muted: #a1a1aa; --fg-subtle: #71717a;
  --accent: #a855f7; --accent-soft: #c4b5fd;
  --border: #27272a; --border-strong: #3f3f46;
  --ok: #22c55e; --key: #f59e0b; --cli: #60a5fa; --optin: #a855f7; --off: #52525b;
  --mono: 'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #ffffff; --bg-elev: #fafafa; --bg-sunk: #f4f4f5;
    --fg: #18181b; --fg-muted: #52525b; --fg-subtle: #71717a;
    --accent: #7c3aed; --accent-soft: #6d28d9;
    --border: #e4e4e7; --border-strong: #d4d4d8;
    --ok: #16a34a; --key: #d97706; --cli: #2563eb; --optin: #7c3aed; --off: #a1a1aa;
  }
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; background: var(--bg); color: var(--fg);
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif;
  font-size: 15px; line-height: 1.6;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1160px; margin: 0 auto; padding: 0 1.5rem 5rem; }

header {
  position: sticky; top: 0; z-index: 20;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
}
.head-in { max-width: 1160px; margin: 0 auto; padding: 1.1rem 1.5rem; display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
.brand { font-weight: 650; letter-spacing: -0.02em; font-size: 1.05rem; }
.brand .dot { color: var(--accent); }
.ver { font-family: var(--mono); font-size: 0.72rem; color: var(--fg-subtle); border: 1px solid var(--border); border-radius: 999px; padding: 0.1rem 0.5rem; }
.spacer { flex: 1; }
.stat { font-size: 0.82rem; color: var(--fg-muted); font-variant-numeric: tabular-nums; }
.stat b { color: var(--fg); font-weight: 600; }

button {
  font: inherit; cursor: pointer; border-radius: 8px;
  border: 1px solid var(--border-strong); background: var(--bg-elev); color: var(--fg);
  padding: 0.42rem 0.8rem; font-size: 0.83rem; transition: border-color .15s, background .15s;
}
button:hover:not(:disabled) { border-color: var(--accent); }
button:disabled { opacity: 0.45; cursor: default; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 550; }
button.ghost { background: transparent; border-color: transparent; color: var(--fg-subtle); padding: 0.3rem 0.5rem; }
button.ghost:hover { color: var(--fg); }

.tabs { display: flex; gap: 0.25rem; margin: 2rem 0 1.25rem; border-bottom: 1px solid var(--border); }
.tab { background: none; border: none; border-bottom: 2px solid transparent; border-radius: 0; color: var(--fg-subtle); padding: 0.6rem 0.9rem; font-size: 0.9rem; }
.tab:hover { color: var(--fg); border-color: transparent; }
.tab.on { color: var(--fg); border-bottom-color: var(--accent); font-weight: 550; }

.toolbar { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; margin-bottom: 1.25rem; }
.chip { border-radius: 999px; padding: 0.3rem 0.75rem; font-size: 0.8rem; color: var(--fg-muted); background: var(--bg-elev); }
.chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }
.chip .n { opacity: 0.65; margin-left: 0.3rem; font-variant-numeric: tabular-nums; }
input[type=search], input[type=password], input[type=text] {
  font: inherit; background: var(--bg-sunk); color: var(--fg);
  border: 1px solid var(--border); border-radius: 8px; padding: 0.45rem 0.7rem; font-size: 0.85rem;
}
input:focus { outline: none; border-color: var(--accent); }
input[type=search] { min-width: 220px; }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 0.9rem; }
.card {
  background: var(--bg-elev); border: 1px solid var(--border); border-radius: 12px;
  padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: 0.55rem;
}
.card:hover { border-color: var(--border-strong); }
.card-top { display: flex; align-items: center; gap: 0.55rem; }
.name { font-weight: 600; letter-spacing: -0.01em; }

/* Brand marks. Colour is carried per-element as two custom properties so a
   near-black mark (X, TikTok, GitHub) can lighten in dark mode without
   losing its real colour in light mode. */
.logo {
  width: 26px; height: 26px; border-radius: 7px; flex: none;
  display: grid; place-items: center;
  background: var(--bg-sunk); border: 1px solid var(--border);
  color: var(--ic-d);
}
@media (prefers-color-scheme: light) { .logo { color: var(--ic-l); } }
.logo svg { width: 15px; height: 15px; fill: currentColor; display: block; }
.logo .mono { font-family: var(--mono); font-size: 0.62rem; font-weight: 700; letter-spacing: -0.02em; color: currentColor; }
.kcard .logo { width: 30px; height: 30px; border-radius: 8px; }
.kcard .logo svg { width: 17px; height: 17px; }

.pill { font-family: var(--mono); font-size: 0.66rem; letter-spacing: 0.04em; text-transform: uppercase; padding: 0.12rem 0.45rem; border-radius: 5px; border: 1px solid var(--border-strong); color: var(--fg-subtle); }
.pill.active { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 45%, transparent); }
.pill.key { color: var(--key); border-color: color-mix(in srgb, var(--key) 45%, transparent); }
.pill.cli { color: var(--cli); border-color: color-mix(in srgb, var(--cli) 45%, transparent); }
.pill.optin { color: var(--optin); border-color: color-mix(in srgb, var(--optin) 45%, transparent); }
.pill.excluded { color: var(--off); }
.blurb { font-size: 0.85rem; color: var(--fg-muted); }
.req { font-family: var(--mono); font-size: 0.72rem; color: var(--fg-subtle); word-break: break-word; }
.fix { font-size: 0.78rem; color: var(--fg-muted); border-left: 2px solid var(--key); padding-left: 0.6rem; }

.switch { display: flex; align-items: center; gap: 0.5rem; margin-left: auto; }
.track { width: 34px; height: 19px; border-radius: 999px; background: var(--border-strong); position: relative; transition: background .16s; flex: none; }
.track.on { background: var(--accent); }
.knob { position: absolute; top: 2px; left: 2px; width: 15px; height: 15px; border-radius: 50%; background: #fff; transition: transform .16s; }
.track.on .knob { transform: translateX(15px); }

.kfield { display: flex; flex-direction: column; gap: 0.25rem; }
.keyrow { display: flex; gap: 0.45rem; align-items: center; }
.keyrow input { flex: 1; min-width: 0; font-family: var(--mono); font-size: 0.78rem; }
.kname { font-family: var(--mono); font-size: 0.7rem; color: var(--fg-subtle); }
.set { color: var(--ok); font-size: 0.75rem; font-weight: 550; }
.muted-hint { margin-left: auto; font-size: 0.7rem; color: var(--fg-subtle); }

details.bk { border-top: 1px solid var(--border); padding-top: 0.5rem; }
details.bk summary { cursor: pointer; font-size: 0.76rem; color: var(--fg-subtle); list-style: none; }
details.bk summary::-webkit-details-marker { display: none; }
details.bk summary:before { content: '▸ '; }
details.bk[open] summary:before { content: '▾ '; }
.bkrow { display: flex; gap: 0.5rem; align-items: baseline; font-size: 0.76rem; padding: 0.22rem 0 0.22rem 0.9rem; }
.bkrow .bkname { font-family: var(--mono); color: var(--fg-muted); min-width: 6.5rem; }
.bkrow .bkreq { color: var(--fg-subtle); font-size: 0.72rem; }
.s-ok { color: var(--ok); } .s-missing { color: var(--fg-subtle); } .s-degraded { color: var(--key); } .s-error { color: #ef4444; }

.klist { display: flex; flex-direction: column; gap: 0.75rem; }
.kcard { background: var(--bg-elev); border: 1px solid var(--border); border-radius: 12px; padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: 0.5rem; }
.kcard.has { border-left: 3px solid var(--ok); }
.ktop { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
.cost { font-size: 0.7rem; color: var(--fg-subtle); border: 1px solid var(--border-strong); border-radius: 999px; padding: 0.08rem 0.5rem; }
.unlocks { display: flex; gap: 0.3rem; flex-wrap: wrap; }
.tag { font-size: 0.68rem; background: var(--bg-sunk); border: 1px solid var(--border); border-radius: 5px; padding: 0.05rem 0.4rem; color: var(--fg-muted); }

.note { font-size: 0.82rem; color: var(--fg-muted); }
.empty { color: var(--fg-subtle); padding: 3rem 0; text-align: center; }
.banner { background: var(--bg-elev); border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: 10px; padding: 0.9rem 1.1rem; margin-bottom: 1.25rem; font-size: 0.85rem; color: var(--fg-muted); }
.banner code { font-family: var(--mono); font-size: 0.78rem; color: var(--accent-soft); }

#toast { position: fixed; bottom: 1.5rem; left: 50%; transform: translateX(-50%) translateY(150%); background: var(--bg-elev); border: 1px solid var(--border-strong); border-radius: 10px; padding: 0.7rem 1.1rem; font-size: 0.85rem; transition: transform .22s; z-index: 50; max-width: 90vw; }
#toast.show { transform: translateX(-50%) translateY(0); }
#toast.bad { border-color: #ef4444; }
#toast.good { border-color: var(--ok); }
.skel { color: var(--fg-subtle); padding: 4rem 0; text-align: center; }
</style>
</head>
<body>
<header><div class="head-in">
  <span class="brand">last30days<span class="dot">.</span></span>
  <span class="ver" id="ver"></span>
  <span class="spacer"></span>
  <span class="stat" id="stat"></span>
  <button id="refresh">Re-check</button>
</div></header>

<div class="wrap">
  <div class="tabs">
    <button class="tab on" data-tab="sources">Sources</button>
    <button class="tab" data-tab="keys">API keys</button>
  </div>

  <div id="pane-sources">
    <div class="toolbar">
      <button class="chip on" data-filter="all">All<span class="n" id="n-all"></span></button>
      <button class="chip" data-filter="active">Active<span class="n" id="n-active"></span></button>
      <button class="chip" data-filter="key">Needs a key<span class="n" id="n-key"></span></button>
      <button class="chip" data-filter="cli">Needs a CLI<span class="n" id="n-cli"></span></button>
      <button class="chip" data-filter="optin">Opt-in<span class="n" id="n-optin"></span></button>
      <span class="spacer"></span>
      <input type="search" id="q" placeholder="Filter sources…  (/)" autocomplete="off">
    </div>
    <div class="grid" id="sources"><div class="skel">Checking sources…</div></div>
  </div>

  <div id="pane-keys" hidden>
    <div class="banner" id="keybanner"></div>
    <div class="klist" id="keys"></div>
  </div>
</div>

<div id="toast"></div>

<script>
const TOKEN = "__TOKEN__";
let STATE = null, FILTER = "all", QUERY = "";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
};

// Built with createElementNS rather than innerHTML so the app has no HTML-
// injection sink anywhere, even for engine-controlled path data.
const SVG_NS = "http://www.w3.org/2000/svg";
function logo(icon, label) {
  const box = el("span", "logo");
  box.title = label;
  if (!icon) return box;
  box.style.setProperty("--ic-l", icon.light);
  box.style.setProperty("--ic-d", icon.dark);
  if (icon.kind === "mono") {
    box.appendChild(el("span", "mono", icon.text));
    return box;
  }
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", icon.d);
  svg.appendChild(path);
  box.appendChild(svg);
  return box;
}

function toast(msg, kind) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show " + (kind || "");
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = ""; }, 2600);
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({
    headers: { "X-L30D-Token": TOKEN, "Content-Type": "application/json" },
  }, opts || {}));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
  return body;
}

function statusWord(s) {
  if (s.excluded) return "off";
  return { active: "active", key: "needs key", cli: "needs cli", optin: "opt-in" }[s.category] || s.status;
}

function sourceCard(s) {
  const card = el("div", "card");

  const top = el("div", "card-top");
  top.appendChild(logo(s.icon, s.label));
  top.appendChild(el("span", "name", s.label));
  top.appendChild(el("span", "pill " + (s.excluded ? "excluded" : s.category), statusWord(s)));

  // A switch appears only when flipping it would change what a run does:
  // opt-in sources write INCLUDE_SOURCES, working sources write
  // EXCLUDE_SOURCES, and a source still missing a key or CLI gets none.
  if (s.toggle_kind) {
    const sw = el("label", "switch");
    const track = el("span", "track" + (s.toggle_on ? " on" : ""));
    track.appendChild(el("span", "knob"));
    sw.appendChild(track);
    sw.onclick = async () => {
      const turningOn = !track.classList.contains("on");
      track.classList.toggle("on");
      try {
        await api("/api/toggle", { method: "POST", body: JSON.stringify({ source: s.id, enabled: turningOn }) });
        toast(s.label + (turningOn ? " enabled" : " disabled"), "good");
        await load(false);
      } catch (e) { track.classList.toggle("on"); toast(e.message, "bad"); }
    };
    top.appendChild(sw);
  }
  card.appendChild(top);

  if (s.blurb) card.appendChild(el("div", "blurb", s.blurb));
  if (s.show_requires) card.appendChild(el("div", "req", s.requires));

  for (const name of s.keys) {
    const meta = STATE.keys.find((k) => k.name === name);
    card.appendChild(keyField(meta || { name, present: false, label: name }, true));
  }

  if (s.cli && s.cli.status !== "ok") {
    const line = el("div", "fix");
    line.textContent = s.cli.off_path
      ? s.cli.name + " is installed but not on the agent's PATH"
      : (s.fix || s.cli.detail || (s.cli.name + " is not installed"));
    card.appendChild(line);
  } else if (s.category !== "active" && s.fix && !s.keys.length) {
    card.appendChild(el("div", "fix", s.fix));
  }

  if (s.backends && s.backends.length) {
    const d = el("details", "bk");
    d.appendChild(el("summary", null, s.backends.length + " backends"));
    for (const b of s.backends) {
      const row = el("div", "bkrow");
      row.appendChild(el("span", "bkname", b.name));
      row.appendChild(el("span", "s-" + (b.status || "missing"), b.status || ""));
      row.appendChild(el("span", "bkreq", b.requires || ""));
      d.appendChild(row);
    }
    card.appendChild(d);
  }
  return card;
}

function keyField(meta, compact) {
  const wrap = el("div", "kfield");
  if (compact) wrap.appendChild(el("div", "kname", meta.name));
  const row = el("div", "keyrow");
  wrap.appendChild(row);

  const input = el("input");
  input.type = meta.secret === false ? "text" : "password";
  input.placeholder = meta.present
    ? (meta.secret === false ? "stored — type to replace" : "•••••••• stored")
    : (meta.secret === false ? "value…" : "paste key…");
  input.autocomplete = "off";
  input.spellcheck = false;
  row.appendChild(input);

  const save = el("button", "primary", "Save");
  save.onclick = async () => {
    const value = input.value.trim();
    if (!value) { toast("Nothing to save", "bad"); return; }
    save.disabled = true;
    try {
      await api("/api/key", { method: "POST", body: JSON.stringify({ name: meta.name, value }) });
      input.value = "";
      toast(meta.name + " saved", "good");
      await load(false);
    } catch (e) { toast(e.message, "bad"); } finally { save.disabled = false; }
  };
  input.onkeydown = (e) => { if (e.key === "Enter") save.click(); };
  row.appendChild(save);

  if (meta.present) {
    const rm = el("button", "ghost", "Remove");
    rm.onclick = async () => {
      if (!confirm("Remove " + meta.name + " from your .env?")) return;
      try {
        await api("/api/key", { method: "POST", body: JSON.stringify({ name: meta.name, action: "remove" }) });
        toast(meta.name + " removed", "good");
        await load(false);
      } catch (e) { toast(e.message, "bad"); }
    };
    row.appendChild(rm);
  }
  return wrap;
}

function keyCard(k) {
  const card = el("div", "kcard" + (k.present ? " has" : ""));
  const top = el("div", "ktop");
  top.appendChild(logo(k.icon, k.label));
  top.appendChild(el("span", "name", k.label));
  if (k.present) top.appendChild(el("span", "set", "✓ set"));
  if (k.cost) top.appendChild(el("span", "cost", k.cost));
  top.appendChild(el("span", "spacer"));
  if (k.signup) {
    const a = el("a", null, "Get a key ↗");
    a.href = k.signup; a.target = "_blank"; a.rel = "noreferrer noopener";
    top.appendChild(a);
  }
  card.appendChild(top);
  card.appendChild(el("div", "kname", k.name));
  if (k.note) card.appendChild(el("div", "note", k.note));
  if (k.unlocks.length) {
    const u = el("div", "unlocks");
    u.appendChild(el("span", "kname", "unlocks"));
    for (const s of k.unlocks) u.appendChild(el("span", "tag", s.label));
    card.appendChild(u);
  }
  card.appendChild(keyField(k, false));
  return card;
}

function render() {
  if (!STATE) return;
  $("ver").textContent = "v" + STATE.engine_version;
  const sm = STATE.summary;
  $("stat").innerHTML = "";
  const stat = $("stat");
  stat.appendChild(el("b", null, sm.active));
  stat.appendChild(document.createTextNode(" of " + sm.total + " sources active · "));
  stat.appendChild(el("b", null, sm.keys_set));
  stat.appendChild(document.createTextNode(" keys set"));

  const counts = { all: STATE.sources.length, active: 0, key: 0, cli: 0, optin: 0 };
  for (const s of STATE.sources) counts[s.category]++;
  for (const c of Object.keys(counts)) $("n-" + c).textContent = " " + counts[c];

  const list = $("sources");
  list.innerHTML = "";
  const q = QUERY.toLowerCase();
  const shown = STATE.sources.filter(
    (s) => (FILTER === "all" || s.category === FILTER) &&
           (!q || s.label.toLowerCase().includes(q) || s.id.includes(q) || (s.requires || "").toLowerCase().includes(q))
  );
  if (!shown.length) { list.appendChild(el("div", "empty", "No sources match.")); }
  for (const s of shown) list.appendChild(sourceCard(s));

  const banner = $("keybanner");
  banner.innerHTML = "";
  banner.appendChild(document.createTextNode("Keys are written to "));
  banner.appendChild(el("code", null, STATE.config_file));
  banner.appendChild(document.createTextNode(" with 0600 permissions and never leave this machine. This page only ever shows whether a key is set, never its value."));

  const kl = $("keys");
  kl.innerHTML = "";
  const ordered = STATE.keys.slice().sort((a, b) =>
    (b.unlocks.length - a.unlocks.length) || a.label.localeCompare(b.label));
  for (const k of ordered) kl.appendChild(keyCard(k));
}

async function load(live) {
  try {
    STATE = await api("/api/state" + (live ? "?refresh=1" : ""));
    render();
  } catch (e) { toast(e.message, "bad"); }
}

for (const t of document.querySelectorAll(".tab")) {
  t.onclick = () => {
    for (const o of document.querySelectorAll(".tab")) o.classList.toggle("on", o === t);
    $("pane-sources").hidden = t.dataset.tab !== "sources";
    $("pane-keys").hidden = t.dataset.tab !== "keys";
  };
}
for (const c of document.querySelectorAll(".chip")) {
  c.onclick = () => {
    FILTER = c.dataset.filter;
    for (const o of document.querySelectorAll(".chip")) o.classList.toggle("on", o === c);
    render();
  };
}
$("q").oninput = (e) => { QUERY = e.target.value; render(); };
$("refresh").onclick = async () => {
  const b = $("refresh");
  b.disabled = true; b.textContent = "Checking…";
  await load(true);
  b.disabled = false; b.textContent = "Re-check";
  toast("Re-checked every source", "good");
};
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $("q")) { e.preventDefault(); $("q").focus(); }
});

load(false);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "last30days-settings"
    sys_version = ""

    # Injected by serve().
    token: str = ""
    config: dict[str, Any] = {}
    port: int = 0
    _report: dict[str, Any] | None = None
    # Monotonic stamp of the last handled request. Only the idle
    # watchdog reads it; a plain float assignment is atomic enough.
    last_activity: float = 0.0
    _lock = threading.Lock()

    def log_message(self, *args: Any) -> None:  # noqa: D102 - quiet by design
        pass

    # ---- security gates -------------------------------------------------

    def _host_ok(self) -> bool:
        """Reject any Host but loopback (DNS-rebinding defense)."""
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _origin_ok(self) -> bool:
        """A write must come from our own page, not a site the user is browsing."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # non-browser client (curl); token still required
        allowed = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        return origin in allowed

    def _token_ok(self, query: dict[str, list[str]]) -> bool:
        supplied = self.headers.get("X-L30D-Token") or (query.get("t") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    # ---- plumbing -------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _state(self, refresh: bool) -> dict[str, Any]:
        """Doctor report, cached for the server's life until Re-check.

        Rebuilding runs live probes (~10s), so the page would feel broken if
        every render paid for them. The user drives refreshes explicitly.
        """
        from . import doctor

        with _Handler._lock:
            if refresh or _Handler._report is None:
                _Handler.config = env.get_config(
                    policy=env.ConfigLoadPolicy(
                        browser_cookies="plan_only", inspect_ignored_project_config=True
                    )
                )
                _Handler._report = doctor.build_report(_Handler.config)
            return build_state(_Handler.config, _Handler._report)

    # ---- routes ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        _Handler.last_activity = time.monotonic()
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._host_ok():
            self._json({"error": "bad host"}, HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if not self._token_ok(query):
            self._json({"error": "bad or missing session token"}, HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/":
            page = PAGE.replace("__TOKEN__", self.token)
            self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self._json(self._state(refresh=bool(query.get("refresh"))))
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        _Handler.last_activity = time.monotonic()
        parsed = urlparse(self.path)
        if not self._host_ok() or not self._origin_ok():
            self._json({"error": "rejected origin"}, HTTPStatus.FORBIDDEN)
            return
        if not self._token_ok(parse_qs(parsed.query)):
            self._json({"error": "bad or missing session token"}, HTTPStatus.FORBIDDEN)
            return
        if env.CONFIG_FILE is None:
            self._json(
                {"error": "no config file: LAST30DAYS_CONFIG_DIR is set to clean mode"},
                HTTPStatus.CONFLICT,
            )
            return
        body = self._body()

        if parsed.path == "/api/key":
            name = str(body.get("name") or "")
            # Same allowlist as `setup --store-key`: only a credential the
            # engine actually loads from .env can be written from here.
            if name not in env.KEYCHAIN_KEYS:
                self._json({"error": "unknown credential name"}, HTTPStatus.BAD_REQUEST)
                return
            if body.get("action") == "remove":
                remove_env_key(env.CONFIG_FILE, name)
                self._invalidate()
                self._json({"ok": True, "name": name, "present": False})
                return
            value = str(body.get("value") or "").strip()
            if not value:
                self._json({"error": "empty value"}, HTTPStatus.BAD_REQUEST)
                return
            ok = _write_value(name, value)
            self._invalidate()
            self._json({"ok": ok, "name": name, "present": ok})
            return

        if parsed.path == "/api/toggle":
            source_id = str(body.get("source") or "")
            enabled = bool(body.get("enabled"))
            state = self._state(refresh=False)
            record = next((s for s in state["sources"] if s["id"] == source_id), None)
            if record is None:
                self._json({"error": "unknown source"}, HTTPStatus.BAD_REQUEST)
                return
            if record["include_token"]:
                _toggle_csv("INCLUDE_SOURCES", record["include_token"], enabled, _Handler.config)
            else:
                # "on" means "not excluded", so the flag is inverted.
                _toggle_csv("EXCLUDE_SOURCES", source_id, not enabled, _Handler.config)
            self._invalidate()
            self._json({"ok": True, "source": source_id, "enabled": enabled})
            return

        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _invalidate(self) -> None:
        """Drop the cached report so the next state read sees the new .env."""
        with _Handler._lock:
            _Handler._report = None


def serve(
    config: dict[str, Any],
    *,
    port: int = 0,
    open_browser: bool = True,
    idle_timeout: int = 0,
) -> int:
    """Run the settings UI until interrupted. Returns a process exit code.

    ``idle_timeout`` (seconds, 0 = never) shuts the server down once it has
    gone that long without handling a request. Interactive use leaves it off
    -- a page left open all afternoon should still work. It exists for
    non-interactive callers like the MCP tool, which start the server
    detached and have no way to stop it afterwards; without a self-imposed
    deadline every invocation would leak a process holding a port.
    """
    if env.CONFIG_FILE is None:
        print(
            "[last30days] settings: LAST30DAYS_CONFIG_DIR is set to clean mode, "
            "so there is no .env to edit. Unset it and rerun."
        )
        return 2

    token = secrets.token_urlsafe(32)
    _Handler.token = token
    _Handler.config = config
    _Handler._report = None

    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    bound_port = httpd.server_address[1]
    _Handler.port = bound_port
    url = f"http://127.0.0.1:{bound_port}/?t={token}"

    # Flush explicitly: stdout is block-buffered whenever this is launched by
    # an agent harness rather than a TTY, and the URL is useless if it only
    # appears once the server exits.
    print("[last30days] settings UI")
    print(f"  {url}")
    print(f"  editing {env.CONFIG_FILE}")
    print("  loopback only; the session token dies with this process. Ctrl-C to stop.", flush=True)

    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()

    if idle_timeout > 0:
        _Handler.last_activity = time.monotonic()

        def _watchdog() -> None:
            # shutdown() must be called from another thread than
            # serve_forever(), which is why this is not a Timer on the
            # request path.
            tick = min(15, max(1, idle_timeout // 4))
            while True:
                time.sleep(tick)
                if time.monotonic() - _Handler.last_activity >= idle_timeout:
                    httpd.shutdown()
                    return

        threading.Thread(target=_watchdog, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[last30days] settings UI stopped.")
    finally:
        httpd.server_close()
    return 0

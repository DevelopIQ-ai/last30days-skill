"""Vendored brand marks (lib/source_icons.py).

The interesting risk here is not logic, it is **silent data corruption**. The
first generated copy of this module wrapped each SVG path across several
string literals; Python concatenated them without the whitespace that sat at
the wrap point, fusing adjacent numbers ("L.258 24" -> "L.25824"). Every mark
still looked like valid path data and every import still succeeded -- the only
symptom was a browser console error and a blank chip.

So these tests validate the path data itself: each `d` must tokenize into
commands whose argument groups are actually complete, which is what catches a
fused coordinate ("L.258 24" leaves L one argument short).

That check has a real blind spot: H and V take a single argument, so fusing
two of *their* numbers produces something still legal. The one-literal-per-path
source check is what covers that remainder -- it pins the layout under which
the corruption cannot happen at all, rather than trying to detect it after the
fact.
"""

import re
import unittest
from pathlib import Path

from lib import env, source_icons

ICON_SOURCE = (
    Path(__file__).resolve().parent.parent
    / "skills" / "last30days" / "scripts" / "lib" / "source_icons.py"
)

# SVG number: optional sign, digits with optional leading/trailing dot, optional
# exponent. Deliberately greedy about adjacency so ".5.5" reads as two numbers,
# exactly like an SVG parser.
_NUMBER = re.compile(r"[+-]?(?:\d*\.\d+|\d+\.?)(?:[eE][+-]?\d+)?")
_COMMAND = re.compile(r"[MmZzLlHhVvCcSsQqTtAa]")

ARITY = {
    "m": 2, "l": 2, "h": 1, "v": 1, "c": 6,
    "s": 4, "q": 4, "t": 2, "a": 7, "z": 0,
}


def parse_path(d: str) -> list[tuple[str, int]]:
    """Split a path into (command, complete-argument-group) pairs.

    Arguments are consumed a whole group at a time, so a fused coordinate
    leaves the group short and raises here rather than passing silently.

    The one subtlety is the arc command: its large-arc and sweep arguments are
    single-digit **flags** that an SVG parser reads without needing a
    separator, so ``a12 12 0 01-4.2-2.9`` carries flags 0 and 1, not a number
    ``01``. Treating them as ordinary numbers makes every valid arc look one
    argument short.
    """
    out: list[tuple[str, int]] = []
    pos = 0

    def skip_separators() -> None:
        nonlocal pos
        while pos < len(d) and d[pos] in " ,":
            pos += 1

    while pos < len(d):
        skip_separators()
        if pos >= len(d):
            break
        command = d[pos]
        if not _COMMAND.match(command):
            raise AssertionError(f"expected a command at {pos}: {d[pos:pos + 12]!r}")
        pos += 1
        arity = ARITY[command.lower()]
        if arity == 0:
            out.append((command, 0))
            continue

        groups = 0
        while True:
            skip_separators()
            if pos >= len(d) or _COMMAND.match(d[pos]):
                break
            for index in range(arity):
                skip_separators()
                is_arc_flag = command.lower() == "a" and index in (3, 4)
                if is_arc_flag:
                    if pos >= len(d) or d[pos] not in "01":
                        raise AssertionError(
                            f"arc flag expected at {pos}: {d[pos:pos + 12]!r}"
                        )
                    pos += 1
                    continue
                match = _NUMBER.match(d, pos)
                if not match or match.end() == pos:
                    raise AssertionError(
                        f"{command} argument {index + 1}/{arity} missing at "
                        f"{pos}: {d[max(0, pos - 8):pos + 12]!r} (a fused coordinate?)"
                    )
                pos = match.end()
            groups += 1
        out.append((command, groups))
    return out


class PathDataTest(unittest.TestCase):
    def test_every_path_is_structurally_valid(self):
        """A fused coordinate leaves a command short of its arity."""
        for name, entry in source_icons.BRAND_ICONS.items():
            with self.subTest(icon=name):
                d = entry["d"]
                self.assertTrue(d[0] in "Mm", f"{name} must start with a moveto")
                # parse_path raises on any incomplete argument group; all that
                # is left to assert is that a command carrying arguments got
                # at least one full group.
                for command, groups in parse_path(d):
                    if ARITY[command.lower()] == 0:
                        self.assertEqual(groups, 0, f"{name}: {command} takes no args")
                    else:
                        self.assertGreater(groups, 0, f"{name}: {command} has no args")

    def test_generic_glyphs_are_valid_too(self):
        for name, entry in source_icons.GENERIC_ICONS.items():
            with self.subTest(icon=name):
                parse_path(entry["d"])

    def test_each_path_is_one_literal_on_one_line(self):
        """Pins the layout that removes the wrap-fusion bug class."""
        offenders = [
            f"{index}: {line.strip()[:60]}"
            for index, line in enumerate(ICON_SOURCE.read_text().splitlines(), 1)
            if line.lstrip().startswith('"d": "') and not line.rstrip().endswith('",')
        ]
        self.assertEqual(offenders, [], "a path was split across lines")

    def test_colors_are_readable_on_both_themes(self):
        """Near-black marks must lighten for the dark theme, and only those."""
        for name, entry in source_icons.BRAND_ICONS.items():
            with self.subTest(icon=name):
                self.assertRegex(entry["light"], r"^#[0-9A-Fa-f]{6}$")
                self.assertRegex(entry["dark"], r"^#[0-9A-Fa-f]{6}$")
        # X, TikTok, Threads, GitHub and Digg are all #000000-ish upstream.
        self.assertEqual(source_icons.BRAND_ICONS["x"]["light"], "#000000")
        self.assertNotEqual(source_icons.BRAND_ICONS["x"]["dark"], "#000000")
        # A mid-tone brand keeps its real colour on both themes.
        self.assertEqual(source_icons.BRAND_ICONS["reddit"]["dark"], "#FF4500")
        self.assertEqual(source_icons.BRAND_ICONS["reddit"]["light"], "#FF4500")


class ResolutionTest(unittest.TestCase):
    def test_known_brands_resolve_to_marks(self):
        for source_id in ("reddit", "x", "youtube", "github", "instagram"):
            icon = source_icons.icon_for_source(source_id, source_id)
            self.assertEqual(icon["kind"], "path", source_id)

    def test_withdrawn_brands_fall_back_to_a_monogram(self):
        """Amazon/LinkedIn/OpenAI were pulled from Simple Icons on request."""
        for source_id, text in (("amazon", "AZ"), ("linkedin", "in")):
            icon = source_icons.icon_for_source(source_id, source_id)
            self.assertEqual(icon["kind"], "mono", source_id)
            self.assertEqual(icon["text"], text)

    def test_non_brand_sources_get_a_generic_glyph(self):
        for source_id in ("web", "jobs", "library"):
            self.assertEqual(source_icons.icon_for_source(source_id)["kind"], "path")

    def test_credentials_show_the_mark_of_what_they_unlock(self):
        x_mark = source_icons.BRAND_ICONS["x"]["d"]
        for key_name in ("GETXAPI_KEY", "XQUIK_API_KEY", "X_BEARER_TOKEN", "AUTH_TOKEN"):
            self.assertEqual(source_icons.icon_for_key(key_name)["d"], x_mark, key_name)

    def test_every_credential_resolves_to_something_renderable(self):
        for name in env.KEYCHAIN_KEYS:
            icon = source_icons.icon_for_key(name)
            with self.subTest(key=name):
                if icon["kind"] == "mono":
                    self.assertTrue(icon["text"])
                else:
                    self.assertTrue(icon["d"])

    def test_monogram_colors_are_stable(self):
        first = source_icons.icon_for_source("polymarket", "Polymarket")
        second = source_icons.icon_for_source("polymarket", "Polymarket")
        self.assertEqual(first["light"], second["light"])
        self.assertNotEqual(
            first["light"], source_icons.icon_for_source("techmeme", "Techmeme")["light"]
        )


if __name__ == "__main__":
    unittest.main()

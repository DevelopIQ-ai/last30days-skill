import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/last30days/scripts/discover.py"


def module():
    spec = importlib.util.spec_from_file_location("discovery_cli", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as exc:
        module().main(["--help"])
    assert exc.value.code == 0
    assert "--include" in capsys.readouterr().out


def test_missing_objective_rejected_before_providers(tmp_path):
    with pytest.raises(SystemExit) as exc:
        module().main(["--output", str(tmp_path / "run.json")])
    assert exc.value.code == 2


def test_cli_parses_filters_and_returns_compact_output(tmp_path, capsys):
    m = module()

    def run(cfg, *args, **kwargs):
        assert (
            cfg.sources == ("x",)
            and cfg.include == ("firsthand",)
            and cfg.language == "English"
        )
        return {
            "stop_reason": "planner_complete",
            "accepted": [],
            "candidates": {},
            "rounds": [{}],
            "calls": 3,
            "searches": 1,
        }

    with (
        patch.object(m, "Planner"),
        patch.object(m, "Jev"),
        patch.object(m, "run", side_effect=run),
    ):
        assert (
            m.main(
                [
                    "Jev users",
                    "--sources",
                    "x",
                    "--include",
                    "firsthand",
                    "--language",
                    "English",
                    "--output",
                    str(tmp_path / "r.json"),
                ]
            )
            == 0
        )
    assert "planner_complete" in capsys.readouterr().out


@pytest.mark.parametrize("payload", ["null", "[]"])
def test_malformed_resume_checkpoint_is_reported_cleanly(tmp_path, payload):
    path = tmp_path / "r.json"
    path.write_text(payload)
    with pytest.raises(SystemExit) as error:
        module().main(["--resume", str(path)])
    assert error.value.code == 2

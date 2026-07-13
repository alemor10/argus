from typer.testing import CliRunner

from argus.cli import app
from argus.config import build_contexts, load_watch_config

runner = CliRunner()


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("watch", "report", "init", "scout"):
        assert command in result.output


def test_init_scaffolds_a_parseable_watchlist_with_no_live_tickers(tmp_path):
    result = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert result.exit_code == 0
    contexts = build_contexts(load_watch_config(tmp_path / "watchlist.yaml"))
    assert contexts == []  # the template parses but adds NO tickers the human never chose

    again = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert again.exit_code == 1  # never touches an existing watchlist


def test_watch_without_watchlist_points_at_init(tmp_path):
    result = runner.invoke(app, ["watch", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "argus init" in result.output


def test_watch_with_empty_watchlist_completes_and_digests(tmp_path):
    """An empty watchlist is a vacuously complete run — a digest is still
    written (silence is a statement) and the exit code is 0."""
    runner.invoke(app, ["init", "--root", str(tmp_path)])
    result = runner.invoke(app, ["watch", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "complete" in result.output
    assert (tmp_path / "argus.db").exists()
    assert len(list((tmp_path / "reports").glob("digest-*.md"))) == 1


def test_report_regenerates_a_past_run(tmp_path):
    runner.invoke(app, ["init", "--root", str(tmp_path)])
    runner.invoke(app, ["watch", "--root", str(tmp_path)])
    result = runner.invoke(app, ["report", "--run", "1", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "run 1" in result.output.lower()


def test_report_on_unknown_run_fails_loudly(tmp_path):
    runner.invoke(app, ["init", "--root", str(tmp_path)])
    runner.invoke(app, ["watch", "--root", str(tmp_path)])
    result = runner.invoke(app, ["report", "--run", "99", "--root", str(tmp_path)])
    assert result.exit_code == 1


def test_scout_names_its_gate(tmp_path):
    result = runner.invoke(app, ["scout"])
    assert result.exit_code == 1
    assert "post-v1" in result.output

"""``peerreview manuscript.pdf`` is the whole review.

The command used to take a bare manuscript path into the LangGraph panel, with
a Textual office, a target journal, an article type, a strictness level, a desk
screen and about twenty-five other flags. The engine has none of those, so the
bare form is now the one-command engine review and nothing else: these tests
pin where it writes, that the review flags still reach it, and that the panel's
flags are refused rather than quietly ignored.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from peerreviewagents.cli import main as cli
from peerreviewagents.engine import __main__ as engine_cli
from peerreviewagents.engine.bundle import default_output

PDF = b"%PDF-1.7\nnot a real manuscript\n"


@pytest.fixture
def reviewed(monkeypatch, tmp_path):
    """Run the command for real up to the model call, and capture that call."""
    seen: dict = {}

    def fake_import(source, workspace):
        Path(workspace).mkdir(parents=True, exist_ok=True)
        (Path(workspace) / "paper.json").write_text("{}", encoding="utf-8")
        return {"title": "A manuscript", "statistics": {"blocks": 1}}

    def fake_run(paths, literature_cutoff, mode="ensemble", **options):
        seen["paths"], seen["mode"], seen["cutoff"] = paths, mode, literature_cutoff
        seen["options"] = options
        return {"progress": {"status": "complete"}, "reports": {}, "failure": None,
                **paths.record()}

    monkeypatch.setattr("peerreviewagents.engine.ingest.import_paper", fake_import)
    monkeypatch.setattr(engine_cli, "run_bundle_review", fake_run)
    return seen


def _pdf(tmp_path, name="paper.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(PDF)
    return path


def _run(argv: list[str], monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["peerreview", *argv])
    with contextlib.redirect_stdout(io.StringIO()):
        cli.run()


def test_a_bare_pdf_is_the_one_command_review_beside_the_manuscript(
    tmp_path, monkeypatch, reviewed
):
    manuscript = _pdf(tmp_path)

    _run([str(manuscript)], monkeypatch)

    bundle = tmp_path / "paper-review"
    assert default_output(manuscript) == bundle
    assert reviewed["paths"].bundle == bundle
    assert reviewed["paths"].run == bundle / "review"
    assert reviewed["paths"].workspace == bundle / "manuscript"
    assert reviewed["mode"] == "ensemble"
    assert reviewed["options"]["reviewers"] == 2


FIXTURE = Path(__file__).parent / "fixtures" / "single-column.pdf"


@pytest.mark.skipif(not FIXTURE.is_file(), reason="shared ingestion fixture missing")
def test_a_real_pdf_is_imported_into_the_default_folder(tmp_path, monkeypatch):
    """Everything up to the first model call is real, on a real PDF.

    The import is the one thing the bare command does before it starts
    spending a subscription, and it is the step a person sees fail. So this
    reads the shared ingestion fixture with the installed converter and stubs
    only the review itself.
    """
    manuscript = tmp_path / "single-column.pdf"
    manuscript.write_bytes(FIXTURE.read_bytes())
    seen = {}

    def capture(paths, literature_cutoff, mode="ensemble", **options):
        seen["paths"] = paths
        return {"progress": {"status": "complete"}, "reports": {}, "failure": None,
                **paths.record()}

    monkeypatch.setattr(engine_cli, "run_bundle_review", capture)

    _run([str(manuscript)], monkeypatch)

    bundle = tmp_path / "single-column-review"
    assert seen["paths"].bundle == bundle
    paper = json.loads((bundle / "manuscript" / "paper.json").read_text(encoding="utf-8"))
    assert paper["blocks"]
    assert (bundle / "manuscript" / "original.pdf").read_bytes() == FIXTURE.read_bytes()


def test_the_review_flags_reach_the_bare_form_too(tmp_path, monkeypatch, reviewed):
    manuscript = _pdf(tmp_path)

    _run([str(manuscript), "--mode", "independent", "--policy", "standard-v1",
          "--provider", "codex", "--literature-cutoff", "2025-01-31"], monkeypatch)

    assert reviewed["mode"] == "independent"
    assert reviewed["options"]["policy"] == "standard-v1"
    assert reviewed["options"]["model"] == "gpt-5.6-terra"
    assert reviewed["cutoff"] == "2025-01-31"


def test_naming_the_run_folder_still_works(tmp_path, monkeypatch, reviewed):
    manuscript = _pdf(tmp_path)

    _run(["review", str(manuscript), str(tmp_path / "named")], monkeypatch)

    assert reviewed["paths"].bundle == tmp_path / "named"


def test_a_path_that_is_not_a_manuscript_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["peerreview", str(tmp_path / "missing.pdf")])
    with pytest.raises(SystemExit) as exit_info:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            cli.run()
    assert exit_info.value.code == 2
    assert "does not exist" in err.getvalue()


def test_the_panel_flags_and_subcommand_are_gone_rather_than_ignored(tmp_path, monkeypatch):
    """A flag that no longer changes anything must fail, not be accepted: a
    review run with --strictness 5 silently at the default would be a lie."""
    manuscript = str(_pdf(tmp_path))
    for gone in (["--journal", "nature"], ["--article-type", "research"],
                 ["--strictness", "5"], ["--desk-screen"], ["--no-tui"],
                 ["--debate-rounds", "2"], ["--list-journals"], ["--show-panel"],
                 ["--research"]):
        monkeypatch.setattr("sys.argv", ["peerreview", manuscript, *gone])
        with pytest.raises(SystemExit) as exit_info:
            with contextlib.redirect_stderr(io.StringIO()):
                cli.run()
        assert exit_info.value.code == 2, gone
    monkeypatch.setattr("sys.argv", ["peerreview", "legacy-panel", manuscript])
    with pytest.raises(SystemExit) as exit_info:
        with contextlib.redirect_stderr(io.StringIO()):
            cli.run()
    assert exit_info.value.code == 2


def test_the_command_names_every_engine_command_and_the_web_ui(monkeypatch):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            parser.parse_args(["--help"])
    text = out.getvalue()
    for named in ("import", "verify", "review", "investigate", "report", "checks", "serve"):
        assert named in text
    for gone in ("legacy-panel", "journal", "strictness", "desk"):
        assert gone not in text


def test_no_arguments_asks_for_a_manuscript_instead_of_running_anything(monkeypatch):
    monkeypatch.setattr("sys.argv", ["peerreview"])
    with pytest.raises(SystemExit) as exit_info:
        with contextlib.redirect_stderr(io.StringIO()):
            cli.run()
    assert exit_info.value.code == 2


def test_the_textual_panel_runner_is_gone():
    with pytest.raises(ImportError):
        __import__("peerreviewagents.cli.tui")

# Contributing

Bug reports, and changes to the plugin, its server or the manuscript
workspace, are all welcome.

## Setup

```bash
git clone https://github.com/pgarrett-scripps/PeerReviewAgents.git
cd PeerReviewAgents
uv venv && uv pip install -e '.[dev]'
```

That is what CI installs, and `just install-dev` is the same thing. The `dev`
extra (pytest, ruff, pyyaml) covers everything `tests/` imports.

The suite ingests a PDF fixture, so it also needs Poppler's `pdftotext` —
`sudo apt install poppler-utils`, `brew install poppler`, or `choco install
poppler`. That is a runtime requirement of the importer, not of the tests, so
CI installs it rather than skipping what needs it.

Keep the virtual environment at `.venv` in the checkout. The plugin launches
its MCP server as `${CLAUDE_PROJECT_DIR}/.venv/bin/python`, so a venv anywhere
else gives you a review that cannot start.

## Before you open a PR

```bash
just lint                 # ruff check
just test                 # pytest tests/ -q
```

Note: `just check` also runs `ruff format --check`, which currently fails — 33
of 79 files would be reformatted, and adopting the formatter would do it in one
mechanical diff. Formatting is not enforced in CI. Match the surrounding style
instead.

The suite starts no model call and makes no network call: there is no API key
to need, and the starter and server tests drive a stubbed `claude` binary, so
an installed one cannot be reached from a test. If a change makes a test need a
subscription login, something real leaked into the test path — stub the
transport instead.

CI runs lint and the suite across Python 3.10–3.13 on Linux, and the suite
again on macOS and Windows.

## Releasing

Use a `v<VERSION>` tag to trigger `.github/workflows/release.yml`.
Before tagging, update `project.version` in `pyproject.toml`, the matching
`version` and release date in `CITATION.cff`, and the `version` in
`plugins/pra/.claude-plugin/plugin.json`, then commit the release changes.
`pytest tests/test_metadata.py -q` checks the citation metadata locally.

The workflow checks the tag against the package version, runs the tests, and
installs the built wheel into a fresh venv to prove it imports before
publishing to PyPI and creating the GitHub Release. The metadata tests also
check the CFF version and reject a `version` field in `.zenodo.json`.

Leave `version` out of `.zenodo.json`. With the repository's Zenodo GitHub
integration enabled, Zenodo uses the published release tag as its version. Keep
authors, licensing, and descriptive metadata in the JSON. Zenodo gives this
file precedence over `CITATION.cff` during release archiving.
See [Zenodo's release-tag versioning](https://blog.zenodo.org/2017/11/02/version-field/)
and [metadata precedence](https://help.zenodo.org/docs/github/describe-software/).

Let the workflow create the GitHub Release after verification. Publishing a
release manually in the GitHub UI bypasses those checks and can trigger Zenodo
immediately. Editing metadata in the checkout does not revise older deposits.

## Where the code is

Since 0.9 the review *is* the plugin. Three places:

| Place | What lives there |
| --- | --- |
| `plugins/pra/` | the workflow (`workflows/review.js`), the agent files that pin each slot's model and turn limit, and the three skills. [Its README](plugins/pra/README.md) describes the run. |
| `peerreviewagents/plugin/` | `server.py`, the MCP server that owns the manuscript, the identities, the tool allowances, the budgets, the guards and the run files; `starter.py`, which probes a session, launches it headless, enforces the token ceilings from the session's own stream, and re-renders the reports. |
| `peerreviewagents/engine/` | the manuscript workspace, the tool implementations behind the server, the domain checklists, the usage accounting and the report writer. [Its README](peerreviewagents/engine/README.md) lists the modules and the boundaries. |

`peerreviewagents/cli/main.py` has two commands, `review` and `report`, and
both are thin: `review` calls `plugin.starter.review`, `report` calls
`engine.run_report.write_reports`. The Python orchestration that ran a review
through 0.8 — `ensemble`, `reviewer`, `model_runtime`, the debate, `engine.api`,
`engine.bundle` — was deleted in 0.9 and is frozen at the tag `v0.8.0`. Please
don't reintroduce a second review path.

## Changing the server

The review's guarantees are enforced by the server, not by a prompt. Keep that
inversion in anything new:

- A finding is anchored to a block with an exact quotation, or it is
  quarantined. A reviewer stage may propose; the guards decide.
- `basis` is assigned by the server from what the agent actually did. An agent
  must not be able to assert its own.
- A tool an arm removes is removed from that slot's allow list, so the refusal
  comes from the server rather than from asking the agent nicely.
- Every tool call, receipt and lookup is written to the ledger with the
  identity that made it. An agent's own report of its work is never the record.

A change that would make a reviewer's self-report load-bearing is the one kind
of change to argue for in the PR body rather than just ship.

## Style

The code's house style is dense one-line bodies and unsorted imports, which
`pyproject.toml` exempts from two ruff rules. Match it rather than reflowing
it: diffs are still read against it.

`ruff` with the config in `pyproject.toml`. Line length 100. `just lint-fix`
applies the auto-fixable rules.

Comments should explain *why*, not restate the code. Match the existing
density rather than adding narration.

## Scope

This is a research tool for **assisting** human peer review, not replacing it.
Contributions that present the output as authoritative — removing the advisory
framing, auto-submitting decisions, hiding the provenance — are out of scope.

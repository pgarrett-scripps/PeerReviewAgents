# Contributing

Bug reports, and changes to the review engine or the surfaces over it, are all
welcome.

## Setup

```bash
git clone https://github.com/pgarrett-scripps/PeerReviewAgents.git
cd PeerReviewAgents
uv venv && uv pip install -e '.[dev]'
```

That is what CI installs, and `just install-dev` is the same thing. The `dev`
extra runs the whole suite on its own: everything `tests/` imports, httpx and
the MCP SDK included, is named in it.

## Before you open a PR

```bash
just lint                 # ruff check
just test                 # pytest tests/ -q
```

Note: `just check` also runs `ruff format --check`, which currently fails —
the codebase predates the formatter and adopting it would reformat 70 of its
117 Python files in one mechanical diff. Formatting is not enforced in CI.
Match the surrounding style instead.

The suite starts no model call and makes no network call: there is no API key
to need, and the provider command lines are stubbed so an installed `claude` or
`codex` cannot be reached from a test. If a change makes a test need a
subscription login, something real leaked into the test path — stub the
transport instead.

CI runs the same checks across Python 3.10–3.13.

## Releasing

Use a `v<VERSION>` tag to trigger `.github/workflows/release.yml`.
Before tagging, update `project.version` in `pyproject.toml` and the matching
`version` and release date in `CITATION.cff`, plus the version in every client
manifest — both Claude plugin manifests, the Codex plugin manifest and
`integrations/pi/package.json` — then commit the release changes.
`tests/test_claude_install.py` checks all four against the package version.
Run `pytest tests/test_metadata.py -q` to check the citation metadata locally.

The workflow checks the tag against the package version and runs the tests
before publishing to PyPI and creating the GitHub Release. The metadata tests
also check the CFF version and reject a `version` field in `.zenodo.json`.

Leave `version` out of `.zenodo.json`. With the repository's Zenodo GitHub
integration enabled, Zenodo uses the published release tag, such as `v0.6.0`,
as its version. Keep authors, licensing, and descriptive metadata in the JSON.
Zenodo gives this file precedence over `CITATION.cff` during release archiving.
See [Zenodo's release-tag versioning](https://blog.zenodo.org/2017/11/02/version-field/)
and [metadata precedence](https://help.zenodo.org/docs/github/describe-software/).

Let the workflow create the GitHub Release after verification. Publishing a
release manually in the GitHub UI bypasses those checks and can trigger Zenodo
immediately. Editing metadata in the checkout does not revise older deposits.

## Where the code is

The review is `peerreviewagents/engine`, and
[`peerreviewagents/engine/README.md`](peerreviewagents/engine/README.md)
documents it: the reviewer stages, the bounded manuscript and literature tools,
the source guards that reject a finding whose quote does not match, and the
stated boundaries. `engine/api.py` is its versioned boundary and `engine/bundle.py`
the one-command review — import, run, report — that every surface calls.

Everything outside the engine is a surface over that one call:
`cli/main.py`, `mcp/server.py`, `web/`, `review_service.py` (the shared
background job) and the plugin, skill and Pi manifests. A change a surface
needs belongs in `engine/bundle.py`, so the command line, the MCP server and
the browser cannot drift apart.

## Changing the engine

The engine's own house style is dense one-line bodies and unsorted imports,
which `pyproject.toml` exempts from two ruff rules. Match it rather than
reflowing it: it came over from upstream and diffs are still read against it.

Every finding is anchored to a source block and an exact quotation, and
`engine/` refuses one that is not. Keep that property in anything new: a
reviewer stage may propose, the guards decide.

## Style

`ruff` with the config in `pyproject.toml`. Line length 100. `just lint-fix`
applies the auto-fixable lint rules.

Comments should explain *why*, not restate the code. The existing codebase is
fairly consistent about this — match its density rather than adding narration.

## Scope

This is a research tool for **assisting** human peer review, not replacing it.
Contributions that present the output as authoritative — removing the advisory
framing, auto-submitting decisions, hiding the provenance — are out of scope.

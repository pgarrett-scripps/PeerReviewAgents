# Contributing

Bug reports, new reviewer agents, and additional providers are all welcome.

## Setup

```bash
git clone https://github.com/pgarrett-scripps/PeerReviewAgents.git
cd PeerReviewAgents
uv venv && uv pip install -e '.[research,web-test,dev]'
```

That is what CI installs. `just install-dev` is a shorter path but omits the
`web-test` and `dev` extras, so `tests/test_web.py` (httpx) and
`tests/test_metadata.py` (pyyaml) won't have what they import.

## Before you open a PR

```bash
just lint                 # ruff check
just test                 # pytest tests/ -q
```

Note: `just check` also runs `ruff format --check`, which currently fails —
the codebase predates the formatter and adopting it would reformat 70 of its
117 Python files in one mechanical diff. Formatting is not enforced in CI.
Match the surrounding style instead.

The test suite stubs the LLM layer, so **it needs no API key and makes no
network calls**. If a change makes the tests require a key, that's a signal
something real leaked into the test path — please reroute it through the fake
provider instead.

CI runs the same checks across Python 3.10–3.13.

## Releasing

Use a `v<VERSION>` tag to trigger `.github/workflows/release.yml`.
Before tagging, update `project.version` in `pyproject.toml` and the matching
`version` and release date in `CITATION.cff`, then commit the release changes.
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

## Adding an agent

Every agent emits a typed pydantic schema through the provider's
structured-output mode. There is no YAML-frontmatter parsing and no
string-matching for verdicts, and new agents should keep it that way.

1. Add the output schema to [`peerreviewagents/agents/schemas.py`](peerreviewagents/agents/schemas.py),
   with a `to_markdown()` renderer. The structured fields stay the source of
   truth; the markdown is a view of them.
2. Implement the agent under `peerreviewagents/agents/`, calling
   `invoke_structured` (or `invoke_structured_after_tools` if it uses tools)
   from `agents/utils/structured.py` — those wrap `with_structured_output` with
   a retry on validation failure.
3. Wire the node into [`peerreviewagents/graph/review_graph.py`](peerreviewagents/graph/review_graph.py).
4. Write the report in [`peerreviewagents/reports.py`](peerreviewagents/reports.py).
5. Add a test that runs it against the fake LLM.

## Adding a provider

Add an entry to `PROVIDERS` in
[`peerreviewagents/runtime/providers.py`](peerreviewagents/runtime/providers.py).
Each provider declares its preferred `structured_method` and whether it honors
`cache_control: ephemeral`. The agent layer reads those capability flags — don't
branch on the provider name in agent code.

## Adding a research vendor

Implement the vendor module under `peerreviewagents/research/` and register it
in the routing table in `research/interface.py`. Rate-limit errors should raise
the shared rate-limit exception so the router falls through to the next vendor;
anything else propagates.

## Style

`ruff` with the config in `pyproject.toml`. Line length 100. `just lint-fix`
applies the auto-fixable lint rules.

Comments should explain *why*, not restate the code. The existing codebase is
fairly consistent about this — match its density rather than adding narration.

## Scope

This is a research tool for **assisting** human peer review, not replacing it.
Contributions that present the output as authoritative — removing the advisory
framing, auto-submitting decisions, hiding the provenance — are out of scope.

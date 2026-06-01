# Journal profiles

Each file in this folder describes **one journal** and is used to give the
review agents *venue-specific context* before they run — so reviewers judge a
manuscript against the standards, length limits, and scope of the journal it is
actually being submitted to, rather than in a vacuum.

## Format

- **One journal per file**, TOML, named with a kebab-case slug
  (`nature-methods.toml`, `bioinformatics.toml`, …). The filename slug is the
  identifier you'll pass as the target journal.
- TOML is chosen over plain `.txt` because the rest of the codebase already
  parses TOML (`tomllib`, see `peerreviewagents/default_config.py`) and keeps
  structured scalars separate from prose. That means these files are both
  human-editable *and* parse into typed fields with no new dependency.
- Copy `_template.toml` to start a new journal. Every field is optional except
  `name`; leave anything you don't know unset rather than guessing.

## Fields

| Key                  | Type        | Meaning                                                        |
| -------------------- | ----------- | -------------------------------------------------------------- |
| `name`               | string      | Venue name exactly as authors write it (e.g. `Nature Methods`).|
| `aliases`            | string[]    | Other names/abbreviations (`Nat. Methods`, `JMLR`).            |
| `publisher`          | string      | Publisher / society.                                           |
| `field`              | string      | Primary discipline (used to group profiles by field).         |
| `impact_factor`      | float       | Approximate JIF. Note the year in `impact_factor_year`.        |
| `impact_factor_year` | int         | Year the impact factor is from.                                |
| `acceptance_rate`    | string      | Rough acceptance rate, e.g. `"~8%"`. Optional.                 |
| `audience`           | string      | Who reads it — sets the bar for framing/significance.          |
| `description`        | string      | 1–3 sentence summary of what the journal publishes.           |
| `scope`              | string      | Topical scope / what's in and out of scope.                    |
| `max_words`          | int         | Main-text word limit (0 / unset = no hard limit).              |
| `abstract_max_words` | int         | Abstract word limit.                                           |
| `max_figures`        | int         | Max display items (figures + tables).                          |
| `max_references`     | int         | Reference cap (0 / unset = none).                              |
| `guidelines`         | string      | Free-text author/reviewer guidelines, evaluation criteria,     |
|                      |             | structure requirements, data/code policies, etc.              |
| `last_updated`       | string      | `YYYY-MM-DD` you last verified these values.                   |

Impact factors and acceptance rates drift year to year — treat the values here
as approximate and record `impact_factor_year` / `last_updated` so a stale
profile is obvious.

## How this gets used

Wired into the pipeline via `peerreviewagents/journals.py`:

- `load_journal(slug, config)` parses a `.toml` into a `JournalProfile`, and
  `JournalProfile.to_prompt_block()` renders it for the agents.
- The `target_journal` config key (a slug) selects the venue. Set it in
  `peerreview.toml`, via `PEERREVIEW_TARGET_JOURNAL`, or per-run with
  `peerreview --journal <slug>` (list options with `peerreview --list-journals`).
  The web UI exposes a dropdown populated from `GET /journals`.
- **`general` is the default.** `general.toml` is a stand-in profile with
  sound, field-general standards (no impact factor, no hard limits) for when the
  intended venue isn't one of the bundled profiles — so a manuscript headed for
  an unlisted journal still gets a sensible journal-style review. It's pinned to
  the top of the web dropdown and marked `(default)`.
- `PeerReviewGraph.initial_state` renders the block once into
  `ReviewState["journal_block"]`; `context_block()` in
  `agents/utils/agent_utils.py` prepends it to the manuscript block so the
  reviewers, meta-reviewer, and editor share one provider-side cache entry. The
  journal recommender injects it into its own prompt to frame suggestions around
  the intended target. The chosen venue is recorded in each run's `summary.md`.

Setting `target_journal = ""` makes the block empty for a fully venue-agnostic
review with no journal framing at all.

### Selection is fail-fast

A `--journal` / `target_journal` slug that doesn't resolve to a file is rejected
at the CLI/web entry point with the list of valid slugs. Inside the graph a
missing slug degrades silently to a venue-agnostic run so a pipeline never
crashes on venue context.

### Scaling up

When the journal count grows, group files into per-discipline subfolders
(`journals/bio/`, `journals/ml/`, …); the loader's glob would need to become
recursive (`rglob`) at that point. The `field` strings are currently free-form —
normalize them to a controlled vocabulary if you start filtering by field.

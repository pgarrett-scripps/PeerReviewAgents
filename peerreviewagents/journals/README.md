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
| `article_types`      | table       | Optional per-manuscript-type overrides — see below.            |
| `last_updated`       | string      | `YYYY-MM-DD` you last verified these values.                   |

## Article types

The *kind* of submission — a full research Article, a Letter, a Review, a
Perspective, etc. — is a venue-general taxonomy: what a "Letter" is, and how a
reviewer should weigh it, barely changes between journals. So the description
and review framing for each type live once in
[`peerreviewagents/article_types.py`](../article_types.py), shared by every
venue. The selectable keys are `article`, `letter`, `communication`,
`perspective`, `review`, `technical-note`, `tutorial`, `conference-paper`,
`grant-proposal`, and `exploratory-grant`.

A journal profile only overrides the **venue-specific specifics** that actually
differ — word/abstract caps and handling notes — as optional `[article_types.<key>]`
tables (these must come *after* all the top-level scalar keys, since a TOML
table header ends the top-level section):

```toml
[article_types.article]
max_words = 8000

[article_types.review]
max_words = 6000
notes = "Peer-reviewed; a bare list of citations is inadequate."
```

Each override field is optional. A venue that doesn't differentiate by type
simply omits the section; a type the user selects that the venue doesn't list
still gets the shared general framing, just without per-venue caps. The
selected type is chosen with `target_journal`-style precedence: `article_type`
in `peerreview.toml`, `PEERREVIEW_ARTICLE_TYPE`, or per-run
`peerreview --article-type <key>` (list options with
`peerreview --list-article-types`). Leave it unset for no manuscript-type
framing.

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

## Funder / grant profiles

A profile can model a **funder or grant mechanism** instead of a journal — the
`JournalProfile` fields are general enough to carry one. The bundled `nih-r01`,
`nih-r21`, `nsf`, and `erc` profiles do this: the `guidelines` field holds the
funding body's review criteria and scoring (NIH's five criteria + 1–9 Impact,
NSF's Intellectual Merit + Broader Impacts, ERC's excellence criterion),
`acceptance_rate` holds the payline/success rate, and `impact_factor` is left at
0 (not applicable). Pair them with the `grant-proposal` (or `exploratory-grant`
for R21-style mechanisms) article type, whose `review_framing` tells the panel
to judge proposed *future* work — significance, innovation, feasibility — rather
than completed results, and to remap the accept/minor/major/reject verdict onto
a funding decision (fundable / resubmit / not competitive). Page limits live in
the `[article_types.<key>].notes` field rather than `max_words`, since the
latter renders as a word count.

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

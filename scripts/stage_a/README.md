# Stage A: does the review catch an error that was put there on purpose?

Stage A is one experiment. Take published open-access papers, plant exactly one
known error in each, run the ordinary review on the altered copy, and score by a
written rule whether any finding caught it — and what the catch cost in tokens
and minutes, in three arms: `single`, `reviewers-only` and `full-two-track`.

The papers, the altered copies, the answer keys and the run folders are **not in
this repository**. They live in a data directory outside it, because they are
tens of megabytes of other people's PDFs and gigabytes of run output, and
because an answer key is only worth having while it stays private. What is in
the repository is the four scripts that make them and the results table in
[`docs/stage-a-results.md`](../../docs/stage-a-results.md).

Every script that touches that directory takes it from `--root`, or from the
`PRA_STAGE_A_ROOT` environment variable, and has no default: with neither set
it prints one line and stops rather than guessing a path:

    export PRA_STAGE_A_ROOT=~/pra-stage-a    # or pass --root to each script

    corpus.py    fetch the papers: CC BY, open access, real statistics and tables
    plant.py     plant one error of a known kind in one paper, and write the key
    score.py     score one run against one key, by rule, with no judgement
    matrix.py    run the arms one review at a time, scoring as each one lands

Install the extra the planter needs:

    pip install -e '.[stage-a,dev]'

## Running it

```sh
python scripts/stage_a/corpus.py roster                      # ~12 PDFs
python scripts/stage_a/corpus.py refill --per-field 6        # top each field up
python scripts/stage_a/corpus.py audit                       # usable PDFs per field
python scripts/stage_a/corpus.py audit --repair              # refetch what is not one
python scripts/stage_a/plant.py <pdf> --kind statistic       # plus the key
python scripts/stage_a/matrix.py --dry-run                   # the run order
python scripts/stage_a/matrix.py --stage pilot               # real reviews
python scripts/stage_a/matrix.py --rescore                   # re-score, run nothing
python scripts/stage_a/score.py <run folder> --key <key.json>
```

A candidate comes down from the [PMC Open Access Subset on AWS Open
Data](https://registry.opendata.aws/pmc-oa-opendata/), which serves the file
rather than a page about it, and is kept only if it begins with `%PDF`: the
first corpus fetch saved twenty "Preparing to download" interstitials and bot
challenges under `.pdf` names, all served with status 200. A file on disk that
is not a PDF counts as missing, so `refill` replaces it, and `audit --repair`
asks the bucket again for each one by the PMCID in its own name. `audit` says
what is usable per field before anything is planted in it.

`--rescore` exists because the scoring rule is written down and gets corrected:
when it does, every run already on disk is scored again by the new rule and
`runs/results.jsonl` is rewritten, so one table is never half one rule and half
another. No review is run and no run folder is touched.

`matrix.py` runs one review at a time, never retries, stops the moment a run
comes back `blocked`, and stops at a token ceiling. Every run is scored and
appended to `runs/results.jsonl` as it finishes, so an interrupted matrix is
still a result.

## Models, and comparing against the results already recorded

Every result in [`docs/stage-a-results.md`](../../docs/stage-a-results.md) ran
every core reviewer on one model. From 0.9.0 the review gives each reviewer a
different one — because Stage A is where three same-model reviewers were shown
to catch no more than one did — so the two cannot be pooled. `--reviewer-models`
pins them back:

```sh
python scripts/stage_a/matrix.py --stage main --reviewer-models sonnet
python scripts/stage_a/matrix.py --stage main --tag mixed-models
```

One name pins every reviewer to it; a comma-separated list sets the order they
are drawn from; unset leaves the review its own default. Run a pinned arm under
its own `--tag` so it gets its own run folders and its own rows, and the model
the row asked for is in `reviewer_models` on every row of `runs/results.jsonl`
and in `runs/matrix-state.json`. A folder that already holds a run under other
models is refused rather than reused.

## The four kinds of planted error

| kind | what is altered | why it is catchable |
| --- | --- | --- |
| `statistic` | a percentage or p-value | it no longer follows from the count, the total or the claim in its own sentence |
| `table-cell` | two numeric cells of one column, swapped | the column no longer totals, or the prose states the other value |
| `sample-size` | one statement of `n` | two places in the paper now disagree |
| `fake-doi` | the digits of one DOI suffix | the reference resolves to nothing |

Each plant is made in the PDF's **own content stream**, as an equal-length
substitution, so figures, layout and pagination are exactly as published and the
altered copy is a paper, not a rendering of one. A plant that does not survive
being imported again is thrown away rather than used.

## The scoring rule

A finding catches the planted error when it is in the right place **and** about
the right thing:

* **place** — it names one of the blocks the key lists, or its quote lies inside
  the altered text;
* **content** — the finding's *own words* name every value the key lists: the
  altered number together with the number it now contradicts. A short value has
  to stand on its own as a number: `.72` is not the `72` of `72%`, and not the
  `72.18` of some other calculation's receipt.

A key says `any` instead of `all` when its values are two ways of showing one
error. A fake DOI is the case: the printed suffix, which resolves to nothing,
and the real suffix, which is nowhere in the altered paper and can only come
from a lookup. The pilot's citations specialist reported the second shape — the
printed DOI returns 404, the preprint's DOI is this one, the last digits differ
— and that is a catch.

Quoting the altered sentence while saying something else about it is a near miss,
not a catch. Near misses are recorded too, because they are what tell an agent
brief that it looked in the right place and did not run the check.

A catch the review guard quarantined is counted apart, as
`quarantined_catches`, and is not a catch: the run did not deliver it. It is
also not a failure of the arm — the arm found the error and the brief threw the
finding away, nearly always because the reviewer paraphrased the quote instead
of copying it. The two numbers have to stay separate, because one says the
reviewers need a better check and the other says the brief needs a clearer
instruction. A quarantined row keeps its block id, so the place rule still
reaches it even when its quote is the reason it was held.

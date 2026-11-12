# Stage A: does the review catch an error that was put there on purpose?

Stage A is one experiment. Take published open-access papers, plant exactly one
known error in each, run the ordinary review on the altered copy, and score by a
written rule whether any finding caught it — and what the catch cost in tokens
and minutes, in three arms: `single`, `reviewers-only` and `full-two-track`.

The papers, the altered copies, the answer keys and the run folders are **not in
this repository**. They live under `/home/ty/pra-stage-a/`, because they are
tens of megabytes of other people's PDFs and gigabytes of run output. What is in
the repository is the four scripts that make them and the results table in
[`docs/stage-a-results.md`](../../docs/stage-a-results.md).

    corpus.py    fetch the papers: CC BY, open access, real statistics and tables
    plant.py     plant one error of a known kind in one paper, and write the key
    score.py     score one run against one key, by rule, with no judgement
    matrix.py    run the arms one review at a time, scoring as each one lands

Install the extra the planter needs:

    pip install -e '.[stage-a,dev]'

## Running it

```sh
python scripts/stage_a/corpus.py roster                      # ~12 PDFs
python scripts/stage_a/plant.py <pdf> --kind statistic       # plus the key
python scripts/stage_a/matrix.py --dry-run                   # the run order
python scripts/stage_a/matrix.py --stage pilot               # real reviews
python scripts/stage_a/score.py <run folder> --key <key.json>
```

`matrix.py` runs one review at a time, never retries, stops the moment a run
comes back `blocked`, and stops at a token ceiling. Every run is scored and
appended to `runs/results.jsonl` as it finishes, so an interrupted matrix is
still a result.

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
  altered number together with the number it now contradicts.

Quoting the altered sentence while saying something else about it is a near miss,
not a catch. Near misses are recorded too, because they are what tell an agent
brief that it looked in the right place and did not run the check.

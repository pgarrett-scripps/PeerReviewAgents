# Stage A results

Stage A asks one question: **does a review of this manuscript find the defect
that was put into it?** Each paper is a real published open-access article with
exactly one planted error, and each arm is given the same altered PDF. A run
either names that error or it does not. Nothing here is a judgement of review
quality beyond that, and nothing here is an estimate of how the engine would do
on a paper with no planted error — except the control arm, which is the only
place a false allegation can be counted.

The numbers in this document come from `runs/results.jsonl` in the Stage A data
directory, written by `scripts/stage_a/matrix.py` and scored by
`scripts/stage_a/score.py`. Every row is one run folder, and the folder is the
evidence: `review/protocol.json` records what each run was allowed to do.

## Read these caveats before the tables

1. **The matrix ran on the pre-fix reviewer briefs.** The pilot showed the
   guard quarantining a *correct* catch because the reviewer had retyped the
   quote instead of copying it (see "Correct catches the guard withheld"). The
   fix — one sentence in each of the six specialist briefs telling the agent to
   copy the quote from the block text — is on this branch but was **not** in
   effect for any run in the tables below, deliberately: changing a brief
   mid-matrix would mean the arms were not run against the same instructions.
   The rerun of the affected papers is reported separately at the end.
2. **One planted defect per paper.** A "miss" is a miss of that one error. A
   run that missed it still wrote a review full of other findings, and those
   findings are not scored here at all.
3. **n is small.** Eight papers in the main stage, one run each per arm, no
   repeats. A one-paper difference moves a catch rate by 12.5 points. Treat
   every number as a direction, not an effect size.
4. **One engine ran the whole matrix.** Every run in the tables below — pilot,
   main, control and reviewers1 — was made by the same non-editable install of
   the engine at commit `f8d8d3b`, in the virtual environment built at 02:22.
   Commits landing on the branch afterwards (the merge of `main` 45d6834, and
   the brief fix) did not reach those runs: the install holds a *copy* of the
   package, not a link to the working tree. See "Which engine each venv held".
5. **Tokens are the engine's own count** from `review/runtime.json`, summed over
   every agent in the run.

## Which engine each venv held

| Venv | Engine | What it ran |
| --- | --- | --- |
| built 02:22 | `f8d8d3b` | every matrix run: pilot, main, control, reviewers1 |
| built for the rerun | `brief-exact-quotes` (45d6834 + the brief fix) | the four brief-fix reruns, and the final `--rescore` of every row |

Both are plain (non-editable) installs, so each run is pinned to the code that
was installed when its venv was built, whatever the working tree says.

The diff from `f8d8d3b` to 45d6834 touches six files under `peerreviewagents/`:
`__init__.py`, `cli/main.py`, `engine/usage.py`, `names.py`, `runs.py` and the
new `workspace.py`. **Nothing under `peerreviewagents/plugin/` changed**, which
is where the reviewer briefs, the MCP server and the quote guard live — so no
run in this document was reviewed or guarded differently from any other. The
check was made against the diff, not assumed.

Two of those files do matter for how the numbers are *read*, and both are read
after a review is over, not during it:

* `runs.py` changed the `publishable` rule. Under `f8d8d3b` a run was
  publishable only if it was `complete`, with findings from the editor package
  and a clean guard; 45d6834 replaced that with `publishable_reasons`, under
  which a `complete-without-findings` run with nothing quarantined is also
  publishable. The `publishable` column below is the **45d6834 rule applied to
  every row**, because the final `--rescore` was run from the rerun venv. It is
  therefore comparable across stages, and it is the rule that ships in 0.9.0.
* `engine/usage.py` changed how agent rows are totalled for the report's agent
  table. The token figures here are summed by `score.py` from
  `review/runtime.json` itself, so this does not move them.

## What ran

| Stage | Papers | Arms | Reviewers | Runs |
| --- | --- | --- | --- | --- |
| pilot | 2 planted | `single`, `reviewers-only`, `full-two-track` | 3 | 6 |
| main | 8 planted | `single`, `reviewers-only`, `full-two-track` | 3 | 24 |
| control | 2 unaltered | `full-two-track` | 3 | 2 (pending) |
| reviewers1 | 3 planted | `reviewers-only` | 1 | 3 (pending) |

The arms are defined in [`study-arms.md`](study-arms.md): `single` is one core
reviewer and no editor, `reviewers-only` is three core reviewers and the editor
with no specialist track, `full-two-track` is the default review — three core
reviewers, all six specialists, the editor.

### Candidates that could not be used

The corpus fetch collected 41 candidate files. **Twenty of them are not PDFs**:
each is PMC's "Preparing to download" interstitial HTML page saved under a
`.pdf` name. The engine's ingest refuses them — `malformed pdf: no %PDF
header` — and the review comes back `blocked` without reviewing anything, which
is how the first control run was lost (that row is kept in the data as an input
failure, not a review failure; 6,250 tokens).

`matrix.py` now checks every queued input for a `%PDF` header before it starts
any session and refuses the whole queue if one fails, naming the file. These
twenty are unusable until the fetch is fixed:

| | | | |
| --- | --- | --- | --- |
| economics-pmc10364581 | economics-pmc6227754 | economics-pmc6227755 | economics-pmc7084952 |
| economics-pmc8500697 | epidemiology-pmc6227754 | genomics-pmc10850029 | materials-pmc5831698 |
| materials-pmc7144564 | microbiology-pmc11718157 | microbiology-pmc6300481 | microbiology-pmc7145624 |
| microbiology-pmc9763654 | microbiology-pmc9825576 | neuroscience-pmc5770339 | trial-pmc6145832 |
| trial-pmc6319250 | trial-pmc6796530 | trial-pmc7262592 | trial-pmc9542252 |

None of them is a planted paper: the ten planted PDFs and the altered copies
made from them are all real PDFs, so no row in the tables below rests on one of
these files. The loss is to the pool the controls were drawn from — twenty of
the thirty-one unplanted candidates are gone, which is also why the controls are
two papers and not more.

## Per-arm summary

Pilot only (2 papers). Main-stage numbers drop in when the matrix finishes.

| Arm | Caught | Agents | Findings (mean) | Tokens | Tokens (mean) | Minutes (mean) | Publishable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `single` | 1 / 2 | 4 | 7.0 | 212,201 | 106,101 | 2.3 | 2 / 2 |
| `reviewers-only` | 1 / 2 | 7 | 18.0 | 570,313 | 285,157 | 2.7 | 1 / 2 |
| `full-two-track` | 2 / 2 | 13 | 56.5 | 1,452,616 | 726,308 | 5.2 | 1 / 2 |

Main stage (8 papers, 24 runs):

| Arm | Caught | Agents | Findings (mean) | Tokens | Tokens (mean) | Minutes (mean) | Publishable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `single` | (pending) | 4 | (pending) | (pending) | (pending) | (pending) | (pending) |
| `reviewers-only` | (pending) | 7 | (pending) | (pending) | (pending) | (pending) | (pending) |
| `full-two-track` | (pending) | 13 | (pending) | (pending) | (pending) | (pending) | (pending) |

Pilot and main together, which is the number to quote:

| Arm | Caught | Tokens | Cost per catch |
| --- | --- | --- | --- |
| `single` | (pending) | (pending) | (pending) |
| `reviewers-only` | (pending) | (pending) | (pending) |
| `full-two-track` | (pending) | (pending) | (pending) |

(pending: state plainly here whether `full-two-track` beat `single` at all, and
by how many papers. If it did not, say that, and say what it cost to not beat
it.)

## Per paper

`•` caught, `—` missed. The arm that first names the defect is in the "who
caught it" column.

| Paper | Planted | `single` | `reviewers-only` | `full-two-track` | Who caught it |
| --- | --- | --- | --- | --- | --- |
| education-pmc6085180 | statistic | • | • | • | core reviewer in all three; `spec-consistency` first in `full` |
| epidemiology-pmc7611074 | fake-doi | — | — | • | `spec-citations` only |
| microbiology-pmc6192448 | sample-size | • | • | • | (pending) |
| microbiology-pmc7296073 | fake-doi | — | — | — | nobody |
| neuroscience-pmc8596853 | table-cell | • | — | • | (pending) |
| neuroscience-pmc9046077 | table-cell | • | • | • | (pending) |
| psychology-pmc7241824 | statistic | • | • | • | (pending) |
| psychology-pmc7609822 | table-cell | — | — | • | (pending) |
| trial-pmc8053280 | statistic | • | • | • | (pending) |
| trial-pmc8938265 | sample-size | • | (pending) | (pending) | (pending) |

## Specialist attribution

Which slot found the planted defect in `full-two-track`, and whether a core
reviewer found it in the same run. This is what the second track is for: if
every catch in `full` also has a core catch, the specialists added nothing on
these papers.

| Paper | Found by | Also found by a core reviewer in the same run? |
| --- | --- | --- |
| education-pmc6085180 | `spec-consistency` | yes (`core-3`, quarantined — see below) |
| epidemiology-pmc7611074 | `spec-citations` | no |
| (pending for the main stage) | | |

(pending: count the papers where only a specialist found it. Those are the
papers the specialist track paid for.)

## Correct catches the guard withheld

The review guard drops any finding whose quote it cannot match to the block the
finding cites. When the quote was retyped or reordered, that drops a finding
that was *right*. These are not errors in the review; they are catches the
report never showed, and they are counted separately per arm because they are
the cost of the guard being strict.

| Arm | Runs with a withheld correct catch | Withheld correct catches | Quarantined in total |
| --- | --- | --- | --- |
| `single` (pilot) | 0 / 2 | 0 | 0 |
| `reviewers-only` (pilot) | 1 / 2 | 1 | 1 |
| `full-two-track` (pilot) | 1 / 2 | 1 | 5 |
| (pending for the main stage) | | | |

The pilot case, exactly: in
`education-pmc6085180--statistic--reviewers-only--r3`, `core-2` filed
"Abstract reports 6-month P = .72 for a result the paper elsewhere reports as
P = .02" with the quote `d = 0.29; P = .72; 95% CI, 0.24 to 2.35`. The block
reads `D = 1.29; P = .72; d = 0.29; 95% CI, 0.24 to 2.35`. The finding is
correct and the guard was correct: the quote is not in the block, because the
reviewer reassembled it. The same thing happened in that paper's
`full-two-track` run, where `core-3` wrote `d = 0.29; P = .72; d = 0.29`.

Both runs still caught the defect through another slot, so the catch counts in
the tables above are unaffected — but a run that had only the withheld finding
would read as a clean review of a paper with a planted error in it.

## False allegations on the controls

Two unaltered papers through `full-two-track`. A "false allegation" is a
finding that asserts a specific error of the kind Stage A plants — a number
that contradicts another number, a sample size that does not add up, a DOI that
does not resolve, a table cell that disagrees with the text — where no such
error exists. A finding that asks a question, requests a clarification, or
criticises the study is not an allegation and is not counted.

| Paper | Findings | Planted-style allegations | Escalated | Quarantined |
| --- | --- | --- | --- | --- |
| (pending) | | | | |

(pending: read each allegation and say in one line why it is or is not false.
This is the number that decides whether the catch rates above mean anything:
an arm that alleges a defect in every paper catches every planted defect for
free.)

## One reviewer or three

`reviewers-only` with one core reviewer against the same arm with three, on the
first three planted papers.

| Paper | 1 reviewer | 3 reviewers | Tokens (1) | Tokens (3) |
| --- | --- | --- | --- | --- |
| (pending) | | | | |

(pending: say whether the third reviewer changed any outcome, and what the two
extra reviewers cost per paper.)

## Briefs that need work

What the runs showed about the reviewer and specialist briefs themselves,
with the evidence from the runs above.

1. **Quotes must be copied, not retyped.** Evidence: the two pilot cases above.
   Fix on this branch: one sentence in each of the six specialist briefs
   (`stats`, `figures-tables`, `methods-reporting`, `citations`, `consistency`,
   `field`) telling the agent to copy the quote from the block text rather than
   retype or paraphrase it. `core-reviewer.md` already said "Do not paraphrase
   inside a quote", and both pilot failures were core reviewers, so the fix also
   makes that sentence explicit about copying rather than only about
   paraphrasing.
2. (pending: anything the main-stage misses show. A defect missed by all three
   arms — microbiology-pmc7296073's fake DOI — is a brief question before it is
   a model question: no slot was asked to resolve an identifier it could check.)

## Rerun after the brief fix

The affected papers, rerun with the fixed briefs, against the same rows above.

| Paper | Arm | Before | After | Withheld correct catches before / after |
| --- | --- | --- | --- | --- |
| (pending) | | | | |

## The follow-up skill

(pending: the stub verification and the one authorised real follow-up run —
which run it followed up, what it reported, and whether the skill's reading of
the run matched the run folder.)

## Stage B sizing

(pending: papers, arms, repeats and a token estimate derived from the per-arm
mean token cost measured above, not from a guess. State what Stage A could not
answer and what Stage B would have to be to answer it.)

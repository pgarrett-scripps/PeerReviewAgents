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
| control | 3 unaltered | `full-two-track` | 3 | 3 (2 reviewed, 1 blocked on input) |
| reviewers1 | 3 planted | `reviewers-only` | 1 | 3 |
| rerun | 2 planted | `full-two-track` | 3 | 2 |
| follow-up | 1 revision | `single` | 1 | 1 |

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

"Agents" is the number of agent sessions the run itself recorded, which is what
the usage table counts tokens for. Every number in this section comes from the
rows `scripts/stage_a/score.py` wrote into `runs/results.jsonl`.

All of Stage A — the 38 scored rows (pilot, main, control, reviewers1, rerun)
plus the one follow-up run — cost **12,938,775 observed tokens**, against the
14 M ceiling it was given.

Pilot (2 papers, 6 runs):

| Arm | Caught | Agents | Findings (mean) | Tokens | Tokens (mean) | Minutes (mean) | Publishable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `single` | 1 / 2 | 2 | 7.0 | 212,201 | 106,100 | 2.2 | 2 / 2 |
| `reviewers-only` | 1 / 2 | 5 | 18.0 | 570,313 | 285,156 | 2.7 | 2 / 2 |
| `full-two-track` | 2 / 2 | 11 | 56.5 | 1,452,616 | 726,308 | 5.2 | 2 / 2 |

Main stage (8 papers, 24 runs):

| Arm | Caught | Agents | Findings (mean) | Tokens | Tokens (mean) | Minutes (mean) | Publishable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `single` | 6 / 8 | 2 | 6.5 | 716,161 | 89,520 | 2.2 | 8 / 8 |
| `reviewers-only` | 5 / 8 | 5 | 19.5 | 1,973,763 | 246,720 | 3.4 | 8 / 8 |
| `full-two-track` | 7 / 8 | 11 | 53.2 | 5,037,101 | 629,638 | 4.8 | 8 / 8 |

Pilot and main together (10 papers, 30 runs), which is the number to quote:

| Arm | Caught | Findings (mean) | Tokens | Tokens (mean) | Cost per catch | Publishable |
| --- | --- | --- | --- | --- | --- | --- |
| `single` | 7 / 10 | 6.6 | 928,362 | 92,836 | 132,623 | 10 / 10 |
| `reviewers-only` | 6 / 10 | 19.2 | 2,544,076 | 254,408 | 424,013 | 10 / 10 |
| `full-two-track` | 9 / 10 | 53.9 | 6,489,717 | 648,972 | 721,080 | 10 / 10 |

Plainly: **`full-two-track` beat `single` by two papers, 9 of 10 against 7 of
10, and cost 7.0 times as many tokens to do it** — 6.49 M against 0.93 M, or
721 k per catch against 133 k. Two papers for 5.56 M extra tokens is about
2.8 M tokens per additional paper caught.

**`reviewers-only` is the arm that did not pay.** Three core reviewers with no
specialist track caught *fewer* papers than one core reviewer alone — 6 of 10
against 7 of 10 — for 2.7 times the tokens. The paper it lost
(neuroscience-pmc8596853) is one the single reviewer caught, so this is not
"three reviewers found less"; it is that the arm's one planted catch rate is
inside run-to-run noise at n = 10 and the extra reviewers bought nothing
measurable. On this corpus the specialist track, not the reviewer count, is
what the extra tokens bought.

All 30 planted runs are publishable under the 0.9.0 rule. Findings per run
scale with agent count almost exactly: 6.6 findings on 2 agents, 19.2 on 5,
53.9 on 11 — roughly 3.3, 3.8 and 4.9 findings per agent. More agents produce
more findings at a slightly rising rate per agent; what changes far less is
whether the planted defect is among them.

## Per paper

`•` caught, `—` missed. The arm that first names the defect is in the "who
caught it" column.

| Paper | Planted | `single` | `reviewers-only` | `full-two-track` | First slot to name it, per arm |
| --- | --- | --- | --- | --- | --- |
| education-pmc6085180 | statistic | • | • | • | `core-1` / `core-3` / `spec-consistency` |
| epidemiology-pmc7611074 | fake-doi | — | — | • | — / — / `spec-citations` |
| microbiology-pmc6192448 | sample-size | • | • | • | `core-1` / `core-1` / `spec-consistency` |
| microbiology-pmc7296073 | fake-doi | — | — | — | nobody, in any arm |
| neuroscience-pmc8596853 | table-cell | • | — | • | `core-1` / — / `spec-figures-tables` |
| neuroscience-pmc9046077 | table-cell | • | • | • | `core-1` / `core-1` / `spec-consistency` |
| psychology-pmc7241824 | statistic | • | • | • | `core-1` / `core-2` / `core-2` |
| psychology-pmc7609822 | table-cell | — | — | • | — / — / `core-3` |
| trial-pmc8053280 | statistic | • | • | • | `core-1` / `core-3` / `core-3` |
| trial-pmc8938265 | sample-size | • | • | • | `core-1` / `core-1` / `core-3` |

The two papers `full-two-track` gained over `single` are
epidemiology-pmc7611074 and psychology-pmc7609822, and they were gained in
different ways. The fake DOI in epidemiology-pmc7611074 was found only by
`spec-citations`, a slot the other two arms do not have — a structural win for
the specialist track. The table-cell error in psychology-pmc7609822 was found
by `core-3`, a slot `reviewers-only` also had and in which it missed the defect,
so that one is a core reviewer getting lucky in one run and not in another, not
something the second track bought. Counted strictly, the specialist track is
responsible for one of the two extra papers.

One defect was missed by every arm: the fake DOI in microbiology-pmc7296073.
`spec-citations` caught the same class of defect in epidemiology-pmc7611074, so
the slot can do it; it did not here.

## Specialist attribution

Which slot found the planted defect in `full-two-track`, and whether a core
reviewer found it in the same run. This is what the second track is for: if
every catch in `full` also has a core catch, the specialists added nothing on
these papers.

| Paper | First catch | Every slot that caught it | Core reviewer too? |
| --- | --- | --- | --- |
| education-pmc6085180 | `spec-consistency` | `core-1`, `core-2`, `core-3`, `spec-consistency`, `spec-field`, `spec-figures-tables`, `spec-methods-reporting`, `spec-stats` | yes |
| epidemiology-pmc7611074 | `spec-citations` | `spec-citations` | **no** |
| microbiology-pmc6192448 | `spec-consistency` | `core-1`, `core-2`, `spec-consistency` | yes |
| microbiology-pmc7296073 | — | — | not caught |
| neuroscience-pmc8596853 | `spec-figures-tables` | `spec-field`, `spec-figures-tables`, `spec-stats` | **no** |
| neuroscience-pmc9046077 | `spec-consistency` | `core-1`, `core-3`, `spec-consistency`, `spec-figures-tables`, `spec-stats` | yes |
| psychology-pmc7241824 | `core-2` | `core-1`, `core-2`, `core-3`, `spec-consistency`, `spec-field`, `spec-figures-tables`, `spec-methods-reporting`, `spec-stats` | yes |
| psychology-pmc7609822 | `core-3` | `core-3`, `spec-stats` | yes |
| trial-pmc8053280 | `core-3` | `core-1`, `core-2`, `core-3`, `spec-consistency`, `spec-field`, `spec-figures-tables`, `spec-stats` | yes |
| trial-pmc8938265 | `core-3` | `core-1`, `core-2`, `core-3`, `spec-consistency`, `spec-field`, `spec-figures-tables`, `spec-methods-reporting`, `spec-stats` | yes |

**Two of the nine catches were specialist-only**: epidemiology-pmc7611074
(`spec-citations`, the fake DOI) and neuroscience-pmc8596853 (`spec-field`,
`spec-figures-tables` and `spec-stats`, the table cell, with no core reviewer
naming it in that run). Those two papers are what the specialist track paid
for in `full-two-track`. On the other seven, at least one core reviewer named
the defect as well, and in four of them a specialist was still the first to
name it — `spec-consistency` three times. Being first is worth something for
the editor package's ordering but it is not a catch the arm would otherwise
have lost.

Note that neuroscience-pmc8596853 is specialist-only in `full` yet was caught
by `core-1` in the `single` arm. The same brief found it with one reviewer and
missed it with three. That is the clearest single piece of evidence in Stage A
that at n = 10 a one-paper difference between arms is not a real difference.

## Correct catches the guard withheld

The review guard drops any finding whose quote it cannot match to the block the
finding cites. When the quote was retyped or reordered, that drops a finding
that was *right*. These are not errors in the review; they are catches the
report never showed, and they are counted separately per arm because they are
the cost of the guard being strict.

Pilot and main together, 10 runs per arm:

| Arm | Runs with a withheld correct catch | Withheld correct catches | Quarantined findings in total | Escalated |
| --- | --- | --- | --- | --- |
| `single` | 1 / 10 | 1 | 2 | 0 |
| `reviewers-only` | 1 / 10 | 1 | 7 | 0 |
| `full-two-track` | 2 / 10 | 3 | 26 | 10 |

The four runs, named:

| Run | Arm | Withheld correct catches | Quarantined in that run | Caught anyway |
| --- | --- | --- | --- | --- |
| education-pmc6085180--statistic--full-two-track--r3 | `full-two-track` | 1 | 5 | yes |
| education-pmc6085180--statistic--reviewers-only--r3 | `reviewers-only` | 1 | 1 | yes |
| neuroscience-pmc9046077--table-cell--full-two-track--r3 | `full-two-track` | 2 | 2 | yes |
| neuroscience-pmc9046077--table-cell--single--r3 | `single` | 1 | 1 | yes |

Every one of the four gives the same reason: "Quote is absent or ambiguous in
the selected source block". No catch was lost outright — all four runs named
the planted defect through another slot — so the catch counts above are
unaffected. Two papers out of ten produced all of them, and
neuroscience-pmc9046077 produced them in two different arms, which points at
the paper's table formatting rather than at any one reviewer.

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

| Paper | Status | Findings | Planted-style allegations | Quarantined | Tokens |
| --- | --- | --- | --- | --- | --- |
| education-pmc7183109 | complete | 65 | 7 | 1 | 558,748 |
| microbiology-pmc8208984 | complete | 48 | 0 | 1 | 453,364 |
| trial-pmc6145832 | blocked | 0 | 0 | 0 | 6,250 |

The blocked row is the input failure described in "Candidates that could not be
used"; it reviewed nothing and is kept only so the token total is honest.

microbiology-pmc8208984 produced 48 findings across all nine slots and **not one
of them alleges a specific error of the planted kind**. That is the clean
result: the arm does not invent contradictions to have something to say.

education-pmc7183109 produced seven. Each is below with the quote the guard
matched to a source block — these are findings that passed the guard, so every
quote is verbatim from the block it cites, and the judgement is about whether
the *claim* is false, not whether the quote is real.

1. `spec-stats`, major — "Claim of no significant subgroup differences
   contradicted by Table 2; no between-group Q or p reported", quoting `for all
   Q statistics are non-signicant (I2 > 75%)`. **Not false.** The paper's own
   sentence asserts non-significant Q together with I² > 75%, which cannot both
   be the basis for the claim it makes.
2. `spec-consistency`, major — abstract and discussion overstate the result,
   quoting `The high homogeneity indicated that the studies were different from
   each other.` **Not false.** That sentence is in the paper, it describes
   I² = 87.8%, and it says the opposite of what "homogeneity" means.
3. `spec-methods-reporting`, major — effect-size computation and data handling
   not specified, quoting the paper's one-sentence description of what was
   extracted. **Not false, and not really an allegation**: it reports an
   omission, which is a reviewer's ordinary job, and the scorer counted it only
   because it names a specific reporting defect.
4. `core-1`, major — "Table 2 and Table 3 intervals are internally inconsistent
   with their SEs and with the pooled model". **Unverified.** Checking it means
   recomputing every interval in two tables from the published coefficients and
   standard errors, which this stage did not do.
5. `core-3`, major — the same claim as 4, filed independently with a different
   quote. **Unverified, and not independent evidence**: it is one claim counted
   twice.
6. `core-2`, major — subgroup differences declared non-significant with no
   between-group test, quoting the paper's own garbled claim to that effect.
   **Not false.**
7. `spec-field`, major — abstract misreads I² = 87.8% as high homogeneity, and
   no prediction interval is reported. **Not false**, same sentence as 2.

So the honest count is **zero confirmed false allegations on the two controls,
and at most two unconfirmed** — items 4 and 5, which are one arithmetic claim
about the same two tables. Five of the seven rest on sentences the paper
actually contains, and education-pmc7183109 is a meta-analysis with a real
internal contradiction in its abstract; a reviewer that said nothing about it
would be the one making a mistake. The catch rates above are therefore not
inflated by an arm that alleges a defect everywhere: the one arm measured on
unaltered papers alleged planted-style defects in one paper of two, and in that
paper the defects are mostly there.

What this does not establish: two papers is far too few to put a number on the
false-allegation rate, and the one claim that would count against the arm
(items 4 and 5) is the one left unchecked. Stage B needs more controls and a
way to adjudicate arithmetic claims without a human recomputing tables.

## One reviewer or three

`reviewers-only` with one core reviewer against the same arm with three, on the
first three planted papers.

| Paper | 1 reviewer | 3 reviewers | Tokens (1) | Tokens (3) | Findings (1) | Findings (3) |
| --- | --- | --- | --- | --- | --- | --- |
| education-pmc6085180 | caught | caught | 143,641 | 335,035 | 6 | 19 |
| epidemiology-pmc7611074 | missed | missed | 108,699 | 235,278 | 4 | 17 |
| microbiology-pmc6192448 | caught | caught | 102,114 | 242,871 | 6 | 19 |

**The two extra reviewers changed no outcome on any of the three papers.** Same
catch, same miss, paper for paper. They cost 152,910 tokens per paper on
average (354,454 against 813,184 in total, so one reviewer runs at 44% of the
three-reviewer cost) and produced about three times the findings — 5.3 against
18.3 per paper. More findings, the same answer on the thing being measured.

Two caveats before this is read as "one reviewer is enough". It is three
papers, and the one-reviewer runs reuse the three-reviewer papers rather than
being drawn fresh, so the comparison is paired but tiny. And the extra findings
are not nothing: nobody has checked whether the extra thirteen findings per
paper are useful to an author, only that they do not change whether the planted
defect is named. Stage A measured planted-defect recall, which is the one thing
a third reviewer did not help with here.

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
   paraphrasing. Measured, on a rerun of both affected papers: the retyped
   statistics quote stopped being quarantined and that paper's withheld correct
   catches went to zero. It fixed its own failure mode and nothing else — see
   "Rerun after the brief fix" for the two causes that remain, neither of which
   is a brief.
2. **No slot is asked to check an identifier.** Evidence: the fake DOI in
   microbiology-pmc7296073 was missed by all three arms, while the fake DOI in
   epidemiology-pmc7611074 was caught by `spec-citations` — so the slot can
   find this class of defect but has no procedure that makes it look. The brief
   tells `spec-citations` to look at how sources are used; it does not tell it
   to list every DOI, PMID and accession in the reference list and say for each
   one whether the identifier is consistent with the title, journal and year
   printed beside it. That is a mechanical pass a reviewer can do from the text
   alone, with no network, and it is the pass that would have caught both.
   Not fixed on this branch: it changes what a specialist is asked to produce,
   not just how it quotes, so it belongs in its own change with its own runs.
3. **An arithmetic claim should carry its arithmetic.** Evidence: items 4 and 5
   in the control section — `core-1` and `core-3` both alleged that Table 2 and
   Table 3 intervals are inconsistent with their standard errors, and neither
   finding shows the recomputation, so neither can be adjudicated without a
   human redoing two tables by hand. The briefs should require that a finding
   asserting a numeric inconsistency state the numbers it used and the value it
   expected. That makes a wrong claim cheap to dismiss and a right one cheap to
   confirm, which is exactly what the control arm needs to be scoreable.
   Not fixed on this branch, for the same reason as 2.

## Rerun after the brief fix

Two papers qualified for the rerun, not four: the rerun was authorised for the
planted papers where a correct catch had been quarantined for a quote mismatch,
and only education-pmc6085180 and neuroscience-pmc9046077 ever had one. Both
were rerun as `full-two-track` with three reviewers, from the rerun venv, into
their own folders — the rows they are compared against are untouched.

| Paper | Caught | Findings | Quarantined | Withheld correct catches | Tokens | Minutes |
| --- | --- | --- | --- | --- | --- | --- |
| education-pmc6085180 before | yes | 62 | 5 | 1 | 812,953 | 5.9 |
| education-pmc6085180 after | yes | 62 | **3** | **0** | 762,367 | 5.3 |
| neuroscience-pmc9046077 before | yes | 44 | 2 | 2 | 720,390 | 4.9 |
| neuroscience-pmc9046077 after | yes | 56 | 2 | 2 | 730,314 | 5.3 |

Across the two papers: withheld correct catches 3 → 2, quarantined findings
7 → 5, catches 2 of 2 → 2 of 2. Nothing got worse, and the reruns cost
1,492,681 tokens.

The two venvs differ by more than the briefs (see "Which engine each venv
held"), so before reading the table as the fix working: the only difference
between the two engines *under `peerreviewagents/plugin/`* — the briefs, the
MCP server and the guard, which are what a reviewer sees and what judges its
quotes — is the brief fix itself. The other changes are the reader surface, the
`publishable` rule and the agent-usage table. None of them can change whether a
quote anchors.

### What the fix fixed, and what it did not

The failure the fix was written for is gone. In the original run, `core-3`
submitted the planted statistic with the quote `d = 0.29; P = .72; d = 0.29`,
which is the block's `D = 1.29; P = .72; d = 0.29; 95% CI, 0.24 to 2.35`
reassembled wrongly. After the fix that finding is not in the quarantine list
at all, and education-pmc6085180's withheld correct catches went to zero.

The two withheld catches in neuroscience-pmc9046077 did not move, and reading
them shows why the briefs cannot move them. Both quote the table row

    Number of participants\t401\t164\t–

and the block *does* contain that row — with real tab characters. The stored
quote contains the two characters `\` and `t` where the tabs should be. The
reviewer copied what it was shown, faithfully, from a rendering in which the
tabs were escaped, and the guard is right that the resulting string is absent
from the block. Telling reviewers to copy rather than retype cannot fix a quote
that was copied. This is a reader or guard problem — either table blocks should
be presented to reviewers unescaped, or the guard should unescape a quote
before matching it — and it is worth fixing because it hit the planted defect
in both arms that found it.

The three quarantines still left in education-pmc6085180 have a third cause,
also not a brief problem. They quote sentences from `b0081`, which is a
two-column page linearised into one stream, so the text reads

    No trial group differences existed at baseline (Table 2), except online
    only). The overall attrition rate at 6 months (trial end point) for
    antidepressant use (18% UC v 10% CALM). Preliminary was 25.5% …

Two columns are interleaved sentence by sentence. "Preliminary analyses that
controlled for this factor" is a real sentence in the paper and does not exist
as a contiguous string in the block, so no quote of it can anchor. A reviewer
reading that block and reconstructing the sentence is doing the right thing;
the extractor is what is wrong. Of the five quarantines left across both
reruns, none is a retyped quote, two are escaped tabs and three are
two-column interleaving.

### Where the fix ships

In **this PR**, with the results. The condition set for that was fewer wrong
quarantines and no drop in catches: wrong quarantines fell from 3 to 2 and
total quarantines from 7 to 5, both catches were kept, both runs stayed
publishable, and the one failure mode the change targeted went from one
withheld catch to none with the cause verified finding by finding. The result
is partial, not mixed — no number moved the wrong way. The two causes it does
not address are named above and belong to the reader and the guard, which are
different code and need their own runs.

## The follow-up skill

### What the stub check settled

The skill originally said to compare an old run against the re-review of a
revision by `finding_uid`. That can never work, and
`tests/test_plugin_server.py::test_a_revision_shares_no_finding_uid_with_the_run_it_revises`
is the test that shows it: a finding's uid is a digest of the paper's id, the
slot, the quote and the blocks cited, and the paper's id is itself a digest of
the PDF. The test submits the same finding, from the same slot, on the same
block, with the same quote, over two files that differ by one comment line, and
the two uids differ. A uid comparison would have reported the whole first
review resolved and the whole second one new for a revision that changed
nothing at all.

The skill now matches on the passage a finding rests on and the claim it makes,
uses the uid only to name findings and to follow `duplicate_uids` and
`reviewers_raising` inside one run, and records the quote each pairing was made
on so a reader can check the pairing.

### The real follow-up run

One real run was authorised and used, from the rerun venv:
`runs/education-pmc6085180--followup-revision`. It followed up
`education-pmc6085180--statistic--single--r3` (the `single` arm, one reviewer,
8 findings), re-reviewing a revision of that paper in which exactly one thing
is different: the planted abstract value `P = .72` is repaired to `P = .02` in
block `b0010`, and `P = .72` occurs nowhere else in the file. The follow-up run
came back `complete` with 6 findings, nothing quarantined, 111,123 observed
tokens. Its comparison is written to `<run>/review/follow-up.md`.

What it reported, against the eight findings of the first review:

| | Count | Which |
| --- | --- | --- |
| still open | 5 | paired on the same passage and the same claim; three of the five quote text identical to the first run's quote |
| resolved | 1 | the planted P value, the one thing the revision changed |
| raised before, not raised again, not resolved | 2 | the compliance/retention strength and the Table 4 score ranges — both sentences unchanged in the revision |
| new | 0 | nothing the revision introduced |

The skill's reading matched the run folder on every check I could make against
it. Each pairing names a block and a quote, and each quote is present in the
block it names in `<run>/manuscript/paper.json` — including the two findings
that were not raised again, whose sentences are still in blocks `b0100` and
`b0080`. The one entry recorded as resolved is resolved by the text and not
merely by silence: the sentence behind it now reads `P = .02`, which agrees
with its own confidence interval and with Table 3.

Two things this settles beyond the stub check:

1. **Uid matching would have been wrong here in practice, not just in
   principle.** The two runs share no `finding_uid`, because the paper id is a
   digest of the PDF and the PDF changed. A uid comparison would have called
   all eight findings resolved and all six new.
2. **"Not raised again" is not "resolved", and the skill has to keep them
   apart.** Two findings vanished from a re-review of text that did not change.
   A procedure that treated absence as resolution would have reported the
   authors as having answered three of eight when they answered one. This is a
   property of one reviewer re-reading a paper, not of the revision, and it is
   the strongest argument for the skill's rule that a resolution must be shown
   in the text.

## Stage B sizing

### What Stage A could not answer

1. **Whether `full-two-track` really beats `single`.** It won by two papers of
   ten, and one of those two (psychology-pmc7609822) was won by a core
   reviewer, a slot `single` also has. Meanwhile `single` caught a paper `full`
   caught only through specialists and `reviewers-only` missed entirely. Two
   discordant pairs out of ten is not a measurement.
2. **The false-allegation rate.** Two controls, one of which is a genuinely
   contradictory paper, and the one allegation that would count against the arm
   was left unverified.
3. **Per-defect-kind recall.** Each kind was planted in two to four papers, so
   "fake DOIs are caught half the time" is one paper either way.
4. **Whether the extra findings are worth anything.** `full` produces 54
   findings a paper against 6.6. Stage A scored only whether the planted defect
   was among them.

### Sizing, from the measured means

Per-run means from the table above: `single` 92,836 tokens and 2.2 min,
`full-two-track` 648,972 tokens and 4.9 min, a `full` control 506,056 tokens
and 4.4 min (the two completed controls).

| Block | Runs | Tokens | Serial run time |
| --- | --- | --- | --- |
| 40 planted papers, `single` | 40 | 3.7 M | 1.5 h |
| 40 planted papers, `full-two-track` | 40 | 26.0 M | 3.3 h |
| 20 unaltered controls, `full-two-track` | 20 | 10.1 M | 1.5 h |
| 10 papers repeated 3× in `full-two-track` | 30 | 19.5 M | 2.5 h |
| **Total** | **130** | **59.3 M** | **8.7 h** |

Without the repeat block it is 100 runs, 39.8 M tokens and 6.2 h. The repeat
block is what turns "this arm caught two more papers" into a statement with a
run-to-run variance attached to it, and Stage A's neuroscience-pmc8596853 flip
is the reason to pay for it.

Three design decisions that follow from the numbers:

1. **Drop `reviewers-only`.** It spent 2.5 M tokens in Stage A to score below
   one reviewer. Spending those tokens on more papers in the two arms that
   differ structurally is strictly better. The one-versus-three question, if it
   is still wanted, is a cheap side experiment: 10 papers at 143 k and 335 k is
   4.8 M.
2. **Forty planted papers, ten of each defect kind.** Forty paired papers gives
   roughly eight to ten discordant pairs at the 20% discordance Stage A saw. If
   they fall nine to one toward `full`, that is an answer; if they fall six to
   four, that is also an answer, and the cheaper arm wins. Ten per kind makes
   per-kind recall a number rather than an anecdote.
3. **Twenty controls, and adjudicate their allegations.** Twenty unaltered
   papers through `full` is the only way to put a rate on false allegations,
   and it is worth nothing unless each planted-style allegation can be judged.
   That needs brief item 3 above — an arithmetic claim stating its arithmetic —
   shipped before Stage B starts, not after.

**The gate is the corpus, not the tokens.** Stage B needs 60 usable PDFs.
Stage A started from 61 candidates and lost 20 to the saved-HTML fetch bug, so
the fetcher has to be fixed and the pool refilled first. At Stage A's yield
that means fetching about 90 candidates to land 60.

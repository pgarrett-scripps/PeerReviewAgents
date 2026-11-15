# SPOT re-run on released 0.9.0

**The citation lane works now.** The pilot judged 38 citation uses across seven
papers; this run judged **434**, matched every one of them to a reference, and
returned 165 verdicts backed by a quoted passage where the pilot returned none.
Three papers that produced **zero** use rows in the pilot — their superscript
markers glued onto the preceding word by the reader — produced 124, 78 and 54.

**And one more annotated error is found.** Photocatalytic AgNPs was `partly` in
the pilot because the artifact and the misspelling both live inside a figure
bitmap. On 0.9.0 one finding names both:

> the lower panel is a flat plateau at ~1.4 Abs from ~320 to 800 nm, with an
> off-scale spike (>9 Abs) at ~260 nm … the x label of B is spelled
> `Wavelenght`

Catch rate on the five annotated papers goes from **3 found / 1 partly /
1 missed** to **4 found / 0 partly / 1 missed**. The miss is the same one, and
for the same reason: a micrograph reused between two figures, which no reading
of a text layer reaches.

What this run cost: **5,666,263 tokens and 105.8 minutes** across the seven
scored runs, against 5,210,737 and 61.2 for the pilot — 9% more tokens for
11× the judged citation uses, and 73% more wall clock, almost all of it the
citation lane now being allowed to finish.

Everything below comes from the run folders under `~/pra-spot/runs-0.9.0/`,
scored by `scripts/spot/score.py`. The pilot's `runs/` and its venv were not
touched; the before column is the pilot's own numbers, re-measured from the
pilot ledgers and matching [`SPOT_PILOT.md`](SPOT_PILOT.md) row for row.

## What was run

- Released `peerreviewagents==0.9.0` from PyPI (tag `v0.9.0`, main `410309b`),
  pinning `rustypaper==0.2.3` on `rustium-pdf` 0.1.3. PRs #40 to #45 are in it.
  Freeze: `~/pra-spot/runs-0.9.0/pip-freeze.txt`, CPython 3.12.12.
- The same seven PDFs and the same command as the pilot, **one paper at a
  time**, sequentially, in one background runner:
  `peerreview review papers/<paper>.pdf runs-0.9.0/<slug> --arm full-two-track
  --reviewers 3 --per-run-tokens 1200000`.
- Claude subscription only, `claude-code` only, no API key in the environment.
- Nothing beyond the seven papers: no 20-paper subset, no Stage B, and one
  retry of one paper, for the launcher fault described below.

## The seven runs, pilot → 0.9.0

| # | paper | SPOT row / error type | annotated error found? | findings (major) | distinct issues worth thanks | tokens | min | run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `medicine-oxytetracycline` | 66 / statistical reporting | **yes** → **yes** | 64 (31) → 62 (30) | 8 → 9 | 878,203 → 892,033 | 9.9 → 22.8 | complete → complete |
| 2 | `chemistry-photocatalytic` | 8 / data inconsistency | **partly** → **yes** | 63 (20) → 69 (23) | 8 → 8 | 758,541 → 876,349 | 10.3 → 8.6 | **blocked** → complete |
| 3 | `biology-dark-kinome` | 6 / reagent identity | **yes** → **yes** | 55 (17) → 49 (13) | 8 → 7 | 833,495 → 937,931 | 10.1 → 11.2 | complete → **blocked** |
| 4 | `biology-cgrp` | 53 / figure duplication | **no** → **no** | 55 (20) → 55 (21) | 8 → 8 | 720,317 → 792,045 | 8.5 → 22.7 | complete → complete |
| 5 | `network-beamforming` | 1 / equation & proof | **yes** → **yes** | 34 (10) → 39 (10) | 5 → 5 | 690,345 → 700,109 | 7.2 → 16.8 | complete → complete |
| 6 | `control-education-pmc7183109` | control, nothing planted | n/a | 55 (12) → 65 (13) | 6 → 6 | 697,001 → 766,215 | 6.9 → 17.2 | complete → complete |
| 7 | `control-microbiology-pmc8208984` | control, nothing planted | n/a | 42 (9) → 39 (8) | 4 → 4 | 632,835 → 701,581 | 8.3 → 6.5 | complete → complete |

Totals: 368 (119) → **378 (118)** findings, 47 → **47** distinct issues,
5,210,737 → **5,666,263** tokens, 61.2 → **105.8** minutes. Guard status is
clean on all seven and **quarantined findings fall from 2 to 0**. The
`distinct issues` column is the same hand count the pilot used — collapsing the
three core reviewers and six specialists reporting one issue into one — and the
reading for each is below.

**The run volume did not change.** Nine findings more, one major fewer, the
same 47 distinct issues. Whatever 0.9.0 bought, it did not buy more review.

### The two changed verdicts

**Photocatalytic, partly → found.** The annotation is the ~260 nm artifact
spike in Fig. 4B and its `Wavelenght` axis label. The quote above is a single
`spec-figures-tables` finding that names both, reads the panel's own y-range
(0–9 against 0–3 in Fig. 4A), and notes there is no resolved SPR band where the
text claims a 310 nm peak. The pilot had the right panel family and the wrong
reason. This is the one place in the run where the reader pin visibly changes
what the engine can see.

**Dark kinome, complete → blocked.** Not a content change: the error is still
found, exactly, by four lanes ("Stated LTK, INSRR and AATK constructs lack the
kinase domain", plus the un-annotated EPHA6 sites outside the construct). The
run is blocked on bookkeeping — see *What broke*.

### What each verdict rests on, in one line

1. **Oxytetracycline — found.** Four findings carry Stage A's value rule,
   naming `587.51` and `1780.34`; 25 candidate findings at the annotated
   location. Negative Kel and AUC(0–∞) called fit failures, not biology.
2. **Photocatalytic — found.** Above.
3. **Dark kinome — found.** Three of five constructs named as lacking the
   catalytic domain, with boundaries; EPHA6 pY259/pY484 placed outside
   R683–V1130.
4. **cgrp — missed.** 6 candidates at the location, none about a duplicated or
   reused image. No vision lane; the duplication is bitmap-only.
5. **Beamforming — found, with the derivation.** Five findings name 12.0 dB;
   `core-2` derives it: "per-symbol SNR is N²/σ²" for beamforming against
   "T/σ²" for MA-TISK, so the schemes are not equivalent at matched rate.

## The citation lane

This is what the run existed to measure.

| paper | reference rows | resolved | uses | matched | judged | with passage | citation-worker tokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| oxytetracycline | 86 → 86 | 26 → **56** | 0 → **124** | 0 → 124 | 0 → 124 | 0 → 56 | 76,097 → 227,652 |
| photocatalytic | 59 → 59 | 43 → **54** | 0 → **78** | 0 → 78 | 0 → 78 | 0 → 15 | 66,722 → 151,321 |
| dark-kinome | 0 → **55** | 0 → **45** | 63 → 63 | 0 → **63** | 0 → **63** | 0 → 47 | 51,608 → 143,462 |
| cgrp | 2 → **36** | 0 → **13** | 0 → **34** | 0 → 34 | 0 → 34 | 0 → 12 | 55,992 → 97,177 |
| beamforming | 25 → 25 | 10 → 10 | 33 → 34 | 33 → 34 | 33 → 34 | 0 → 0 | 76,455 → 75,192 |
| control-education | 5 → **31** | 0 → 0 | 47 → 47 | 5 → **47** | 5 → **47** | 0 → 0 | 57,722 → 86,628 |
| control-microbiology | 33 → 33 | 17 → **33** | 0 → **54** | 0 → 54 | 0 → 54 | 0 → 35 | 48,468 → 124,935 |
| **all seven** | 210 → **325** | 96 → **211** | 143 → **434** | 38 → **434** | 38 → **434** | 0 → **165** | 433,064 → **906,367** |

Every use row now matches a reference and every matched use is judged: the
`uses_unmatched` and `uses_unjudged` counts are zero on all seven papers. The
pilot's superscript gap is closed — 358 of the 434 use rows have derivation
`reader-link` and 18 `reader-link-range`, so the markers come from the reader's
link annotations rather than from a text pattern. Only 24 came from
`marker-numeric` and 34 from `marker-author-year`.

### Verdicts

| paper | supports | partly supports | does not support | cannot tell |
| --- | --- | --- | --- | --- |
| oxytetracycline | 10 | 36 | 10 | 68 |
| photocatalytic | 4 | 9 | 2 | 63 |
| dark-kinome | 18 | 27 | 2 | 16 |
| cgrp | 3 | 9 | 0 | 22 |
| beamforming | 0 | 0 | 0 | 34 |
| control-education | 0 | 0 | 0 | 47 |
| control-microbiology | 9 | 25 | 1 | 19 |
| **all seven** | **44** | **106** | **15** | **269** |

The pilot's 38 verdicts were all `cannot tell`.

**A verdict is something other than `cannot tell` exactly when a passage was
retrieved.** 165 uses carry a passage and 165 carry a graded verdict, per paper
and in total: 56/56 on oxytetracycline, 15/15 on photocatalytic, 47/47 on
dark-kinome, 12/12 on cgrp, 35/35 on control-microbiology, 0/0 on the two that
got none. So `cannot tell` is not the judge hedging. It is the judge having
nothing to read.

What it had to read: of 325 references, **0 returned full text** and 153
returned an abstract. Every graded verdict in this run rests on an abstract.

### Why a reference was not resolved

| state | n | cause |
| --- | --- | --- |
| resolved (`yes`) | 211 | — |
| could not check | 55 | **55 of 55** had no title extracted, so no lookup was ever made |
| not located | 51 | printed no usable DOI; a title search ran and came back empty |
| does not exist (`no`) | 8 | a printed DOI with no handle at any registration agency |

Two of these need naming.

**The `no` state is reachable from a broken text layer.** Seven of the eight
are on cgrp, whose text layer breaks words across spaces (`block-ade`,
`tran-scription`) — and breaks DOIs the same way. The resolver received
`10.1016/j.jconrel.2016.01` where the page prints
`10.1016/j.jconrel.2016.01. 026`, found no handle for the truncated prefix, and
recorded the ledger's hardest state, `exists: no`, for a reference that exists
and is indexed. `does not exist` should not be reachable from a DOI that does
not look complete.

**Title extraction still loses whole reference lists.** 29 of
control-education's 31 references are `could not check` for want of a title,
and its list is ordinary Vancouver-with-colon:

> McLachlan JC, Patten D: Anatomy teaching: ghosts of the past, present and
> future. Medical education 2006, 40(3):243-253.

The parser read the venue as `Medical education 2006, 40(3):243-253` and the
title as `null`. That paper's citation lane then returns 47 uses, 47 judged and
47 `cannot tell` — the lane's whole cost with none of its value. The pilot saw
the same cause on 53 of 56 rows; 0.9.0 extracts far more reference rows (5 → 31
here, 2 → 36 on cgrp, 0 → 55 on dark-kinome) and the titles inside them are now
the binding constraint.

One wording bug falls out of this: the run's own `problems` list says those
references "could not be checked because the lookups did not answer", when the
ledger's note says no lookup was made for want of a title. The summary blames
the indexes for the parser.

### Ten sampled verdicts, read by hand

Each row is the ledger's verdict, the sentence it judged, and the reference it
judged against. My own read is in the last column.

| # | paper | ref | sentence (short) | verdict | right? |
| --- | --- | --- | --- | --- | --- |
| 1 | oxytetracycline | 13 — florfenicol PK in rainbow trout | species whose OTC pharmacokinetics have been studied | does not support | **yes** — the source is florfenicol, not oxytetracycline |
| 2 | oxytetracycline | 29 — "Abuse potential of loperamide" | cited for a creatinine kinetic assay | does not support | **yes** — unrelated |
| 3 | oxytetracycline | 3 — OTC pharmacokinetics in trout | OTC's "pivotal role in treating bacterial infections" | partly supports | **yes** — right drug, but the therapeutic-role claim is not in it |
| 4 | oxytetracycline | 8 — drug-induced mitochondrial impairment | hepatotoxicity and nephrotoxicity in fish | partly supports | **yes** — mechanism yes, fish no |
| 5 | oxytetracycline | 21 — antibiotic residues in fish | residues in fish tissues are a consumer hazard | supports | **yes** |
| 6 | oxytetracycline | 26 — biuret assay | total protein measured by biuret | supports | **yes** |
| 7 | photocatalytic | 7 — Emerich & Thanos, *Nanotechnology and medicine* (2003) | AgNP interest for biodegradation and bioremediation | does not support | **yes** — a nanomedicine review says nothing about bioremediation |
| 8 | photocatalytic | 27 — peptide-coated AgNP pH-triggered assembly | SBT-AgNPs work as a redox catalyst by electron relay | does not support | **yes** — surface chemistry and assembly, not redox catalysis |
| 9 | dark-kinome | 9 — clinical kinase index | "less than 10% of kinases have FDA approved small molecule inhibitors or activators" | supports | **yes**, with a nuance: the passage says 8% of the kinome is "effectively drugged", which is a looser claim than FDA-approved inhibitors |
| 10 | cgrp | 20 — Maruyama 2017, fungal osteo-inflammation via TRP–CGRP–Jdp2 | CGRP targets have "garnered attention in temporomandibular arthritis research" | partly supports | **yes** — supports CGRP as an anti-inflammatory target, not the TMJ framing |

Ten of ten read correctly, and the grading discriminates: two more rows on the
same cgrp reference (Nozawa-Inoue 2003, TMJ synovial membrane) are judged
`supports` for "two distinct cell types … fibroblast-like stromal cells and
lining cells" and `partly supports` for a sentence that adds neutrophils and
mast cells, with the note "Source covers macrophage-like cells only". That is
the distinction an author would want drawn.

The notes are also where the lane is most useful on its own: `spec-citations`
turned two of these into findings — "Ref 13 is a florfenicol study cited as
oxytetracycline pharmacokinetics" and, on photocatalytic, "Ref 40: DOI resolves
to an unrelated graphene/Fe3O4 paper".

## Finding volume and duplication

| paper | findings | major | distinct issues (hand) | clusters (mechanical) | cross-lane clusters | findings in them |
| --- | --- | --- | --- | --- | --- | --- |
| oxytetracycline | 62 | 30 | 9 | 49 | 5 | 16 |
| photocatalytic | 69 | 23 | 8 | 55 | 7 | 19 |
| dark-kinome | 49 | 13 | 7 | 34 | 5 | 20 |
| cgrp | 55 | 21 | 8 | 45 | 6 | 14 |
| beamforming | 39 | 10 | 5 | 31 | 2 | 8 |
| control-education | 65 | 13 | 6 | 51 | 6 | 18 |
| control-microbiology | 39 | 8 | 4 | 30 | 3 | 12 |
| **all seven** | **378** | **118** | **47** | **295** | **34** | **107** |

Two counts, because they measure different things. `clusters` is mechanical —
findings grouped by title and summary word overlap — and it only catches
duplicates that are worded alike: 295 clusters for 378 findings, with 34
clusters spanning more than one lane and 107 findings inside them. The hand
count is what an author would see: **47 distinct issues behind 378 findings, so
about eight findings per issue the author would thank us for.** That ratio is
unchanged from the pilot (47 behind 368).

The duplication is not noise, it is lane overlap on the paper's worst problems.
The largest cluster on oxytetracycline is six findings from four lanes (core,
field, figures-tables, numbers) on one table: "Method validation table reports
impossible values (negative LOD/LOQ, R2 of 6.96)". On control-microbiology,
eight findings from three lanes say Table 1 is cited for gene counts it does not
contain. On dark-kinome, eight from three lanes say the AATK construct lies
outside the kinase domain. A reviewer's report should say each of those once.

## What broke

1. **The launcher stops the review 10 minutes after the session's last turn.**
   The first attempt on oxytetracycline came back `blocked` at 590,239 tokens
   and 10.3 minutes with `resolve references` in state `error`, no citation
   workers and no editor. The cause is in the log's first line: *"Background
   tasks still running 10m after the last turn … stopping them. Set
   `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` to wait indefinitely."* The review
   **is** that background task. The run folder is kept, unscored, at
   `runs-0.9.0/ceiling-10m/`, and its tokens count toward this run's budget.
   The retry, with the ceiling raised to 40 minutes, completed in 22.8 minutes
   with all 17 agents done — so the ceiling cost nothing but time.

   Read this next to the pilot's wall clock: **every pilot run finished between
   6.9 and 10.3 minutes**, and the one that did not finish was the one at 10.3.
   Four of this run's seven papers took longer than 10.3 minutes once the
   ceiling was raised (16.8, 17.2, 22.7, 22.8). The pilot's "driver race" and
   this ceiling are plausibly the same wall; the pilot's numbers were measured
   against it without knowing it was there.

2. **A stage whose findings were delivered and accepted is still reported as
   never having reported.** dark-kinome ended `blocked` with
   `"Stages that never reported: core-1"` — while `findings.jsonl` holds five
   core-1 findings, including the one that names the annotated error,
   `findings_delivered` is 49, `editor_accepted` is `true`, the citation lane is
   `complete`, and `core-1` spent 72,673 tokens over 8 tool calls. The review is
   there and it is good; the stage ledger lost core-1's report. The scorer then
   reads the findings from the ledger instead of the editor package and marks
   the run not publishable, which is the right call on the evidence it has.

3. **`exists: no` from a truncated DOI** — seven rows on cgrp, detailed above.

4. **Reference titles still lost to a common list style** — 55 rows, 29 of them
   one paper's whole list, detailed above.

Nothing else: no quarantined findings, guard clean on all seven, no engine
exception, no run over the 1.2M per-run token limit, and no retry other than
the one above.

## The three most useful fixes this run suggests

None of these is started.

1. **The launcher sets its own background wait.** The engine knows its own
   timeout (`WALL_TIMEOUT`, 5400s) and should set
   `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` to match it before starting the
   session, instead of inheriting a 10-minute default that kills a healthy run
   mid-stage. Everything else in this document was measured against a wall that
   a single environment variable moves.
2. **Make a passage, not a lookup, the goal of the resolve stage.** A graded
   verdict happened exactly 165 times out of 434, and exactly when a passage
   existed; 0 of 325 references returned full text. Two papers got no passage at
   all and so returned 81 `cannot tell` verdicts for 161,820 citation-worker
   tokens. Fetching an abstract from somewhere other than the registry record
   (Europe PMC full text, arXiv, the publisher's landing page) is what turns the
   lane's cost into verdicts.
3. **Do not conclude a reference does not exist from text the reader mangled,
   and parse the `Authors: Title. Venue year` style.** These are the two ends of
   the same failure: 8 references called non-existent on a truncated DOI and 55
   never looked up for want of a title. A DOI that does not match the shape of a
   DOI should fall back to a title search, and a `could not check` row should say
   no lookup was made rather than that the lookups did not answer.

## How to reproduce

```sh
# a fresh venv with the released engine, kept apart from the pilot's
uv venv ~/pra-spot/engine-venv-0.9.0 --python 3.12
~/pra-spot/engine-venv-0.9.0/bin/python -m pip install 'peerreviewagents==0.9.0'

export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=2400000   # see "What broke", 1
peerreview review ~/pra-spot/papers/<paper>.pdf ~/pra-spot/runs-0.9.0/<slug> \
    --arm full-two-track --reviewers 3 --per-run-tokens 1200000

python scripts/spot/score.py ~/pra-spot/runs-0.9.0/<slug> --slug <paper>
```

The pilot's run folders are untouched under `~/pra-spot/runs/`; the before
column of every table here was recomputed from those ledgers, not retyped from
[`SPOT_PILOT.md`](SPOT_PILOT.md).

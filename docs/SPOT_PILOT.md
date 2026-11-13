# SPOT pilot results

**The engine found the author-acknowledged error in three of the five papers,
partly found a fourth, and missed the fifth.** The three it found it found
hard: the exact protein fragments by residue number, the exact negative
constants, and the beamforming gain with an arithmetic receipt showing
10log10(256/16) = 12.04 dB. The one it missed is the one no reader of the text
could catch — a micrograph reused between two figures. And the finding that
matters most is not on the scorecard: **one of the two "clean" controls is not
clean.** The engine accused it of a contradiction its own Table 2 confirms, and
the accusation is correct.

This is the harder sibling of [`stage-a-results.md`](stage-a-results.md).
Stage A planted the errors it looked for. Here nothing is planted: every error
is one the authors themselves acknowledged, in a retraction notice or in a
PubPeer thread they answered, recorded by the
[SPOT benchmark](https://arxiv.org/abs/2505.11855) (`amphora/SPOT`, CC BY 4.0).
SPOT publishes annotations, not PDFs, so each paper was fetched from its own
open-access source: the engine was given the file a reader is actually served.

Everything below comes from the run folders under `~/pra-spot/runs/`, scored by
`scripts/spot/score.py --all`, which writes one row per run to
`~/pra-spot/runs/results.jsonl`. Papers, runs and logs are outside this
repository; each run folder is the evidence, and `review/protocol.json` records
what that run was allowed to do. The cost numbers are the engine's own, from
`review/runtime.json`; none was typed by hand.

## The limits, before any of the results

1. **n is five.** Five papers, one run each, one arm (`--arm full-two-track
   --reviewers 3`). A one-paper difference moves the catch rate by 20 points,
   and no paper was reviewed twice, so nothing here measures how much a second
   run of the same paper would differ.
2. **An image-only error is out of this arm's reach.** This arm reads a text
   layer. Two of the five annotated errors live inside a figure bitmap — a
   reused micrograph and a misspelled axis label — and no reasoning over text
   reaches them. The one miss and the one partial below are both that limit.
3. **One of the two controls contains a real error.** It was chosen as a paper
   with nothing wrong in it and it is not one: the engine's allegation against
   it is confirmed by its own Table 2 (see
   [the controls](#the-controls-and-why-one-of-them-is-not-a-control)). So this
   pilot bounds no false-allegation rate. The other control holds up.
4. **One engine ran all seven runs**, the non-editable install of `main`
   `6a7ed12`. `starter.py` was deliberately **not** fixed mid-pilot even after
   run 2 hit a driver race, so that the count of hits is a number about one
   build. That race has since been fixed, in PR #40, and no run here was redone
   with the fix.
5. **The verdict is written by hand, the evidence is not.** Two of the five
   errors name an exact value, and for those the scorer applies Stage A's own
   value rule. The other three — a duplicated micrograph, a misspelled axis
   label inside a figure bitmap, a recombinant fragment without a kinase
   domain — no regular expression settles. For those the scorer reports
   candidate findings (at the annotated location, or carrying the key's words)
   and the yes/partly/no below is a reading, with the finding quoted so it can
   be disagreed with.
6. **"Other findings" are counted, not judged.** The engine wrote 34–64
   findings per paper. The last column of the main table is my own blunt count
   of how many *distinct* issues in each review an author would actually thank
   us for, after collapsing the three core reviewers and the six specialists
   reporting the same issue into one. It is a judgement, and it is much smaller
   than the major count.

## The seven runs

| # | paper | SPOT row / error type | annotated error found? | findings (major) | distinct issues worth thanks | tokens | min | run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `medicine-oxytetracycline` | 66 / statistical reporting | **yes** | 64 (31) | 8 | 878,203 | 9.9 | complete, guard clean |
| 2 | `chemistry-photocatalytic` | 8 / data inconsistency | **partly** | 63 (20) | 8 | 758,541 | 10.3 | **blocked** (driver race), 1 quarantined |
| 3 | `biology-dark-kinome` | 6 / reagent identity | **yes** | 55 (17) | 8 | 833,495 | 10.1 | complete, guard clean |
| 4 | `biology-cgrp` | 53 / figure duplication | **no** | 55 (20) | 8 | 720,317 | 8.5 | complete, guard clean |
| 5 | `network-beamforming` | 1 / equation & proof | **yes** | 34 (10) | 5 | 690,345 | 7.2 | complete, guard clean |
| 6 | `control-education-pmc7183109` | control, nothing planted | n/a | 55 (12) | 6 | 697,001 | 6.9 | complete, 1 quarantined |
| 7 | `control-microbiology-pmc8208984` | control, nothing planted | n/a | 42 (9) | 4 | 632,835 | 8.3 | complete, guard clean |

Seven runs, **5,210,737 tokens** and 61.2 minutes of wall clock in all, 368
findings of which 119 are major. One run of seven was blocked by a race in the
launcher rather than by anything in the paper, since fixed in PR #40; six of
seven finished clean. Catch rate on the five annotated papers: **3 found,
1 partly, 1 missed** — with the partial and the miss both on errors that live
inside a figure bitmap, where this arm cannot look.

## What each verdict rests on

**1. Oxytetracycline in fish — found.** SPOT annotates a pharmacokinetic table
reporting negative AUC(0–inf) and a negative LOQ. Seven findings land on it and
six name an exact value from the key:

> Negative AUC(0-inf) values come from negative elimination constants and are
> misread as incomplete absorption

The review goes further than the annotation: it also reports that the muscle
LOQ is about 34 times the MRL being tested, which makes every sub-LOQ residue
value in the withdrawal analysis unusable, and that the residues exceed the MRL
in most dose groups — contradicting the paper's own biosafety conclusion.

**2. Photocatalytic AgNPs — partly.** The annotation is about Fig. 4B: a
~260 nm absorbance spike that should not be there, on a panel whose axis is
labelled `Wavelenght (nm)` where Fig. 4A beside it reads `Wavelength`. The
engine lands on the right kind of problem in the right region —

> Figs 11 and 12 show the sunlight sample at or above the room-light sample at
> the dye maximum, contradicting the reported values

— and reports that the UV-vis spectra do not show what the text claims. It
names neither the 260 nm spike nor the misspelling. Both live inside the figure
bitmap, not in the text layer, and this run had no vision lane. Scored
**partly**: right instrument, right panel family, wrong reason.

**3. Dark kinome — found, exactly.** The annotation says three of five
recombinant "kinases" were fragments without a catalytic kinase domain. Five
findings say so, with the construct boundaries:

> Three of five recombinant "kinases" (LTK, INSRR, AATK) lack the catalytic
> kinase domain, so their array profiles cannot report that kinase's activity

It then found something the annotation does not list: the reported EPHA6
autophosphorylation sites pY259 and pY484 lie outside the purchased fragment
and in the extracellular region. That is the same class of error, in the same
paper, un-annotated.

**4. CGRP and macrophage polarization — missed.** The annotation is a
duplicated micrograph: column C of Fig. 5 and column C of Fig. 6 are crops of
one field. The review contains nine findings about those figures, including
that the panels do not show what the text says and that the star marks disagree
with the p-values — but **not one** says an image is reused. It cannot: this
arm reads a text layer. A text-only reviewer cannot see image reuse, and no
amount of statistical suspicion substitutes.

**5. MA-TISK beamforming — found, with a receipt.** The annotation: the
beamforming gain for N antennas is computed as N when coherent beamforming
makes received power rise with N², so 16 antennas are stated as 12.0 dB instead
of 24 dB. The core-2 reviewer, `basis: computed`, receipt
`10*log((16*16)/16)/log(10)` = 12.041:

> The 12.0 dB that Section V attributes to beamforming is that directivity gain
> alone. It leaves out the additional factor N from power aggregation, which
> MA-TISK's "temporal combining gain" is in fact measuring. … That is
> 10log10(256/16) = 12.04 dB for N = 16.

## The controls, and why one of them is not a control

The two controls are the Stage A papers that were reviewed with nothing planted
in them, copied byte for byte so that the control is the same file Stage A
reviewed. They are the pilot's only measurement of what the engine alleges
about a paper with no known error, and the question for them is not "did it
catch anything" but **"did it accuse the paper of something untrue?"**

The answer, on both, is **no — not one fabricated allegation.** Every severe
finding on both controls is checkable against the paper's own printed numbers,
and I checked the strongest one by hand.

**Control 1, a meta-analysis of VR in anatomy education.** 55 findings, 12
major, 5 of them severe enough to count as allegations under the Stage A rule —
and all 5 are the same issue, found independently by `core-1`, `core-2`,
`core-3` and `spec-stats`:

> Subgroup claim of "no significant difference" contradicted by Table 2; no
> between-group test reported

The paper's sentence is *"we found that did not make a significant difference
in the differences in the subgroups for all Q statistics are non-significant
(I2 > 75%)"*. Its own Table 2, which I read off the PDF:

| intervention subgroup | n | SMD | 95% CI | p |
| --- | --- | --- | --- | --- |
| 3D interactive models | 8 | 0.64 | 0.47, 0.81 | 0.00 |
| **VR** | 7 | **−0.09** | **−0.37, 0.18** | **0.50** |

A paper whose title and conclusion are that VR improves examination scores
reports, in its own moderator table, that the VR subgroup has no effect and
that the pooled effect comes from desktop 3D models — with intervals that do
not overlap. The engine is right, and the control is therefore **not a clean
paper**. Six distinct issues here an author would thank us for, and the
"false-positive rate" this control was built to measure cannot be read off it.

**Control 2, a bacterial genotyping tool paper.** 42 findings, 9 major, guard
clean, 2 allegations — both about the validation, not about the authors:

> Reference standard is prior genotype calls, and discrepancies are resolved in
> the tool's favour without phenotype data shown

The nine majors collapse to four distinct issues: the validation is 19 isolates
and two *Salmonella* sets; the newly added virulence and stress-response
functions are claimed validated but never measured; "high concordance" is
asserted with no concordance statistic, denominator or interval; and the
validation datasets carry no accessions or run options, so nothing can be
re-run. All four are ordinary software-paper review, all four are true of the
paper as printed, and none is an accusation of misconduct.

**What this means for the Stage A control arm.** A control that contains a real
error cannot bound a false-allegation rate, and we had no way to know it did:
nothing in Stage A's design reads a control paper's own tables against its own
conclusions. Both of these controls need re-reading by hand before any
false-positive number is quoted from them.

## The citation lane

| paper | refs | resolved | mismatched | not located | could not check | in-text uses | uses matched |
| --- | --- | --- | --- | --- | --- | --- | --- |
| oxytetracycline | 86 | 26 | 4 | 27 | 33 | 0 | 0 |
| photocatalytic | 59 | 43 | 9 | 8 | 8 | 0 | 0 |
| dark-kinome | 0 | — | — | — | — | 63 | 0 |
| cgrp | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| beamforming | 25 | 10 | 10 | 14 | 1 | 33 | 33 |
| control-education | 5 | 0 | 0 | 1 | 4 | 47 | 5 |
| control-microbiology | 33 | 17 | 1 | 8 | 8 | 0 | 0 |

### Why zero in-text citation markers were found on three papers

On oxytetracycline, photocatalytic and control-microbiology the lane found
**0 use rows** against a full reference list. The cause is the marker shape,
not the lane's logic. The engine matches bracketed numerals (`[12]`, `[3,4]`)
and author-year; these three journals set citations as **superscript
numerals**, and `pdftotext` flattens a superscript onto the word before it:

| paper | glued superscripts | bracketed | author-year | uses found |
| --- | --- | --- | --- | --- |
| oxytetracycline | 92 | 0 | 24 | 0 |
| photocatalytic | 72 | 0 | 7 | 0 |
| control-microbiology | 66 | 0 | 3 | 0 |

So `aquaculture12` arrives as one token, and no pattern in
`citation_ledger._uses_in` can see a marker there. This is a **reader gap, not
a marker-pattern gap**: adding a pattern for a numeral glued to a word would
match every gene name, dose and year in the manuscript. The fix belongs in the
reader, which has the font size and baseline offset that say "this numeral is a
superscript" and throws them away. Until then, a superscript-style journal
loses its entire use lane — **three of the seven papers here, 230 markers**.

### Why a reference could not be checked, or was not located

The two states are reported as one number each in the ledger summary and they
are not one cause each. Split over all seven runs:

| state | n | cause |
| --- | --- | --- |
| could not check | 56 | **53 of 56** had no title extracted, so **no lookup was ever made**; the other 3 had a title and a lookup that did not answer |
| not located | 58 | **58 of 58** printed no DOI, a title search ran and came back empty; 9 of the 58 read like books or agency reports, where an empty answer from an article index is the right answer |

Almost none of this is a refused or rate-limited lookup, and none of it is a
judgement about the reference. The dominant cause is the reader: a reference
list whose titles the parser swallowed into the author field is a reference list
that is never searched. The remaining 49 not-located references are ordinary
journal articles whose title search missed — a resolver question, and the
smaller of the two.

## What broke

1. **The driver race, 1 hit in 7 runs** (run 2). The review is a background
   workflow running inside the headless session, and `starter.py` had no
   completion signal of its own: it ended the run when the *session* stopped
   talking. On run 2 the session had already sent its turn-ending `result`, and
   the read loop ended when the session **closed its event stream** at ~617s
   with the process still alive; the `finally` block then terminated it and the
   workflow died with it (exit 143, 758k of 1.2M tokens, 10.3 min of 90, nine
   of ten stages reported, last folder write at 601s). The editor agent had
   already been reported in state `error` at 136,755 tokens before the stream
   closed, so **whether this review would have completed had the driver waited
   is unknown** — the run may have been failing on its own. Paper-independent,
   and unrelated to the ceilings. Findings were recovered from the ledger, so
   run 2 is scored, not lost. **Fixed in PR #40:** the driver now waits on the
   run folder the server writes, and a session that ends its turn with the
   workflow alive no longer takes the workflow with it.
2. **A reference list the reader cannot see at all.** On dark-kinome the list
   arrived as one run-on block: 0 references extracted while 63 in-text uses
   were found, so every use was unmatched and no lookup ran. The lane reported
   itself honestly — "the paper has 63 in-text citation marker(s) and no
   reference list this version could read".
3. **Superscript markers flattened onto the preceding word**: 0 use rows on
   3 of 7 papers, 230 markers lost (table above).
4. **Ligature and letter-spacing loss in the text layer.** The control paper's
   text arrives as `non-signicant`, `ndings`, `benecial`; the photocatalytic
   DOIs arrive as `h t t p s : / / do i . o r g / 1 0 .1 0 0 7`. This cost one
   finding: the single quarantine on the control is "quote is absent or
   ambiguous in the selected source block" — the guard working exactly as
   designed, on a quote the reviewer could not have copied correctly because
   the block itself is corrupt.
5. **The resolve stage cannot finish inside a short run.** On the control,
   `citations.stages.resolve` ended `done: false` with `retry_after: 298` after
   2 lookups: the run finished in 6.9 minutes and the index had asked for a
   300-second wait. The lane said only `complete: false`, which is correct and
   useless — a 5-minute backoff inside a 7-minute run means the citation lane
   is structurally unfinishable at this run length, and the four affected rows
   were reported as "could not be checked", which reads as the indexes having
   been asked and failed. Fixed since: the driver hands the session its own
   deadline, and the resolve stage waits a retry gap out when the gap ends
   inside the run and inside the 15 minutes one tool call may spend waiting —
   in a worker thread, so the rest of the session keeps answering and the run's
   progress file keeps moving — and otherwise comes straight back and records
   which rows were left and why. A 90-minute run now finishes the lookups this
   run could not, and a 7-minute run says that is what happened instead of
   implying the indexes answered.

## A finding about the benchmark, not the engine

**The open-access PDF is frequently the corrected article.** Three of the first
eight SPOT candidates fetched had the annotated error already fixed in the file
served today — a replaced equation, a replaced panel, a text now agreeing with
its figure. Reviewing those and scoring the engine as having missed something
would have measured the fetch.

Searching for a correction notice does not find them: **none** of the thirteen
PDFs fetched here contains the words of an erratum, including the three that are
demonstrably corrected. The publisher replaced the content and left the article
looking untouched. The only thing that works is reading the annotated location,
which is what `scripts/spot/check.py` records, candidate by candidate, with the
anchor text that decided it. arXiv has the opposite shape: a withdrawn version
serves no PDF at all, so the one arXiv paper here is pinned to `v1`, where the
error is.

Anyone reusing SPOT at scale should assume a double-digit fraction of the
share-permissive subset is unusable as served, and that no automated check will
tell them which.

## How to reproduce

```sh
python scripts/spot/fetch.py --root ~/pra-spot            # fetch and verify
python scripts/spot/check.py --root ~/pra-spot --verify   # re-check the anchors
peerreview review ~/pra-spot/papers/<slug>.pdf ~/pra-spot/runs/<slug> \
    --arm full-two-track --reviewers 3 --per-run-tokens 1200000
python scripts/spot/score.py --root ~/pra-spot --all      # score every run
```

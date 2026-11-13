# The SPOT pilot

Stage A asked whether the engine finds an error somebody planted. This asks a
harder question: does it find errors the authors themselves later admitted to,
in the PDFs their publishers actually serve?

The errors come from [SPOT](https://arxiv.org/abs/2505.11855) — 83 papers and
91 author-acknowledged errors, collected from retraction notices and from
PubPeer threads an author answered, published as `amphora/SPOT` under CC BY
4.0. SPOT publishes the annotations, not the papers, so each paper is fetched
from its open-access source and the pilot is a test on real PDFs rather than on
somebody's parse of one.

Nothing here makes a model call or writes inside the repo. Papers, runs, logs
and scores live under `~/pra-spot/`.

## The three scripts

| | |
|---|---|
| `fetch.py` | Fetches each candidate — PMC's Open Access bucket, then the publisher's own path, then OpenAlex — keeping only bodies that start with `%PDF`, and copies the two Stage A controls byte for byte. |
| `check.py` | Records, per candidate, whether the annotated error is still in the PDF that is served today, and what was read to decide that. `--verify` re-checks the anchors that live in the text layer. |
| `score.py` | Reads a finished run with the Stage A scorer and reports the findings that land at the annotated location or carry the key's words, every finding in brief, the citation ledger's counts by state, and the run's own cost. |

## The corrected-copy trap

**The open-access PDF is frequently the corrected article.** Three of the first
eight candidates had the annotated error already fixed in the file served
today — a replaced equation, a replaced panel, a text now agreeing with its
figure. Reviewing those and scoring the engine as having missed something would
have measured the fetch.

Searching for a correction notice does not find them: none of the thirteen PDFs
fetched here contains the words of an erratum, including the three that are
demonstrably corrected. The publisher replaced the content and left the article
looking untouched. The only thing that works is reading the annotated location,
which is why `check.py` exists and why `fetch.py` holds a pool of candidates
rather than a list of picks.

arXiv has the opposite shape: a withdrawn version serves no PDF at all, so the
one arXiv paper here is pinned to `v1`, where the error is.

## What the scorer will and will not decide

Stage A's scorer decides a catch by rule, because Stage A knew the exact value
a finding had to name. Two of the five errors here are like that — a negative
AUC and LOQ, a 12.0 dB gain — and for those `score.py` applies Stage A's own
value rule. The other three are a duplicated micrograph, a misspelled axis
label inside a figure bitmap, and a recombinant fragment without a kinase
domain. No regular expression settles those, so the scorer reports candidates —
findings at the annotated location, findings carrying the key's words — and the
yes/partly/no verdict is written by hand, with the finding quoted, in the
results document. The cost numbers are never typed by hand.

## Running it

```sh
python scripts/spot/fetch.py --root ~/pra-spot            # fetch and verify
python scripts/spot/check.py --root ~/pra-spot --verify   # re-check the anchors
peerreview review ~/pra-spot/papers/<slug>.pdf ~/pra-spot/runs/<slug> \
    --arm full-two-track --reviewers 3 --per-run-tokens 1200000
python scripts/spot/score.py --root ~/pra-spot --all      # score every run
```

The results of the first pilot are in `docs/SPOT_PILOT.md`.

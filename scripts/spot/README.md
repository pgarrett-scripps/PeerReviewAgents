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
| `score.py` | Reads a finished run with the Stage A scorer and reports, for every finding, which piece of the paper it cites, who wrote that piece and — when the reader did — whether it rebuilt the piece or only cut debris out of it; the findings that land at the annotated location, sit in the annotated kind of piece, or carry the key's words; the citation ledger's counts by state; the Read stage's own account of itself; and the run's own cost. |

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
findings at the annotated location, findings in the annotated kind of piece,
findings carrying the key's words — and the yes/partly/no verdict is written by
hand, with the finding quoted, in the results document. The cost numbers are
never typed by hand.

Location is decided in the manuscript, not in the reviewer's prose. A finding
cites a line; the map row over that line says what kind of piece it is, which
page it is on, and whether the converter read it, a model rewrote it from the
page, or a model wrote it from nothing. So a finding that quotes the table and
never writes "Table 1" is a finding in Table 1, and a finding that writes
"Table 1" while quoting a sentence in the discussion is not. The same rows give
the two counts the reader comparison is for: findings that cite a table, an
equation or a figure, and findings that rest on text the reader wrote or
repaired.

That second count is reported split three ways, because `ai-reconstructed` is
two different acts. A rebuilt equation or a figure the reader described is a
piece of the paper the reviewers could not see before; a paragraph the reader
only cut a running header out of is the converter's own sentences with debris
gone. So the row carries `on_reader_rebuilt` for displayed pieces the reader
rebuilt and pieces it added, `on_reader_cut` for prose whose every word is still
the converter's, and `on_reader_rewritten` for prose the reader put a word of
its own into. On the first antenna pair, ten of the eleven findings resting on
reader text were cut paragraphs, which a single number would have reported as
reader-written evidence.

## Running it

```sh
python scripts/spot/fetch.py --root ~/pra-spot            # fetch and verify
python scripts/spot/check.py --root ~/pra-spot --verify   # re-check the anchors
peerreview review ~/pra-spot/papers/<slug>.pdf ~/pra-spot/runs/<slug> \
    --arm full-two-track --reviewers 3 --per-run-tokens 1200000
python scripts/spot/score.py ~/pra-spot/runs/cmp-antenna-on \
    --slug network-beamforming                            # score one run
python scripts/spot/score.py --root ~/pra-spot --all      # score every run
```

A run folder's name is whatever the person who made the run chose — a
comparison makes one folder per arm, `cmp-antenna-on` and `cmp-antenna-off` —
so `--slug` is how a run says which paper it is about. Without it the folder
name is matched against the keys and a short list of aliases, and a name that
matches nothing is scored as a run with no key: its findings and its cost, no
candidates.

The scorer reads the manuscript the review was made against, so it wants a run
folder written by today's engine. A folder from before `paper-base-v2` is
reported as not scored, with the refusal beside its findings and its cost,
rather than scored as a paper in which every finding is nowhere.

The results of the first pilot are in `docs/SPOT_PILOT.md`.

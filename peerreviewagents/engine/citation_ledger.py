"""The citation ledger: one row per reference, one row per in-text use.

Stage A ran one citations specialist with a ration of lookups, told to sample
"the claims the paper leans on hardest". On the planted-error papers that arm
caught one of two fabricated DOIs — not because the model was weak, but because
sampling cannot answer a question about *every* reference, and a tool that
returns bibliographic metadata cannot answer whether a source says what the
sentence citing it claims it says.

So the work is a table, not a reading, and it is filled in four stages:

1. **Fill** (:func:`build`, no model). Every reference in the manuscript gets a
   row, with or without a DOI, and every in-text use gets a row: the citing
   sentence, the block it sits in and the reference it points at.
2. **Resolve** (:func:`resolve`, no model). Every reference row — not a ration —
   is looked up. A DOI goes to Crossref; a reference without one goes to a
   bibliographic title search, Crossref first and then Europe PMC. The row
   records whether the work exists, whether the title, first author, year and
   venue match what the paper printed, any retraction notice, and the abstract
   and open-access full text with the URL they came from.
3. **Judge** (:func:`batch` and :func:`verdict`, Haiku workers). A worker claims
   a batch of use rows and answers one question per row — does the source
   support this sentence — quoting the passage it relied on. A row with no
   source text has one allowed answer: ``cannot tell``.
4. **Confirm** (the citations specialist, in the plugin). It reads the rows that
   say something is wrong, re-checks each one, and only what it confirms becomes
   a finding through the ordinary guard. A worker verdict is never a finding.

Three rules hold throughout.

**A failed lookup is "could not check", never "does not exist".** A rate limit, a
timeout and an unreachable host say nothing about whether a paper is real, and a
review that called them fabrications would be worse than one that said nothing.

**The table is the record.** It is a JSONL file in the run folder, written only
by the server, append-only, last row per id wins. The ledger is resumable
because replaying it rebuilds the state: a run that died mid-resolve re-runs the
unresolved rows and nothing else. JSONL rather than SQLite because every other
run file is JSON or JSONL, :func:`peerreviewagents.read_run` already degrades
gracefully over a half-written one, and a consumer reads the folder without a
database driver or a lock discipline — and the single-writer rule is what makes
that safe.

**Nothing here identifies the user.** Lookups go out under a generic client
name, with no email address and nothing about whose run this is.
"""

from __future__ import annotations

import datetime
import difflib
import json
import re
from pathlib import Path
from urllib.parse import urlencode

from .external_evidence import retrieve
from .file_lock import exclusive_lock
from .storage import digest, read, write
from .targeted_sources import literature_request, parse_literature

#: Where the ledger lives inside the run directory.
LEDGER_DIRECTORY = 'citations'
LEDGER_FILE = 'ledger.jsonl'
STATE_FILE = 'stages.json'
CACHE_DIRECTORY = 'lookups'
#: The relative path a consumer is given, so it never has to join these itself.
LEDGER_PATH = LEDGER_DIRECTORY + '/' + LEDGER_FILE

#: What a worker may answer about one use of one reference.
VERDICTS = ('supports', 'partly supports', 'does not support', 'cannot tell')
#: The one answer allowed when the ledger holds no source text for a row.
NO_SOURCE_VERDICT = 'cannot tell'
#: Whether the cited work could be found at all.
EXISTENCE = ('yes', 'no', 'could not check')
#: The printed details that are compared against the resolved record.
COMPARED = ('title', 'first_author', 'year', 'venue')

#: How much source text one use row shows a worker. Haiku reads the abstract and
#: the opening of the full text; the whole full text would multiply the token
#: cost of a 60-reference paper by an order of magnitude for passages that are
#: almost never where the supporting sentence is.
SOURCE_TEXT_LIMIT = 6_000
#: How much full text the ledger stores for a reference.
FULL_TEXT_LIMIT = 40_000
#: How much of a reference's raw string is kept in its row.
RAW_LIMIT = 1_200
#: Similarity above which two titles are the same work, and two venues the same
#: venue. Titles are compared strictly: a search index returns near neighbours,
#: and treating one as a match is how a fabricated reference gets confirmed.
TITLE_MATCH = 0.90
SEARCH_MATCH = 0.82
VENUE_MATCH = 0.80

HEADERS = {'Accept': 'application/json'}
CROSSREF_WORK = 'https://api.crossref.org/works/'
CROSSREF_SEARCH = 'https://api.crossref.org/works?'
EUROPEPMC_SEARCH = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?'
EUROPEPMC_FULL_TEXT = 'https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML'

_DOI = re.compile(r'\b(10\.\d{4,9}/[^\s"<>,;)\]]+)', re.IGNORECASE)
_LABEL = re.compile(r'^\s*[\[(]?\s*(\d{1,3})\s*[\]).]\s*')
_NUMERIC_MARKER = re.compile(
    r'\[((?:\d{1,3}(?:\s*[‐-―-]\s*\d{1,3})?)'
    r'(?:\s*[,;]\s*\d{1,3}(?:\s*[‐-―-]\s*\d{1,3})?)*)\]')
_AUTHOR_YEAR = re.compile(
    r'([A-Z][A-Za-zÀ-ɏ’\'-]{1,30})'
    r'(?:\s+(?:et\s+al\.?|and\s+[A-Z][A-Za-z’\'-]+|&\s+[A-Z][A-Za-z’\'-]+))?'
    r'[,\s]*\(?((?:1[89]|20)\d{2})[a-z]?\)?')
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?”"])\s+(?=[(\[“"A-Z0-9])')
_ABBREVIATIONS = ('et al.', 'e.g.', 'i.e.', 'cf.', 'vs.', 'viz.', 'approx.', 'ca.', 'ref.',
                  'refs.', 'fig.', 'figs.', 'eq.', 'eqs.', 'tab.', 'no.', 'vol.', 'pp.',
                  'dr.', 'prof.', 'st.', 'al.')
_TAGS = re.compile(r'<[^>]+>')
_SPACE = re.compile(r'\s+')
#: Block kinds whose text is prose a citation can sit in.
PROSE_KINDS = ('paragraph', 'heading', 'caption', 'abstract', 'list', 'quote', 'text')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def squeeze(text, limit=None):
    """One string with its whitespace normalized, optionally truncated."""
    out = _SPACE.sub(' ', str(text or '')).strip()
    return out[:limit] if limit else out


def strip_markup(text, limit=None):
    """JATS or XML as plain text: the tags removed, the words kept."""
    return squeeze(_TAGS.sub(' ', str(text or '')), limit)


def normal(text):
    """One title or venue as it is compared: letters, digits and single spaces."""
    return _SPACE.sub(' ', re.sub(r'[^a-z0-9 ]+', ' ', str(text or '').casefold())).strip()


def similar(one, other):
    """How alike two titles are, 0.0 to 1.0, on their compared forms."""
    left, right = normal(one), normal(other)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def doi_in(text):
    """The first DOI in a string, lowercased and with trailing punctuation dropped."""
    match = _DOI.search(str(text or ''))
    if not match:
        return None
    return match.group(1).rstrip('.,;)]').casefold()


def surname(name):
    """The family name in one printed author name, as a comparison key.

    ``Example, A.``, ``A. Example`` and ``Ada Example`` all answer ``example``.
    A name nobody printed answers ``None`` rather than the empty string, because
    "no author to compare" and "the authors disagree" are different answers.
    """
    text = squeeze(name)
    if not text:
        return None
    if ',' in text:
        text = text.split(',')[0]
    words = [word for word in re.split(r'[\s.]+', text) if len(word) > 1]
    if not words:
        return None
    return normal(words[-1]) or None


def _printed_from_raw(raw):
    """First author and venue read off a reference's raw string, or ``None``.

    A reference list is prose and this is a heuristic over it, so a part it
    cannot find stays ``None`` and the comparison for that part answers "could
    not compare" rather than "does not match". Guessing here would turn a
    reference-list style this code does not know into a finding about the paper.
    """
    text = _LABEL.sub('', squeeze(raw))
    if not text:
        return None, None
    parts = [part.strip() for part in text.split('.') if part.strip()]
    author = surname(parts[0]) if parts else None
    venue = None
    for part in parts[1:]:
        if re.search(r'(1[89]|20)\d{2}', part):
            venue = squeeze(re.split(r',\s*(?:1[89]|20)\d{2}', part)[0], 200) or None
            break
    return author, venue


# -- stage 1: fill ----------------------------------------------------------


def sentences(text):
    """One block's text as sentences, keeping abbreviations whole."""
    body = squeeze(text)
    if not body:
        return []
    guarded = body
    for abbreviation in _ABBREVIATIONS:
        for form in (abbreviation, abbreviation.capitalize(), abbreviation.upper()):
            guarded = guarded.replace(form, form.replace('.', '\x00'))
    return [part.replace('\x00', '.').strip()
            for part in _SENTENCE_SPLIT.split(guarded) if part.strip()]


def _numbers(group):
    """``1,3-5`` as ``['1', '3', '4', '5']``, ranges expanded, order kept."""
    out = []
    for piece in re.split(r'[,;]', group):
        piece = piece.strip()
        if not piece:
            continue
        span = re.split(r'\s*[‐-―-]\s*', piece)
        if len(span) == 2 and span[0].isdigit() and span[1].isdigit():
            first, last = int(span[0]), int(span[1])
            if 0 < last - first <= 60:
                out.extend(str(number) for number in range(first, last + 1))
                continue
        if piece.isdigit():
            out.append(piece)
    return out


def _by_label(references):
    """``{printed label: reference id}``, with the row's own number as a fallback."""
    index = {}
    for position, reference in enumerate(references, start=1):
        label = squeeze(reference.get('label'))
        if label:
            index.setdefault(label, reference['id'])
        index.setdefault(str(position), reference['id'])
    return index


def _by_author_year(references):
    """``{(surname, year): reference id}`` for author–year citation styles."""
    index = {}
    for reference in references:
        author = surname(reference.get('first_author')) or _printed_from_raw(
            reference.get('raw'))[0]
        year = squeeze(reference.get('year'))
        if author and year:
            index.setdefault((author, year[:4]), reference['id'])
    return index


def reference_rows(paper):
    """One row per reference the manuscript lists, DOI or no DOI."""
    rows = []
    for reference in paper.get('references') or []:
        identifier = reference.get('id')
        if not identifier:
            continue
        author, venue = _printed_from_raw(reference.get('raw'))
        year = squeeze(reference.get('year'))
        rows.append({
            'kind': 'reference',
            'row_id': 'ref:' + str(identifier),
            'reference_id': str(identifier),
            'label': squeeze(reference.get('label')) or None,
            'raw': squeeze(reference.get('raw'), RAW_LIMIT) or None,
            'printed': {
                'title': squeeze(reference.get('title')) or None,
                'year': year[:4] or None,
                'first_author': surname(reference.get('first_author')) or author,
                'venue': venue,
                'doi': (reference.get('doi') or '').strip().casefold() or doi_in(
                    reference.get('raw')),
            },
            'stage': 'fill',
            'filled_at': now(),
        })
    return rows


def use_rows(paper):
    """One row per in-text use: the citing sentence, its block, the reference.

    Where the citations come from is recorded per row, because the two ingest
    paths differ. A manuscript imported from JATS carries the reference ids per
    block, so a sentence's own marker refines a list that is already correct. A
    PDF carries nothing, so the markers in the prose are all there is, and a
    bracketed number that matches no reference is kept as an unmatched use —
    ``[42]`` in a paper with thirty references is the citation problem, not a
    parsing failure to be hidden. An author–year candidate that matches no
    reference is dropped instead: ``(Fig. 2)`` and ``(2019)`` are not citations,
    and a row per bracketed year would bury the real ones.
    """
    references = list(paper.get('references') or [])
    by_label = _by_label(references)
    by_author = _by_author_year(references)
    known = {str(reference.get('id')) for reference in references}
    rows = []
    for block in paper.get('blocks') or []:
        kind = str(block.get('kind') or '')
        text = block.get('text') or ''
        if not text or not (kind.startswith(PROSE_KINDS) or kind in PROSE_KINDS):
            continue
        listed = [str(one) for one in (block.get('citations') or []) if str(one) in known]
        seen_in_block = set()
        for position, sentence in enumerate(sentences(text), start=1):
            for reference_id, derivation, marker in _uses_in(sentence, by_label, by_author,
                                                             listed):
                row_id = 'use:' + str(block.get('id')) + ':' + str(position) + ':' + str(
                    reference_id or 'unmatched-' + marker)
                if row_id in seen_in_block:
                    continue
                seen_in_block.add(row_id)
                rows.append({
                    'kind': 'use',
                    'row_id': row_id,
                    'reference_id': reference_id,
                    'block_id': block.get('id'),
                    'section': block.get('section'),
                    'sentence_index': position,
                    'sentence': squeeze(sentence, 1_200),
                    'marker': marker,
                    'derivation': derivation,
                    'stage': 'fill',
                    'filled_at': now(),
                })
            seen_in_block |= {row['row_id'] for row in rows[-4:]}
        if listed and not any(row.get('block_id') == block.get('id') for row in rows):
            # The import said this block cites these references and no marker
            # survived extraction. The block is the citing passage then, which
            # is a worse anchor than a sentence and is recorded as being one.
            for reference_id in listed:
                rows.append({
                    'kind': 'use', 'row_id': 'use:' + str(block.get('id')) + ':0:' + reference_id,
                    'reference_id': reference_id, 'block_id': block.get('id'),
                    'section': block.get('section'), 'sentence_index': 0,
                    'sentence': squeeze(text, 1_200), 'marker': None,
                    'derivation': 'block-citations', 'stage': 'fill', 'filled_at': now(),
                })
    return rows


def _uses_in(sentence, by_label, by_author, listed):
    """Every (reference id, derivation, marker) one sentence cites."""
    out = []
    for match in _NUMERIC_MARKER.finditer(sentence):
        for number in _numbers(match.group(1)):
            reference_id = by_label.get(number)
            if reference_id and (not listed or reference_id in listed):
                out.append((reference_id, 'marker-numeric', '[' + number + ']'))
            elif reference_id:
                out.append((reference_id, 'marker-numeric-outside-block', '[' + number + ']'))
            else:
                out.append((None, 'marker-unmatched', '[' + number + ']'))
    for match in _AUTHOR_YEAR.finditer(sentence):
        key = (normal(match.group(1)), match.group(2))
        reference_id = by_author.get(key)
        if reference_id:
            out.append((reference_id, 'marker-author-year', squeeze(match.group(0), 80)))
    if not out and listed:
        return []
    return out


# -- the table --------------------------------------------------------------


class Ledger:
    """The rows on disk. Append-only; the last row for an id is the row.

    Every write goes through one process — the server — and under a lock, so a
    worker's verdict and another worker's claim cannot interleave into a
    half-written line. A line that is not readable JSON is skipped and counted,
    the same way the run readers degrade: one corrupt line must not hide the
    other fifty.
    """

    def __init__(self, review):
        self.directory = Path(review) / LEDGER_DIRECTORY
        self.path = self.directory / LEDGER_FILE
        self.cache = self.directory / CACHE_DIRECTORY
        self.state_path = self.directory / STATE_FILE

    def exists(self):
        return self.path.is_file()

    def append(self, row):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as stream:
            exclusive_lock(stream)
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        return row

    def load(self):
        """``{row_id: row}``, replayed in order, plus the unreadable line count."""
        rows, broken = {}, 0
        if not self.path.is_file():
            return rows, broken
        with self.path.open('r', encoding='utf-8') as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    broken += 1
                    continue
                if isinstance(row, dict) and row.get('row_id'):
                    rows[row['row_id']] = row
                else:
                    broken += 1
        return rows, broken

    def rows(self, kind=None):
        rows, _ = self.load()
        return [row for row in rows.values() if kind is None or row.get('kind') == kind]

    def state(self):
        if not self.state_path.is_file():
            return {}
        try:
            stored = read(self.state_path)
        except (ValueError, OSError):
            return {}
        return stored if isinstance(stored, dict) else {}

    def record_stage(self, stage, **fields):
        stored = self.state()
        stored[stage] = {**stored.get(stage, {}), **fields, 'at': now()}
        self.directory.mkdir(parents=True, exist_ok=True)
        write(self.state_path, stored)
        return stored


def build(ledger, paper):
    """Stage 1: fill the table from the manuscript. No model, no network.

    Safe to call twice: a row that is already there keeps whatever later stages
    wrote on it, and only new rows are appended.
    """
    known, _ = ledger.load()
    references, uses = reference_rows(paper), use_rows(paper)
    added = 0
    for row in references + uses:
        if row['row_id'] in known:
            continue
        ledger.append(row)
        added += 1
    ledger.record_stage('fill', done=True, references=len(references), uses=len(uses),
                        added=added)
    return {'references': len(references), 'uses': len(uses), 'added': added,
            'ledger': str(ledger.path)}


# -- stage 2: resolve -------------------------------------------------------


class Lookups:
    """The ledger's own bounded fetcher: its own cache, its own receipts.

    Separate from the agents' :class:`~peerreviewagents.engine.external_evidence.SourceClient`
    on purpose. That client rations a reviewer to a handful of lookups and caps
    a run at two investigation targets, which is what keeps a reviewer from
    turning a review into a literature search. Resolving every reference is the
    opposite job — sixty references is a hundred-odd requests — and it is the
    server doing it, with no model in the loop, so it gets its own allowance and
    its own receipts rather than eating the reviewers'.

    The allowlist, the redirect policy and the client name are the shared ones.
    Nothing here sends anything about the user.
    """

    def __init__(self, directory, opener=None, limit=600):
        if type(limit) is not int or not 0 <= limit <= 5_000:
            raise ValueError('The citation lookup allowance must be between zero and 5000')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.opener = opener
        self.receipts_path = self.directory / 'requests.json'

    def receipts(self):
        if not self.receipts_path.is_file():
            return {'limit': self.limit, 'requests': []}
        try:
            stored = read(self.receipts_path)
        except (ValueError, OSError):
            return {'limit': self.limit, 'requests': []}
        if not isinstance(stored, dict) or not isinstance(stored.get('requests'), list):
            return {'limit': self.limit, 'requests': []}
        return stored

    def count(self):
        return len(self.receipts()['requests'])

    def _record(self, url, status):
        stored = self.receipts()
        stored['limit'] = self.limit
        stored['requests'].append({'source_url': url, 'status': status, 'at': now()})
        write(self.receipts_path, stored)

    def fetch(self, url, json_body=True):
        """One allowlisted GET, cached in the run folder by URL.

        The answer is always a status: ``ok``, or one of the reasons it is not,
        which the caller turns into "could not check" rather than into a claim
        about the paper.
        """
        key = digest(url)
        meta = self.directory / (key + '.json')
        body = self.directory / (key + ('.raw' if json_body else '.txt'))
        if meta.is_file() and body.is_file():
            try:
                record = read(meta)
                raw = body.read_bytes()
            except (ValueError, OSError):
                record, raw = None, None
            if record and raw is not None and record.get('source_url') == url:
                return self._decode(record | {'cache_hit': True}, raw, json_body)
        if self.count() >= self.limit:
            return {'status': 'budget_exhausted', 'source_url': url}
        answer = retrieve(url, opener=self.opener, headers=HEADERS)
        self._record(url, answer['status'])
        if answer['status'] != 'ok':
            return {key: value for key, value in answer.items() if key != 'raw'} | {
                'source_url': url}
        raw = answer['raw']
        record = {'status': 'ok', 'source_url': url, 'retrieved_at': now(),
                  'raw_sha256': digest(raw)}
        body.write_bytes(raw)
        write(meta, record)
        return self._decode(record | {'cache_hit': False}, raw, json_body)

    @staticmethod
    def _decode(record, raw, json_body):
        if not json_body:
            try:
                return record | {'text': raw.decode('utf-8', 'replace')}
            except (AttributeError, UnicodeError):
                return {'status': 'unavailable', 'source_url': record.get('source_url')}
        try:
            return record | {'data': json.loads(raw)}
        except ValueError:
            return {'status': 'schema_error', 'source_url': record.get('source_url')}


def crossref_search_url(title, first_author=None, year=None, rows=3):
    """A bibliographic title search: what a reference without a DOI gets.

    ``source_adapters.request('crossref', …)`` takes a DOI and nothing else, so
    this is its own query rather than a loosened identifier pattern: a search
    that accepted anything DOI-shaped would let a reference's raw string be sent
    as an identifier.
    """
    query = squeeze(title, 300)
    if not query:
        raise ValueError('A bibliographic search needs the title the paper printed')
    fields = {'query.bibliographic': query, 'rows': max(1, min(int(rows), 5)),
              'select': 'DOI,title,author,issued,published,container-title,type,relation,'
                        'update-to,abstract,subtitle'}
    if first_author:
        fields['query.author'] = squeeze(first_author, 80)
    if year and str(year).isdigit():
        fields['filter'] = 'from-pub-date:' + str(int(year) - 1) + '-01-01,until-pub-date:' + str(
            int(year) + 1) + '-12-31'
    return CROSSREF_SEARCH + urlencode(fields)


def europepmc_doi_url(doi):
    """The Europe PMC record for one DOI: where an abstract comes from."""
    return EUROPEPMC_SEARCH + urlencode({'query': 'DOI:"' + squeeze(doi, 200) + '"',
                                         'format': 'json', 'resultType': 'core', 'pageSize': 1})


def _crossref_record(message):
    """One Crossref work as the fields the ledger compares and stores."""
    titles = message.get('title') or []
    authors = message.get('author') or []
    first = authors[0] if authors else {}
    issued = (message.get('issued') or {}).get('date-parts') or [[]]
    published = (message.get('published') or {}).get('date-parts') or [[]]
    parts = (issued[0] or published[0] or [])
    container = message.get('container-title') or []
    relation = message.get('relation') or {}
    notices = [key for key in relation if 'retract' in key.casefold()]
    updates = [one for one in (message.get('update-to') or [])
               if 'retract' in str(one.get('type', '')).casefold()
               or 'withdraw' in str(one.get('type', '')).casefold()]
    retracted = bool(notices or updates
                     or str(titles[0] if titles else '').upper().startswith('RETRACTED'))
    notice = None
    if retracted:
        notice = squeeze('; '.join(notices + [str(one.get('type')) + ' ' + str(one.get('DOI'))
                                              for one in updates]), 200) or 'retraction notice'
    return {
        'doi': (message.get('DOI') or '').casefold() or None,
        'title': squeeze(titles[0]) if titles else None,
        'first_author': surname(' '.join(
            part for part in [first.get('given'), first.get('family')] if part)),
        'year': str(parts[0]) if parts else None,
        'venue': squeeze(container[0]) if container else None,
        'type': message.get('type'),
        'abstract': strip_markup(message.get('abstract'), FULL_TEXT_LIMIT) or None,
        'retracted': retracted,
        'retraction_notice': notice,
    }


def _europepmc_record(hit):
    """One Europe PMC search hit as the same fields."""
    types = [str(one).casefold() for one in (hit.get('publication_types') or [])]
    title = squeeze(hit.get('title'))
    retracted = any('retract' in one or 'withdraw' in one for one in types) or title.upper(
    ).startswith('RETRACTED')
    return {
        'doi': (hit.get('doi') or '').casefold() or None,
        'pmcid': hit.get('pmcid'),
        'title': title or None,
        'first_author': surname((hit.get('author_string') or '').split(',')[0]),
        'year': (squeeze(hit.get('publication_date')) or '')[:4] or None,
        'venue': squeeze(hit.get('journal')) or None,
        'abstract': squeeze(hit.get('abstract'), FULL_TEXT_LIMIT) or None,
        'open_access': str(hit.get('open_access') or '').upper() in ('Y', 'TRUE', '1'),
        'retracted': retracted,
        'retraction_notice': 'Europe PMC publication type: ' + ', '.join(
            types) if retracted else None,
    }


def _compare(printed, record):
    """Printed against resolved, per field: ``True``, ``False`` or ``None``.

    ``None`` is "could not compare" — the paper printed nothing to compare, or
    the record carries nothing — and it is never counted as a mismatch. A
    citation audit that reported every reference whose venue the reference-list
    parser could not read would report the parser, not the paper.
    """
    out = {}
    for field in COMPARED:
        mine, theirs = printed.get(field), record.get(field)
        if not mine or not theirs:
            out[field] = None
        elif field == 'title':
            out[field] = similar(mine, theirs) >= TITLE_MATCH
        elif field == 'year':
            out[field] = str(mine)[:4] == str(theirs)[:4]
        elif field == 'first_author':
            out[field] = surname(mine) == surname(theirs)
        else:
            score = similar(mine, theirs)
            out[field] = (score >= VENUE_MATCH or normal(mine) in normal(theirs)
                          or normal(theirs) in normal(mine))
    return out


def _best(printed, candidates):
    """The candidate whose title is the printed title, or ``None``.

    Strict on purpose. A bibliographic index answers every query with its
    nearest neighbours, so "the first hit" is not evidence that a work exists:
    the title has to actually match.
    """
    best, score = None, 0.0
    for candidate in candidates:
        ratio = similar(printed.get('title'), candidate.get('title'))
        if ratio > score:
            best, score = candidate, ratio
    if best is None or score < SEARCH_MATCH:
        return None
    if printed.get('year') and best.get('year') and abs(
            int(printed['year']) - int(best['year'])) > 1 and score < TITLE_MATCH:
        return None
    return best


def _abstract_and_text(lookups, record, resolution):
    """The abstract and any open-access full text for a resolved record."""
    abstract, source = record.get('abstract'), None
    pmcid, open_access = record.get('pmcid'), record.get('open_access')
    if (not abstract or not pmcid) and record.get('doi'):
        answer = lookups.fetch(europepmc_doi_url(record['doi']))
        resolution['requests'].append({'url': answer.get('source_url'),
                                       'status': answer.get('status')})
        if answer.get('status') == 'ok':
            try:
                hits = parse_literature(record['doi'], answer['data'], None, 1)['hits']
            except (TypeError, ValueError, KeyError):
                hits = []
            if hits:
                found = _europepmc_record(hits[0])
                abstract = abstract or found.get('abstract')
                pmcid = pmcid or found.get('pmcid')
                open_access = open_access or found.get('open_access')
                source = answer.get('source_url')
                if found.get('retracted') and not resolution.get('retracted'):
                    resolution['retracted'] = True
                    resolution['retraction_notice'] = found.get('retraction_notice')
    full_text, full_text_url = None, None
    if pmcid and open_access:
        url = EUROPEPMC_FULL_TEXT.format(pmcid=squeeze(pmcid, 20))
        answer = lookups.fetch(url, json_body=False)
        resolution['requests'].append({'url': url, 'status': answer.get('status')})
        if answer.get('status') == 'ok':
            full_text = strip_markup(answer.get('text'), FULL_TEXT_LIMIT) or None
            full_text_url = url
    return abstract, source, full_text, full_text_url


def resolve_reference(row, lookups):
    """Look one reference up and answer what the ledger stores about it.

    Order: the DOI the paper printed, then a bibliographic title search at
    Crossref, then the same search at Europe PMC. A DOI that is not registered
    does not end the question — a typo in a real paper's DOI is common — so the
    title search runs anyway and the row records both answers.
    """
    printed = row.get('printed') or {}
    resolution = {'exists': 'could not check', 'method': None, 'checked_at': now(),
                  'requests': [], 'retracted': None, 'retraction_notice': None,
                  'printed_doi': printed.get('doi'), 'printed_doi_registered': None,
                  'note': None}
    record, searched, failed = None, 0, 0
    if printed.get('doi'):
        url = CROSSREF_WORK + printed['doi'].replace(' ', '')
        answer = lookups.fetch(url)
        resolution['requests'].append({'url': url, 'status': answer.get('status')})
        if answer.get('status') == 'ok':
            try:
                record = _crossref_record((answer.get('data') or {}).get('message') or {})
                resolution.update(exists='yes', method='doi',
                                  printed_doi_registered=True, source_url=url)
            except (AttributeError, KeyError, TypeError, ValueError):
                record = None
                resolution['note'] = 'Crossref answered for this DOI in a shape this version ' \
                                     'could not read.'
                failed += 1
        elif answer.get('status') == 'not_found':
            resolution['printed_doi_registered'] = False
            resolution['note'] = 'The DOI the paper printed is not registered at Crossref.'
        else:
            failed += 1
            resolution['note'] = 'The DOI lookup did not answer (' + str(
                answer.get('status')) + ').'
    if record is None and printed.get('title'):
        url = crossref_search_url(printed['title'], printed.get('first_author'),
                                  printed.get('year'))
        answer = lookups.fetch(url)
        resolution['requests'].append({'url': url, 'status': answer.get('status')})
        if answer.get('status') == 'ok':
            searched += 1
            items = ((answer.get('data') or {}).get('message') or {}).get('items') or []
            candidates = []
            for item in items:
                try:
                    candidates.append(_crossref_record(item))
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
            found = _best(printed, candidates)
            if found:
                record = found
                resolution.update(exists='yes', method='crossref-title', source_url=url)
        else:
            failed += 1
    if record is None and printed.get('title'):
        url = literature_request(squeeze(printed['title'], 300), None, 3)
        answer = lookups.fetch(url)
        resolution['requests'].append({'url': url, 'status': answer.get('status')})
        if answer.get('status') == 'ok':
            searched += 1
            try:
                hits = parse_literature(squeeze(printed['title'], 300), answer['data'],
                                        None, 3)['hits']
            except (TypeError, ValueError, KeyError):
                hits = []
            found = _best(printed, [_europepmc_record(hit) for hit in hits])
            if found:
                record = found
                resolution.update(exists='yes', method='europepmc-title', source_url=url)
        else:
            failed += 1
    if record is None:
        if not printed.get('doi') and not printed.get('title'):
            resolution['note'] = ('The paper printed neither a DOI nor a title for this '
                                  'reference, so there was nothing to look up.')
        elif searched >= 2 and not failed:
            resolution['exists'] = 'no'
            resolution['method'] = 'title-search'
            resolution['note'] = ('No work with this title is indexed at Crossref or Europe '
                                  'PMC' + ('; the printed DOI is not registered either.'
                                           if resolution['printed_doi_registered'] is False
                                           else '.'))
        else:
            resolution['note'] = resolution['note'] or (
                'The lookups did not answer, so whether this reference exists could not be '
                'checked.')
        resolution['matches'] = {field: None for field in COMPARED}
        resolution['mismatched'] = []
        return resolution
    resolution['record'] = {key: record.get(key) for key in
                            ('doi', 'title', 'first_author', 'year', 'venue', 'type', 'pmcid')}
    resolution['resolved_doi'] = record.get('doi')
    resolution['retracted'] = bool(record.get('retracted'))
    resolution['retraction_notice'] = record.get('retraction_notice')
    resolution['matches'] = _compare(printed, record)
    resolution['mismatched'] = [field for field, same in resolution['matches'].items()
                                if same is False]
    abstract, abstract_url, full_text, full_text_url = _abstract_and_text(
        lookups, record, resolution)
    resolution['abstract'] = abstract
    resolution['abstract_url'] = abstract_url or resolution.get('source_url')
    resolution['full_text'] = full_text
    resolution['full_text_url'] = full_text_url
    if resolution['printed_doi'] and resolution.get(
            'resolved_doi') and resolution['printed_doi'] != resolution['resolved_doi']:
        resolution['mismatched'] = sorted(set(resolution['mismatched']) | {'doi'})
    return resolution


def resolve(ledger, lookups, limit=0):
    """Stage 2: resolve every unresolved reference row. No model.

    ``limit`` resolves at most that many rows and reports how many are left, so
    a caller can work through a long reference list in several calls without
    holding one tool call open for minutes. Resumable: a row that already has a
    resolution is not looked up again, and every answer is cached by URL in the
    run folder.
    """
    rows, _ = ledger.load()
    pending = [row for row in rows.values()
               if row.get('kind') == 'reference' and not row.get('resolution')]
    pending.sort(key=lambda row: row['row_id'])
    taken = pending[:limit] if limit and limit > 0 else pending
    before = lookups.count()
    for row in taken:
        resolution = resolve_reference(row, lookups)
        ledger.append({**row, 'stage': 'resolve', 'resolution': resolution})
    remaining = len(pending) - len(taken)
    totals = counts(ledger)
    ledger.record_stage('resolve', done=not remaining, resolved=totals['resolved'],
                        references=totals['references'], remaining=remaining,
                        lookups=lookups.count())
    # Read again, now that the stage is recorded: the lookup count in `counts`
    # comes from that record, so the first reading would hand this call's
    # caller a number that stops short of the lookups it just paid for.
    totals = counts(ledger)
    return {'resolved_now': len(taken), 'remaining': remaining,
            'lookups_now': lookups.count() - before, 'lookups_total': lookups.count(),
            'counts': totals}


# -- stage 3: judge ---------------------------------------------------------


def source_window(reference_row):
    """The source text one use row shows a worker, and where it came from."""
    resolution = (reference_row or {}).get('resolution') or {}
    abstract = squeeze(resolution.get('abstract'))
    full_text = squeeze(resolution.get('full_text'))
    parts, kinds = [], []
    if abstract:
        parts.append('ABSTRACT\n' + abstract)
        kinds.append('abstract')
    room = SOURCE_TEXT_LIMIT - len(parts[0]) if parts else SOURCE_TEXT_LIMIT
    if full_text and room > 400:
        parts.append('FULL TEXT (opening)\n' + full_text[:room - 20])
        kinds.append('full text')
    text = '\n\n'.join(parts)[:SOURCE_TEXT_LIMIT]
    return {
        'source_text': text,
        'source_kind': ' and '.join(kinds) or 'none',
        'source_url': resolution.get('full_text_url') or resolution.get(
            'abstract_url') or resolution.get('source_url'),
        'truncated': bool(full_text) and len(full_text) > max(0, room - 20),
    }


def judgeable(ledger):
    """The use rows a worker can be given: a reference, and no verdict yet."""
    rows, _ = ledger.load()
    return [row for row in rows.values()
            if row.get('kind') == 'use' and row.get('reference_id') and not row.get('verdict')]


def _stale(row, seconds):
    """Whether a row's claim has gone cold, so another worker may take it.

    A worker that dies holding a batch must not take those rows with it, and
    nothing in a review can wait on a session that is gone. So a claim expires:
    after ``seconds`` with no verdict the row is open again, and the ledger
    still shows who claimed it and never judged it.
    """
    claimed_at = row.get('claimed_at')
    if not claimed_at:
        return True
    try:
        when = datetime.datetime.fromisoformat(str(claimed_at))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds() >= seconds


def batch(ledger, slot, size=10, stale_seconds=900):
    """Stage 3: hand one worker a batch of use rows with their source text.

    The claim is recorded on the row, so two workers cannot be handed the same
    citation, and a worker that dies does not take its batch with it: after
    ``stale_seconds`` an unjudged claim is open again.
    """
    rows, _ = ledger.load()
    references = {row['reference_id']: row for row in rows.values()
                  if row.get('kind') == 'reference'}
    open_rows = [row for row in rows.values()
                 if row.get('kind') == 'use' and row.get('reference_id')
                 and not row.get('verdict')
                 and (row.get('claimed_by') in (None, slot) or _stale(row, stale_seconds))]
    open_rows.sort(key=lambda row: (str(row.get('block_id')), row.get('sentence_index') or 0,
                                    row['row_id']))
    size = max(1, min(int(size or 10), 25))
    out = []
    for row in open_rows[:size]:
        reference = references.get(row['reference_id']) or {}
        printed = reference.get('printed') or {}
        resolution = reference.get('resolution') or {}
        window = source_window(reference)
        ledger.append({**row, 'claimed_by': slot, 'claimed_at': now()})
        out.append({
            'row_id': row['row_id'],
            'block_id': row.get('block_id'),
            'sentence': row.get('sentence'),
            'marker': row.get('marker'),
            'reference': {
                'reference_id': row['reference_id'],
                'label': reference.get('label'),
                'printed_title': printed.get('title'),
                'printed_year': printed.get('year'),
                'exists': resolution.get('exists'),
                'retracted': resolution.get('retracted'),
                'resolved_title': (resolution.get('record') or {}).get('title'),
            },
            **window,
        })
    return out


def verdict(ledger, slot, row_id, answer, passage='', note=''):
    """Store one worker's verdict on one use row, or refuse it with a reason.

    Two rules, both enforced here rather than asked for in a prompt. A row the
    ledger has no source text for can only be ``cannot tell``: a worker with
    nothing to read cannot know whether the source supports the sentence. And a
    passage has to be *in* that source text, so "the exact source passage relied
    on" is the source's words and not the worker's.
    """
    rows, _ = ledger.load()
    row = rows.get(row_id)
    if row is None or row.get('kind') != 'use':
        return None, 'There is no citation use row with the id ' + str(row_id) + '.'
    if answer not in VERDICTS:
        return None, 'A verdict is one of: ' + ', '.join(VERDICTS) + '.'
    reference = next((one for one in rows.values() if one.get('kind') == 'reference'
                      and one.get('reference_id') == row.get('reference_id')), None)
    window = source_window(reference)
    if not window['source_text'] and answer != NO_SOURCE_VERDICT:
        return None, ('The ledger holds no abstract or full text for this reference, so the only '
                      'verdict available is "' + NO_SOURCE_VERDICT + '".')
    passage = squeeze(passage)
    if answer != NO_SOURCE_VERDICT:
        if not passage:
            return None, ('Quote the passage of the source you relied on, exactly as the source '
                          'text gives it.')
        if squeeze(passage) not in squeeze(window['source_text']):
            return None, ('That passage is not in the source text for this row. Quote the '
                          'source, character for character, from the text you were given.')
    stored = {**row, 'stage': 'judge', 'verdict': answer, 'passage': passage or None,
              'note': squeeze(note, 600) or None, 'judged_by': slot, 'judged_at': now(),
              'source_kind': window['source_kind'], 'source_url': window['source_url']}
    ledger.append(stored)
    # Stage 3 is recorded the way stages 1 and 2 are, so a reader can see what
    # each stage did without re-deriving it from the rows: how many verdicts
    # are in, which workers wrote them, and whether anything is still waiting.
    judgeable_rows = [one for one in rows.values()
                      if one.get('kind') == 'use' and one.get('reference_id')]
    judged_now = sum(1 for one in judgeable_rows
                     if one['row_id'] == row_id or one.get('verdict'))
    workers = sorted({str(one.get('judged_by')) for one in judgeable_rows
                      if one.get('judged_by')} | {str(slot)})
    ledger.record_stage('judge', done=judged_now >= len(judgeable_rows),
                        verdicts=judged_now, uses=len(judgeable_rows), workers=workers)
    return stored, None


# -- stage 4: what the specialist reads -------------------------------------


def reference_view(row):
    """One reference row as the specialist reads it, without the stored text.

    The abstract and full text are left out on purpose: they are what the
    workers judged against, they are long, and the specialist asks for one row
    at a time when it wants to read the source itself.
    """
    resolution = row.get('resolution') or {}
    return {
        'row_id': row.get('row_id'),
        'reference_id': row.get('reference_id'),
        'label': row.get('label'),
        'printed': row.get('printed'),
        'raw': row.get('raw'),
        'looked_up': bool(row.get('resolution')),
        'exists': resolution.get('exists') if row.get('resolution') else None,
        'method': resolution.get('method'),
        'record': resolution.get('record'),
        'matches': resolution.get('matches'),
        'mismatched': resolution.get('mismatched'),
        'printed_doi': resolution.get('printed_doi'),
        'printed_doi_registered': resolution.get('printed_doi_registered'),
        'retracted': resolution.get('retracted'),
        'retraction_notice': resolution.get('retraction_notice'),
        'has_source_text': bool(resolution.get('abstract') or resolution.get('full_text')),
        'source_url': (resolution.get('full_text_url') or resolution.get('abstract_url')
                       or resolution.get('source_url')),
        'note': resolution.get('note'),
        'lookup_statuses': [one.get('status') for one in resolution.get('requests') or []],
    }


def use_view(row):
    """One in-text use row and whatever verdict a worker left on it."""
    return {
        'row_id': row.get('row_id'),
        'reference_id': row.get('reference_id'),
        'block_id': row.get('block_id'),
        'section': row.get('section'),
        'sentence': row.get('sentence'),
        'marker': row.get('marker'),
        'derivation': row.get('derivation'),
        'verdict': row.get('verdict'),
        'passage': row.get('passage'),
        'note': row.get('note'),
        'judged_by': row.get('judged_by'),
        'claimed_by': row.get('claimed_by'),
        'source_kind': row.get('source_kind'),
        'source_url': row.get('source_url'),
    }


def reference_problem(row):
    """Why one reference row needs a second look, or ``''`` if it does not."""
    if not row.get('resolution'):
        return 'never looked up'
    resolution = row['resolution']
    reasons = []
    if resolution.get('exists') == 'no':
        reasons.append('no work with this title or DOI is indexed')
    elif resolution.get('exists') != 'yes':
        reasons.append('could not be checked')
    if resolution.get('retracted'):
        reasons.append('the resolved work is retracted')
    if resolution.get('mismatched'):
        reasons.append('printed ' + ', '.join(resolution['mismatched'])
                       + ' differs from the indexed record')
    if resolution.get('printed_doi_registered') is False:
        reasons.append('the printed DOI is not registered')
    return '; '.join(reasons)


def use_problem(row):
    """Why one use row needs a second look, or ``''`` if it does not."""
    if not row.get('reference_id'):
        return 'the marker ' + str(row.get('marker')) + ' matches no entry in the reference list'
    if row.get('verdict') in ('does not support', 'partly supports'):
        return 'a worker read the source as "' + row['verdict'] + '"'
    return ''


def problem_rows(ledger, limit=60):
    """Stage 4: every row the citations specialist has to re-check itself.

    Not a sample. The specialist's job changes shape here: instead of picking a
    few citations to spot-check, it is handed the rows the table already knows
    are wrong or unverifiable, and nothing becomes a finding until it has
    confirmed one for itself. A row the ledger judged clean is not in this list
    and is not a finding.
    """
    rows, _ = ledger.load()
    references, uses = [], []
    for row in sorted(rows.values(), key=lambda one: str(one.get('row_id'))):
        if row.get('kind') == 'reference':
            reason = reference_problem(row)
            if reason:
                references.append({**reference_view(row), 'problem': reason})
        elif row.get('kind') == 'use':
            reason = use_problem(row)
            if reason:
                uses.append({**use_view(row), 'problem': reason})
    kept_references, kept_uses = references[:limit], uses[:limit]
    return {
        'references': kept_references,
        'uses': kept_uses,
        'omitted': (len(references) - len(kept_references)) + (len(uses) - len(kept_uses)),
    }


def unverified_rows(ledger, limit=60):
    """The use rows nobody could judge: 'cannot tell', or never judged at all.

    These are not findings and they are not clean either. They are the part of
    the citation list this run did not manage to check, and the specialist
    reports them as that.
    """
    rows, _ = ledger.load()
    out = [{**use_view(row), 'problem': 'no source text to judge against'
            if row.get('verdict') == NO_SOURCE_VERDICT else 'never judged'}
           for row in sorted(rows.values(), key=lambda one: str(one.get('row_id')))
           if row.get('kind') == 'use' and row.get('reference_id')
           and row.get('verdict') in (None, '', NO_SOURCE_VERDICT)]
    return {'uses': out[:limit], 'omitted': max(0, len(out) - limit)}


def row_detail(ledger, row_id):
    """One row in full, with the source text, so a verdict can be re-checked."""
    rows, _ = ledger.load()
    row = rows.get(row_id)
    if row is None:
        return None
    if row.get('kind') == 'reference':
        return {'row': {**reference_view(row), 'problem': reference_problem(row)},
                **source_window(row)}
    reference = next((one for one in rows.values() if one.get('kind') == 'reference'
                      and one.get('reference_id') == row.get('reference_id')), None)
    return {'row': {**use_view(row), 'problem': use_problem(row)},
            'reference': reference_view(reference) if reference else None,
            **source_window(reference)}


# -- what the run says ------------------------------------------------------


def counts(ledger):
    """The citation numbers a report and :func:`peerreviewagents.read_run` print."""
    rows, broken = ledger.load()
    references = [row for row in rows.values() if row.get('kind') == 'reference']
    uses = [row for row in rows.values() if row.get('kind') == 'use']
    resolutions = [row.get('resolution') or {} for row in references]
    judged = [row for row in uses if row.get('verdict')]
    verdicts = {name: sum(1 for row in judged if row.get('verdict') == name)
                for name in VERDICTS}
    state = ledger.state()
    return {
        'references': len(references),
        'resolved': sum(1 for one in resolutions if one.get('exists') == 'yes'),
        'not_found': sum(1 for one in resolutions if one.get('exists') == 'no'),
        'could_not_check': sum(1 for one in resolutions
                               if one.get('exists') == 'could not check'),
        'unresolved': sum(1 for row in references if not row.get('resolution')),
        'mismatched': sum(1 for one in resolutions if one.get('mismatched')),
        'retracted': sum(1 for one in resolutions if one.get('retracted')),
        'with_source_text': sum(1 for one in resolutions
                                if one.get('abstract') or one.get('full_text')),
        'uses': len(uses),
        'uses_unmatched': sum(1 for row in uses if not row.get('reference_id')),
        'uses_judged': len(judged),
        'supported': verdicts['supports'],
        'partly_supported': verdicts['partly supports'],
        'not_supported': verdicts['does not support'],
        'cannot_tell': verdicts['cannot tell'],
        'uses_unjudged': sum(1 for row in uses if row.get('reference_id')
                             and not row.get('verdict')),
        'lookups': int((state.get('resolve') or {}).get('lookups') or 0),
        'unreadable_rows': broken,
        'ledger': LEDGER_PATH,
        'stages': state,
        'complete': bool((state.get('fill') or {}).get('done')
                         and (state.get('resolve') or {}).get('done')
                         and not sum(1 for row in uses if row.get('reference_id')
                                     and not row.get('verdict'))),
    }


def problems(totals):
    """One line per citation stage that did not finish. Never a blocker.

    An unfinished ledger does not hold a review back: the findings that were
    confirmed are still findings, and the counts say plainly how much of the
    reference list was checked. What it must not do is read as a clean bill of
    health, which is what silence would have done.
    """
    lines = []
    if not totals or not totals.get('references'):
        return lines
    stages = totals.get('stages') or {}
    if not (stages.get('fill') or {}).get('done'):
        lines.append('the citation ledger was never filled, so no reference or in-text use was '
                     'checked')
        return lines
    if totals.get('unresolved'):
        lines.append(f"{totals['unresolved']} of {totals['references']} references were never "
                     'looked up, so the citations were not fully checked')
    if totals.get('could_not_check'):
        lines.append(f"{totals['could_not_check']} reference(s) could not be checked because the "
                     'lookups did not answer; this says nothing about whether they exist')
    if totals.get('uses_unjudged'):
        lines.append(f"{totals['uses_unjudged']} of {totals['uses']} in-text citation uses were "
                     'never judged against the source')
    if totals.get('unreadable_rows'):
        lines.append(f"{totals['unreadable_rows']} line(s) of the citation ledger could not be "
                     'read and were skipped')
    return lines


def summary_line(totals):
    """One sentence a brief or a report can print about the ledger's state."""
    if not totals or not totals.get('references'):
        return 'No citation ledger was filled for this run.'
    return (f"{totals['references']} references: {totals['resolved']} resolved, "
            f"{totals['mismatched']} with a metadata mismatch, {totals['not_found']} not found, "
            f"{totals['could_not_check']} could not be checked, {totals['retracted']} retracted. "
            f"{totals['uses']} in-text uses, {totals['uses_judged']} judged "
            f"({totals['supported']} supported, {totals['partly_supported']} partly, "
            f"{totals['not_supported']} not supported, {totals['cannot_tell']} cannot tell).")

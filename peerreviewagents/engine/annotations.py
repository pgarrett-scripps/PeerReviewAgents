"""Source-bound suggestions, with editorial decisions separate from findings."""
import re
from pathlib import Path

from . import paper_base, quotes
from .storage import digest, read, relative_name, write

#: How many evidence lines one finding may list. The same cap the server
#: applies to a submitted finding, applied here so the public anchoring path
#: and the review server agree on what a finding may carry.
MAX_EVIDENCE_LINES = 12


def verify_workspace(root):
    """Re-check an imported manuscript folder against its own manifest.

    Returns the folder's :class:`~peerreviewagents.engine.paper_base.PaperBase`
    — the manuscript's own text and the map over it — because that is what a
    finding is anchored against. Every file the manifest lists is re-digested
    first, so a folder whose Markdown was edited after import is refused by
    name instead of producing line numbers that no longer mean anything.
    """
    root=Path(root)
    manifest=read(root/'manifest.json')
    for name, expected in manifest['files'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()) or digest(path.read_bytes())!=expected:
            raise ValueError('Manuscript source changed: '+name)
    # A manuscript imported from a source tree keeps that tree beside the base.
    # The manifest's digests cover the files it listed; this covers the ones it
    # did not, which is how a file added to the tree after import is caught.
    if (root/'scaffold').is_dir():
        actual=set()
        for path in (root/'scaffold').rglob('*'):
            if path.is_symlink():
                raise ValueError('Manuscript source contains a new symlink')
            if path.is_file():actual.add(relative_name(path,root))
        expected={name for name in manifest['files'] if name.startswith('scaffold/')}
        if actual!=expected:
            raise ValueError('Manuscript source inventory changed')
    return paper_base.load(root)


def normalize_review(base, response, review_id):
    """Anchor a model's findings to the manuscript's lines and number them.

    This is the public half of what the review server does on every
    ``submit_finding``: each finding's quote is placed in the base — by
    :mod:`peerreviewagents.engine.quotes`, over the whole document's normal
    form — and the finding records the document, the line, the end line and
    the offsets inside them. A quote that is absent, or that occurs in more
    than one place and was cited with no line, is kept as an unplaced finding
    with the refusal as its reason; it is not silently dropped and it is not
    guessed at.
    """
    if 'paper_id' in response and response['paper_id']!=base.paper_id:
        raise ValueError('Review belongs to a different manuscript version')
    if 'source_sha256' in response and response['source_sha256']!=(
            base.manifest.get('source') or {}).get('sha256'):
        raise ValueError('Review belongs to a different manuscript version')
    notes=[]
    for index, raw in enumerate(response.get('findings',[]),1):
        document=str(raw.get('document') or paper_base.MAIN_DOCUMENT)
        answer=quotes.anchor(base, str(raw.get('quote') or ''), raw.get('line'),
                             raw.get('end_line'), document)
        note=line_finding(raw, review_id, index, answer.get('placement'))
        if 'refused' in answer:
            note['placement_reason']=answer['refused']
        wanted=[number for number in raw.get('evidence_lines') or []
                if isinstance(number, int) and not isinstance(number, bool)]
        count=base.line_count(document if document in base.documents
                              else paper_base.MAIN_DOCUMENT)
        note['evidence_lines']=[number for number in wanted
                                if 1<=number<=count][:MAX_EVIDENCE_LINES]
        note['invalid_evidence_lines']=[number for number in wanted if not 1<=number<=count]
        notes.append(note)
    return {'id':review_id,'paper_id':base.paper_id,'summary':response.get('summary',''),
            'findings':notes,'limitations':response.get('limitations',[]),'raw_response':response,
            'annotation_counts':{'total':len(notes),'anchored':sum(a['placement']=='anchored' for a in notes)}}


#: Where a finding sits in the manuscript, as a placement from
#: :func:`peerreviewagents.engine.quotes.anchor` gives it. ``line`` through
#: ``end_line`` are the numbered lines of the document a reader was served,
#: ``start`` and ``end`` are offsets within the first and last of those lines —
#: enough to highlight the quote — and ``anchor_text`` is the file's own text
#: for the span, which differs from the quote where the reviewer retyped a dash
#: or lowered a capital. ``piece_kind``, ``page``, ``bbox``, ``section`` and
#: ``piece_lines`` are the piece the line belongs to: what it is, where it was
#: on the page it came from, and the whole of its line span.
LINE_FIELDS = ('document', 'line', 'end_line', 'start', 'end', 'anchor_text', 'quoted_exactly',
               'page', 'bbox', 'section', 'piece_kind', 'piece_lines', 'fingerprint')

#: The placement keys that are spelled differently on a finding, because the
#: finding already uses the shorter name for something of its own: a finding's
#: ``kind`` is ``edit`` or ``question``, and its ``text`` would be the paper's.
_RENAMED = {'kind': 'piece_kind', 'lines': 'piece_lines', 'text': 'anchor_text'}


def line_finding(raw, review_id, index, placement):
    """One submitted finding as the ledger keeps it, anchored to lines.

    The anchoring itself is :mod:`peerreviewagents.engine.quotes`, which has
    already said yes or no by the time this is called: *placement* is the one
    position it found, or ``None`` where the caller is recording a refusal.
    What is left is the finding's own shape — its kind, its category, and the
    two rules that withhold an edit — and those are the same rules the block-id
    era applied, for the same reasons.

    An edit is withheld unless it is anchored and carries a replacement, and
    again where the replacement prints a number the quote does not: a model
    rewriting a sentence to read well does not get to change what it reports,
    and a withheld edit becomes an author question rather than being dropped.
    """
    out = {**raw, 'id': f'{review_id}-{index}', 'review_id': review_id,
           'issue_status': 'open', 'edit_decision': 'proposed'}
    out['kind'] = raw.get('kind') if raw.get('kind') in ('edit', 'question',
                                                         'literature') else 'question'
    out['category'] = raw.get('category') if raw.get('category') in ('scientific',
                                                                     'copyedit') else 'scientific'
    out.update({field: None for field in LINE_FIELDS})
    if placement:
        out.update({_RENAMED.get(key, key): value for key, value in placement.items()})
        out['placement'] = 'anchored'
    else:
        out['placement'] = 'unplaced'
        out['placement_reason'] = ('The quote is not in the manuscript, or occurs in more than '
                                   'one place and was cited with no line')
    quote = str(raw.get('quote') or '')
    replacement = raw.get('replacement', '')
    if out['kind'] == 'edit' and (not isinstance(replacement, str) or not replacement.strip()
                                  or out['placement'] != 'anchored'):
        out['kind'] = 'question'
        out['edit_withheld'] = 'A valid anchored replacement was not supplied'
    numeric = r'(?<![\w])[-+]?\d+(?:\.\d+)?(?![\w])'
    if out['kind'] == 'edit' and not set(re.findall(numeric, replacement)) <= set(
            re.findall(numeric, quote)):
        out['kind'] = 'question'
        out['edit_withheld'] = ('Replacement introduces a numerical literal. Keep it as an '
                                'author question until checked.')
    out['replacement'] = replacement if out['kind'] == 'edit' else None
    return out


def quarantine_findings(findings):
    """Split normalized findings into the ones that passed evidence validation and the ones that did not.

    A finding is quarantined when its quote could not be placed in the
    manuscript, or when it cites lines the manuscript does not have. Each
    quarantined finding keeps its content and gains a reason, so one unanchored
    quote costs that finding and nothing else. The caller writes both lists to
    the run output: the quarantine is evidence about the model, not something
    to discard quietly.
    """
    kept, quarantined = [], []
    for finding in findings:
        reasons = []
        if finding.get('placement') != 'anchored':
            reasons.append(finding.get('placement_reason')
                           or 'Finding is not anchored to an exact source quote')
        if finding.get('invalid_evidence_lines'):
            reasons.append('Cites lines the manuscript does not have: '
                           + ', '.join(str(number)
                                       for number in finding['invalid_evidence_lines']))
        if reasons:
            quarantined.append({**finding, 'quarantined': True, 'issue_status': 'quarantined',
                                'quarantine_reason': ' '.join(reasons)})
        else:
            kept.append(finding)
    return kept, quarantined


def _offsets(base, finding):
    """The file offsets an anchored finding covers, in its own document."""
    document=str(finding.get('document') or paper_base.MAIN_DOCUMENT)
    if document not in base.documents:
        raise ValueError('Edit anchor names a document this manuscript does not hold')
    _, _, starts=base.projection(document)
    try:
        begin=starts[int(finding['line'])-1]+int(finding['start'])
        finish=starts[int(finding['end_line'])-1]+int(finding['end'])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError('Edit anchor is not a position in the manuscript') from exc
    return document, begin, finish


def export_draft(base, review, decisions):
    """The accepted wording edits applied to the manuscript's own text.

    The base is immutable, so nothing here writes a file: the package carries
    the document as it stands and the document as the accepted edits would
    leave it, side by side, for whoever applies them. An accepted edit whose
    span no longer reads as the text it was anchored on is refused for the
    whole package rather than applied somewhere near where it meant.
    """
    if review['paper_id']!=base.paper_id or decisions['paper_id']!=base.paper_id:
        raise ValueError('Decisions belong to a different manuscript')
    proposed={name: text for name, text in base.documents.items()}
    edits={}
    for a in review['findings']:
        if decisions['items'].get(a['id'],'proposed')!='accepted':
            continue
        if a['kind']!='edit' or a['placement']!='anchored':
            raise ValueError('Only anchored edit suggestions can be accepted')
        document, begin, finish=_offsets(base, a)
        if base.documents[document][begin:finish]!=a.get('anchor_text'):
            raise ValueError('Edit anchor no longer matches the immutable source')
        edits.setdefault(document,[]).append((begin, finish, a['replacement']))
    for document, pending in edits.items():
        ordered=sorted(pending)
        for (_, left_end, _), (right_start, _, _) in zip(ordered, ordered[1:]):
            if left_end>right_start:
                raise ValueError('Accepted suggestions overlap. Choose one or revise them before applying.')
        text=proposed[document]
        for begin, finish, replacement in reversed(ordered):
            text=text[:begin]+replacement+text[finish:]
        proposed[document]=text
    return {'paper_id':base.paper_id,
            'source_sha256':(base.manifest.get('source') or {}).get('sha256'),
            'review_id':review['id'],
            'original_documents':dict(base.documents),'proposed_documents':proposed,
            'document_sha256':{name: digest(text) for name, text in proposed.items()},
            'decisions':decisions,
            'findings':review['findings'],'source_findings':review.get('raw_response',{}).get('source_findings',[]),
            'source_hypotheses':review.get('raw_response',{}).get('source_hypotheses',[]),
            'candidate_inventory':review.get('raw_response',{}).get('candidate_inventory'),
            'evidence_handle_audit':review.get('raw_response',{}).get('evidence_handle_audit'),
            'source_cards':review.get('raw_response',{}).get('source_cards',[]),
            'result_map':review.get('raw_response',{}).get('result_map'),
            'result_comparisons':review.get('raw_response',{}).get('result_comparisons',[]),
            'domain_profile':review.get('raw_response',{}).get('domain_profile'),
            'source_validation':review.get('raw_response',{}).get('source_validation'),
            'investigation_context':review.get('raw_response',{}).get('investigation_context',{}),
            'source_dispositions':review.get('raw_response',{}).get('source_dispositions',[]),
            # `component_decisions` and `epistemic_audit` were the debate's own
            # keys. 0.9 has no debate, so every export carried two empty fields
            # a consumer had to decide what to do with; they are gone rather
            # than kept as a shape nothing fills.
            'component_validation':review.get('raw_response',{}).get('component_validation'),
            'workflow_version':review.get('raw_response',{}).get('workflow_version'),
            'coverage_record':review.get('raw_response',{}).get('coverage_record'),
            'experiment_map':review.get('raw_response',{}).get('experiment_map'),
            'task_results':review.get('raw_response',{}).get('task_results'),
            'task_validation':review.get('raw_response',{}).get('task_validation'),
            'evidence_provenance':review.get('raw_response',{}).get('evidence_provenance'),
            'source_disposition_validation':review.get('raw_response',{}).get('source_disposition_validation',[]),
            'unexpected_source_disposition_ids':review.get('raw_response',{}).get('unexpected_source_disposition_ids',[]),
            'handoff':{key:review.get('raw_response',{}).get(key) for key in
                       ('editor_findings_count','verification_queue_count','ledger_limitation')},
            'limitations':review.get('limitations',[]),'issue_status':'Acceptance edits wording only. Scientific issues remain open.',
            'format':'PRA review package. This is not native Word tracked changes or a PDF layout reproduction.'}


def save_review(root, response, review_id):
    if not re.fullmatch(r'[a-z0-9_-]{1,50}',review_id):
        raise ValueError('Invalid review ID')
    base=verify_workspace(root)
    value=normalize_review(base,response,review_id)
    path=Path(root)/'reviews'/f'{review_id}.json'
    if path.exists():
        if read(path)==value:
            return value
        raise ValueError('Review ID already exists with different content')
    write(path,value)
    return value

"""Typed, immutable handles for exact manuscript and external evidence spans."""

import copy
import json
import re
from dataclasses import dataclass

from .storage import digest


VERSION = 'evidence-handles-v1'
STRING = {'type': 'string', 'minLength': 1}
MAX_SPAN_LENGTH = 350


@dataclass(frozen=True)
class EvidenceHandle:
    handle_id: str
    source_kind: str
    source_id: str
    start: int
    end: int
    text: str
    source_sha256: str
    anchor_eligible: bool

    def model_record(self):
        """Return the bounded record shown to a model."""
        return {
            'handle_id': self.handle_id,
            'source_kind': self.source_kind,
            'source_id': self.source_id,
            'text': self.text,
            'anchor_eligible': self.anchor_eligible,
        }


@dataclass(frozen=True)
class EvidenceRegistry:
    handles: tuple
    manifest_sha256: str

    def by_id(self):
        return {handle.handle_id: handle for handle in self.handles}


def _source_sha256(text, declared=None):
    actual = digest(text)
    if declared is not None and declared != actual:
        raise ValueError('Evidence source hash does not match its text')
    return actual


def _trimmed_span(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _line_spans(text):
    spans = []
    offset = 0
    for line in text.splitlines(keepends=True):
        start, end = _trimmed_span(text, offset, offset + len(line))
        if start < end:
            spans.append((start, end))
        offset += len(line)
    if offset < len(text):
        start, end = _trimmed_span(text, offset, len(text))
        if start < end:
            spans.append((start, end))
    if not text.splitlines(keepends=True) and text.strip():
        start, end = _trimmed_span(text, 0, len(text))
        spans.append((start, end))
    return spans


def _sentence_spans(text):
    """Split lines into exact, contiguous sentence-like spans."""
    spans = []
    for line_start, line_end in _line_spans(text):
        cursor = line_start
        i = line_start
        while i < line_end:
            if text[i] in '.?!':
                after = i + 1
                while after < line_end and text[after] in '\"\'\u2019\u201d)]}':
                    after += 1
                if after == line_end or text[after].isspace():
                    following = after
                    while following < line_end and text[following].isspace():
                        following += 1
                    if following == line_end or text[following].isupper() or text[following].isdigit() or text[following] in '\"\u201c([':
                        start, end = _trimmed_span(text, cursor, after)
                        if start < end:
                            spans.append((start, end))
                        cursor = following
                        i = following
                        continue
            i += 1
        start, end = _trimmed_span(text, cursor, line_end)
        if start < end:
            spans.append((start, end))
    bounded = []
    for start, end in spans:
        while end - start > MAX_SPAN_LENGTH:
            split = text.rfind(' ', start, start + MAX_SPAN_LENGTH + 1)
            if split <= start:
                split = start + MAX_SPAN_LENGTH
            chunk_start, chunk_end = _trimmed_span(text, start, split)
            if chunk_start < chunk_end:
                bounded.append((chunk_start, chunk_end))
            start = split
            while start < end and text[start].isspace():
                start += 1
        if start < end:
            bounded.append((start, end))
    return bounded


def _bounded_line_spans(text):
    bounded = []
    for start, end in _line_spans(text):
        while end - start > MAX_SPAN_LENGTH:
            split = text.rfind(' ', start, start + MAX_SPAN_LENGTH + 1)
            if split <= start:
                split = start + MAX_SPAN_LENGTH
            chunk_start, chunk_end = _trimmed_span(text, start, split)
            if chunk_start < chunk_end:
                bounded.append((chunk_start, chunk_end))
            start = split
            while start < end and text[start].isspace():
                start += 1
        if start < end:
            bounded.append((start, end))
    return bounded


def _make_handle(source_kind, source_id, text, start, end, source_sha256):
    if source_kind not in ('manuscript', 'external'):
        raise ValueError('Unknown evidence source kind')
    if (type(start) is not int or type(end) is not int
            or start < 0 or end <= start or end > len(text)):
        raise ValueError('Evidence span must be one nonempty contiguous source range')
    excerpt = text[start:end]
    identity = json.dumps({
        'version': VERSION,
        'source_kind': source_kind,
        'source_id': source_id,
        'source_sha256': source_sha256,
        'start': start,
        'end': end,
        'text_sha256': digest(excerpt),
    }, sort_keys=True, ensure_ascii=False)
    prefix = 'mh-' if source_kind == 'manuscript' else 'eh-'
    return EvidenceHandle(
        handle_id=prefix + digest(identity)[:24],
        source_kind=source_kind,
        source_id=source_id,
        start=start,
        end=end,
        text=excerpt,
        source_sha256=source_sha256,
        anchor_eligible=(source_kind == 'manuscript' and
                         sum(1 for _ in re.finditer('(?='+re.escape(excerpt)+')', text)) == 1),
    )


def manuscript_block_handles(block):
    """Create finite exact handles for the sentence-like spans in one block."""
    if not isinstance(block, dict) or not isinstance(block.get('id'), str):
        raise ValueError('Manuscript block requires an ID')
    text = block.get('text')
    if not isinstance(text, str):
        raise ValueError('Manuscript block text must be a string')
    source_sha256 = _source_sha256(text, block.get('sha256'))
    spans = _sentence_spans(text)
    trimmed_start, trimmed_end = _trimmed_span(text, 0, len(text))
    if (trimmed_start < trimmed_end and trimmed_end - trimmed_start <= MAX_SPAN_LENGTH
            and not any(text.count(text[start:end]) == 1 for start, end in spans)
            and (trimmed_start, trimmed_end) not in spans):
        spans.append((trimmed_start, trimmed_end))
    return tuple(_make_handle('manuscript', block['id'], text, start, end, source_sha256)
                 for start, end in spans)


def source_card_handles(card):
    """Create exact line handles for one eligible normalized source card."""
    if not isinstance(card, dict) or not isinstance(card.get('evidence_id'), str):
        raise ValueError('Source card requires an evidence ID')
    if card.get('eligible') is not True:
        return ()
    text = card.get('evidence_text')
    if not isinstance(text, str):
        raise ValueError('Source card evidence text must be a string')
    source_sha256 = _source_sha256(text)
    return tuple(_make_handle('external', card['evidence_id'], text, start, end, source_sha256)
                 for start, end in _bounded_line_spans(text))


def build_registry(paper, cards=()):
    """Build a deterministic registry from the manuscript and final source cards."""
    if not isinstance(paper, dict) or not isinstance(paper.get('blocks'), list):
        raise ValueError('Paper requires manuscript blocks')
    handles = []
    seen_sources = set()
    for block in paper['blocks']:
        key = ('manuscript', block.get('id'))
        if key in seen_sources:
            raise ValueError('Duplicate manuscript block ID')
        seen_sources.add(key)
        handles.extend(manuscript_block_handles(block))
    for card in cards:
        key = ('external', card.get('evidence_id'))
        if key in seen_sources:
            raise ValueError('Duplicate external evidence ID')
        seen_sources.add(key)
        handles.extend(source_card_handles(card))
    identifiers = [handle.handle_id for handle in handles]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError('Evidence handle collision')
    manifest = json.dumps([
        [handle.handle_id, handle.source_kind, handle.source_id, handle.source_sha256,
         handle.start, handle.end, digest(handle.text)]
        for handle in handles
    ], ensure_ascii=False, separators=(',', ':'))
    return EvidenceRegistry(tuple(handles), digest(manifest))


def model_catalog(registry, source_ids=None):
    """Return model-facing copies, optionally limited to selected source IDs."""
    if not isinstance(registry, EvidenceRegistry):
        raise TypeError('Expected an EvidenceRegistry')
    selected = None if source_ids is None else set(source_ids)
    return [handle.model_record() for handle in registry.handles
            if selected is None or handle.source_id in selected]


def annotate_manuscript(paper, include_references=True):
    """Replace copied block text with exact handles for a compact model payload."""
    result = copy.deepcopy(paper)
    for block in result.get('blocks', []):
        section = str(block.get('section', '')).casefold()
        if not include_references and section in ('references', 'bibliography'):
            block['evidence_handles'] = []
            block.pop('text', None)
            continue
        block['evidence_handles'] = [handle.model_record()
                                     for handle in manuscript_block_handles(block)]
        block.pop('text', None)
    return result


def _replace_pair(schema, container):
    properties = schema.get('properties')
    required = schema.get('required')
    if not isinstance(properties, dict) or not isinstance(required, list):
        return
    pair = None
    handle_name = None
    if 'block_id' in properties and 'quote' in properties:
        pair = ('block_id', 'quote')
        finding_shape = all(name in properties for name in
                            ('title', 'kind', 'comment', 'replacement', 'evidence_blocks'))
        handle_name = 'anchor_handle' if container == 'findings' or finding_shape else 'evidence_handle'
    elif 'evidence_id' in properties and 'quote' in properties:
        pair = ('evidence_id', 'quote')
        handle_name = 'source_handle'
    if pair is None:
        return
    if not all(name in required for name in pair):
        raise ValueError('Evidence schema pair must be required before handle conversion')
    for name in pair:
        properties.pop(name)
        required.remove(name)
    properties[handle_name] = copy.deepcopy(STRING)
    required.append(handle_name)
    if handle_name == 'anchor_handle' and 'evidence_blocks' in properties:
        evidence_schema = properties.pop('evidence_blocks')
        properties['manuscript_evidence_handles'] = {
            **copy.deepcopy(evidence_schema),
            'items': copy.deepcopy(STRING),
        }
        if 'evidence_blocks' in required:
            required.remove('evidence_blocks')
            required.append('manuscript_evidence_handles')


def with_evidence_handles(schema):
    """Return a schema whose source quote pairs use typed handle IDs."""
    result = copy.deepcopy(schema)

    def walk(node, container=None):
        if not isinstance(node, dict):
            return
        _replace_pair(node, container)
        properties = node.get('properties')
        if isinstance(properties, dict):
            for name, child in properties.items():
                walk(child, name)
        if isinstance(node.get('items'), dict):
            walk(node['items'], container)
        for keyword in ('anyOf', 'oneOf', 'allOf'):
            for branch in node.get(keyword, []):
                walk(branch, container)

    walk(result)
    return result


def constrain_manuscript_handles(schema, registry):
    """Restrict manuscript references to the supplied finite registry."""
    result = copy.deepcopy(schema)
    ids = [h.handle_id for h in registry.handles if h.source_kind == 'manuscript']
    if not ids:
        raise ValueError('Cannot constrain references without manuscript handles')
    # Large papers use one equivalent finite pattern instead of oversized enum lists.
    choice = ({'type':'string','enum':ids} if len(ids) <= 950 else
              {'type':'string','pattern':'^(?:'+'|'.join(re.escape(i) for i in ids)+')$'})
    definitions = result.setdefault('$defs', {})
    if 'manuscriptEvidenceHandle' in definitions:
        raise ValueError('Evidence schema definition already exists')
    definitions['manuscriptEvidenceHandle'] = choice
    manuscript = {'$ref':'#/$defs/manuscriptEvidenceHandle'}
    external = {'type':'string','pattern':'^eh-[0-9a-f]{24}$'}

    def walk(node, coverage=False):
        if not isinstance(node, dict):
            return
        properties = node.get('properties', {})
        if 'anchor_handle' in properties:
            properties['anchor_handle'] = copy.deepcopy(manuscript)
        if 'manuscript_evidence_handles' in properties:
            properties['manuscript_evidence_handles']['items'] = copy.deepcopy(manuscript)
        if 'source_handle' in properties:
            properties['source_handle'] = copy.deepcopy(external)
        if 'evidence_handle' in properties:
            properties['evidence_handle'] = (copy.deepcopy(manuscript) if coverage else
                {'anyOf':[copy.deepcopy(manuscript),copy.deepcopy(external)]})
        for name, child in properties.items():
            walk(child, coverage or name == 'coverage_checks')
        walk(node.get('items'), coverage)
        for keyword in ('anyOf', 'oneOf', 'allOf'):
            for branch in node.get(keyword, []):
                walk(branch, coverage)

    walk(result)
    return result


def hydrate_review_response(response, registry):
    """Keep scientific references strict while isolating invalid advisory coverage."""
    body = copy.deepcopy(response)
    coverage = body.pop('coverage_checks', None)
    result = hydrate_response(body, registry)
    if coverage is None:
        return result
    if not isinstance(coverage, list):
        raise ValueError('Coverage checks must be an array')
    rows = []
    quarantined = []
    for index, row in enumerate(coverage):
        if not isinstance(row, dict):
            raise ValueError('Coverage check must be a structured record')
        try:
            checked = hydrate_response({'coverage_checks':[row]}, registry)
        except ValueError as exc:
            rows.append({**row,'status':'unresolved','evidence':[],
                         'reference_validation_error':str(exc)})
            quarantined.append({'path':f'$.coverage_checks[{index}]','error':str(exc),
                                'original_record':copy.deepcopy(row)})
        else:
            rows.append(checked['response']['coverage_checks'][0])
            for receipt in checked['audit']['transformations']:
                receipt['path'] = receipt['path'].replace('$.coverage_checks[0]',f'$.coverage_checks[{index}]',1)
                result['audit']['transformations'].append(receipt)
    result['response']['coverage_checks'] = rows
    result['audit']['transformation_count'] = len(result['audit']['transformations'])
    result['audit']['quarantined_coverage_records'] = quarantined
    return result


def _resolve(registry, handle_id, expected, path):
    if not isinstance(handle_id, str) or not handle_id:
        raise ValueError(f'Missing evidence handle at {path}')
    handle = registry.by_id().get(handle_id)
    if handle is None:
        raise ValueError(f'Unknown evidence handle at {path}')
    if expected == 'manuscript-anchor':
        if handle.source_kind != 'manuscript':
            raise ValueError('External evidence cannot replace a manuscript finding anchor')
        if not handle.anchor_eligible:
            raise ValueError('Finding handle text is ambiguous in its manuscript block')
    elif expected == 'external' and handle.source_kind != 'external':
        raise ValueError('Manuscript evidence cannot replace an external source reference')
    return handle


def hydrate_response(response, registry):
    """Hydrate handle references into legacy fields and return an audit receipt."""
    if not isinstance(response, dict):
        raise ValueError('Model response must be an object')
    if not isinstance(registry, EvidenceRegistry):
        raise TypeError('Expected an EvidenceRegistry')
    hydrated = copy.deepcopy(response)
    transformations = []

    def receipt(path, handle, role):
        transformations.append({
            'path': path,
            'role': role,
            'handle_id': handle.handle_id,
            'source_kind': handle.source_kind,
            'source_id': handle.source_id,
            'start': handle.start,
            'end': handle.end,
            'source_sha256': handle.source_sha256,
            'text_sha256': digest(handle.text),
        })

    def walk(node, path='$', container=None):
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f'{path}[{index}]', container)
            return
        if not isinstance(node, dict):
            return
        handle_fields = [name for name in ('anchor_handle', 'source_handle', 'evidence_handle')
                         if name in node]
        had_anchor = 'anchor_handle' in node
        legacy_pair = (('block_id' in node and 'quote' in node)
                       or ('evidence_id' in node and 'quote' in node))
        if len(handle_fields) > 1 or (handle_fields and legacy_pair):
            raise ValueError(f'Mixed or ambiguous evidence reference at {path}')
        if handle_fields:
            name = handle_fields[0]
            expected = {'anchor_handle': 'manuscript-anchor',
                        'source_handle': 'external',
                        'evidence_handle': 'any'}[name]
            handle = _resolve(registry, node.pop(name), expected, path)
            if '.coverage_checks[' in path and handle.source_kind != 'manuscript':
                raise ValueError('Coverage evidence must refer to the manuscript')
            if name == 'source_handle':
                node['evidence_id'] = handle.source_id
            else:
                node['block_id'] = handle.source_id
            node['quote'] = handle.text
            receipt(path, handle, name)
        elif container == 'findings':
            raise ValueError(f'Missing manuscript anchor handle at {path}')
        elif container in ('source_evidence', 'evidence'):
            raise ValueError(f'Missing evidence handle at {path}')
        if 'manuscript_evidence_handles' in node:
            raw_handles = node.pop('manuscript_evidence_handles')
            if not isinstance(raw_handles, list):
                raise ValueError(f'Manuscript evidence handles must be an array at {path}')
            block_ids = []
            for index, handle_id in enumerate(raw_handles):
                handle = _resolve(registry, handle_id, 'manuscript',
                                  f'{path}.manuscript_evidence_handles[{index}]')
                if handle.source_kind != 'manuscript':
                    raise ValueError('External evidence cannot be used as a manuscript evidence block')
                if handle.source_id not in block_ids:
                    block_ids.append(handle.source_id)
                receipt(f'{path}.manuscript_evidence_handles[{index}]', handle,
                        'manuscript_evidence_handle')
            node['evidence_blocks'] = block_ids
        elif had_anchor:
            raise ValueError(f'Missing manuscript evidence handles at {path}')
        elif container == 'findings' and 'evidence_blocks' in node:
            raise ValueError(f'Raw manuscript evidence blocks are not accepted at {path}')
        for name, child in list(node.items()):
            walk(child, f'{path}.{name}', name)

    walk(hydrated)
    audit = {
        'version': VERSION,
        'registry_manifest_sha256': registry.manifest_sha256,
        'transformations': transformations,
        'transformation_count': len(transformations),
        'limitation': ('Handles establish exact contiguous source spans and source identity. '
                       'They do not establish scientific correctness or interpretation.'),
    }
    return {'response': hydrated, 'audit': audit}

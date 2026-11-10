"""Regression tests for typed exact-span evidence handles."""

import copy
import unittest

from peerreviewagents.engine.evidence_handles import (
    annotate_manuscript,
    build_registry,
    hydrate_response,
    model_catalog,
    source_card_handles,
    with_evidence_handles,
    constrain_manuscript_handles,
    hydrate_review_response,
)
from peerreviewagents.engine.debate_revision import DEBATE_REVISION
from peerreviewagents.engine.investigation import Investigation
from peerreviewagents.engine.review_schema import REVIEW, with_external_evidence
from peerreviewagents.engine.storage import digest


class EvidenceHandleTests(unittest.TestCase):
    def setUp(self):
        ecology = ('Ultimately, 10 and 15 individuals were chosen as arbitrary cut off values '
                   'for the hand-caught and pan-trapped datasets, respectively, which meant '
                   'that some transects were removed from analysis in all datasets except 1 and 6.')
        second = 'The regression line is fitted from the SLR model (Table 2g).'
        self.paper = {
            'id': 'paper',
            'blocks': [
                {'id': 'b0022', 'text': ecology, 'sha256': digest(ecology)},
                {'id': 'b0037', 'text': second, 'sha256': digest(second)},
            ],
        }
        self.health_card = {
            'evidence_id': 'source-health',
            'eligible': True,
            'evidence_text': (
                'studyFirstPostDateStruct: {"date": "2021-01-22", "type": "ACTUAL"}\n'
                'studyFirstSubmitDate: "2021-01-06"\n'
                'studyFirstSubmitQcDate: "2021-01-19"'
            ),
        }

    def test_schema_conversion_preserves_input_and_types_each_reference_role(self):
        original = with_external_evidence(REVIEW)
        before = copy.deepcopy(original)
        converted = with_evidence_handles(original)
        finding = converted['properties']['findings']['items']
        self.assertEqual(original, before)
        self.assertIn('anchor_handle', finding['required'])
        self.assertNotIn('block_id', finding['properties'])
        self.assertIn('manuscript_evidence_handles', finding['required'])
        self.assertNotIn('evidence_blocks', finding['properties'])
        source = finding['properties']['source_evidence']['items']
        self.assertEqual(source['required'], ['source_handle'])
        debate = with_evidence_handles(DEBATE_REVISION)
        reference = debate['properties']['decisions']['items']['properties']['evidence']['items']
        self.assertEqual(reference['required'], ['evidence_handle'])

    def test_ecology_typo_cannot_be_expressed_as_a_handle(self):
        registry = build_registry(self.paper)
        exact = next(row for row in model_catalog(registry, ['b0022']))
        response = {'findings': [{
            'anchor_handle': exact['handle_id'],
            'title': 'Rarefaction exclusions',
            'manuscript_evidence_handles': [exact['handle_id']],
        }], 'decisions': [{
            'evidence': [{'evidence_handle': exact['handle_id']}],
        }]}
        result = hydrate_response(response, registry)
        finding = result['response']['findings'][0]
        self.assertIn('chosen as arbitrary cut off values', finding['quote'])
        self.assertNotIn('chosen an arbitrary cut off values', finding['quote'])
        self.assertEqual(finding['evidence_blocks'], ['b0022'])
        self.assertEqual(result['audit']['transformation_count'], 3)
        raw_typo = {'findings': [{'block_id': 'b0022',
                                 'quote': exact['text'].replace('as arbitrary', 'an arbitrary')}]}
        with self.assertRaisesRegex(ValueError, 'Missing manuscript anchor handle'):
            hydrate_response(raw_typo, registry)

    def test_health_fields_are_separate_handles_and_order_is_not_rewritten(self):
        handles = source_card_handles(self.health_card)
        self.assertEqual(len(handles), 3)
        registry = build_registry(self.paper, [self.health_card])
        selected = [handle.handle_id for handle in handles]
        response = {'findings': [{
            'anchor_handle': model_catalog(registry, ['b0037'])[0]['handle_id'],
            'manuscript_evidence_handles': [],
            'source_evidence': [{'source_handle': handle_id} for handle_id in selected],
        }]}
        result = hydrate_response(response, registry)['response']['findings'][0]
        self.assertEqual([row['quote'] for row in result['source_evidence']], [
            'studyFirstPostDateStruct: {"date": "2021-01-22", "type": "ACTUAL"}',
            'studyFirstSubmitDate: "2021-01-06"',
            'studyFirstSubmitQcDate: "2021-01-19"',
        ])
        self.assertTrue(all(row['evidence_id'] == 'source-health'
                            for row in result['source_evidence']))

    def test_external_handle_cannot_be_used_as_manuscript_anchor(self):
        registry = build_registry(self.paper, [self.health_card])
        external = next(row for row in model_catalog(registry)
                        if row['source_kind'] == 'external')
        with self.assertRaisesRegex(ValueError, 'External evidence cannot replace'):
            hydrate_response({'findings': [{'anchor_handle': external['handle_id'],
                                            'manuscript_evidence_handles': []}]}, registry)

    def test_unknown_missing_mixed_and_noncontiguous_references_are_rejected(self):
        registry = build_registry(self.paper, [self.health_card])
        manuscript = next(row for row in model_catalog(registry)
                          if row['source_kind'] == 'manuscript')
        cases = [
            ({'findings': [{'anchor_handle': 'mh-unknown',
                            'manuscript_evidence_handles': []}]}, 'Unknown evidence handle'),
            ({'findings': [{}]}, 'Missing manuscript anchor handle'),
            ({'findings': [{'anchor_handle': manuscript['handle_id'],
                            'manuscript_evidence_handles': [],
                            'block_id': 'b0022', 'quote': manuscript['text']}]}, 'Mixed or ambiguous'),
            ({'findings': [{'anchor_handle': manuscript['handle_id'],
                            'manuscript_evidence_handles': [],
                            'source_evidence': [{}]}]}, 'Missing evidence handle'),
            ({'findings': [{'anchor_handle': manuscript['handle_id']}]},
             'Missing manuscript evidence handles'),
        ]
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                hydrate_response(value, registry)
        from peerreviewagents.engine.evidence_handles import _make_handle
        with self.assertRaisesRegex(ValueError, 'contiguous source range'):
            _make_handle('manuscript', 'b', 'abcdef', 4, 2, digest('abcdef'))

    def test_registry_rejects_source_mutation_and_is_deterministic_for_midcall_cards(self):
        first = build_registry(self.paper, [self.health_card])
        second = build_registry(self.paper, [copy.deepcopy(self.health_card)])
        self.assertEqual(first, second)
        lookup_handles = [row.handle_id for row in source_card_handles(self.health_card)]
        final_handles = [row['handle_id'] for row in model_catalog(first, ['source-health'])]
        self.assertEqual(lookup_handles, final_handles)
        changed = copy.deepcopy(self.paper)
        changed['blocks'][0]['text'] += ' Changed.'
        with self.assertRaisesRegex(ValueError, 'hash does not match'):
            build_registry(changed, [self.health_card])

    def test_annotation_is_a_copy_and_only_issues_unique_anchor_handles(self):
        repeated = 'Same sentence. Same sentence.'
        paper = {'blocks': [{'id': 'b1', 'text': repeated, 'sha256': digest(repeated)}]}
        annotated = annotate_manuscript(paper)
        self.assertNotIn('evidence_handles', paper['blocks'][0])
        self.assertNotIn('text', annotated['blocks'][0])
        self.assertTrue(annotated['blocks'][0]['evidence_handles'])
        self.assertTrue(any(row['anchor_eligible']
                           for row in annotated['blocks'][0]['evidence_handles']))

    def test_external_handle_cannot_enter_manuscript_evidence_blocks(self):
        registry = build_registry(self.paper, [self.health_card])
        manuscript = next(row for row in model_catalog(registry)
                          if row['source_kind'] == 'manuscript')
        external = next(row for row in model_catalog(registry)
                        if row['source_kind'] == 'external')
        response = {'findings': [{
            'anchor_handle': manuscript['handle_id'],
            'manuscript_evidence_handles': [external['handle_id']],
        }]}
        with self.assertRaisesRegex(ValueError, 'External evidence cannot be used'):
            hydrate_response(response, registry)

    def test_coverage_rejects_external_sources(self):
        registry = build_registry(self.paper, [self.health_card])
        handle = source_card_handles(self.health_card)[0].handle_id
        with self.assertRaisesRegex(ValueError, 'Coverage evidence must refer'):
            hydrate_response({'coverage_checks':[{'evidence':[{'evidence_handle':handle}]}]}, registry)

    def test_unique_sentences_do_not_duplicate_the_whole_block(self):
        text = 'First distinct sentence. Second distinct sentence.'
        registry = build_registry({'blocks':[{'id':'b1','text':text}]})
        self.assertEqual([h.text for h in registry.handles],
                         ['First distinct sentence.','Second distinct sentence.'])

    def test_schema_restricts_manuscript_ids_before_generation(self):
        registry = build_registry(self.paper)
        from peerreviewagents.engine.discovery import INVESTIGATION_REVIEW
        schema = constrain_manuscript_handles(with_evidence_handles(INVESTIGATION_REVIEW),registry)
        choices = schema['$defs']['manuscriptEvidenceHandle']['enum']
        self.assertEqual(set(choices),set(registry.by_id()))
        self.assertNotIn('mh-4bb75eb46e856bfc51184dbf6c1a83fa2f599b7e9df0f5b1fe5',choices)
        self.assertNotIn('mh-'+'0'*24,choices)
        ref = schema['properties']['coverage_checks']['items']['properties']['evidence']['items']
        self.assertEqual(ref['properties']['evidence_handle'],{'$ref':'#/$defs/manuscriptEvidenceHandle'})

    def test_large_registry_has_an_equivalent_finite_pattern(self):
        import re
        paper={'blocks':[{'id':str(i),'text':f'Distinct observation {i}.'} for i in range(951)]}
        registry=build_registry(paper)
        schema=constrain_manuscript_handles(with_evidence_handles(REVIEW),registry)
        pattern=schema['$defs']['manuscriptEvidenceHandle']['pattern']
        self.assertTrue(all(re.fullmatch(pattern,h.handle_id) for h in registry.handles))
        self.assertIsNone(re.fullmatch(pattern,'mh-'+'0'*24))

    def test_invalid_advisory_coverage_does_not_discard_valid_findings(self):
        from peerreviewagents.engine.discovery import coverage_record
        registry=build_registry(self.paper)
        handle=registry.handles[0].handle_id
        raw={'findings':[{'anchor_handle':handle,'manuscript_evidence_handles':[handle]}],
             'coverage_checks':[{'area':'measurement','status':'examined','reason':'Checked the source.',
                                 'evidence':[{'evidence_handle':'mh-unknown'}]}]}
        result=hydrate_review_response(raw,registry)
        self.assertEqual(result['response']['findings'][0]['block_id'],'b0022')
        row=result['response']['coverage_checks'][0]
        self.assertEqual(row['status'],'unresolved')
        self.assertEqual(row['evidence'],[])
        self.assertEqual(result['audit']['quarantined_coverage_records'][0]['original_record'],raw['coverage_checks'][0])
        checked=coverage_record(self.paper,result['response'])
        self.assertEqual(next(a for a in checked['areas'] if a['area']=='measurement')['validation'],'invalid')
        raw['findings'][0]['anchor_handle']='mh-unknown'
        with self.assertRaisesRegex(ValueError,'Unknown evidence handle'):
            hydrate_review_response(raw,registry)

    def test_overlapping_occurrences_are_not_unique_anchors(self):
        from peerreviewagents.engine.evidence_handles import _make_handle
        handle=_make_handle('manuscript','b','aaa',0,2,digest('aaa'))
        self.assertFalse(handle.anchor_eligible)

    def test_long_spans_are_bounded_for_existing_validators(self):
        text = 'A ' + ('longword ' * 80) + 'ending.'
        paper = {'blocks': [{'id': 'long', 'text': text, 'sha256': digest(text)}]}
        self.assertTrue(all(len(row['text']) <= 350
                            for row in model_catalog(build_registry(paper))))

    def test_nested_candidate_finding_schema_is_converted_by_shape(self):
        finding = copy.deepcopy(REVIEW['properties']['findings']['items'])
        schema = {'type': 'object', 'properties': {
            'secondary_candidates': {'type': 'array', 'items': finding},
        }, 'required': ['secondary_candidates'], 'additionalProperties': False}
        converted = with_evidence_handles(schema)
        candidate = converted['properties']['secondary_candidates']['items']
        self.assertIn('anchor_handle', candidate['required'])
        self.assertIn('manuscript_evidence_handles', candidate['required'])
        self.assertNotIn('quote', candidate['properties'])

    def test_dynamic_card_attachment_preserves_data_and_matches_final_registry(self):
        eligible = copy.deepcopy(self.health_card)
        eligible.update(
            provider='clinicaltrials',
            limitation='Identity metadata only. It does not establish scientific support.',
            fields={'status': 'completed'},
        )
        ineligible = {
            'evidence_id': 'source-unavailable',
            'eligible': False,
            'evidence_text': '',
            'status': 'version_unavailable',
            'limitation': 'No eligible historical record was established.',
        }
        original = {'result': {'source_card': eligible}, 'alternatives': [ineligible]}
        before = copy.deepcopy(original)
        attached = Investigation.attach_handles(original)

        self.assertEqual(original, before)
        self.assertEqual(attached['result']['source_card']['fields'], eligible['fields'])
        self.assertEqual(attached['result']['source_card']['limitation'], eligible['limitation'])
        self.assertEqual(attached['alternatives'][0]['status'], ineligible['status'])
        self.assertEqual(attached['alternatives'][0]['evidence_handles'], [])

        exposed = attached['result']['source_card']['evidence_handles']
        registry = build_registry(self.paper, [eligible, ineligible])
        final_ids = {row['handle_id'] for row in model_catalog(registry)}
        self.assertTrue(exposed)
        self.assertTrue(all(row['handle_id'] in final_ids for row in exposed))
        self.assertTrue(all(row['source_kind'] == 'external' for row in exposed))
        self.assertTrue(all(row['anchor_eligible'] is False for row in exposed))
        self.assertTrue(all(set(row) == {
            'handle_id', 'source_kind', 'source_id', 'text', 'anchor_eligible'
        } for row in exposed))


if __name__ == '__main__':
    unittest.main()

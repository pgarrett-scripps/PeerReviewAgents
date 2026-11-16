"""Result linkage, comparability and source validation checks.

A node of the map is a position in ``paper.md`` — a document and a line range —
rather than a block id, so every assertion here asks where a display is and
what the line that cites it is linked to.
"""
import unittest
from pathlib import Path
from tempfile import mkdtemp
from shutil import rmtree

from manuscripts import build_base
from peerreviewagents.engine.result_map import (
    build_result_map,
    compare_observations,
    inspect_results,
)

#: The manuscript every test starts from: a claim citing two displays, the
#: figure caption that names a different model class, the table it points at, a
#: reference the map must ignore, a supplement citation with nothing to link to,
#: and two estimates to compare.
PIECES = [
    {'kind': 'paragraph', 'text': 'The result was significant (Fig 3 and Table 2).'},
    {'kind': 'caption', 'text': 'Fig 3 The line is from the SLR model (Table 2g).'},
    {'kind': 'table', 'table': [['Table 2 Multiple regression models'],
                                ['Diversity', '1.6763']]},
    {'kind': 'reference', 'text': '2019. Figure 8 in another publication.'},
    {'kind': 'paragraph', 'text': 'A claim refers to Table S1.'},
    {'kind': 'paragraph', 'text': 'The estimate is 1.2.'},
    {'kind': 'paragraph', 'text': 'The estimate is 2.4.'},
]


class ResultMapTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(mkdtemp())
        self.addCleanup(rmtree, self.root, ignore_errors=True)
        self.base = self.manuscript()

    def manuscript(self, extra=(), name='manuscript'):
        return build_base(self.root / name, list(PIECES) + list(extra))

    def line_of(self, needle, base=None):
        """The line of ``paper.md`` this text is printed on."""
        lines = (base or self.base).lines['paper.md']
        for number, text in enumerate(lines, 1):
            if needle in text:
                return number
        raise AssertionError(needle + ' is not in the manuscript')

    def where(self, node):
        return (node['document'], node['line'])

    def test_map_links_claims_and_labels_without_equating_models(self):
        caption, table = self.line_of('Fig 3 The line'), self.line_of('Multiple regression')
        result = build_result_map(self.base)
        self.assertEqual(len(result['nodes']), 4)
        edge = next(e for e in result['edges'] if e['from'] == caption)
        self.assertEqual(edge['to'], [{'document': 'paper.md', 'line': table}])
        self.assertEqual(result['checks'][0]['status'], 'needs_comparability_check')
        self.assertFalse(result['checks'][0]['scientifically_verified'])
        self.assertEqual(result['coverage']['unresolved_links'], 1)
        self.assertFalse(result['coverage']['figure_pixels_transcribed'])
        view = inspect_results(result, caption)
        self.assertEqual(self.where(view['nodes'][0]), ('paper.md', caption))
        self.assertIn(('paper.md', table), [self.where(n) for n in view['nodes']])
        # A line that cites no display is a line to read, not a node.
        self.assertEqual(inspect_results(result, self.line_of('estimate is 1.2'))['nodes'], [])

    def test_a_line_inside_a_table_is_the_table_it_belongs_to(self):
        """A reviewer citing the row of a table is asking about the table."""
        table = self.line_of('Multiple regression')
        result = build_result_map(self.base)
        self.assertEqual(self.where(inspect_results(result, self.line_of('1.6763'))['nodes'][0]),
                         ('paper.md', table))

    def observations(self):
        common = dict(metric='slope', outcome='diversity', model='SLR',
                      population='all transects', denominator='not applicable',
                      contrast='per unit abundance', timepoint='full season',
                      units='species', scale='raw')
        return [dict(common, line=self.line_of('estimate is 1.2'),
                     quote='The estimate is 1.2.', value='1.2'),
                dict(common, line=self.line_of('estimate is 2.4'),
                     quote='The estimate is 2.4.', value='2.4')]

    def test_numeric_difference_needs_reconciliation_not_endorsement(self):
        result = compare_observations(self.base, self.observations())
        self.assertEqual(result['status'], 'needs_reconciliation')
        self.assertFalse(result['scientifically_verified'])
        self.assertEqual(result, compare_observations(self.base, self.observations()))

    def test_model_units_population_and_scale_prevent_false_equivalence(self):
        for field in ('model', 'units', 'population', 'denominator', 'contrast', 'scale',
                      'metric', 'timepoint', 'outcome'):
            values = self.observations()
            values[1][field] = 'different'
            result = compare_observations(self.base, values)
            self.assertEqual(result['status'], 'different_context')
            self.assertEqual(result['different_dimensions'], [field])
        values = self.observations()
        values[1]['model'] = 'unknown'
        self.assertEqual(compare_observations(self.base, values)['status'], 'incomplete_context')

    def test_wrong_anchor_and_substring_value_rejected(self):
        wrong = [('line', self.line_of('estimate is 2.4')),
                 ('quote', 'The estimate was 1.2.'),
                 ('value', '1'),
                 ('document', 'supplement-nothing.md')]
        for field, value in wrong:
            observations = self.observations()
            observations[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                compare_observations(self.base, observations)

    def test_duplicate_display_ids_remain_ambiguous(self):
        second = self.manuscript([{'kind': 'caption',
                                   'text': 'Table 2 A second extracted caption.'}], 'twice')
        result = build_result_map(second)
        self.assertEqual(result['coverage']['ambiguous_definitions'], ['table-2'])
        edge = next(e for e in result['edges']
                    if e['from'] == self.line_of('Fig 3 The line', second))
        self.assertEqual([one['line'] for one in edge['to']],
                         [self.line_of('Multiple regression', second),
                          self.line_of('A second extracted caption', second)])

    def test_supplement_reference_and_paragraph_are_not_false_caption(self):
        more = self.manuscript([{'kind': 'paragraph', 'text': 'Table 2 confirms the effect.'},
                                {'kind': 'caption', 'text': 'S1 Table Additional counts.'}],
                               'supplemented')
        result = build_result_map(more)
        self.assertEqual(result['coverage']['ambiguous_definitions'], [])
        edge = next(e for e in result['edges'] if e['from'] == self.line_of('Table S1', more))
        self.assertEqual([one['line'] for one in edge['to']],
                         [self.line_of('Additional counts', more)])


if __name__ == '__main__':
    unittest.main()

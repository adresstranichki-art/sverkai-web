import json
import re
import subprocess
import unittest
from pathlib import Path


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / 'static' / 'index.html'
        ).read_text(encoding='utf-8')

    def test_window_inputs_allow_120_days(self):
        for input_id in ('cfg-dw-pay', 'cfg-dw-del'):
            tag = re.search(
                rf'<input[^>]+id="{input_id}"[^>]+>',
                self.html,
            )
            self.assertIsNotNone(tag)
            self.assertIn('min="0"', tag.group(0))
            self.assertIn('max="120"', tag.group(0))

    def test_window_inputs_explain_and_enforce_the_maximum(self):
        self.assertGreaterEqual(
            self.html.count('Максимально допустимое окно — 120 дней'),
            2,
        )
        self.assertIn('id="cfg-dw-pay-error"', self.html)
        self.assertIn('id="cfg-dw-del-error"', self.html)
        self.assertIn('const MAX_RECON_WINDOW_DAYS=120', self.html)
        self.assertIn('function validateWindowInput', self.html)
        self.assertIn('input.value=MAX_RECON_WINDOW_DAYS', self.html)
        self.assertIn("input.addEventListener('input'", self.html)

    def test_expert_summary_shows_completeness_line(self):
        self.assertIn('report.completeness', self.html)
        self.assertIn('Проверено строк', self.html)
        self.assertIn('сопоставлено', self.html)
        self.assertIn('в расхождениях', self.html)

    def test_independent_expert_contract_is_rendered(self):
        self.assertIn("result_mode==='independent_expert'", self.html)
        self.assertIn('expert_report', self.html)
        self.assertIn('Источник не найден', self.html)
        self.assertIn('confirmed_missing', self.html)
        self.assertIn('opening_balance_bridge', self.html)
        self.assertIn('evidence.document', self.html)
        self.assertIn('evidence.amount', self.html)

    def test_independent_summary_uses_compact_layout_and_grouped_findings(self):
        self.assertIn('class="expert-summary-layout"', self.html)
        self.assertNotIn('expert-summary-metrics', self.html)
        self.assertIn('class="expert-summary-conclusion"', self.html)
        self.assertIn('const EXPERT_CATEGORY_ORDER=', self.html)
        self.assertIn('expert-group-heading', self.html)
        self.assertIn('openDiscrepancy', self.html)
        self.assertNotIn('Подтверждено / к проверке', self.html)
        self.assertNotIn('Рекомендации и ограничения', self.html)
        self.assertNotIn('report.actions', self.html)
        self.assertNotIn('report.limitations', self.html)

    def test_independent_expert_findings_use_document_influence_explanation_columns(self):
        renderer = re.search(
            r'function renderIndependentExpertRows\(items\)\{.*?\n\}',
            self.html,
            re.S,
        )
        self.assertIsNotNone(renderer)
        self.assertIn('<div>Документ</div>', renderer.group(0))
        self.assertIn('<div>Влияние</div>', renderer.group(0))
        self.assertIn('<div>Пояснение</div>', renderer.group(0))
        self.assertIn('expertDocumentLabel(item.source_documents)', renderer.group(0))
        self.assertIn('item.reason', renderer.group(0))
        self.assertIn('item.evidence_warning', renderer.group(0))
        self.assertIn(
            'source_documents:[...new Set(resolved.map(evidence=>evidence.document).filter(Boolean))]',
            self.html,
        )

    def test_expert_document_label_handles_pairs_duplicates_and_missing_source(self):
        function = re.search(
            r'function expertDocumentLabel\(documents\)\{.*?\n\}',
            self.html,
            re.S,
        )
        self.assertIsNotNone(function)
        script = (
            function.group(0)
            + '\nconst result=['
            + 'expertDocumentLabel(["Акт 1"]),'
            + 'expertDocumentLabel(["Акт 1","Акт 2","Акт 1"]),'
            + 'expertDocumentLabel([])];'
            + 'process.stdout.write(JSON.stringify(result));'
        )
        completed = subprocess.run(
            ['node', '-e', script],
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        self.assertEqual(
            json.loads(completed.stdout),
            ['Акт 1', 'Акт 1 ↔ Акт 2', 'Документ не определён'],
        )

    def test_legacy_expert_results_remain_supported(self):
        self.assertIn("result_mode==='balance_reason_analysis'", self.html)
        self.assertIn('balance_reason_analysis', self.html)

    def test_discrepancy_table_uses_priority_column_label(self):
        self.assertIn('>Приоритет</th>', self.html)
        self.assertNotIn('>Уровень</th>', self.html)

    def test_discrepancy_table_has_no_document_column(self):
        header = re.search(
            r'<tr id="disc-thead-row">(?P<cells>.*?)</tr>',
            self.html,
            re.S,
        )
        self.assertIsNotNone(header)
        self.assertEqual(header.group('cells').count('<th'), 7)
        self.assertNotIn('>Документ</th>', header.group('cells'))
        self.assertIn('colspan="7"', self.html)
        self.assertNotIn('colspan="8"', self.html)

    def test_expert_categories_are_normalized_to_programmatic_types(self):
        self.assertIn('function standardTypeForExpertItem', self.html)
        self.assertIn("sign_difference:'sign_mismatch'", self.html)
        self.assertIn("amount_difference:'amount_diff'", self.html)
        self.assertIn("likely_date_pair:'date_diff'", self.html)
        self.assertIn("expert_category:item.category", self.html)
        self.assertIn("return sides.has('doc2')?'missing_in_company':'missing_in_counterparty'", self.html)

    def test_expert_discrepancy_filters_use_normalized_types(self):
        self.assertIn('id="fb-opening_balance_bridge"', self.html)
        self.assertIn('id="fb-ambiguous"', self.html)
        self.assertIn('function syncExpertFilterButtons', self.html)
        self.assertIn('activeFilters.has(r.dataset.type)', self.html)

    def test_sign_label_and_opening_balance_highlight_are_standardized(self):
        self.assertNotIn('Зеркальная КСФ', self.html)
        self.assertNotIn('Зеркальный КСФ', self.html)
        self.assertNotIn('Зеркальные КСФ', self.html)
        self.assertIn('Односторонние операции', self.html)
        self.assertIn(
            'операции, которые у обеих организаций отражены на одной стороне '
            'учёта: Дт–Дт или Кт–Кт',
            self.html,
        )
        self.assertIn('Связь с начальным сальдо', self.html)
        self.assertIn("item.category==='opening_balance_bridge'?'expert'", self.html)

    def test_discrepancy_filters_use_native_checkboxes(self):
        filter_types = (
            'all',
            'missing_in_counterparty',
            'missing_in_company',
            'amount_diff',
            'sign_mismatch',
            'date_diff',
            'opening_balance_bridge',
            'ambiguous',
        )
        for filter_type in filter_types:
            control = re.search(
                rf'<label[^>]+data-filter-type="{filter_type}"[^>]*>'
                rf'(?P<body>.*?)</label>',
                self.html,
                re.S,
            )
            self.assertIsNotNone(control, filter_type)
            self.assertIn('type="checkbox"', control.group('body'))
            self.assertIn(
                f"onchange=\"toggleFilter('{filter_type}',this.checked)\"",
                control.group('body'),
            )

    def test_filter_selection_supports_partial_clear_and_select_all(self):
        function = re.search(
            r'function nextFilterSelection\(current,type,checked,available\)\{.*?\n\}',
            self.html,
            re.S,
        )
        self.assertIsNotNone(function)
        cases = [
            {'current': ['a', 'b', 'c'], 'type': 'b', 'checked': False},
            {'current': ['a', 'b'], 'type': 'all', 'checked': False},
            {'current': ['b'], 'type': 'all', 'checked': True},
        ]
        script = (
            function.group(0)
            + '\nconst available=["a","b","c"];'
            + f'const cases={json.dumps(cases)};'
            + 'const result=cases.map(c=>[...nextFilterSelection('
            + 'new Set(c.current),c.type,c.checked,available)]);'
            + 'process.stdout.write(JSON.stringify(result));'
        )
        completed = subprocess.run(
            ['node', '-e', script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            [['a', 'c'], [], ['a', 'b', 'c']],
        )

    def test_hidden_expert_filter_controls_are_not_displayed(self):
        self.assertIn(
            '.filter-btn[hidden] { display: none !important; }',
            self.html,
        )

    def test_new_result_resets_all_discrepancy_filters(self):
        self.assertIn(
            'setExpertModeTabs(expertMode);\n  resetDiscFilters();',
            self.html,
        )
        self.assertNotIn('if(expertMode)resetDiscFilters();', self.html)


if __name__ == '__main__':
    unittest.main()

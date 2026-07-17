import re
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

    def test_independent_expert_contract_is_rendered(self):
        self.assertIn("result_mode==='independent_expert'", self.html)
        self.assertIn('expert_report', self.html)
        self.assertIn('Источник не найден', self.html)
        self.assertIn('confirmed_missing', self.html)
        self.assertIn('opening_balance_bridge', self.html)
        self.assertIn('evidence.document', self.html)
        self.assertIn('evidence.amount', self.html)

    def test_legacy_expert_results_remain_supported(self):
        self.assertIn("result_mode==='balance_reason_analysis'", self.html)
        self.assertIn('balance_reason_analysis', self.html)


if __name__ == '__main__':
    unittest.main()

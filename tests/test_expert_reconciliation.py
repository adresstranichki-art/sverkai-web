import json
import types
import unittest

import pandas as pd

from expert_reconciliation import (
    build_expert_payload,
    resolve_expert_evidence,
    run_independent_expert_analysis,
)


def _valid_report(evidence=None):
    return {
        'version': '1',
        'conclusion': 'Найдена вероятная пара с разными датами.',
        'confidence': 'high',
        'balances': {
            'opening_difference': 3543.0,
            'period_movement': 0.0,
            'closing_difference': 3543.0,
            'formula': '3543 + 0 = 3543',
        },
        'totals': {
            'confirmed_count': 0,
            'confirmed_amount': 0.0,
            'review_count': 1,
        },
        'actions': ['Проверить даты отражения корректировки.'],
        'limitations': [],
        'discrepancies': [{
            'category': 'likely_date_pair',
            'title': 'Корректировка 7 070 руб.',
            'doc1_date': '21.01.2026',
            'doc2_date': '27.02.2026',
            'doc1_document': 'Корректировка 77',
            'doc2_document': 'Корректировка 77',
            'doc1_value': 7070.0,
            'doc2_value': 7070.0,
            'influence': 0.0,
            'reason': 'Сумма и документ совпадают, даты различаются.',
            'action': 'Проверить дату проведения.',
            'confidence': 'high',
            'evidence': evidence or [
                {'side': 'doc1', 'row_id': 'd1:r11'},
                {'side': 'doc2', 'row_id': 'd2:r22'},
            ],
        }],
    }


class _FakeMessages:
    def __init__(self, report):
        self.report = report
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text=json.dumps(self.report, ensure_ascii=False))],
            usage=types.SimpleNamespace(input_tokens=321, output_tokens=123),
        )


class ExpertReconciliationTests(unittest.TestCase):
    def _frames(self):
        df1 = pd.DataFrame([{
            'date': pd.Timestamp('2026-01-21'),
            'date_str': '21.01.2026',
            'document': 'Корректировка 77',
            'debit': 7070.0,
            'credit': None,
            'raw_row': 11,
        }])
        df2 = pd.DataFrame([{
            'date': pd.Timestamp('2026-02-27'),
            'date_str': '27.02.2026',
            'document': 'Корректировка 77',
            'debit': 7070.0,
            'credit': None,
            'raw_row': 22,
        }])
        df1.attrs.update(
            start_balance=12972.0,
            start_balance_side='debit',
            end_balance=28052.0,
            end_balance_side='debit',
        )
        df2.attrs.update(
            start_balance=9429.0,
            start_balance_side='credit',
            end_balance=4536.0,
            end_balance_side='debit',
        )
        return df1, df2

    def test_payload_contains_rows_and_balance_sides_without_standard_result(self):
        df1, df2 = self._frames()

        payload, row_index = build_expert_payload(df1, df2)

        self.assertEqual(payload['documents'][0]['rows'][0]['id'], 'd1:r11')
        self.assertEqual(payload['documents'][0]['opening_balance']['side'], 'debit')
        self.assertEqual(payload['documents'][1]['opening_balance']['side'], 'credit')
        self.assertNotIn('discrepancies', json.dumps(payload))
        self.assertIn(('doc1', 'd1:r11'), row_index)

    def test_invalid_evidence_keeps_report_with_warning(self):
        df1, df2 = self._frames()
        _, row_index = build_expert_payload(df1, df2)
        report = _valid_report([
            {'side': 'doc1', 'row_id': 'd1:r999'},
        ])
        report['discrepancies'][0].update({
            'doc1_document': 'Несуществующая операция',
            'doc1_value': 999.0,
        })

        result, warnings = resolve_expert_evidence(report, row_index)

        self.assertEqual(result['status'], 'complete_with_warnings')
        self.assertEqual(len(result['report']['discrepancies']), 1)
        self.assertFalse(result['report']['discrepancies'][0]['clickable'])
        self.assertTrue(warnings)

    def test_expert_analysis_uses_one_structured_output_call(self):
        df1, df2 = self._frames()
        messages = _FakeMessages(_valid_report())
        client = types.SimpleNamespace(messages=messages)

        result = run_independent_expert_analysis(df1, df2, client, 'claude-sonnet-test')

        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(messages.calls), 1)
        call = messages.calls[0]
        self.assertEqual(call['model'], 'claude-sonnet-test')
        self.assertEqual(call['max_tokens'], 2200)
        self.assertEqual(call['output_config']['format']['type'], 'json_schema')
        sent_payload = json.loads(call['messages'][0]['content'])
        self.assertNotIn('summary', sent_payload)
        self.assertNotIn('discrepancies', sent_payload)
        self.assertEqual(result['usage'], {'input_tokens': 321, 'output_tokens': 123})


if __name__ == '__main__':
    unittest.main()

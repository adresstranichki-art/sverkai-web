import json
import types
import unittest

import pandas as pd

from expert_reconciliation_v2 import (
    _assemble_expert_report,
    _row_influence,
    build_expert_payload,
    build_expert_review_request,
    build_expert_review_state,
    run_independent_expert_analysis,
)


def _review(**overrides):
    base = {
        'version': '2',
        'conclusion': 'Сальдо расходятся из-за отсутствующих операций.',
        'confidence': 'high',
        'pair_verdicts': [],
        'unmatched_verdicts': [],
        'cross_matches': [],
    }
    base.update(overrides)
    return base


class _FakeMessages:
    def __init__(self, review):
        self.review = review
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(
                text=json.dumps(self.review, ensure_ascii=False),
            )],
            usage=types.SimpleNamespace(input_tokens=321, output_tokens=123),
        )


def _add_row(df, date, document, debit=None, credit=None, raw_row=0):
    df.loc[len(df)] = {
        'date': pd.Timestamp(date),
        'date_str': pd.Timestamp(date).strftime('%d.%m.%Y'),
        'document': document,
        'debit': debit,
        'credit': credit,
        'raw_row': raw_row,
    }


class ExpertReconciliationTests(unittest.TestCase):
    def _frames(self):
        columns = ['date', 'date_str', 'document', 'debit', 'credit', 'raw_row']
        df1 = pd.DataFrame(columns=columns)
        df2 = pd.DataFrame(columns=columns)
        _add_row(df1, '2026-01-21', 'Корректировка 77', debit=7070.0, raw_row=11)
        _add_row(df2, '2026-02-27', 'Корректировка 77', credit=7070.0, raw_row=22)
        df1.attrs.update(
            start_balance=12972.0,
            start_balance_side='debit',
            end_balance=28052.0,
            end_balance_side='debit',
            display_name='ООО «Организация»',
        )
        df2.attrs.update(
            start_balance=9429.0,
            start_balance_side='credit',
            end_balance=4536.0,
            end_balance_side='debit',
            display_name='ООО «Контрагент»',
        )
        return df1, df2

    # ── программное сопоставление ────────────────────────────────────

    def test_equal_amounts_with_different_dates_become_date_pair(self):
        df1, df2 = self._frames()
        state = build_expert_review_state(df1, df2)
        self.assertEqual(list(state['pair_items']), ['p1'])
        pair = state['pair_items']['p1']
        self.assertEqual(pair['days'], 37)
        self.assertEqual(pair['doc1']['row_id'], 'd1:r11')
        self.assertEqual(pair['doc2']['row_id'], 'd2:r22')
        self.assertEqual(state['unmatched_items'], {})
        self.assertEqual(state['matched_row_count'], 2)

    def test_rows_without_amount_match_stay_unmatched(self):
        df1, df2 = self._frames()
        _add_row(df1, '2026-02-09', 'Поставка 90', debit=11845.0, raw_row=33)
        state = build_expert_review_state(df1, df2)
        self.assertEqual(list(state['unmatched_items']), ['u1'])
        self.assertEqual(state['unmatched_items']['u1']['row_id'], 'd1:r33')

    def test_min_amount_skips_small_unmatched_rows(self):
        df1, df2 = self._frames()
        _add_row(df1, '2026-02-09', 'Мелочь', debit=3.0, raw_row=44)
        state = build_expert_review_state(df1, df2, {'min_amount': 100})
        self.assertEqual(state['unmatched_items'], {})
        self.assertEqual(state['skipped_small_count'], 1)

    def test_disabled_date_search_treats_pairs_as_matched(self):
        df1, df2 = self._frames()
        state = build_expert_review_state(df1, df2, {'find_date_diff': False})
        self.assertEqual(state['pair_items'], {})
        self.assertEqual(state['matched_row_count'], 2)

    def test_row_influence_is_credit_minus_debit(self):
        self.assertEqual(_row_influence({'debit': 11845.0}), -11845.0)
        self.assertEqual(_row_influence({'credit': 3543.0}), 3543.0)

    # ── payload для Claude ───────────────────────────────────────────

    def test_review_request_contains_only_questionable_items(self):
        df1, df2 = self._frames()
        _add_row(df1, '2026-02-09', 'Поставка 90', debit=11845.0, raw_row=33)
        state = build_expert_review_state(
            df1, df2, {'date_window_payment': 5, 'date_window_delivery': 37},
        )
        request = build_expert_review_request(state)
        self.assertEqual(len(request['date_pairs']), 1)
        pair = request['date_pairs'][0]
        self.assertEqual(pair['id'], 'p1')
        self.assertEqual(pair['days'], 37)
        self.assertEqual(pair['window'], 37)
        self.assertTrue(pair['within_window'])
        self.assertEqual(
            [row['id'] for row in request['unmatched']], ['u1'],
        )
        for document in request['documents']:
            self.assertNotIn('rows', document)
        self.assertEqual(
            request['documents'][0]['display_name'], 'ООО «Организация»',
        )

    def test_payload_row_ids_remain_stable(self):
        df1, df2 = self._frames()
        payload, row_index = build_expert_payload(df1, df2)
        self.assertEqual(payload['documents'][0]['rows'][0]['id'], 'd1:r11')
        self.assertIn(('doc2', 'd2:r22'), row_index)
        self.assertEqual(
            payload['balance_comparison']['opening_difference'], -3543.0,
        )

    # ── сборка отчёта ────────────────────────────────────────────────

    def test_date_pair_within_window_has_zero_influence(self):
        df1, df2 = self._frames()
        state = build_expert_review_state(
            df1, df2, {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        report = _assemble_expert_report(state, _review(
            pair_verdicts=[{'id': 'p1', 'verdict': 'date_pair', 'note': 'Одна операция.'}],
        ))
        item = report['discrepancies'][0]
        self.assertEqual(item['category'], 'likely_date_pair')
        self.assertEqual(item['influence'], 0.0)
        self.assertEqual(
            {row['side'] for row in item['resolved_evidence']},
            {'doc1', 'doc2'},
        )

    def test_date_pair_beyond_window_is_ambiguous_even_if_claude_confirms(self):
        df1, df2 = self._frames()
        state = build_expert_review_state(df1, df2)  # окна по умолчанию 5/3
        report = _assemble_expert_report(state, _review(
            pair_verdicts=[{'id': 'p1', 'verdict': 'date_pair', 'note': 'Одна операция.'}],
        ))
        item = report['discrepancies'][0]
        self.assertEqual(item['category'], 'ambiguous')
        self.assertEqual(item['influence'], 0.0)
        self.assertIn('превышает допуск', item['reason'])

    def test_unrelated_pair_splits_into_two_missing_rows(self):
        df1, df2 = self._frames()
        state = build_expert_review_state(
            df1, df2, {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        report = _assemble_expert_report(state, _review(
            pair_verdicts=[{'id': 'p1', 'verdict': 'unrelated', 'note': 'Разные операции.'}],
        ))
        categories = [item['category'] for item in report['discrepancies']]
        self.assertEqual(categories, ['confirmed_missing', 'confirmed_missing'])
        influences = sorted(item['influence'] for item in report['discrepancies'])
        self.assertEqual(influences, [-7070.0, 7070.0])

    def test_unmatched_row_defaults_to_confirmed_missing(self):
        df1, df2 = self._frames()
        _add_row(df1, '2026-02-09', 'Поставка 90', debit=11845.0, raw_row=33)
        state = build_expert_review_state(
            df1, df2, {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        report = _assemble_expert_report(state, _review())
        missing = [
            item for item in report['discrepancies']
            if item['category'] == 'confirmed_missing'
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['influence'], -11845.0)
        self.assertEqual(missing[0]['resolved_evidence'][0]['row_id'], 'd1:r33')

    def test_unmatched_row_can_become_opening_balance_bridge(self):
        df1, df2 = self._frames()
        _add_row(df2, '2026-01-17', 'Корректировка продажи', credit=3543.0, raw_row=55)
        state = build_expert_review_state(
            df1, df2, {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        report = _assemble_expert_report(state, _review(
            unmatched_verdicts=[{
                'id': 'u1', 'verdict': 'opening_balance_bridge',
                'note': 'Закрывает начальное сальдо.',
            }],
        ))
        bridge = [
            item for item in report['discrepancies']
            if item['category'] == 'opening_balance_bridge'
        ]
        self.assertEqual(len(bridge), 1)
        self.assertEqual(bridge[0]['influence'], 3543.0)

    def test_cross_match_amount_difference_combines_both_rows(self):
        df1, df2 = self._frames()
        _add_row(df1, '2026-02-09', 'Поставка 90', debit=100.0, raw_row=33)
        _add_row(df2, '2026-02-10', 'Поставка 90', credit=90.0, raw_row=44)
        state = build_expert_review_state(
            df1, df2, {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        doc1_id = next(
            item_id for item_id, row in state['unmatched_items'].items()
            if row['side'] == 'doc1'
        )
        doc2_id = next(
            item_id for item_id, row in state['unmatched_items'].items()
            if row['side'] == 'doc2'
        )
        report = _assemble_expert_report(state, _review(
            cross_matches=[{
                'doc1_id': doc1_id, 'doc2_id': doc2_id,
                'category': 'amount_difference', 'note': 'Суммы различаются.',
            }],
        ))
        diff = [
            item for item in report['discrepancies']
            if item['category'] == 'amount_difference'
        ]
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]['influence'], -10.0)
        self.assertEqual(len(diff[0]['resolved_evidence']), 2)
        # обе строки использованы — как missing они больше не выводятся
        self.assertNotIn(
            'confirmed_missing',
            [item['category'] for item in report['discrepancies']],
        )

    def test_cross_match_with_unknown_id_is_ignored_and_logged(self):
        df1, df2 = self._frames()
        _add_row(df1, '2026-02-09', 'Поставка 90', debit=11845.0, raw_row=33)
        state = build_expert_review_state(
            df1, df2, {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        report = _assemble_expert_report(state, _review(
            cross_matches=[{
                'doc1_id': 'u1', 'doc2_id': 'u99',
                'category': 'amount_difference', 'note': '-',
            }],
        ))
        self.assertTrue(any(
            entry.startswith('cross_match_ignored')
            for entry in report['guard_log']
        ))
        # строка осталась в отчёте как отсутствующая
        self.assertEqual(
            report['discrepancies'][0]['category'], 'confirmed_missing',
        )

    def test_disabled_missing_search_hides_missing_rows(self):
        df1, df2 = self._frames()
        _add_row(df1, '2026-02-09', 'Поставка 90', debit=11845.0, raw_row=33)
        state = build_expert_review_state(df1, df2, {
            'find_missing': False,
            'date_window_payment': 60, 'date_window_delivery': 60,
        })
        report = _assemble_expert_report(state, _review())
        self.assertEqual(
            [item['category'] for item in report['discrepancies']],
            ['likely_date_pair'],
        )

    def test_report_is_sorted_missing_first(self):
        df1, df2 = self._frames()
        _add_row(df1, '2026-02-09', 'Поставка 90', debit=11845.0, raw_row=33)
        state = build_expert_review_state(
            df1, df2, {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        report = _assemble_expert_report(state, _review())
        self.assertEqual(
            [item['category'] for item in report['discrepancies']],
            ['confirmed_missing', 'likely_date_pair'],
        )

    def test_completeness_and_totals_are_derived(self):
        df1, df2 = self._frames()
        _add_row(df1, '2026-02-09', 'Поставка 90', debit=11845.0, raw_row=33)
        state = build_expert_review_state(
            df1, df2, {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        report = _assemble_expert_report(state, _review())
        self.assertEqual(report['completeness'], {
            'rows_total': 3,
            'rows_matched': 2,
            'rows_in_findings': 3,
        })
        self.assertEqual(report['totals'], {
            'confirmed_count': 1,
            'confirmed_amount': 11845.0,
            'review_count': 1,
        })
        self.assertEqual(report['balances']['opening_difference'], -3543.0)

    # ── запуск и вызов Claude ────────────────────────────────────────

    def test_single_structured_call_with_compact_payload(self):
        df1, df2 = self._frames()
        messages = _FakeMessages(_review(
            pair_verdicts=[{'id': 'p1', 'verdict': 'date_pair', 'note': '-'}],
        ))
        result = run_independent_expert_analysis(
            df1, df2, types.SimpleNamespace(messages=messages),
            'claude-sonnet-test',
            {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(messages.calls), 1)
        call = messages.calls[0]
        self.assertEqual(call['model'], 'claude-sonnet-test')
        self.assertEqual(call['max_tokens'], 4000)
        self.assertEqual(call['output_config']['format']['type'], 'json_schema')
        serialized = json.dumps(call['output_config']['format']['schema'])
        for unsupported in (
            'minimum', 'maximum', 'minLength', 'maxLength', 'maxItems',
        ):
            self.assertNotIn(f'"{unsupported}"', serialized)
        sent = json.loads(call['messages'][0]['content'])
        self.assertIn('date_pairs', sent)
        self.assertIn('balance_comparison', sent)
        self.assertEqual(result['usage'], {'input_tokens': 321, 'output_tokens': 123})

    def test_no_claude_call_when_everything_is_matched(self):
        df1, df2 = self._frames()
        df2.loc[0, 'date'] = pd.Timestamp('2026-01-21')
        df2.loc[0, 'date_str'] = '21.01.2026'
        messages = _FakeMessages(_review())
        result = run_independent_expert_analysis(
            df1, df2, types.SimpleNamespace(messages=messages),
            'claude-sonnet-test',
        )
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(messages.calls, [])
        self.assertEqual(result['report']['discrepancies'], [])
        self.assertEqual(result['usage'], {'input_tokens': 0, 'output_tokens': 0})
        self.assertTrue(result['report']['conclusion'])

    def test_missing_api_client_fails_fast(self):
        df1, df2 = self._frames()
        result = run_independent_expert_analysis(
            df1, df2, None, 'claude-sonnet-test',
        )
        self.assertEqual(result, {'status': 'failed', 'error': 'api_key_required'})

    def test_token_limit_returns_a_specific_failure(self):
        df1, df2 = self._frames()

        class TokenLimitedMessages:
            def create(self, **kwargs):
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text='{"version":')],
                    stop_reason='max_tokens',
                    usage=types.SimpleNamespace(
                        input_tokens=100, output_tokens=2200,
                    ),
                )

        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=TokenLimitedMessages()),
            'claude-sonnet-test',
        )
        self.assertEqual(result, {
            'status': 'failed',
            'error': 'max_tokens',
            'usage': {'input_tokens': 100, 'output_tokens': 2200},
        })

    def test_invalid_review_shape_fails(self):
        df1, df2 = self._frames()
        messages = _FakeMessages({'version': '2', 'oops': True})
        result = run_independent_expert_analysis(
            df1, df2, types.SimpleNamespace(messages=messages),
            'claude-sonnet-test',
        )
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'invalid_report_schema')


if __name__ == '__main__':
    unittest.main()

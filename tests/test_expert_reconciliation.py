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
        df1.attrs['display_name'] = 'ООО «ПРООПТ»'
        df2.attrs['display_name'] = 'ООО «Автомир-Трейд»'
        settings = {
            'find_missing': True,
            'find_amount_diff': False,
            'find_sign_mismatch': True,
            'find_date_diff': False,
            'date_window_payment': 45,
            'date_window_delivery': 60,
            'min_amount': 1000,
        }

        payload, row_index = build_expert_payload(df1, df2, settings)

        self.assertEqual(payload['documents'][0]['rows'][0]['id'], 'd1:r11')
        self.assertEqual(payload['documents'][0]['display_name'], 'ООО «ПРООПТ»')
        self.assertEqual(payload['documents'][1]['display_name'], 'ООО «Автомир-Трейд»')
        self.assertEqual(payload['analysis_scope'], settings)
        self.assertEqual(payload['documents'][0]['opening_balance']['side'], 'debit')
        self.assertEqual(payload['documents'][1]['opening_balance']['side'], 'credit')
        self.assertEqual(
            payload['balance_comparison']['opening_difference'],
            -3543.0,
        )
        self.assertEqual(
            payload['balance_comparison']['closing_difference'],
            -32588.0,
        )
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

    def test_resolved_evidence_contains_source_details_for_the_ui(self):
        df1, df2 = self._frames()
        _, row_index = build_expert_payload(df1, df2)

        result, warnings = resolve_expert_evidence(
            _valid_report(), row_index,
        )

        self.assertFalse(warnings)
        source = result['report']['discrepancies'][0]['resolved_evidence'][0]
        self.assertEqual(source['date'], '21.01.2026')
        self.assertEqual(source['document'], 'Корректировка 77')
        self.assertEqual(source['amount'], 7070.0)

    def test_expert_analysis_uses_one_structured_output_call(self):
        df1, df2 = self._frames()
        messages = _FakeMessages(_valid_report())
        client = types.SimpleNamespace(messages=messages)

        result = run_independent_expert_analysis(df1, df2, client, 'claude-sonnet-test')

        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(messages.calls), 1)
        call = messages.calls[0]
        self.assertEqual(call['model'], 'claude-sonnet-test')
        self.assertEqual(call['max_tokens'], 8000)
        self.assertEqual(call['output_config']['format']['type'], 'json_schema')
        sent_payload = json.loads(call['messages'][0]['content'])
        self.assertNotIn('summary', sent_payload)
        self.assertNotIn('discrepancies', sent_payload)
        self.assertEqual(result['usage'], {'input_tokens': 321, 'output_tokens': 123})

    def test_structured_output_schema_uses_only_supported_constraints(self):
        df1, df2 = self._frames()
        messages = _FakeMessages(_valid_report())
        client = types.SimpleNamespace(messages=messages)

        result = run_independent_expert_analysis(
            df1, df2, client, 'claude-sonnet-test',
        )

        self.assertEqual(result['status'], 'complete')
        sent_schema = messages.calls[0]['output_config']['format']['schema']
        serialized = json.dumps(sent_schema)
        for unsupported in (
            'minimum', 'maximum', 'minLength', 'maxLength', 'maxItems',
        ):
            self.assertNotIn(f'"{unsupported}"', serialized)

    def test_structured_report_does_not_repeat_source_row_fields(self):
        df1, df2 = self._frames()
        messages = _FakeMessages(_valid_report())

        run_independent_expert_analysis(
            df1, df2, types.SimpleNamespace(messages=messages),
            'claude-sonnet-test',
        )

        item_properties = (
            messages.calls[0]['output_config']['format']['schema']
            ['properties']['discrepancies']['items']['properties']
        )
        root_properties = (
            messages.calls[0]['output_config']['format']['schema']
            ['properties']
        )
        self.assertNotIn('totals', root_properties)
        self.assertNotIn('balances', root_properties)
        self.assertNotIn('actions', root_properties)
        self.assertNotIn('limitations', root_properties)
        for duplicate in (
            'doc1_date', 'doc2_date', 'doc1_document', 'doc2_document',
            'doc1_value', 'doc2_value', 'action',
        ):
            self.assertNotIn(duplicate, item_properties)
        self.assertIn('без ограничения количества', messages.calls[0]['system'].lower())
        self.assertIn('balance_comparison уже рассчитан', messages.calls[0]['system'].lower())
        self.assertIn(
            'opening_balance_bridge — только операция',
            messages.calls[0]['system'].lower(),
        )
        self.assertIn(
            'likely_date_pair требует одинаковый модуль суммы',
            messages.calls[0]['system'].lower(),
        )
        self.assertIn(
            'отдельной записью',
            messages.calls[0]['system'].lower(),
        )
        system = messages.calls[0]['system'].lower()
        self.assertIn('analysis_scope', system)
        self.assertIn('display_name', system)
        self.assertIn('doc1', system)
        self.assertIn('если find_date_diff=false', system)
        self.assertIn('любые выводы о разнице дат запрещены', system)
        self.assertIn('conclusion — до 450 знаков', system)
        self.assertIn('title — до 90, reason — до 140 знаков', system)
        ordered_categories = (
            'confirmed_missing', 'sign_difference', 'amount_difference',
            'opening_balance_bridge', 'likely_date_pair', 'ambiguous',
        )
        positions = [system.index(category) for category in ordered_categories]
        self.assertEqual(positions, sorted(positions))

    def test_disabled_categories_are_repeated_in_the_run_instruction(self):
        df1, df2 = self._frames()
        messages = _FakeMessages(_valid_report())

        run_independent_expert_analysis(
            df1, df2, types.SimpleNamespace(messages=messages),
            'claude-sonnet-test',
            {'find_date_diff': False, 'find_amount_diff': False},
        )

        system = messages.calls[0]['system'].lower()
        self.assertIn(
            'разрешены=confirmed_missing,sign_difference,opening_balance_bridge,ambiguous',
            system,
        )
        self.assertIn('запрещены=amount_difference,likely_date_pair', system)
        self.assertIn('запрещённые категории и их темы не упоминай ни в одном поле', system)

    def test_prompt_uses_standard_user_facing_terms(self):
        df1, df2 = self._frames()
        messages = _FakeMessages(_valid_report())

        run_independent_expert_analysis(
            df1, df2, types.SimpleNamespace(messages=messages),
            'claude-sonnet-test',
        )

        system = messages.calls[0]['system'].lower()
        for term in (
            'нет у контрагента',
            'нет у организации',
            'разница в суммах',
            'разница в датах',
            'зеркальный ксф',
            'связь с начальным сальдо',
            'требуется проверка',
        ):
            self.assertIn(term, system)
        self.assertIn('conclusion, title и reason', system)
        self.assertIn('не используй альтернативные названия типов', system)

    def test_report_is_sorted_by_category_then_absolute_influence(self):
        df1, df2 = self._frames()
        report = _valid_report()
        report['discrepancies'] = [
            {
                'category': category,
                'title': category,
                'influence': influence,
                'reason': category,
                'confidence': 'high',
                'evidence': [{'side': 'doc1', 'row_id': f'd1:rx{influence}'}],
            }
            for category, influence in (
                ('ambiguous', 100),
                ('amount_difference', 200),
                ('confirmed_missing', 300),
                ('likely_date_pair', 0),
                ('amount_difference', -500),
                ('opening_balance_bridge', 50),
                ('sign_difference', 250),
            )
        ]

        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
            {'find_date_diff': False},
        )

        items = result['report']['discrepancies']
        self.assertEqual(
            [(item['category'], item['influence']) for item in items],
            [
                ('confirmed_missing', 300),
                ('sign_difference', 250),
                ('amount_difference', -500),
                ('amount_difference', 200),
                ('opening_balance_bridge', 50),
                ('likely_date_pair', 0),
                ('ambiguous', 100),
            ],
        )

    def test_report_totals_are_derived_from_discrepancies(self):
        df1, df2 = self._frames()
        df1.loc[len(df1)] = {
            'date': pd.Timestamp('2026-02-09'),
            'date_str': '09.02.2026',
            'document': 'Adjustment 88',
            'debit': 11845.0,
            'credit': None,
            'raw_row': 33,
        }
        report = _valid_report()
        report.pop('totals')
        report['discrepancies'].append({
            'category': 'confirmed_missing',
            'title': 'Отсутствующая корректировка',
            'influence': -11845.0,
            'reason': 'Нет зеркальной операции.',
            'confidence': 'high',
            'evidence': [{'side': 'doc1', 'row_id': 'd1:r33'}],
        })

        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
        )

        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['report']['totals'], {
            'confirmed_count': 1,
            'confirmed_amount': 11845.0,
            'review_count': 1,
        })

    def test_report_balances_are_derived_from_source_sides(self):
        df1, df2 = self._frames()
        report = _valid_report()
        report.pop('balances')

        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
        )

        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['report']['balances'], {
            'opening_difference': -3543.0,
            'period_movement': -29045.0,
            'closing_difference': -32588.0,
            'formula': '-3 543,00 + -29 045,00 = -32 588,00',
        })

    def test_confirmed_missing_with_mirror_row_is_downgraded_to_date_pair(self):
        df1, df2 = self._frames()
        report = _valid_report([{'side': 'doc1', 'row_id': 'd1:r11'}])
        report.pop('balances')
        report.pop('totals')
        report['discrepancies'][0].update({
            'category': 'confirmed_missing',
            'influence': 7070.0,
            'reason': 'AI ошибочно решил, что пары нет.',
        })

        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
        )

        item = result['report']['discrepancies'][0]
        self.assertEqual(item['category'], 'likely_date_pair')
        self.assertEqual(item['influence'], 0.0)
        self.assertEqual(
            {source['side'] for source in item['resolved_evidence']},
            {'doc1', 'doc2'},
        )
        self.assertEqual(result['report']['totals'], {
            'confirmed_count': 0,
            'confirmed_amount': 0.0,
            'review_count': 1,
        })

    def test_disabled_date_scope_does_not_create_date_pair(self):
        df1, df2 = self._frames()
        report = _valid_report([{'side': 'doc1', 'row_id': 'd1:r11'}])
        report['discrepancies'][0].update({
            'category': 'confirmed_missing',
            'influence': 7070.0,
        })

        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
            {'find_date_diff': False},
        )

        self.assertEqual(
            result['report']['discrepancies'][0]['category'],
            'confirmed_missing',
        )

    def test_date_pair_influence_is_forced_to_zero(self):
        df1, df2 = self._frames()
        report = _valid_report()
        report['discrepancies'][0]['influence'] = -7070.0
        # даты 21.01 и 27.02 = 37 дней; окно должно позволять пару
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
            {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        item = result['report']['discrepancies'][0]
        self.assertEqual(item['category'], 'likely_date_pair')
        self.assertEqual(item['influence'], 0.0)
        self.assertIn('date_pair_influence_zeroed', result['report']['guard_log'])

    def test_date_pair_beyond_window_becomes_ambiguous(self):
        df1, df2 = self._frames()
        report = _valid_report()
        # окна по умолчанию 5/3 дня, разница 37 дней
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
        )
        item = result['report']['discrepancies'][0]
        self.assertEqual(item['category'], 'ambiguous')
        self.assertEqual(item['influence'], 0.0)
        self.assertIn('превышает допуск', item['reason'])
        self.assertIn('date_window_exceeded', result['report']['guard_log'])

    def test_date_pair_without_both_sides_becomes_ambiguous(self):
        df1, df2 = self._frames()
        report = _valid_report([{'side': 'doc1', 'row_id': 'd1:r11'}])
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
            {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        item = result['report']['discrepancies'][0]
        self.assertEqual(item['category'], 'ambiguous')
        self.assertIn('date_pair_demoted', result['report']['guard_log'])

    def test_confirmed_missing_influence_is_fixed_from_evidence(self):
        df1, df2 = self._frames()
        df1.loc[len(df1)] = {
            'date': pd.Timestamp('2026-02-09'),
            'date_str': '09.02.2026',
            'document': 'Поставка 90',
            'debit': 11845.0,
            'credit': None,
            'raw_row': 33,
        }
        report = _valid_report()
        report['discrepancies'] = [{
            'category': 'confirmed_missing',
            'title': 'Нет у контрагента',
            'influence': -999.0,
            'reason': 'Нет зеркальной операции.',
            'confidence': 'high',
            'evidence': [{'side': 'doc1', 'row_id': 'd1:r33'}],
        }]
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
        )
        item = result['report']['discrepancies'][0]
        self.assertEqual(item['influence'], -11845.0)
        self.assertIn('missing_influence_fixed', result['report']['guard_log'])

    def test_prompt_has_no_finding_limit_and_forbids_grouping(self):
        from expert_reconciliation import EXPERT_SYSTEM_PROMPT
        self.assertNotIn('не более 8', EXPERT_SYSTEM_PROMPT)
        self.assertNotIn('группируй', EXPERT_SYSTEM_PROMPT.replace('Не группируй', ''))
        self.assertIn('отдельной записью', EXPERT_SYSTEM_PROMPT)
        self.assertIn('conclusion не перечисляй отдельные расхождения', EXPERT_SYSTEM_PROMPT)

    def test_duplicate_rows_are_removed_after_reclassification(self):
        df1, df2 = self._frames()
        report = _valid_report()
        # Claude вернул одну и ту же строку в двух категориях (случай 4 559 руб.)
        report['discrepancies'] = [
            {
                'category': 'confirmed_missing',
                'title': 'Нет у контрагента',
                'influence': -7070.0,
                'reason': 'Первый вывод.',
                'confidence': 'high',
                'evidence': [{'side': 'doc1', 'row_id': 'd1:r11'}],
            },
            {
                'category': 'amount_difference',
                'title': 'Разница в суммах',
                'influence': -7070.0,
                'reason': 'Второй вывод о той же строке.',
                'confidence': 'medium',
                'evidence': [{'side': 'doc1', 'row_id': 'd1:r11'}],
            },
        ]
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
        )
        items = result['report']['discrepancies']
        self.assertEqual(len(items), 1)
        self.assertTrue(any(
            entry.startswith('duplicates_removed')
            for entry in result['report']['guard_log']
        ))

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


if __name__ == '__main__':
    unittest.main()

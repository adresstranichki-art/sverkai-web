import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_stubs() -> None:
    if 'anthropic' not in sys.modules:
        anthropic = types.ModuleType('anthropic')

        class Anthropic:
            def __init__(self, *args, **kwargs):
                pass

        anthropic.Anthropic = Anthropic
        sys.modules['anthropic'] = anthropic

    if 'fastapi' not in sys.modules:
        fastapi = types.ModuleType('fastapi')

        class FastAPI:
            def __init__(self, *args, **kwargs):
                pass

            def add_middleware(self, *args, **kwargs):
                pass

            def mount(self, *args, **kwargs):
                pass

            def __getattr__(self, name):
                def route(*args, **kwargs):
                    def deco(fn):
                        return fn
                    return deco
                return route

        class UploadFile:
            pass

        def File(*args, **kwargs):
            return None

        def Form(*args, **kwargs):
            return None

        class HTTPException(Exception):
            def __init__(self, status_code=None, detail=None):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        class Request:
            headers = {}

        fastapi.FastAPI = FastAPI
        fastapi.UploadFile = UploadFile
        fastapi.File = File
        fastapi.Form = Form
        fastapi.HTTPException = HTTPException
        fastapi.Request = Request
        sys.modules['fastapi'] = fastapi

        staticfiles = types.ModuleType('fastapi.staticfiles')

        class StaticFiles:
            def __init__(self, *args, **kwargs):
                pass

        staticfiles.StaticFiles = StaticFiles
        sys.modules['fastapi.staticfiles'] = staticfiles

        responses = types.ModuleType('fastapi.responses')

        class JSONResponse(dict):
            def __init__(self, content=None, *args, **kwargs):
                self.content = content

        class FileResponse:
            def __init__(self, *args, **kwargs):
                pass

        responses.JSONResponse = JSONResponse
        responses.FileResponse = FileResponse
        sys.modules['fastapi.responses'] = responses

        cors = types.ModuleType('fastapi.middleware.cors')

        class CORSMiddleware:
            pass

        cors.CORSMiddleware = CORSMiddleware
        sys.modules['fastapi.middleware.cors'] = cors


class RegressionReconcileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stubs()
        repo_root = Path(__file__).resolve().parents[1]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        cls.main = importlib.import_module('main')
        cls.cfg = {
            'find_missing': True,
            'find_amount_diff': True,
            'find_date_diff': True,
            'find_sign_mismatch': True,
            'date_window_payment': 5,
            'date_window_delivery': 3,
            'min_amount': 0,
            'ai_comment': False,
        }

    def _run_case(self, file1: str, file2: str):
        logs = []
        _, candidates1 = self.main._collect_parse_candidates(file1, logs, '')
        _, candidates2 = self.main._collect_parse_candidates(file2, logs, '')
        best = self.main._select_best_candidate_pair(candidates1, candidates2, self.cfg, logs)
        return best['cand1'], best['cand2'], best['result']

    def test_proopt_parser_handles_sequence_column_before_date(self):
        pd = self.main.pd
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sequence_act.xlsx'
            rows = [['' for _ in range(8)] for _ in range(15)]
            rows[0][1] = 'Акт сверки взаиморасчетов'
            rows[2][1] = 'взаимных расчетов за период с 01.03.2026 по 31.03.2026'
            rows[8][1] = 'По данным ООО "М Партс", руб.'
            rows[8][7] = 'По данным ООО "ПРООПТ", руб.'
            rows[9][1] = '№ п/п'
            rows[9][2] = 'Дата операции'
            rows[9][3] = 'Наименование операции, документы'
            rows[9][5] = 'Дебет'
            rows[9][6] = 'Кредит'
            rows[10][3] = 'Сальдо начальное'
            rows[10][5] = 100
            rows[11][1] = 1
            rows[11][2] = '01.03.2026'
            rows[11][3] = 'Реализация товаров МПр-1 от 01.03.2026'
            rows[11][5] = 50
            rows[12][1] = 2
            rows[12][2] = '02.03.2026'
            rows[12][3] = 'Строка выписки приход МП-1 от 02.03.2026'
            rows[12][6] = 20
            rows[13][3] = 'Обороты за период'
            rows[13][5] = 50
            rows[13][6] = 20
            rows[14][3] = 'Сальдо конечное'
            rows[14][5] = 130
            pd.DataFrame(rows).to_excel(path, header=False, index=False)

            logs = []
            _, candidates = self.main._collect_parse_candidates(str(path), logs, '', path.name)
            self.assertEqual(candidates[0]['parser_id'], 'proopt')
            df = candidates[0]['df']
            self.assertEqual(len(df), 2)
            self.assertEqual(df.iloc[0]['date_str'], '01.03.2026')
            self.assertEqual(df.iloc[0]['document'], 'Реализация товаров МПр-1 от 01.03.2026')
            self.assertAlmostEqual(df.iloc[0]['debit'], 50.0, places=2)
            self.assertAlmostEqual(df.iloc[1]['credit'], 20.0, places=2)
            self.assertAlmostEqual(df['signed_amount'].sum(), 30.0, places=2)

    def test_ai_profile_skipped_for_confident_structured_parse(self):
        pd = self.main.pd
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'confident_sequence_act.xlsx'
            rows = [['' for _ in range(8)] for _ in range(15)]
            rows[0][1] = 'Акт сверки взаиморасчетов'
            rows[8][1] = 'По данным ООО "М Партс", руб.'
            rows[8][7] = 'По данным ООО "ПРООПТ", руб.'
            rows[9][1] = '№ п/п'
            rows[9][2] = 'Дата операции'
            rows[9][3] = 'Наименование операции, документы'
            rows[9][5] = 'Дебет'
            rows[9][6] = 'Кредит'
            rows[11][1] = 1
            rows[11][2] = '01.03.2026'
            rows[11][3] = 'Реализация товаров МПр-1 от 01.03.2026'
            rows[11][5] = 50
            rows[12][1] = 2
            rows[12][2] = '02.03.2026'
            rows[12][3] = 'Строка выписки приход МП-1 от 02.03.2026'
            rows[12][6] = 20
            pd.DataFrame(rows).to_excel(path, header=False, index=False)

            calls = []
            orig_key = self.main.ANTHROPIC_API_KEY
            orig_detect = self.main.claude_detect_columns
            orig_load_cache = self.main._load_profile_cache
            orig_save_cache = self.main._save_profile_cache
            self.main.ANTHROPIC_API_KEY = 'sk-ant-test'
            self.main.claude_detect_columns = lambda *_: calls.append(True) or None
            self.main._load_profile_cache = lambda: {}
            self.main._save_profile_cache = lambda *_: None
            try:
                logs = []
                _, candidates = self.main._collect_parse_candidates(str(path), logs, '', path.name)
            finally:
                self.main.ANTHROPIC_API_KEY = orig_key
                self.main.claude_detect_columns = orig_detect
                self.main._load_profile_cache = orig_load_cache
                self.main._save_profile_cache = orig_save_cache

            self.assertFalse(calls)
            self.assertEqual(candidates[0]['parser_id'], 'proopt')

    def test_ai_profile_runs_when_structured_parsers_find_no_operations(self):
        pd = self.main.pd
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ai_needed_act.xlsx'
            rows = [['' for _ in range(6)] for _ in range(10)]
            rows[0][0] = 'Акт сверки взаиморасчетов'
            rows[1][0] = 'Дата'
            rows[1][2] = 'Документ'
            rows[1][4] = 'Дебет'
            rows[2][0] = '01.03.2026'
            rows[2][2] = 'Реализация товаров МПр-1 от 01.03.2026'
            rows[2][4] = 50
            rows[3][0] = '02.03.2026'
            rows[3][2] = 'Реализация товаров МПр-2 от 02.03.2026'
            rows[3][4] = 70
            pd.DataFrame(rows).to_excel(path, header=False, index=False)

            calls = []
            profile = {
                'data_start_row': 2,
                'date_col': 0,
                'doc_col': 2,
                'doc_num_col': None,
                'doc_type_col': None,
                'debit_col': 4,
                'credit_col': None,
                'amount_col': None,
                'amount_sign': 'unknown',
                'footer_keywords': ['Обороты за период', 'Сальдо конечное'],
                'confidence': 'high',
            }

            orig_key = self.main.ANTHROPIC_API_KEY
            orig_detect = self.main.claude_detect_columns
            orig_load_cache = self.main._load_profile_cache
            orig_save_cache = self.main._save_profile_cache
            self.main.ANTHROPIC_API_KEY = 'sk-ant-test'
            self.main.claude_detect_columns = lambda *_: calls.append(True) or profile
            self.main._load_profile_cache = lambda: {}
            self.main._save_profile_cache = lambda *_: None
            try:
                logs = []
                _, candidates = self.main._collect_parse_candidates(str(path), logs, '', path.name)
            finally:
                self.main.ANTHROPIC_API_KEY = orig_key
                self.main.claude_detect_columns = orig_detect
                self.main._load_profile_cache = orig_load_cache
                self.main._save_profile_cache = orig_save_cache

            ai_candidates = [c for c in candidates if c['parser_id'] == 'ai_profile_high']
            self.assertTrue(calls)
            self.assertEqual(len(ai_candidates), 1)
            self.assertEqual(len(ai_candidates[0]['df']), 2)

    def test_balance_state_vs_two_sided(self):
        cand1, cand2, result = self._run_case(
            'tmp_analysis1/balance_248098_10.xls',
            'tmp_analysis1/act_667.xlsx',
        )
        self.assertEqual(cand1['label'], 'Акт сверки (сальдо по операциям)')
        self.assertEqual(cand2['label'], 'Акт сверки (двусторонний)')
        self.assertEqual(len(result['discrepancies']), 3)
        self.assertEqual(result['summary']['critical_count'], 3)
        self.assertEqual(result['summary']['technical_mirror_count'], 0)
        self.assertEqual(
            sum(1 for d in result['discrepancies'] if d['type'] == 'sign_mismatch'),
            2,
        )
        self.assertAlmostEqual(result['summary']['net_period'], 27199.0, places=2)
        self.assertAlmostEqual(result['summary']['closing_balance_difference'], 27199.0, places=2)

    def test_proopt_period_difference_case(self):
        cand1, cand2, result = self._run_case(
            'tmp_analysis/proopt.xls',
            'tmp_analysis/counterparty.xls',
        )
        self.assertEqual(cand1['label'], 'Акт сверки (двусторонний)')
        self.assertEqual(cand2['label'], 'Акт сверки (двусторонний)')
        self.assertEqual(len(result['discrepancies']), 50)
        self.assertAlmostEqual(result['summary']['opening_balance_difference'], 43163.54, places=2)
        self.assertAlmostEqual(result['summary']['closing_balance_difference'], 23934.04, places=2)
        hint = result['summary'].get('window_suggestion')
        self.assertIsNotNone(hint)
        self.assertGreaterEqual(hint['candidate_pairs'], 5)
        self.assertEqual(hint['current_delivery_window'], 3)
        self.assertGreater(hint['recommended_delivery_window'], hint['current_delivery_window'])

    def test_proopt_registry_case(self):
        cand1, cand2, result = self._run_case(
            'tmp_analysis3/proopt12.xls',
            'tmp_analysis3/counterparty1014.xlsx',
        )
        self.assertEqual(cand1['label'], 'Акт сверки (реестр проводок)')
        self.assertEqual(cand2['label'], 'Акт сверки (двусторонний)')
        self.assertEqual(len(result['discrepancies']), 8)
        self.assertAlmostEqual(abs(result['summary']['net_period']), 16414.48, places=2)

    def test_exxe_case(self):
        cand1, cand2, result = self._run_case(
            'tmp_regression/exxe_m221.xlsx',
            'tmp_regression/exxe_220.xlsx',
        )
        self.assertEqual(cand1['label'], 'Акт сверки (односторонний)')
        self.assertEqual(cand2['label'], 'Акт сверки (двусторонний)')
        self.assertEqual(len(result['discrepancies']), 3)
        self.assertEqual(result['summary']['critical_count'], 2)
        self.assertEqual(result['summary']['technical_mirror_count'], 5)
        self.assertEqual(result['summary']['total_discrepancies'], 3)
        self.assertEqual(sum(1 for d in result['discrepancies'] if d['type'] == 'technical_mirror'), 0)
        self.assertEqual(sum(1 for d in result['discrepancies'] if d['type'] == 'sign_mismatch'), 0)
        self.assertAlmostEqual(result['summary']['net_period'], 1436253.6, places=2)

    def test_prefixed_document_number_wins_over_plain_number_duplicate(self):
        pd = self.main.pd
        df1 = pd.DataFrame([
            {
                'date': pd.Timestamp('2026-03-01'),
                'date_str': '01.03.2026',
                'document': 'Корректировка М-156 от 01.03.2026',
                'doc_num': '156',
                'debit': 100.0,
                'credit': None,
                'raw_row': 10,
            },
        ])
        df2 = pd.DataFrame([
            {
                'date': pd.Timestamp('2026-03-01'),
                'date_str': '01.03.2026',
                'document': 'Платеж №156 от 01.03.2026',
                'doc_num': '156',
                'debit': 100.0,
                'credit': None,
                'raw_row': 20,
            },
            {
                'date': pd.Timestamp('2026-03-01'),
                'date_str': '01.03.2026',
                'document': 'Корректировка М-156 от 01.03.2026',
                'doc_num': '156',
                'debit': 100.0,
                'credit': None,
                'raw_row': 21,
            },
        ])

        result = self.main._reconcile_structured(
            df1, df2, 'generic_detected', 'generic_detected', None, lambda *_: None, self.cfg
        )

        self.assertIn(10, result['matched1'])
        self.assertIn(21, result['matched2'])
        self.assertIn(20, result['missing_rows2'])

    def test_pdf_table_parser_handles_numbered_rows_with_dates_in_document(self):
        table = [
            ['По данным ООО «МСН Телеком» руб.', None, None, None, 'По данным ООО "КОКОС" руб.', None, None, None],
            ['№ п/п', 'Наименование операции,\nдокументы', 'Дебет', 'Кредит', '№ п/п', 'Наименование операции,\nдокументы', 'Дебет', 'Кредит'],
            ['1', 'Сальдо на 01.01.2025', '0,00', '998,34', '', '', '', ''],
            ['2', 'Оплата (23.01.2025, №54)', '', '2 338,66', '', '', '', ''],
            ['3', 'Акт (31.01.2025, №1250101-0775)', '2 337,00', '', '', '', '', ''],
            ['34', 'Обороты за период', '103 030,40', '102 442,06', '', '', '', ''],
            ['35', 'Сальдо на 31.03.2026', '0,00', '410,00', '', '', '', ''],
        ]

        df = self.main._parse_pdf_tables_to_structured([table], side='left')

        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]['date_str'], '23.01.2025')
        self.assertEqual(df.iloc[0]['doc_num'], '54')
        self.assertAlmostEqual(df.iloc[0]['credit'], 2338.66, places=2)
        self.assertEqual(df.iloc[1]['doc_num'], '1250101-0775')
        self.assertAlmostEqual(df.iloc[1]['debit'], 2337.0, places=2)
        self.assertEqual(df.attrs.get('start_balance'), 998.34)
        self.assertEqual(df.attrs.get('end_balance'), 410.0)

    def test_pdf_table_parser_handles_date_column_layout(self):
        table = [
            ['По данным ООО "КОКОС ГРУПП", руб.', None, None, None, 'По данным ООО "МСН ТЕЛЕКОМ", руб.', None, None, None],
            ['Дата', 'Документ', 'Дебет', 'Кредит', 'Дата', 'Документ', 'Дебет', 'Кредит'],
            ['Сальдо начальное', '', '', '3 753,71', 'Сальдо начальное', '', '', '3 753,71'],
            ['01.01.25', 'Приход (124001-0816 от 30.04.2024)', '', '1 297,00', '', '', '', ''],
            ['23.01.25', 'Оплата (54 от 23.01.2025)', '2 338,66', '', '', '', '', ''],
            ['31.03.26', 'Сальдо конечное', '1 778,23', '', '', '', '', ''],
        ]

        df = self.main._parse_pdf_tables_to_structured([table], side='left')

        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]['date_str'], '01.01.2025')
        self.assertEqual(df.iloc[0]['doc_num'], '124001-0816')
        self.assertAlmostEqual(df.iloc[0]['credit'], 1297.0, places=2)
        self.assertEqual(df.iloc[1]['date_str'], '23.01.2025')
        self.assertAlmostEqual(df.iloc[1]['debit'], 2338.66, places=2)
        self.assertEqual(df.attrs.get('start_balance'), 3753.71)
        self.assertEqual(df.attrs.get('end_balance'), 1778.23)

    def test_pdf_table_parser_keeps_closing_balance_when_opening_is_empty(self):
        table = [
            ['По данным ООО "Первая", руб.', None, None, None, 'По данным ООО "Вторая", руб.', None, None, None],
            ['Дата', 'Документ', 'Дебет', 'Кредит', 'Дата', 'Документ', 'Дебет', 'Кредит'],
            ['Сальдо начальное', None, '', '', 'Сальдо начальное', None, '', ''],
            ['22.04.25', 'Приход (123 от 22.04.2025)', '', '56 398,80', '22.04.25', 'Продажа (123 от 22.04.2025)', '56 398,80', ''],
            ['22.04.25', 'Оплата (4569/99 от 22.04.2025)', '41 500,00', '', '22.04.25', 'Оплата (4569/99 от 22.04.2025)', '', '41 500,00'],
            ['Обороты за период', None, '41 500,00', '56 398,80', 'Обороты за период', None, '56 398,80', '41 500,00'],
            ['Сальдо конечное', None, '', '14 898,80', 'Сальдо конечное', None, '14 898,80', ''],
        ]

        df = self.main._parse_pdf_tables_to_structured([table], side='left')

        self.assertEqual(len(df), 2)
        self.assertIsNone(df.attrs.get('start_balance'))
        self.assertEqual(df.attrs.get('end_balance'), 14898.8)

    def test_pdf_table_parser_reuses_header_for_continuation_tables(self):
        first_page = [
            ['По данным ООО "Первая", руб.', None, None, None, 'По данным ООО "Вторая", руб.', None, None, None],
            ['Дата', 'Документ', 'Дебет', 'Кредит', 'Дата', 'Документ', 'Дебет', 'Кредит'],
            ['Сальдо начальное', '', '', '1 000,00', '', '', '', ''],
            ['01.01.26', 'Приход (1 от 01.01.2026)', '', '100,00', '', '', '', ''],
        ]
        continuation = [
            ['02.01.26', 'Оплата (2 от 02.01.2026)', '50,00', '', '', '', '', ''],
            ['Сальдо конечное', '', '', '1 050,00', '', '', '', ''],
        ]

        df = self.main._parse_pdf_tables_to_structured([first_page, continuation], side='left')

        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[1]['doc_num'], '2')
        self.assertAlmostEqual(df.iloc[0]['signed_amount'], 100.0, places=2)
        self.assertEqual(df.attrs.get('start_balance'), 1000.0)
        self.assertEqual(df.attrs.get('end_balance'), 1050.0)

    def test_pdf_table_parser_handles_multirow_standard_header(self):
        table = [
            ['Дата', 'Документ', 'Валюта\nдокумента', 'по данным', '', '', '', '', ''],
            ['', '', '', 'ООО "МА"', '', '', 'ООО "АРВ"', '', ''],
            ['', '', '', 'Сумма\nдокумента', 'Дебет', 'Кредит', 'Сумма\nдокумента', 'Дебет', 'Кредит'],
            ['Сальдо начальное', '', '', '', '12 143 583,16', '-', '', '', ''],
            ['12.01.2026', 'Платежное поручение №10 от 12.01.2026', 'руб.', '1 189 004,16', '-', '1 189 004,16', '', '', ''],
        ]

        df = self.main._parse_pdf_tables_to_structured([table], side='left')

        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]['document'], 'Платежное поручение №10 от 12.01.2026')
        self.assertEqual(df.iloc[0]['doc_num'], '10')
        self.assertAlmostEqual(df.iloc[0]['credit'], 1189004.16, places=2)
        self.assertAlmostEqual(df.iloc[0]['signed_amount'], -1189004.16, places=2)
        self.assertEqual(df.attrs.get('start_balance'), 12143583.16)

    def test_pdf_table_parser_handles_shifted_ledger_continuation(self):
        first_page = [
            ['По данным АО "РОЛЬФ", руб', '', '', '', '', '', '', '', ''],
            ['Наименование договора', '', '', 'Номер С/Ф', 'Дата С/Ф', 'Дебет', 'Кредит', 'Дебет', 'Кредит'],
            ['САЛЬДО НАЧАЛЬНОЕ на 01.01.2026', '', '', '', '', '', '161 570,38', '', ''],
            ['', 'Продажа № РГО 2069 от\n12.01.26', '', 'РГО 2069', '12.01.26', '14 002,73', '', '', ''],
        ]
        continuation = [
            ['Оплата № 971 от 16.02.26', '', '', '', '124 107,77', '', ''],
            ['Продажа № РГО 13595 от\n17.02.26', 'РГО 13595', '17.02.26', '115 797,28', '', '', ''],
            ['САЛЬДО КОНЕЧНОЕ на 31.03.2026', '', '', '', '', '121 308,64', '', ''],
        ]

        df = self.main._parse_pdf_tables_to_structured([first_page, continuation], side='left')

        self.assertEqual(len(df), 3)
        self.assertAlmostEqual(df.iloc[0]['debit'], 14002.73, places=2)
        self.assertAlmostEqual(df.iloc[1]['credit'], 124107.77, places=2)
        self.assertAlmostEqual(df.iloc[2]['debit'], 115797.28, places=2)
        self.assertAlmostEqual(df['signed_amount'].sum(), 5692.24, places=2)
        self.assertEqual(df.attrs.get('start_balance'), 161570.38)
        self.assertEqual(df.attrs.get('end_balance'), 121308.64)

    def test_pdf_contract_detail_ignores_contract_balances_and_shifted_rows(self):
        first_page = [
            ['', 'Дата', 'Документ', 'Дебет', 'Кредит', 'Дата', 'Документ', 'Дебет', 'Кредит'],
            ['', 'Сальдо начальное', None, '26 288 695,96', '', 'Сальдо начальное', None, '', '26 288 695,96'],
            ['', 'Договор оферты id(315413340)', None, None, None, None, None, None, None],
            ['', 'Сальдо начальное по договору:', None, '', '7 385,31', '', None, None, None],
            ['', '19.03.26', 'Оплата (552 от 19.03.2026)', '', '8 000,00', '', '', '', ''],
        ]
        shifted_continuation = [
            ['Сальдо начальное по договору:', None, '', '7 385,31', '', None, None, None],
            ['25.03.26', 'Оплата (618 от 25.03.2026)', '', '114 000,00', '', '', '', ''],
            ['31.03.26', 'УПД (03-179712 от 31.03.2026)', '10 340,07', '', '', '', '', ''],
            ['Сальдо конечное по договору:', None, '', '5 045,24', '', None, None, None],
            ['Сальдо конечное', None, '31 033 380,31', '', 'Сальдо конечное', None, '', '31 033 380,31'],
        ]

        df = self.main._parse_pdf_tables_to_structured([first_page, shifted_continuation], side='left')

        self.assertEqual(len(df), 3)
        self.assertEqual(df.attrs.get('start_balance'), 26288695.96)
        self.assertEqual(df.attrs.get('end_balance'), 31033380.31)
        self.assertIn('552', set(df['doc_num']))
        self.assertIn('618', set(df['doc_num']))
        self.assertIn('3-179712', set(df['doc_num']))

    def test_balance_reason_analysis_groups_complex_pdf_case(self):
        pd = self.main.pd

        def make_row(raw_row, document, doc_num, effect):
            amount = abs(effect)
            return {
                'date': pd.to_datetime('31.03.2026', dayfirst=True),
                'date_str': '31.03.2026',
                'document': document,
                'doc_num': doc_num,
                'debit': amount if effect < 0 else None,
                'credit': amount if effect > 0 else None,
                'signed_amount': effect,
                'raw_row': raw_row,
            }

        doc1_rows = [
            make_row(0, 'Принято (3 от 31.03.2026)', '3', -55000.0),
            make_row(1, 'Принято (63 от 31.03.2026)', '63', -344200.0),
            make_row(2, 'Продажа (67 от 31.03.2026)', '67', -537746.0),
        ]
        doc2_rows = [
            make_row(10, 'Приход (бн от 31.12.2025)', None, 423043.30),
            make_row(11, 'Приход (бн от 31.12.2025)', None, 2398674.56),
        ]
        for idx, amount in enumerate([3800, 26000, 17000, 8100, 16800, 223500, 49000], 20):
            doc2_rows.append(make_row(idx, 'Списание дебиторской (кредиторской) задолженности (31.03.2026)', None, float(amount)))
        doc2_rows.append(make_row(40, 'Оплата (85620 от 31.03.2026)', '85620', 15000.0))
        for idx in range(64):
            effect = 1000.0 if idx % 2 == 0 else -1000.0
            doc2_rows.append(make_row(100 + idx, 'Принято (94485 от 31.03.2026)' if effect < 0 else 'Оплата (94485 от 31.03.2026)', '94485', effect))

        df1 = pd.DataFrame(doc1_rows)
        df2 = pd.DataFrame(doc2_rows)
        df1.attrs['parser_id'] = 'pdf_text_left'
        df2.attrs['parser_id'] = 'pdf_text_left'
        result = {
            'summary': {
                'total_discrepancies': 77,
                'opening_balance_difference': -3831198.19,
                'transaction_net_difference': 2243971.86,
                'closing_balance_difference': -1587226.33,
            },
            'missing_rows1': [0, 1, 2],
            'missing_rows2': [row['raw_row'] for row in doc2_rows],
        }

        analysis = self.main._build_balance_reason_analysis(df1, df2, result, None, self.cfg)

        self.assertIsNotNone(analysis)
        self.assertEqual(analysis['primary_tab'], 'summary')
        self.assertEqual(analysis['trigger'], 'complex_structured_balance_case')
        self.assertFalse(analysis['forced'])
        self.assertAlmostEqual(analysis['period_movement_difference'], 2243971.86, places=2)
        self.assertAlmostEqual(analysis['explained_movement'], 2243971.86, places=2)
        self.assertAlmostEqual(analysis['unexplained_difference'], 0.0, places=2)

        reasons = {item['title']: item for item in analysis['reasons']}
        bn_title = next(title for title in reasons if 'бн от 31.12.2025' in title)
        self.assertAlmostEqual(reasons[bn_title]['influence'], 2821717.86, places=2)
        self.assertAlmostEqual(reasons['Документы есть только в первом акте']['influence'], -936946.0, places=2)
        writeoff_title = next(title for title in reasons if title.startswith('Списания задолженности'))
        self.assertAlmostEqual(reasons[writeoff_title]['influence'], 344200.0, places=2)
        self.assertAlmostEqual(reasons['Оплата 85620 есть только во втором акте']['influence'], 15000.0, places=2)

        self.assertEqual(len(analysis['neutral_groups']), 1)
        self.assertIn('94485', analysis['neutral_groups'][0]['title'])
        self.assertEqual(analysis['neutral_groups'][0]['row_count'], 64)
        self.assertAlmostEqual(analysis['neutral_groups'][0]['influence'], 0.0, places=2)

        df1.attrs['parser_id'] = 'standard_act'
        df2.attrs['parser_id'] = 'standard_act'
        excel_analysis = self.main._build_balance_reason_analysis(df1, df2, result, None, self.cfg)
        self.assertIsNotNone(excel_analysis)
        self.assertEqual(excel_analysis['trigger'], 'complex_structured_balance_case')
        self.assertFalse(excel_analysis['forced'])

    def test_balance_reason_analysis_can_be_forced_for_small_case(self):
        pd = self.main.pd
        df1 = pd.DataFrame([{
            'date': pd.to_datetime('31.03.2026', dayfirst=True),
            'date_str': '31.03.2026',
            'document': 'Принято (1 от 31.03.2026)',
            'doc_num': '1',
            'debit': 100.0,
            'credit': None,
            'signed_amount': -100.0,
            'raw_row': 0,
        }])
        df2 = pd.DataFrame([{
            'date': pd.to_datetime('31.03.2026', dayfirst=True),
            'date_str': '31.03.2026',
            'document': 'Оплата (2 от 31.03.2026)',
            'doc_num': '2',
            'debit': None,
            'credit': 100.0,
            'signed_amount': 100.0,
            'raw_row': 0,
        }])
        df1.attrs['parser_id'] = 'standard_act'
        df2.attrs['parser_id'] = 'standard_act'
        result = {
            'summary': {
                'total_discrepancies': 1,
                'opening_balance_difference': 0.0,
                'transaction_net_difference': -100.0,
                'closing_balance_difference': -100.0,
            },
            'missing_rows1': [0],
            'missing_rows2': [],
        }

        self.assertIsNone(self.main._build_balance_reason_analysis(df1, df2, result, None, self.cfg))

        forced_cfg = {**self.cfg, 'force_balance_reason_analysis': True}
        analysis = self.main._build_balance_reason_analysis(df1, df2, result, None, forced_cfg)

        self.assertIsNotNone(analysis)
        self.assertEqual(analysis['trigger'], 'manual_balance_reason_analysis')
        self.assertTrue(analysis['forced'])
        self.assertEqual(analysis['primary_tab'], 'summary')
        self.assertAlmostEqual(analysis['period_movement_difference'], -100.0, places=2)

    def test_forced_balance_reason_analysis_recovers_missing_balances_with_ai(self):
        pd = self.main.pd

        def make_row(raw_row, document, doc_num, effect):
            amount = abs(effect)
            return {
                'date': pd.to_datetime('31.03.2026', dayfirst=True),
                'date_str': '31.03.2026',
                'document': document,
                'doc_num': doc_num,
                'debit': amount if effect < 0 else None,
                'credit': amount if effect > 0 else None,
                'signed_amount': effect,
                'raw_row': raw_row,
            }

        df1 = pd.DataFrame([make_row(0, 'Принято (1 от 31.03.2026)', '1', -100.0)])
        df2 = pd.DataFrame([make_row(0, 'Оплата (2 от 31.03.2026)', '2', 50.0)])
        df1.attrs['parser_id'] = 'standard_act'
        df2.attrs['parser_id'] = 'standard_act'

        class FakeMessages:
            def __init__(self):
                self.calls = 0

            def create(self, *args, **kwargs):
                self.calls += 1
                payload = {
                    'doc1': {
                        'opening_balance': 1000,
                        'closing_balance': 1200,
                        'period_from': '01.03.2026',
                        'period_to': '31.03.2026',
                        'evidence': {'opening': 'Сальдо начальное 1 000,00', 'closing': 'Сальдо конечное 1 200,00'},
                    },
                    'doc2': {
                        'opening_balance': 900,
                        'closing_balance': 1050,
                        'period_from': '01.03.2026',
                        'period_to': '31.03.2026',
                        'evidence': {'opening': 'Сальдо начальное 900,00', 'closing': 'Сальдо конечное 1 050,00'},
                    },
                }
                return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))])

        class FakeClient:
            def __init__(self):
                self.messages = FakeMessages()

        client = FakeClient()
        cfg = {**self.cfg, 'force_balance_reason_analysis': True, 'ai_comment': False}
        logs = []
        result = self.main._reconcile_structured(df1, df2, 'generic_detected', 'generic_detected', client, logs.append, cfg)
        summary = result['summary']
        analysis = summary['balance_reason_analysis']

        self.assertEqual(summary['result_mode'], 'balance_reason_analysis')
        self.assertTrue(summary['balance_basis_recovered'])
        self.assertTrue(summary['balance_basis_recovery']['available'])
        self.assertEqual(analysis['trigger'], 'manual_balance_reason_analysis')
        self.assertTrue(analysis['basis_recovery']['available'])
        self.assertAlmostEqual(analysis['opening_balance_difference'], 100.0, places=2)
        self.assertAlmostEqual(analysis['closing_balance_difference'], 150.0, places=2)
        self.assertAlmostEqual(analysis['period_movement_difference'], 50.0, places=2)
        self.assertEqual(client.messages.calls, 1)


class AuthKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stubs()
        repo_root = Path(__file__).resolve().parents[1]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        cls.main = importlib.import_module('main')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_allowed_file = self.main._ALLOWED_KEYS_FILE
        self.orig_guest_usage_file = self.main._GUEST_USAGE_FILE
        self.orig_api_key = self.main.ANTHROPIC_API_KEY
        self.orig_allow_all = self.main.AUTH_ALLOW_ALL
        self.orig_guest_limit = self.main.GUEST_RECONCILE_LIMIT
        self.orig_guest_window = self.main.GUEST_USAGE_WINDOW_DAYS
        self.main._ALLOWED_KEYS_FILE = Path(self.tmp.name) / 'allowed_keys.json'
        self.main._GUEST_USAGE_FILE = Path(self.tmp.name) / 'guest_usage.json'
        self.main.ANTHROPIC_API_KEY = ''
        self.main.AUTH_ALLOW_ALL = False
        self.main.GUEST_RECONCILE_LIMIT = 2
        self.main.GUEST_USAGE_WINDOW_DAYS = 30

    def tearDown(self):
        self.main._ALLOWED_KEYS_FILE = self.orig_allowed_file
        self.main._GUEST_USAGE_FILE = self.orig_guest_usage_file
        self.main.ANTHROPIC_API_KEY = self.orig_api_key
        self.main.AUTH_ALLOW_ALL = self.orig_allow_all
        self.main.GUEST_RECONCILE_LIMIT = self.orig_guest_limit
        self.main.GUEST_USAGE_WINDOW_DAYS = self.orig_guest_window
        self.tmp.cleanup()

    def _guest_request(self, guest_id='browser-a', ip='10.0.0.1'):
        req = types.SimpleNamespace()
        req.headers = {'X-Guest-Id': guest_id, 'X-Forwarded-For': ip}
        req.client = types.SimpleNamespace(host=ip)
        return req

    class _Upload:
        def __init__(self, filename, data):
            self.filename = filename
            self._data = data
            self._pos = 0

        async def read(self, size=-1):
            if self._pos >= len(self._data):
                return b''
            if size is None or size < 0:
                size = len(self._data) - self._pos
            chunk = self._data[self._pos:self._pos + size]
            self._pos += len(chunk)
            return chunk

    def test_empty_allowlist_rejects_user_login_by_default(self):
        ok, reason = self.main._key_access_status('sk-ant-api03-arbitrary')
        self.assertFalse(ok)
        self.assertEqual(reason, 'no_allowlist')

    def test_user_role_allows_login_and_guest_role_blocks_login(self):
        user_key = 'sk-ant-api03-user'
        guest_key = 'sk-ant-api03-guest'
        self.main._save_allowed_keys([
            {'hash': self.main._user_id(user_key), 'label': 'User', 'role': 'user', 'enabled': True},
            {'hash': self.main._user_id(guest_key), 'label': 'Guest', 'role': 'guest', 'enabled': True},
        ])

        self.assertTrue(self.main._is_key_allowed(user_key))
        ok, reason = self.main._key_access_status(guest_key)
        self.assertFalse(ok)
        self.assertEqual(reason, 'guest_key')

    def test_guest_reconcile_limit_is_recorded_per_browser_and_ip(self):
        req = self._guest_request()

        status = self.main._guest_usage_status(req)
        self.assertEqual(status['limit'], 2)
        self.assertEqual(status['remaining'], 2)

        status = self.main._record_guest_reconcile(req)
        self.assertEqual(status['used'], 1)
        self.assertEqual(status['remaining'], 1)

        status = self.main._record_guest_reconcile(req)
        self.assertEqual(status['used'], 2)
        self.assertEqual(status['remaining'], 0)

        with self.assertRaises(self.main.HTTPException) as cm:
            self.main._guest_limit_or_raise(req)
        self.assertEqual(cm.exception.status_code, 429)

    def test_system_api_key_cannot_be_used_as_user_login(self):
        guest_key = 'sk-ant-api03-system'
        self.main.ANTHROPIC_API_KEY = guest_key
        self.main._save_allowed_keys([
            {'hash': self.main._user_id(guest_key), 'label': 'System', 'role': 'user', 'enabled': True},
        ])

        ok, reason = self.main._key_access_status(guest_key)
        self.assertFalse(ok)
        self.assertEqual(reason, 'guest_key')

    def test_guest_upload_rejects_pdf(self):
        import asyncio

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                upload = self._Upload('act.pdf', b'%PDF-1.4\n')
                with self.assertRaises(self.main.HTTPException) as cm:
                    await self.main._save_upload_to_path(upload, str(Path(tmp) / 'act.pdf'), '', 'File')
                self.assertEqual(cm.exception.status_code, 400)
                self.assertIn('PDF', cm.exception.detail)

        asyncio.run(run())

    def test_authorized_upload_allows_pdf(self):
        import asyncio

        async def run():
            data = b'%PDF-1.4\n'
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'act.pdf'
                upload = self._Upload('act.pdf', data)
                size = await self.main._save_upload_to_path(upload, str(path), 'sk-ant-api03-user', 'File')
                self.assertEqual(size, len(data))
                self.assertEqual(path.read_bytes(), data)

        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()

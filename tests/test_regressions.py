import importlib
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


if __name__ == '__main__':
    unittest.main()

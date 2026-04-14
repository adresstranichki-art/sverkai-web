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
        self.orig_api_key = self.main.ANTHROPIC_API_KEY
        self.orig_allow_all = self.main.AUTH_ALLOW_ALL
        self.main._ALLOWED_KEYS_FILE = Path(self.tmp.name) / 'allowed_keys.json'
        self.main.ANTHROPIC_API_KEY = ''
        self.main.AUTH_ALLOW_ALL = False

    def tearDown(self):
        self.main._ALLOWED_KEYS_FILE = self.orig_allowed_file
        self.main.ANTHROPIC_API_KEY = self.orig_api_key
        self.main.AUTH_ALLOW_ALL = self.orig_allow_all
        self.tmp.cleanup()

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

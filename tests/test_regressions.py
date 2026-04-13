import importlib
import sys
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
        self.assertEqual(len(result['discrepancies']), 8)
        self.assertEqual(result['summary']['critical_count'], 2)
        self.assertEqual(result['summary']['technical_mirror_count'], 5)
        self.assertEqual(
            sum(1 for d in result['discrepancies'] if d['type'] == 'technical_mirror'),
            5,
        )
        self.assertAlmostEqual(result['summary']['net_period'], 1436253.6, places=2)


if __name__ == '__main__':
    unittest.main()

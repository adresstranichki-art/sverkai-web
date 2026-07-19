import unittest
from pathlib import Path


class SummaryFrontendContractTests(unittest.TestCase):
    def test_summary_does_not_render_legacy_green_debt_card(self):
        index_html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('id="debt-card"', index_html)
        self.assertNotIn("document.getElementById('debt-card')", index_html)


if __name__ == "__main__":
    unittest.main()

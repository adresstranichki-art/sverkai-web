#!/usr/bin/env python3
"""Run local Excel reconciliation cases without committing customer files.

Usage:
  python tools/local_regression.py --write-baseline
  python tools/local_regression.py

The script scans local_cases/<case-name>/ for exactly two .xls/.xlsx files,
runs the structured reconciliation, and compares compact summaries against
local_cases/_baseline_current.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402


BASELINE_PATH = ROOT / "local_cases" / "_baseline_current.json"
CASE_ROOT = ROOT / "local_cases"


CFG = {
    **main.DEFAULT_RECON_SETTINGS,
    "ai_comment": False,
}


SUMMARY_KEYS = [
    "total_discrepancies",
    "critical_count",
    "technical_mirror_count",
    "fuzzy_count",
    "exact_matches",
    "net_period",
    "opening_balance_difference",
    "closing_balance_difference",
    "transaction_net_difference",
    "period",
]


def _case_files(case_dir: Path) -> list[Path]:
    return sorted(
        [p for p in case_dir.iterdir() if p.suffix.lower() in {".xls", ".xlsx"}],
        key=lambda p: p.name.lower(),
    )


def run_case(case_dir: Path) -> dict:
    files = _case_files(case_dir)
    if len(files) != 2:
        return {
            "case": case_dir.name,
            "skipped": True,
            "reason": f"expected 2 Excel files, found {len(files)}",
        }

    logs: list[str] = []
    _, candidates1 = main._collect_parse_candidates(str(files[0]), logs, "", files[0].name)
    _, candidates2 = main._collect_parse_candidates(str(files[1]), logs, "", files[1].name)
    best = main._select_best_candidate_pair(candidates1, candidates2, CFG, logs)
    result = best["result"]
    counts = Counter(d.get("type") for d in result.get("discrepancies", []))

    return {
        "case": case_dir.name,
        "file1": files[0].name,
        "file2": files[1].name,
        "parser1": best["cand1"]["label"],
        "parser2": best["cand2"]["label"],
        "rows1": len(best["cand1"]["df"]),
        "rows2": len(best["cand2"]["df"]),
        "score": best["score"],
        "summary": {key: result["summary"].get(key) for key in SUMMARY_KEYS},
        "type_counts": dict(sorted(counts.items())),
        "debt_label": result["summary"].get("debt_label"),
    }


def comparable(item: dict) -> dict:
    return {
        "parser1": item.get("parser1"),
        "parser2": item.get("parser2"),
        "summary": item.get("summary"),
        "type_counts": item.get("type_counts"),
    }


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--json", action="store_true", help="print full JSON output")
    args = parser.parse_args()

    if not CASE_ROOT.exists():
        print(f"Local case folder not found: {CASE_ROOT}")
        return 2

    results = [
        run_case(case_dir)
        for case_dir in sorted(p for p in CASE_ROOT.iterdir() if p.is_dir())
    ]

    if args.write_baseline:
        BASELINE_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote baseline: {BASELINE_PATH}")
        return 0

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    if not BASELINE_PATH.exists():
        print(f"Baseline not found. Run: python tools/local_regression.py --write-baseline")
        return 2

    expected = {
        item["case"]: item
        for item in json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        if not item.get("skipped")
    }
    changed = []
    for item in results:
        if item.get("skipped"):
            print(f"SKIP {item['case']}: {item['reason']}")
            continue
        prev = expected.get(item["case"])
        if prev is None:
            changed.append({"case": item["case"], "before": None, "after": comparable(item)})
        elif comparable(prev) != comparable(item):
            changed.append({"case": item["case"], "before": comparable(prev), "after": comparable(item)})
        else:
            s = item["summary"]
            print(
                f"OK {item['case']}: total={s['total_discrepancies']}, "
                f"critical={s['critical_count']}, net={s['net_period']}"
            )

    if changed:
        print("\nChanged cases:")
        print(json.dumps(changed, ensure_ascii=False, indent=2))
        return 1

    print(f"\nAll local regression cases match baseline ({len(expected)} cases).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())

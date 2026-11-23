"""The golden set: reference values from reviewed quarters, for evaluation.

Each file under `golden/` is a trimmed, human-reviewed copy of a `_work/
<key>_<period>_values.json` run — the company, the period, and every mapped
row's label/section/value, with the provenance (`resolve`, `source`,
`adjustments`) stripped out because that is run-specific detail, not the
truth being asserted. Rows are kept in the order they were resolved, so a
label repeated for two occurrences of the same caption is still matched to
the right one positionally.

    build   write/refresh golden/<key>_<period>.json from the matching
            _work/<key>_<period>_values.json (a run already reviewed by hand)
    check   re-read _work/<key>_<period>_values.json (a fresh run) and report
            any value that drifted from golden beyond tolerance, or any label
            newly missing or added

Usage:
    python scripts/golden_set.py build [company ...]
    python scripts/golden_set.py check [company ...]

With no company names, every golden/*.json (for `check`) or every
_work/*_values.json (for `build`) is used.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "_work"
GOLDEN_DIR = ROOT / "golden"

#: Relative tolerance, matching finscan.validate.TOLERANCE — filings round, a
#: rounding difference is not a drift worth failing a check over.
TOLERANCE = 0.005
#: Absolute floor so values near zero don't need an unreasonably tight match.
ABS_FLOOR = 0.01


def _stem_parts(path: Path) -> tuple[str, str] | None:
    """`<key>_<period>_values.json` -> (key, period). None if it doesn't parse."""
    name = path.name
    if not name.endswith("_values.json"):
        return None
    stem = name[: -len("_values.json")]
    # period_end is YYYY-MM-DD (dash-separated), so splitting on the last '_'
    # separates it from the key even when the key itself has underscores
    # (e.g. "axiata_issue").
    if "_" not in stem:
        return None
    key, period = stem.rsplit("_", 1)
    return key, period


def _trim(values_json: dict) -> dict:
    rows = []
    for v in values_json.get("values", []):
        rows.append({"label": v["label"], "section": v["section"], "value": v["value"]})
    return {
        "company": values_json["company"],
        "period_end": values_json["period_end"],
        "units": values_json["units"],
        "values": rows,
    }


def build(companies: list[str]) -> None:
    GOLDEN_DIR.mkdir(exist_ok=True)
    run_files = sorted(WORK_DIR.glob("*_values.json"))
    written = 0
    for path in run_files:
        parsed = _stem_parts(path)
        if parsed is None:
            continue
        key, period = parsed
        if companies and key not in companies:
            continue
        values_json = json.loads(path.read_text(encoding="utf-8"))
        golden = GOLDEN_DIR / f"{key}_{period}.json"
        golden.write_text(
            json.dumps(_trim(values_json), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        print(f"built {golden.relative_to(ROOT)}")
        written += 1
    if not written:
        print("nothing built — no matching _work/*_values.json found", file=sys.stderr)


def _close(expected: float | None, actual: float | None) -> bool:
    if expected is None or actual is None:
        return expected == actual
    if abs(expected - actual) <= ABS_FLOOR:
        return True
    denom = max(abs(expected), abs(actual))
    return abs(expected - actual) / denom <= TOLERANCE


def check(companies: list[str]) -> bool:
    golden_files = sorted(GOLDEN_DIR.glob("*.json"))
    if companies:
        golden_files = [p for p in golden_files
                        if _stem_parts_golden(p) and _stem_parts_golden(p)[0] in companies]
    if not golden_files:
        print("no golden files to check", file=sys.stderr)
        return False

    all_ok = True
    for golden_path in golden_files:
        key, period = _stem_parts_golden(golden_path)
        run_path = WORK_DIR / f"{key}_{period}_values.json"
        print(f"== {key} {period} ==")
        if not run_path.exists():
            print(f"  MISSING run: {run_path.relative_to(ROOT)} — run finscan first")
            all_ok = False
            continue

        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        run = _trim(json.loads(run_path.read_text(encoding="utf-8")))

        golden_rows = golden["values"]
        run_by_pos: dict[tuple[str, str], list[dict]] = {}
        for row in run["values"]:
            run_by_pos.setdefault((row["label"], row["section"]), []).append(row)

        mismatches = 0
        for row in golden_rows:
            bucket = run_by_pos.get((row["label"], row["section"]))
            if not bucket:
                print(f"  MISSING label: {row['label']!r} ({row['section']})")
                mismatches += 1
                continue
            actual = bucket.pop(0)
            if not _close(row["value"], actual["value"]):
                print(f"  DRIFT {row['label']!r}: golden={row['value']!r} "
                     f"now={actual['value']!r}")
                mismatches += 1
        extra = [r for rows in run_by_pos.values() for r in rows]
        for row in extra:
            print(f"  EXTRA label: {row['label']!r} ({row['section']}) = {row['value']!r}")
            mismatches += 1

        if mismatches:
            all_ok = False
            print(f"  {mismatches} mismatch(es)")
        else:
            print(f"  ok — {len(golden_rows)} row(s) match")
    return all_ok


def _stem_parts_golden(path: Path) -> tuple[str, str] | None:
    name = path.stem  # <key>_<period>
    if "_" not in name:
        return None
    return name.rsplit("_", 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["build", "check"])
    parser.add_argument("companies", nargs="*", help="limit to these company keys")
    args = parser.parse_args(argv)

    if args.action == "build":
        build(args.companies)
        return 0
    ok = check(args.companies)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

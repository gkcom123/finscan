"""Resolve PDF + Excel under <root>/<company>/ for CLI and Copilot demo runs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from finscan.profiles import company_key

# Common misspellings / spoken names in demo prompts.
COMPANY_ALIASES: dict[str, str] = {
    "tancent": "tencent",
    "ten cent": "tencent",
    "tencent_holdings": "tencent",
    "tencent_holdings_limited": "tencent",
}

# Short period codes from chat → column header override.
PERIOD_LABELS: dict[str, str] = {
    "q325": "3Q2025",
    "q3_2025": "3Q2025",
    "3q2025": "3Q2025",
    "3q25": "3Q2025",
    "q126": "1Q2026",
    "q1_2026": "1Q2026",
    "1q2026": "1Q2026",
}


@dataclass(frozen=True)
class DemoFiles:
    company_key: str
    pdf: Path
    workbook: Path
    period_label: str | None
    period_token: str | None


def normalize_company(name: str) -> str:
    raw = (name or "").strip().lower()
    if raw in COMPANY_ALIASES:
        return COMPANY_ALIASES[raw]
    key = company_key(name)
    return COMPANY_ALIASES.get(key, key)


def normalize_period(period: str | None) -> tuple[str | None, str | None]:
    """Return (search token, column header label)."""
    if not period or not period.strip():
        return None, None
    token = re.sub(r"[^a-z0-9]", "", period.lower())
    label = PERIOD_LABELS.get(token)
    if not label and token:
        # q325 → 3Q2025 heuristic: q + digit + 2-digit year suffix
        m = re.fullmatch(r"q(\d)(\d{2,4})", token)
        if m:
            q, yr = m.group(1), m.group(2)
            yr = yr if len(yr) == 4 else f"20{yr}"
            label = f"Q{q} {yr}"
    return token or None, label


def _is_output_workbook(path: Path) -> bool:
    stem = path.stem.lower()
    return "_updated" in stem or stem.endswith("_out") or stem.endswith("_output")


def _score_pdf(path: Path, period_token: str | None) -> int:
    name = path.stem.lower()
    score = 0
    if period_token and period_token in re.sub(r"[^a-z0-9]", "", name):
        score += 100
    if "results" in name:
        score += 5
    return score


def output_path_for_company(input_root: Path, company_key: str, workbook: Path) -> Path:
    """Write beside input root: input/<company>_output.<same suffix as model>."""
    return Path(input_root) / f"{company_key}_output{workbook.suffix}"


def resolve_demo_files(
    test_root: Path,
    company: str,
    period: str | None = None,
) -> DemoFiles:
    """Locate PDF + workbook under <root>/<company>/."""
    root = Path(test_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Input folder not found: {root.resolve()}")

    key = normalize_company(company)
    period_token, period_label = normalize_period(period)

    company_dir = root / key
    if not company_dir.is_dir():
        # Allow test/Tencent/ as well as test/tencent/
        matches = [d for d in root.iterdir() if d.is_dir() and d.name.lower() == key]
        if not matches:
            available = sorted(p.name for p in root.iterdir() if p.is_dir())
            raise FileNotFoundError(
                f"No test folder for company '{company}' (key '{key}'). "
                f"Available: {', '.join(available) or '(none)'}"
            )
        company_dir = matches[0]

    pdfs = sorted(company_dir.glob("*.pdf"))
    workbooks = [
        p for p in list(company_dir.glob("*.xlsx")) + list(company_dir.glob("*.xlsm"))
        if not _is_output_workbook(p)
    ]
    if not pdfs:
        raise FileNotFoundError(f"No PDF in {company_dir}")
    if not workbooks:
        raise FileNotFoundError(f"No Excel model in {company_dir} (exclude *_updated*)")

    pdf = max(pdfs, key=lambda p: (_score_pdf(p, period_token), p.name))
    workbook = workbooks[0] if len(workbooks) == 1 else min(workbooks, key=lambda p: p.name)

    return DemoFiles(
        company_key=key,
        pdf=pdf,
        workbook=workbook,
        period_label=period_label,
        period_token=period_token,
    )

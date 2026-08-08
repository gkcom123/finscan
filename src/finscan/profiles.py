"""Per-company layout profiles.

The scaling problem with 100 companies is not the extraction, it is that every
model is shaped differently and a human should only ever have to explain that
shape once. So:

    upload 1  -> discover the layout, propose it, a human confirms -> profile saved
    upload 2+ -> profile reused silently
    layout changes -> fingerprint mismatch, the human is asked again

No format knowledge lives in code. Profiles are data, stored as JSON, one file
per company, editable by hand if someone wants to.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from finscan.excel.discovery import WorkbookPlan

DEFAULT_STORE = Path("./profiles")


@dataclass
class SheetSetting:
    sheet: str
    enabled: bool
    label_col: int
    header_row: int
    write_mode: str
    note: str = ""


@dataclass
class CompanyProfile:
    company_key: str
    display_name: str = ""
    fingerprint: str = ""
    confirmed: bool = False
    confirmed_by: str = ""
    confirmed_at: str = ""
    source_workbook: str = ""
    colors_found: bool = False
    sheets: list[SheetSetting] = field(default_factory=list)
    #: caption -> canonical field overrides a reviewer supplied for this company
    label_overrides: dict[str, str] = field(default_factory=dict)
    #: canonical field -> the fields this company's row actually adds up.
    #: A row captioned "Other gains, net" that really holds investment gains plus
    #: other gains cannot be expressed as a caption mapping — the caption is
    #: right, the *contents* are a composite. Recorded here once, per company.
    field_composites: dict[str, list[str]] = field(default_factory=dict)
    #: sheet -> rows a reviewer has ruled out. Used when a model's line is not
    #: the filing's line despite matching captions — e.g. an "Operating profit"
    #: row built to the analyst's own definition rather than the company's.
    unmapped_rows: dict[str, list[int]] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    def enabled_sheets(self) -> set[str]:
        return {s.sheet for s in self.sheets if s.enabled}

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "CompanyProfile":
        data = json.loads(raw)
        data["sheets"] = [SheetSetting(**s) for s in data.get("sheets", [])]
        return cls(**data)

    @classmethod
    def from_plan(cls, company_key: str, plan: WorkbookPlan,
                  display_name: str = "") -> "CompanyProfile":
        return cls(
            company_key=company_key,
            display_name=display_name or company_key,
            fingerprint=plan.fingerprint,
            confirmed=False,
            source_workbook=Path(plan.path).name,
            colors_found=plan.colors_found,
            sheets=[
                SheetSetting(
                    sheet=p.sheet,
                    enabled=p.in_scope,
                    label_col=p.label_col,
                    header_row=p.header_row,
                    write_mode=p.write_mode,
                    note=p.reason,
                )
                for p in plan.sheets
            ],
        )

    def confirm(self, by: str = "reviewer") -> "CompanyProfile":
        self.confirmed = True
        self.confirmed_by = by
        self.confirmed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.history.append(f"{self.confirmed_at} confirmed by {by} (fp {self.fingerprint})")
        return self

    def drifted(self, plan: WorkbookPlan) -> bool:
        return bool(self.fingerprint) and self.fingerprint != plan.fingerprint

    def adopt(self, plan: WorkbookPlan) -> "CompanyProfile":
        """Layout changed: keep the human's sheet choices where the names still
        exist, reset confirmation so someone re-checks."""
        previous = {s.sheet: s.enabled for s in self.sheets}
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.history.append(f"{stamp} layout drift {self.fingerprint} -> {plan.fingerprint}")
        self.fingerprint = plan.fingerprint
        self.colors_found = plan.colors_found
        self.confirmed = False
        self.sheets = [
            SheetSetting(sheet=p.sheet, enabled=previous.get(p.sheet, p.in_scope),
                         label_col=p.label_col, header_row=p.header_row,
                         write_mode=p.write_mode, note=p.reason)
            for p in plan.sheets
        ]
        return self


def company_key(name: str) -> str:
    """Stable slug so 'Northwind Industries Ltd.' and 'northwind industries ltd'
    resolve to the same profile."""
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    suffix = re.compile(r"_(ltd|limited|inc|plc|corp|corporation|co|company|pvt|private|llp|sa|nv|ag)$")
    while True:  # 'Acme Manufacturing Pvt Ltd' sheds both suffixes, not just one
        stripped = suffix.sub("", s)
        if stripped == s:
            break
        s = stripped
    return s.strip("_") or "unknown"


#: Keys produced when the PDF yields no usable company name. In that case we
#: fall back to a confirmed profile with the same workbook fingerprint.
WEAK_COMPANY_KEYS = frozenset({"unknown", "n_a", "na", "n"})


def is_weak_company_key(key: str) -> bool:
    return not key or key in WEAK_COMPANY_KEYS


class ProfileStore:
    def __init__(self, root: str | Path = DEFAULT_STORE):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> CompanyProfile | None:
        p = self._path(key)
        if not p.exists():
            return None
        return CompanyProfile.from_json(p.read_text(encoding="utf-8"))

    def save(self, profile: CompanyProfile) -> Path:
        p = self._path(profile.company_key)
        p.write_text(profile.to_json(), encoding="utf-8")
        return p

    def list(self) -> list[str]:
        return sorted(f.stem for f in self.root.glob("*.json"))

    def find_by_fingerprint(self, fingerprint: str,
                            *, prefer_confirmed: bool = True) -> CompanyProfile | None:
        """Locate a stored profile for this workbook layout."""
        matches: list[CompanyProfile] = []
        for key in self.list():
            pr = self.get(key)
            if pr and pr.fingerprint == fingerprint:
                matches.append(pr)
        if not matches:
            return None
        if prefer_confirmed:
            confirmed = [p for p in matches if p.confirmed]
            if confirmed:
                return confirmed[0]
        return matches[0]

    def resolve(self, key: str, plan: WorkbookPlan,
                display_name: str = "") -> tuple[CompanyProfile, str]:
        """Return (profile, state) where state is new | drifted | reused."""
        existing = self.get(key)

        # Name missing from the PDF → do not create/use a throwaway "unknown"
        # profile when a confirmed layout for this workbook already exists.
        if is_weak_company_key(key) or (existing is not None and not existing.confirmed):
            alt = self.find_by_fingerprint(plan.fingerprint, prefer_confirmed=True)
            if alt and (is_weak_company_key(key) or alt.confirmed):
                existing = alt
                key = alt.company_key

        if existing is None:
            return CompanyProfile.from_plan(key, plan, display_name), "new"
        if existing.drifted(plan):
            return existing.adopt(plan), "drifted"
        return existing, "reused"

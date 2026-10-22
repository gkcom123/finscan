"""Resolve what a cell's formatting actually *means*.

Financial models carry their own contract, and it is the same one at every shop:

    blue text   = hardcoded input      -> we may write here
    black text  = formula / calculated -> never touch
    green text  = link to another sheet
    red text    = link to another workbook

That convention is the only company-agnostic signal available, so it is what
FinScan keys on rather than any per-company layout knowledge.

The work is in getting the *effective* colour. A cell's font colour can be
stored four different ways — literal RGB, a legacy palette index, a theme slot
plus a tint, or "automatic" — and financial models use all four. Reading
`cell.font.color.rgb` alone silently misses theme-coloured workbooks, which is
most of them once a corporate template is involved.
"""
from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from xml.etree import ElementTree as ET

from openpyxl.styles.colors import COLOR_INDEX

_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}

# Order of <a:clrScheme> children in theme1.xml.
_SCHEME_ORDER = [
    "dk1", "lt1", "dk2", "lt2",
    "accent1", "accent2", "accent3", "accent4", "accent5", "accent6",
    "hlink", "folHlink",
]
# Excel's theme *indices* swap the first two pairs relative to the XML order.
_THEME_INDEX = [
    "lt1", "dk1", "lt2", "dk2",
    "accent1", "accent2", "accent3", "accent4", "accent5", "accent6",
    "hlink", "folHlink",
]

#: Built-in Excel style names that state the intent outright.
INPUT_STYLE_NAMES = {"Input", "Note"}
FORMULA_STYLE_NAMES = {"Calculation", "Total", "Output", "Check Cell"}


class Role(str, Enum):
    input = "input"          # blue - a hardcode we may overwrite
    formula = "formula"      # black or an actual formula - never touch
    link_sheet = "link"      # green - cross-sheet reference
    link_external = "external"  # red - external workbook reference
    empty = "empty"
    unknown = "unknown"


@dataclass(frozen=True)
class CellStyle:
    role: Role
    rgb: str | None
    has_formula: bool
    style_name: str | None
    hue: float | None = None
    sat: float | None = None
    lum: float | None = None

    @property
    def writable(self) -> bool:
        """A cell may be written only if it is a hardcode slot and holds no formula."""
        return self.role is Role.input and not self.has_formula


# --------------------------------------------------------------------------- #
# Theme resolution
# --------------------------------------------------------------------------- #
class Theme:
    """Maps theme slot index + tint to a concrete RGB."""

    def __init__(self, scheme: dict[str, str] | None = None):
        self.scheme = scheme or {}

    @classmethod
    def from_workbook(cls, wb: Any) -> "Theme":
        raw = getattr(wb, "loaded_theme", None)
        if not raw:
            return cls()
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return cls()
        clr = root.find(".//a:clrScheme", _NS)
        if clr is None:
            return cls()

        scheme: dict[str, str] = {}
        for idx, child in enumerate(list(clr)):
            name = re.sub(r"^\{.*\}", "", child.tag)
            if name not in _SCHEME_ORDER:
                name = _SCHEME_ORDER[idx] if idx < len(_SCHEME_ORDER) else name
            srgb = child.find("a:srgbClr", _NS)
            if srgb is not None and srgb.get("val"):
                scheme[name] = srgb.get("val").upper()
                continue
            sys_clr = child.find("a:sysClr", _NS)
            if sys_clr is not None:
                scheme[name] = (sys_clr.get("lastClr") or "000000").upper()
        return cls(scheme)

    def resolve(self, theme_index: int, tint: float = 0.0) -> str | None:
        if not (0 <= theme_index < len(_THEME_INDEX)):
            return None
        rgb = self.scheme.get(_THEME_INDEX[theme_index])
        if rgb is None:
            # Sensible defaults for the Office theme when theme1.xml is absent.
            rgb = {"lt1": "FFFFFF", "dk1": "000000",
                   "lt2": "E7E6E6", "dk2": "44546A"}.get(_THEME_INDEX[theme_index])
        if rgb is None:
            return None
        return apply_tint(rgb, tint) if tint else rgb


def apply_tint(rgb: str, tint: float) -> str:
    """ECMA-376 tint: shift lightness toward white (tint>0) or black (tint<0)."""
    r, g, b = (int(rgb[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    h, lum, s = colorsys.rgb_to_hls(r, g, b)
    lum = lum * (1.0 + tint) if tint < 0 else lum * (1.0 - tint) + tint
    lum = min(1.0, max(0.0, lum))
    r, g, b = colorsys.hls_to_rgb(h, lum, s)
    return "{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


# --------------------------------------------------------------------------- #
# Colour -> role
# --------------------------------------------------------------------------- #
def _normalize_rgb(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    v = value.strip().upper()
    if len(v) == 8:      # ARGB
        v = v[2:]
    return v if re.fullmatch(r"[0-9A-F]{6}", v) else None


def resolve_color(color_obj: Any, theme: Theme) -> str | None:
    """Effective RGB for an openpyxl Color, whichever way it was stored."""
    if color_obj is None:
        return None
    ctype = getattr(color_obj, "type", None)

    if ctype == "rgb":
        return _normalize_rgb(getattr(color_obj, "rgb", None))
    if ctype == "theme":
        idx = getattr(color_obj, "theme", None)
        if idx is None:
            return None
        return theme.resolve(int(idx), float(getattr(color_obj, "tint", 0.0) or 0.0))
    if ctype == "indexed":
        idx = int(getattr(color_obj, "indexed", 0) or 0)
        if 0 <= idx < len(COLOR_INDEX):
            return _normalize_rgb(COLOR_INDEX[idx])
        return None
    if ctype == "auto":
        return "000000"
    return _normalize_rgb(getattr(color_obj, "rgb", None))


def classify_color(rgb: str | None) -> tuple[Role, float | None, float | None, float | None]:
    """Hue-based classification. Works for any blue a company happens to use —
    pure 0000FF, Office 0070C0, navy 1F4E79 — without a hardcoded palette."""
    if rgb is None:
        return Role.formula, None, None, None      # no colour set == automatic == black
    r, g, b = (int(rgb[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    h, lum, s = colorsys.rgb_to_hls(r, g, b)
    hue = h * 360.0

    if s < 0.15 or lum < 0.06:
        return Role.formula, hue, s, lum           # black / grey / near-black
    if 190.0 <= hue <= 265.0 and lum <= 0.80:
        # Saturation floor matters: Office's "Text 2" (#44546A) is a desaturated
        # blue-grey used for *headings*. Treating it as an input slot would have
        # FinScan overwriting section titles. Genuine input blues are saturated.
        if s < 0.30:
            return Role.unknown, hue, s, lum
        return Role.input, hue, s, lum             # blue
    if 80.0 <= hue < 190.0 and lum <= 0.80:
        return Role.link_sheet, hue, s, lum        # green
    if (hue < 25.0 or hue > 330.0) and lum <= 0.80:
        return Role.link_external, hue, s, lum     # red
    return Role.unknown, hue, s, lum


_CELL_REF = re.compile(r"(?<![A-Za-z0-9_$])\$?[A-Za-z]{1,3}\$?[0-9]{1,7}(?![0-9(])")


def formula_has_references(formula: str | None) -> bool:
    """Does this formula actually point at other cells?

    Analysts routinely type arithmetic straight into a "formula": a D&A row
    holding `=6076+1575+8134`, or an other-income row holding `=2638-3578`.
    Those look like formulas — black text, leading `=` — but they are last
    quarter's hardcoded numbers wearing a formula's clothes. Copying one into
    the new column silently asserts that this quarter's figures are identical to
    last quarter's, which is both wrong and invisible.
    """
    if not formula or not isinstance(formula, str) or not formula.startswith("="):
        return False
    body = re.sub(r'"[^"]*"', "", formula)          # ignore string literals
    return bool(_CELL_REF.search(body))


def cell_has_formula(cell: Any) -> bool:
    if getattr(cell, "data_type", None) == "f":
        return True
    v = cell.value
    return isinstance(v, str) and v.startswith("=")


def probe_cell(cell: Any, theme: Theme) -> CellStyle:
    """Full verdict for one cell."""
    has_formula = cell_has_formula(cell)
    style_name = getattr(cell, "style", None)
    font = getattr(cell, "font", None)
    rgb = resolve_color(getattr(font, "color", None), theme)
    role, hue, sat, lum = classify_color(rgb)

    # An explicit named style beats colour inference — it states the intent.
    if style_name in INPUT_STYLE_NAMES and not has_formula:
        role = Role.input
    elif style_name in FORMULA_STYLE_NAMES:
        role = Role.formula

    # A formula is a formula no matter how it is painted. This is the hard veto:
    # a blue-coloured formula cell is still never overwritten.
    if has_formula:
        role = Role.formula
    elif cell.value is None and role is Role.formula and rgb is None:
        role = Role.empty

    return CellStyle(role=role, rgb=rgb, has_formula=has_formula,
                     style_name=style_name, hue=hue, sat=sat, lum=lum)

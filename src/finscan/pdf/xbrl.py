"""A BMV XBRL package (the `.zip` the exchange publishes) read into a PdfDoc.

Mexican issuers file their quarterly report with the BMV as XBRL. The exchange
serves it as a zip holding one JSON file of the whole report: the taxonomy (each
statement is a "role", `[520000] Estado de flujos de efectivo`, with its lines in
presentation order and their labels) and every reported fact with its period.

Nothing is OCR'd or read by a model: each figure is the filing's own fact, so it
needs no verification against a page. The result is the same PdfDoc the PDF
readers produce, so the mapping, period selection and de-cumulation work
unchanged, and a mapping written against a BMV PDF (`title:[520000]`, Spanish
captions, full pesos) reads the package identically.

Only facts without a dimension are kept: a dimensional fact belongs to a member of
an axis (one line of a segment note, one column of the equity statement), not to
the statement's own total.
"""
from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

from finscan.pdf.statements import classify
from finscan.schema import Column, Issue, PdfDoc, Statement, StatementRow

#: A role is a statement only when its name opens with the BMV's six-digit code.
_ROLE_CODE = "["


def is_package(path: str | Path) -> bool:
    return str(path).lower().endswith(".zip")


def _load(path: str | Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".json")]
        if not names:
            raise ValueError(f"{Path(path).name} holds no JSON report; it is not a BMV "
                             f"XBRL package")
        data = json.loads(archive.read(names[0]).decode("utf-8"))
    if not isinstance(data, dict) or "Taxonomia" not in data or "HechosPorId" not in data:
        raise ValueError(f"{Path(path).name} is not a BMV XBRL package (no taxonomy or facts)")
    return data


def _label(concept: dict[str, Any]) -> str:
    """The Spanish standard label, else English, else the concept's own name."""
    labels = concept.get("Etiquetas") or {}
    for language in ("es", "en"):
        by_role = labels.get(language) or {}
        entry = by_role.get("http://www.xbrl.org/2003/role/label") or next(
            iter(by_role.values()), None)
        if entry and entry.get("Valor"):
            return str(entry["Valor"]).strip()
    return str(concept.get("Nombre") or "")


def _day(stamp: str | None) -> date | None:
    return date.fromisoformat(stamp[:10]) if stamp else None


def _monetary_units(data: dict[str, Any]) -> set[str]:
    return {uid for uid, unit in (data.get("UnidadesPorId") or {}).items()
            if any("iso4217" in (m.get("EspacioNombres") or "")
                   for m in unit.get("Medidas") or [])}


def _facts(data: dict[str, Any]) -> dict[tuple[str, tuple], float]:
    """(concept, period) -> amount, for monetary facts with no dimension."""
    contexts = data.get("ContextosPorId") or {}
    money = _monetary_units(data)
    out: dict[tuple[str, tuple], float] = {}
    for fact in (data.get("HechosPorId") or {}).values():
        value = fact.get("ValorNumerico")
        context = contexts.get(fact.get("IdContexto"))
        if (value is None or context is None or fact.get("EsValorNil")
                or context.get("ContieneInformacionDimensional")
                or fact.get("IdUnidad") not in money):
            continue
        period = context.get("Periodo") or {}
        if period.get("FechaInstante"):
            key = ("instant", _day(period["FechaInstante"]))
        elif period.get("FechaInicio") and period.get("FechaFin"):
            key = ("duration", _day(period["FechaInicio"]), _day(period["FechaFin"]))
        else:
            continue
        out.setdefault((fact["IdConcepto"], key), float(value))
    return out


def _column(index: int, key: tuple) -> Column:
    if key[0] == "instant":
        return Column(index=index, header=key[1].isoformat(), kind="point_in_time",
                      months=None, end=key[1].isoformat())
    _, start, end = key
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    return Column(index=index, header=f"{start.isoformat()} - {end.isoformat()}",
                  kind="period", months=months, end=end.isoformat())


def _order(key: tuple):
    """Latest period first; within one end date the longest span first."""
    end = key[1] if key[0] == "instant" else key[2]
    span = 0 if key[0] == "instant" else (key[2] - key[1]).days
    return (-end.toordinal(), -span)


def _statement(ordinal: int, role: dict[str, Any], concepts: dict[str, Any],
               facts: dict[tuple[str, tuple], float]) -> Statement | None:
    lines: list[tuple[str, str]] = []

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            concept = concepts.get(node["IdConcepto"], {})
            if not concept.get("EsAbstracto"):
                lines.append((node["IdConcepto"], _label(concept)))
            walk(node.get("SubEstructuras") or [])

    walk(role.get("Estructuras") or [])
    wanted = {concept for concept, _ in lines}
    periods = sorted({key for (concept, key) in facts if concept in wanted}, key=_order)
    if not periods:
        return None
    # A period only a stray line reports (the cash flow prints its quarter and
    # twelve-month net income but nothing else beside them) is not a column of this
    # statement: standing there it would be chosen for every row and read as blank.
    coverage = {key: sum(1 for concept, _ in lines if (concept, key) in facts)
                for key in periods}
    fullest = max(coverage.values())
    periods = [key for key in periods if coverage[key] * 2 >= fullest]
    title = role["Nombre"].strip()
    columns = [_column(i, key) for i, key in enumerate(periods)]
    rows = []
    for concept, label in lines:
        values = [facts.get((concept, key)) for key in periods]
        if any(v is not None for v in values):
            rows.append(StatementRow(caption=label, values=values, raw=concept))
    if not rows:
        return None
    return Statement(page=ordinal, kind=classify(title), title=title,
                     heading="BMV XBRL package", columns=columns, rows=rows,
                     units="units")


def read_package(path: str | Path, sha: str) -> PdfDoc:
    """Read a BMV XBRL package into the statements a PDF reader would produce."""
    data = _load(path)
    concepts = data["Taxonomia"].get("ConceptosPorId") or {}
    facts = _facts(data)
    doc = PdfDoc(path=str(path), sha256=sha, units="units", currency="MXN")
    for ordinal, role in enumerate(data["Taxonomia"].get("RolesPresentacion") or [], start=1):
        if not str(role.get("Nombre", "")).lstrip().startswith(_ROLE_CODE):
            continue
        statement = _statement(ordinal, role, concepts, facts)
        if statement is not None:
            doc.statements.append(statement)

    if doc.statements:
        doc.issues.append(Issue(
            code="xbrl_package", severity="info",
            message=f"Read from the BMV XBRL package {Path(path).name}: "
                    f"{len(doc.statements)} statement(s). Every figure is the filing's "
                    f"own fact, in whole pesos; no page was read or verified."))
    else:
        doc.issues.append(Issue(
            code="no_statements", severity="error",
            message=f"{Path(path).name} holds no statement with amounts."))
    return doc

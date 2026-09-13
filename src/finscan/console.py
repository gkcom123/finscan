"""Terminal progress and colour for CLI runs.

A FinScan run is a dozen nodes, several of which make LLM calls and can sit quiet
for tens of seconds. Without progress the operator cannot tell a slow vision-OCR
pass from a hang, and cannot see which phase produced the warnings that scroll past
at the end. This module prints one line per phase as it completes, with the issues
that phase raised, so the run reads as a sequence of steps rather than a pause
followed by a wall of text.

Colour is opt-out and auto-disabling: a redirected stdout, NO_COLOR, or
TERM=dumb all fall back to plain ASCII, so `finscan company x > run.txt` stays
readable and greppable.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"


def supports_color(stream: Any = None) -> bool:
    """True when it is safe to emit ANSI escapes.

    NO_COLOR is honoured as an unset-or-set convention (https://no-color.org):
    any value, including the empty string, disables colour.
    """
    stream = stream or sys.stdout
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("FINSCAN_FORCE_COLOR"):
        return True
    try:
        return bool(stream.isatty())
    except Exception:      # pragma: no cover - exotic stream objects
        return False


class Palette:
    """Colour functions that degrade to identity when colour is off, so callers
    never branch on whether colour is enabled."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.enabled else text

    def bold(self, t: str) -> str:
        return self._wrap(BOLD, t)

    def dim(self, t: str) -> str:
        return self._wrap(DIM, t)

    def red(self, t: str) -> str:
        return self._wrap(RED, t)

    def green(self, t: str) -> str:
        return self._wrap(GREEN, t)

    def yellow(self, t: str) -> str:
        return self._wrap(YELLOW, t)

    def blue(self, t: str) -> str:
        return self._wrap(BLUE, t)

    def cyan(self, t: str) -> str:
        return self._wrap(CYAN, t)


#: Node name -> what that phase actually does, in the operator's terms rather than
#: the function's. Keep in step with graph/build.py.
PHASE_LABELS: dict[str, str] = {
    "ingest_pdf": "Read PDF text, tables, OCR",
    "discover": "Discover workbook layout",
    "extract": "Extract financials from PDF",
    "resolve_profile": "Resolve company profile",
    "normalize": "Normalize units and signs",
    "resolve_cumulative_periods": "Correct cumulative periods",
    "refine_mapping": "Map workbook captions",
    "extract_label_rows": "Match remaining row labels",
    "check_periods": "Check period continuity",
    "crosscheck": "Crosscheck model formulas",
    "validate": "Validate identities and coverage",
    "prepare_retry": "Prepare extraction retry",
    "write_excel": "Write output workbook",
    "report": "Build review report",
}

#: Phases that make at least one LLM call, so a long pause there is expected work
#: rather than a stall. Shown to the operator as a hint while the phase runs.
_LLM_PHASES = {"extract", "refine_mapping", "extract_label_rows"}


class ProgressPrinter:
    """Prints one line per pipeline phase, with that phase's own issue counts."""

    def __init__(self, total: int, stream: Any = None, color: bool | None = None):
        self.stream = stream or sys.stdout
        self.c = Palette(supports_color(self.stream) if color is None else color)
        self.total = total
        self.index = 0
        self.started = time.perf_counter()
        self._live = self.c.enabled      # only overwrite lines on a real terminal

    def _write(self, text: str) -> None:
        self.stream.write(text)
        self.stream.flush()

    def header(self, title: str, rows: list[tuple[str, str]]) -> None:
        self._write(f"\n{self.c.bold(self.c.cyan(title))}\n")
        for key, value in rows:
            self._write(f"  {self.c.dim(key + ':'):<20} {value}\n")
        self._write("\n")

    def start(self, name: str) -> None:
        self.index += 1
        if not self._live:
            return
        label = PHASE_LABELS.get(name, name)
        hint = self.c.dim("  (calling the model)") if name in _LLM_PHASES else ""
        self._write(f"  {self.c.dim(f'[{self.index:>2}/{self.total}]')} "
                    f"{self.c.blue('•')} {label}{hint}")

    def finish(self, name: str, issues: list, seconds: float) -> None:
        errors = sum(1 for i in issues if getattr(i, "severity", "") == "error")
        warnings = sum(1 for i in issues if getattr(i, "severity", "") == "warning")

        if errors:
            mark, colour = "x", self.c.red
        elif warnings:
            mark, colour = "!", self.c.yellow
        else:
            mark, colour = "+", self.c.green

        counts = []
        if errors:
            counts.append(self.c.red(f"{errors} error{'s' if errors > 1 else ''}"))
        if warnings:
            counts.append(self.c.yellow(f"{warnings} warning{'s' if warnings > 1 else ''}"))
        detail = f"  {' · '.join(counts)}" if counts else ""

        label = PHASE_LABELS.get(name, name)
        line = (f"  {self.c.dim(f'[{self.index:>2}/{self.total}]')} {colour(mark)} "
                f"{label:<34}{self.c.dim(f'{seconds:5.1f}s')}{detail}")
        self._write(f"\r\033[K{line}\n" if self._live else f"{line}\n")

    def failed(self, name: str, exc: BaseException, seconds: float) -> None:
        label = PHASE_LABELS.get(name, name)
        line = (f"  {self.c.dim(f'[{self.index:>2}/{self.total}]')} {self.c.red('x')} "
                f"{label:<34}{self.c.dim(f'{seconds:5.1f}s')}  "
                f"{self.c.red(type(exc).__name__)}: {exc}")
        self._write(f"\r\033[K{line}\n" if self._live else f"{line}\n")

    def total_elapsed(self) -> float:
        return time.perf_counter() - self.started


def instrument(name: str, fn: Callable[[dict], dict], printer: ProgressPrinter) -> Callable[[dict], dict]:
    """Wrap a graph node so it announces itself and reports what it raised.

    The issues a node returns are exactly the ones it raised itself (GraphState
    accumulates them with an add-reducer), so no diffing against prior state is
    needed to attribute them to a phase.
    """
    def instrumented(state: dict) -> dict:
        printer.start(name)
        t0 = time.perf_counter()
        try:
            out = fn(state)
        except BaseException as exc:
            printer.failed(name, exc, time.perf_counter() - t0)
            raise
        issues = (out or {}).get("issues") or []
        printer.finish(name, issues, time.perf_counter() - t0)
        return out

    instrumented.__name__ = getattr(fn, "__name__", name)
    instrumented.__doc__ = getattr(fn, "__doc__", None)
    return instrumented

import sys
from finscan.extract.pdf_reader import read_pdf
from finscan.extract.extractor import extract
from finscan.extract.normalize import to_target_units
from finscan.validate import enforce_sign_rules
from finscan.excel.discovery import discover
from finscan.excel.cumulative import resolve_cumulative_periods

pdf, xlsx = sys.argv[1], sys.argv[2]
doc = read_pdf(pdf)
ex, _ = extract(doc.as_prompt_text(),
                statements_text=doc.as_prompt_text(statements_only=True))
values, months, _ = to_target_units(ex, "units")
values, _ = enforce_sign_rules(values)
plan = discover(xlsx)

print("tagged cumulative:", months or "(none)")
before = dict(values)
values, issues = resolve_cumulative_periods(
    plan, xlsx, values, months, {"Model"}, ex.meta)

for k in sorted(set(before) | set(values)):
    b, a = before.get(k), values.get(k)
    if b != a:
        print(f"  {k:<40} {b:>20,.0f} -> "
              f"{'DROPPED' if a is None else format(a, ',.0f')}")
for i in issues:
    print(f"[{i.severity}] {i.code}: {i.message}")

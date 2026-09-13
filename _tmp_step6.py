import sys
from finscan.extract.pdf_reader import read_pdf
from finscan.extract.extractor import extract
from finscan.extract.normalize import to_target_units
from finscan.validate import enforce_sign_rules

doc = read_pdf(sys.argv[1])
ex, notes = extract(doc.as_prompt_text(),
                    statements_text=doc.as_prompt_text(statements_only=True))
print("PDF units:", ex.meta.units, "| period:", ex.meta.period_label)

values, months, i1 = to_target_units(ex, "units")
values, i2 = enforce_sign_rules(values)

for k in sorted(values):
    tag = f"  [{months[k]}-month cumulative]" if k in months else ""
    print(f"  {k:<40} {values[k]:>22,.0f}{tag}")
print()
for i in i1 + i2:
    print(f"[{i.severity}] {i.code}: {i.message}")

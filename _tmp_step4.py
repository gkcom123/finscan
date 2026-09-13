import sys
from finscan.extract.pdf_reader import read_pdf
from finscan.extract.extractor import extract

path = sys.argv[1]
doc = read_pdf(path)
ex, notes = extract(doc.as_prompt_text(),
                    statements_text=doc.as_prompt_text(statements_only=True))

print("PDF  :", path)
print("META :", ex.meta.model_dump())
for n in notes:
    print("NOTE :", n)
if ex.notes:
    print("LLM  :", ex.notes)

print(f"{len(ex.line_items)} line items")
for li in ex.line_items:
    mc = f"{li.months_covered}m" if li.months_covered else "standalone"
    row = (li.source_row_text or "")[:110]
    print(f"  {li.field.value:<38} {li.value:>16,.2f}  {mc:<10} "
          f"conf={li.confidence:.2f}  <- {li.label_in_pdf}")
    print(f"      row: {row}")

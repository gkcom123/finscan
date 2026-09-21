"""Stage 4: values.json + workbook -> a new column in a copy of the workbook."""
from finscan2.write.column import WriteResult, run, write_column

__all__ = ["write_column", "run", "WriteResult"]

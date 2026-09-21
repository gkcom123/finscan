"""Company path resolution — the convention behind `finscan2 company <key> <stage>`.

The interesting cases are the refusals. Picking one of two PDFs is how a run
silently reads last quarter's filing, which looks exactly like a correct run.
"""
from __future__ import annotations

import pytest

from finscan2.company import CompanyError, project_root, resolve


def _tree(tmp_path, *, pdfs=("filing.pdf",), excels=("Model_1Q26.xlsx",), extra=()):
    folder = tmp_path / "inbox" / "Almarai"
    folder.mkdir(parents=True)
    (tmp_path / "maps").mkdir()
    for name in (*pdfs, *excels, *extra):
        (folder / name).write_text("x", encoding="utf-8")
    return tmp_path


def test_every_stage_path_comes_from_the_company_key(tmp_path):
    root = _tree(tmp_path)
    paths = resolve("almarai", root)
    assert paths.pdf.name == "filing.pdf"
    assert paths.excel.name == "Model_1Q26.xlsx"
    assert paths.pdf_json.name == "filing.pdf.json"
    assert paths.map == root / "maps" / "almarai.json"
    assert paths.values == root / "_work" / "almarai_values.json"
    assert paths.output == root / "inbox" / "almarai_v2_output.xlsx"


def test_the_folder_name_is_matched_case_insensitively(tmp_path):
    """The folder is 'Almarai'; nobody types that on a command line."""
    root = _tree(tmp_path)
    assert resolve("ALMARAI", root).folder.name == "Almarai"
    assert resolve("almarai", root).key == "almarai"


def test_two_filings_are_refused_rather_than_one_being_picked(tmp_path):
    root = _tree(tmp_path, pdfs=("q1.pdf", "q2.pdf"))
    with pytest.raises(CompanyError) as caught:
        resolve("almarai", root)
    assert "q1.pdf" in str(caught.value) and "--pdf" in str(caught.value)


def test_a_generated_workbook_is_not_mistaken_for_the_source_model(tmp_path):
    """The output lands beside the model on a second run; it must not then become
    the input, which would write a column into a copy of a copy."""
    root = _tree(tmp_path, extra=("almarai_v2_output.xlsx", "Model_1Q26_updated.xlsx"))
    assert resolve("almarai", root).excel.name == "Model_1Q26.xlsx"


def test_an_open_workbook_lock_file_is_ignored(tmp_path):
    """Excel leaves '~$name.xlsx' beside an open file."""
    root = _tree(tmp_path, extra=("~$Model_1Q26.xlsx",))
    assert resolve("almarai", root).excel.name == "Model_1Q26.xlsx"


def test_an_unknown_company_lists_what_is_available(tmp_path):
    root = _tree(tmp_path)
    with pytest.raises(CompanyError) as caught:
        resolve("tencent", root)
    assert "Almarai" in str(caught.value)


def test_a_missing_stage_input_names_the_command_that_makes_it(tmp_path):
    root = _tree(tmp_path)
    paths = resolve("almarai", root)
    with pytest.raises(CompanyError) as caught:
        paths.require("values", "python -m finscan2.cli company almarai match")
    assert "company almarai match" in str(caught.value)


def test_an_explicit_override_wins_over_the_convention(tmp_path):
    root = _tree(tmp_path, pdfs=("q1.pdf", "q2.pdf"))
    paths = resolve("almarai", root, pdf=str(root / "inbox" / "Almarai" / "q2.pdf"))
    assert paths.pdf.name == "q2.pdf"


def test_the_root_is_found_by_walking_up_to_the_inbox(tmp_path):
    root = _tree(tmp_path)
    deep = root / "src" / "finscan2" / "match"
    deep.mkdir(parents=True)
    assert project_root(deep) == root.resolve()

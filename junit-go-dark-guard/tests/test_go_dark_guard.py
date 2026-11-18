"""Tests for the junit go-dark guard, run against JUnit XML fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guard  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def fx(name: str) -> str:
    return str(FIXTURES / name)


def test_mixed_report_passes_with_individual_skips():
    counts, problems = guard.check([fx("mixed.xml")], 1, False)
    assert problems == []
    assert (counts.tests, counts.skipped) == (3, 1)


def test_missing_report_fails():
    _, [problem] = guard.check([fx("nope.xml")], 1, False)
    assert "the suite did not run" in problem


def test_zero_tests_fails():
    _, [problem] = guard.check([fx("empty.xml")], 1, False)
    assert "0 test cases ran" in problem


def test_zero_tests_passes_when_minimum_is_zero():
    assert guard.check([fx("empty.xml")], 0, False)[1] == []


def test_all_skipped_fails_unless_allowed():
    _, [problem] = guard.check([fx("all-skipped.xml")], 1, False)
    assert "all 2 test cases were skipped" in problem
    assert guard.check([fx("all-skipped.xml")], 1, True)[1] == []


def test_minimum_is_enforced():
    _, [problem] = guard.check([fx("mixed.xml")], 4, False)
    assert "fewer than the minimum of 4" in problem


def test_counts_sum_across_files_and_globs():
    # mixed (3, 1 skipped) + all-skipped (2, 2 skipped): not all skipped overall.
    counts, problems = guard.check([str(FIXTURES / "*.xml")], 1, False)
    assert problems == []
    assert (counts.tests, counts.skipped) == (5, 3)


def test_duplicate_matches_count_once():
    counts, _ = guard.check([fx("mixed.xml"), str(FIXTURES / "mix*.xml")], 1, False)
    assert counts.tests == 3


def test_nested_testsuites_count_each_case_once(tmp_path: Path):
    report = tmp_path / "nested.xml"
    report.write_text(
        '<testsuites tests="2"><testsuite name="outer" tests="2">'
        '<testsuite name="inner" tests="2"><testcase name="a"/><testcase name="b"/>'
        "</testsuite></testsuite></testsuites>"
    )
    counts, problems = guard.check([str(report)], 1, False)
    assert problems == [] and counts.tests == 2


def test_unparseable_report_fails(tmp_path: Path):
    report = tmp_path / "broken.xml"
    report.write_text("<testsuite><testcase")
    _, [problem] = guard.check([str(report)], 1, False)
    assert "not valid XML" in problem


def test_cli_writes_outputs_and_exit_code(tmp_path: Path, monkeypatch, capsys):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert guard.main([fx("mixed.xml")]) == 0
    assert out.read_text() == "tests=3\nskipped=1\n"
    assert guard.main([fx("all-skipped.xml")]) == 1
    assert "::error title=Test suite went dark::" in capsys.readouterr().out


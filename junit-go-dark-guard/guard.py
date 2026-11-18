"""Fail when a test suite went dark: it ran nothing, or skipped everything.

A test step passes when zero tests are collected, and it passes when every
test skips because a service, a credential or a marker is missing. Both look
green. This guard reads the JUnit XML the suite wrote and fails the build when:

* no report file exists (the suite did not run, or wrote somewhere else);
* a report cannot be parsed;
* fewer than `--min-tests` test cases ran;
* every test case was skipped (unless `--allow-all-skipped`).

Individual skips still pass. Counting is by `<testcase>` element, so it works
for any tool that writes JUnit XML (pytest, Jest, Go, Maven, Gradle), with or
without nested `<testsuite>` elements.

Usage: python guard.py [--min-tests N] [--allow-all-skipped] REPORT [REPORT ...]
REPORT may be a glob, for example `reports/**/*.xml`.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class Counts:
    tests: int = 0
    skipped: int = 0


def count(path: str) -> Counts:
    counts = Counts()
    for case in ET.parse(path).getroot().iter("testcase"):
        counts.tests += 1
        if case.find("skipped") is not None:
            counts.skipped += 1
    return counts


def resolve(patterns: list[str]) -> list[str]:
    files: list[str] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=True))
        files += [m for m in matches if os.path.isfile(m) and m not in files]
    return files


def check(patterns: list[str], min_tests: int, allow_all_skipped: bool) -> tuple[Counts, list[str]]:
    total = Counts()
    files = resolve(patterns)
    if not files:
        return total, [f"no JUnit report found at {', '.join(patterns)}: the suite did not run"]
    problems = []
    for path in files:
        try:
            c = count(path)
        except ET.ParseError as e:
            problems.append(f"{path}: not valid XML ({e})")
            continue
        print(f"{path}: {c.tests} test cases, {c.skipped} skipped")
        total.tests += c.tests
        total.skipped += c.skipped
    if problems:
        return total, problems
    if total.tests < min_tests:
        problems.append(f"{total.tests} test cases ran, fewer than the minimum of {min_tests}")
    elif total.tests and total.skipped >= total.tests and not allow_all_skipped:
        problems.append(f"all {total.tests} test cases were skipped")
    return total, problems


def write_outputs(counts: Counts) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a") as fh:
        fh.write(f"tests={counts.tests}\nskipped={counts.skipped}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reports", nargs="+")
    ap.add_argument("--min-tests", type=int, default=1)
    ap.add_argument("--allow-all-skipped", action="store_true")
    args = ap.parse_args(argv)

    counts, problems = check(args.reports, args.min_tests, args.allow_all_skipped)
    write_outputs(counts)
    print(f"go-dark guard: {counts.tests} test cases, {counts.skipped} skipped")
    for p in problems:
        print(f"::error title=Test suite went dark::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

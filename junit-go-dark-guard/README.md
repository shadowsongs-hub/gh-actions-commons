# junit-go-dark-guard

A composite action that fails the build when a test suite **went dark**. A test step
reports green in two cases where nothing was tested:

- the runner collected **zero** tests (a wrong path, a renamed directory, a marker filter
  that matches nothing);
- **every** test skipped (a missing service, credential or environment variable makes
  each test skip itself).

The guard reads the JUnit XML report the suite wrote and fails when:

| Case | Message |
|------|---------|
| No report file matches | the suite did not run |
| A report is not valid XML | not valid XML |
| Fewer than `min-tests` test cases ran | fewer than the minimum |
| Every test case was skipped | all N test cases were skipped |

Individual skips still pass. Test failures are not the guard's job: the test step
already fails on those.

Counting is by `<testcase>` element, so it works for any tool that writes JUnit XML
(pytest, Jest with `jest-junit`, `gotestsum`, Maven Surefire, Gradle), with or without
nested `<testsuite>` elements.

## Usage

Run it after the test step with `if: always()`. Then it also runs when the test step
crashed before it wrote a report, and says so.

```yaml
- name: Integration tests
  run: pytest tests/integration --junitxml=integration-report.xml

- name: Go-dark guard
  if: always()
  uses: shadowsongs-hub/gh-actions-commons/junit-go-dark-guard@main
  with:
    report: integration-report.xml
```

Several reports, or a glob:

```yaml
  with:
    report: |
      reports/unit.xml
      reports/integration/**/*.xml
    min-tests: "50"
```

## Inputs

| Input | Default | Purpose |
|-------|---------|---------|
| `report` | required | Report path or glob. One per line. Counts are summed across files, and a file matched twice counts once. |
| `min-tests` | `1` | Fail when fewer test cases than this ran. Set it near the real count to also catch a suite that shrank. |
| `allow-all-skipped` | `false` | Pass even when every test case skipped. |

## Outputs

| Output | Value |
|--------|-------|
| `tests` | Test cases in the reports. |
| `skipped` | Skipped test cases. |

## Requirements

Python 3 on the runner. No packages.

## Run it locally

```bash
python junit-go-dark-guard/guard.py --min-tests 1 integration-report.xml
```

# release-please-lint

A composite action that fails a PR when a [release-please](https://github.com/googleapis/release-please)
manifest setup would break **without an error**. release-please reports none of these. You
find out when a release does not happen.

| Check | What goes wrong without it |
|-------|----------------------------|
| A manifest version has no matching tag | release-please treats the version as released, finds no release, and opens no release PR. A new package that starts at `0.1.0` instead of `0.0.0` is never released. |
| A workflow `on.push.tags` pattern matches no tag release-please creates | The release PR merges and the tag is pushed, but the deploy never runs. Example: the trigger says `app@v*` while the config emits `app-v1.2.3`. |
| The manifest lists a package the config does not | Stale entry. |
| A package path does not exist, or a manifest version is not semver | Broken config. |

The expected tag comes from the config: `include-component-in-tag`, `include-v-in-tag`
and `tag-separator`, with per-package values overriding top-level ones. The component is
`component`, else `package-name`, else the name in `package.json` (node) or
`pyproject.toml` (python), with any npm scope removed.

## Usage

```yaml
on:
  pull_request:
    paths:
      - release-please-config.json
      - .release-please-manifest.json
      - .github/workflows/**

jobs:
  release-please-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: shadowsongs-hub/gh-actions-commons/release-please-lint@main
```

A shallow checkout is enough. Tags are read from `origin` with `git ls-remote`, and from
local tags if `origin` is not reachable.

## Inputs

| Input | Default | Purpose |
|-------|---------|---------|
| `working-directory` | `.` | Repo root to lint. Other paths are relative to it. |
| `config-file` | `release-please-config.json` | release-please config. |
| `manifest-file` | `.release-please-manifest.json` | release-please manifest. |
| `workflows-dir` | `.github/workflows` | Workflows scanned for tag triggers. |
| `ignore-tag-patterns` | empty | Newline-separated tag patterns to skip, for tags pushed by something else (a manual release tag, another tool). |
| `check-tags` | `true` | Check that each manifest version has its tag. |

Errors and warnings print as GitHub annotations. Only errors fail the step.

## Requirements

`git` and Python 3.11 or newer on the runner. PyYAML is used if present. If it is not, the
action installs it into a private venv under `$RUNNER_TEMP`, so the runner's Python is not
changed.

## Run it locally

```bash
python release-please-lint/lint.py --root /path/to/repo
```

## Limits

- Tag existence stands in for "a release exists". release-please also reads GitHub
  releases. A tag with no release passes this check.
- Only `on.push.tags` triggers are checked. Workflows that start from `release` events or
  `workflow_run` are not.
- `bootstrap-sha` and `last-release-sha` are not taken into account.

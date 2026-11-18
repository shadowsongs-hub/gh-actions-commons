"""Lint a release-please manifest setup for mistakes that fail silently.

Each check below matches a failure that release-please does not report:

* A manifest entry claims a version that has no tag. release-please finds no
  release for it, then either skips the package ("No commits for path") or
  proposes a bump past it. Either way that version is never released, and the
  first release of a new package never happens.
* A workflow triggers only on tag patterns that release-please never produces,
  for example `app@v*` when the config emits `app-v1.2.3`. The release PR merges,
  the tag is pushed, and the deploy never runs.
* The manifest and the config disagree on which packages exist, a package path
  is missing on disk, or a manifest version is not semver.

Usage: python lint.py [--root DIR] [--config FILE] [--manifest FILE]
                      [--workflows-dir DIR] [--ignore-tag-pattern PATTERN ...]
                      [--no-check-tags]
Exit code 1 if any error was found. Warnings never fail the run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
SAMPLE_VERSION = "1.2.3"


@dataclass
class Package:
    path: str
    component: str | None
    include_component: bool
    include_v: bool
    separator: str

    def tag(self, version: str) -> str | None:
        """The tag release-please creates for this package at `version`."""
        v = "v" if self.include_v else ""
        if not self.include_component:
            return f"{v}{version}"
        if not self.component:
            return None
        return f"{self.component}{self.separator}{v}{version}"


@dataclass
class Report:
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    def error(self, file: str, msg: str) -> None:
        self.errors.append((file, msg))

    def warn(self, file: str, msg: str) -> None:
        self.warnings.append((file, msg))


def _option(pkg: dict, config: dict, key: str, default):
    """A per-package option overrides the top-level one, as in release-please."""
    if key in pkg:
        return pkg[key]
    return config.get(key, default)


def _declared_name(root: Path, path: str, release_type: str | None) -> str | None:
    """The package name release-please reads from the package manifest file."""
    pkg_dir = root / path
    if release_type == "node" and (pkg_dir / "package.json").is_file():
        return json.loads((pkg_dir / "package.json").read_text()).get("name")
    if release_type == "python" and (pkg_dir / "pyproject.toml").is_file():
        data = tomllib.loads((pkg_dir / "pyproject.toml").read_text())
        return data.get("project", {}).get("name")
    return None


def component_name(root: Path, path: str, pkg: dict, config: dict) -> str | None:
    if pkg.get("component"):
        return pkg["component"]
    name = pkg.get("package-name") or _declared_name(
        root, path, _option(pkg, config, "release-type", None)
    )
    if not name:
        return None
    # release-please drops an npm scope from the component: @org/app -> app.
    return name.split("/")[-1]


def load_packages(root: Path, config: dict) -> list[Package]:
    packages = []
    for path, pkg in (config.get("packages") or {}).items():
        pkg = pkg or {}
        packages.append(
            Package(
                path=path,
                component=component_name(root, path, pkg, config),
                include_component=_option(pkg, config, "include-component-in-tag", True),
                include_v=_option(pkg, config, "include-v-in-tag", True),
                separator=_option(pkg, config, "tag-separator", "-"),
            )
        )
    return packages


def github_filter_regex(pattern: str) -> re.Pattern[str]:
    """Translate a GitHub Actions branch/tag filter pattern to a regex.

    GitHub rules: `**` any characters, `*` any characters except `/`, `?` zero
    or one of the preceding character, `+` one or more of the preceding
    character, `[...]` a character class, `\\` escapes the next character.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c in "?+":
            out.append(c)
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                out.append(pattern[i : end + 1])
                i = end + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def workflow_tag_patterns(workflows_dir: Path) -> list[tuple[str, str]]:
    """(workflow file, pattern) for every positive `on.push.tags` filter."""
    found = []
    if not workflows_dir.is_dir():
        return found
    for wf in sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")]):
        doc = yaml.safe_load(wf.read_text()) or {}
        # PyYAML reads the bare key `on` as boolean True.
        on = doc.get("on", doc.get(True))
        if not isinstance(on, dict):
            continue
        push = on.get("push")
        if not isinstance(push, dict):
            continue
        tags = push.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        found += [(str(wf), str(t)) for t in tags if not str(t).startswith("!")]
    return found


def existing_tags(root: Path) -> tuple[set[str], str]:
    """Tags on the remote if reachable, else local tags. Returns (tags, source)."""
    remote = subprocess.run(
        ["git", "-C", str(root), "ls-remote", "--tags", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode == 0:
        tags = set()
        for line in remote.stdout.splitlines():
            ref = line.split("\t", 1)[-1]
            tags.add(ref.removeprefix("refs/tags/").removesuffix("^{}"))
        return tags, "origin"
    local = subprocess.run(
        ["git", "-C", str(root), "tag", "-l"], capture_output=True, text=True, check=True
    )
    return set(local.stdout.split()), "local"


def lint(
    root: Path,
    config_file: str,
    manifest_file: str,
    workflows_dir: str,
    ignore_patterns: list[str],
    check_tags: bool,
) -> Report:
    report = Report()
    try:
        config = json.loads((root / config_file).read_text())
    except FileNotFoundError:
        report.error(config_file, "config file not found")
        return report
    except json.JSONDecodeError as e:
        report.error(config_file, f"invalid JSON: {e}")
        return report
    try:
        manifest = json.loads((root / manifest_file).read_text())
    except FileNotFoundError:
        report.error(manifest_file, "manifest file not found")
        return report
    except json.JSONDecodeError as e:
        report.error(manifest_file, f"invalid JSON: {e}")
        return report

    packages = load_packages(root, config)
    by_path = {p.path: p for p in packages}

    for path in manifest:
        if path not in by_path:
            report.error(
                manifest_file,
                f"'{path}' is in the manifest but not in {config_file} packages; "
                "remove the stale entry",
            )
    for pkg in packages:
        if not (root / pkg.path).is_dir():
            report.error(config_file, f"package path '{pkg.path}' does not exist")
        if pkg.include_component and not pkg.component:
            report.warn(
                config_file,
                f"cannot work out the tag component for '{pkg.path}'; set \"component\" "
                "explicitly. Tag checks skip this package.",
            )

    tags: set[str] = set()
    if check_tags:
        tags, source = existing_tags(root)
        print(f"release-please-lint: {len(tags)} tags read from {source}")

    for path, version in manifest.items():
        if not isinstance(version, str) or not SEMVER.match(version):
            report.error(manifest_file, f"'{path}' version {version!r} is not semver")
            continue
        pkg = by_path.get(path)
        if not check_tags or pkg is None or version == "0.0.0":
            continue
        tag = pkg.tag(version)
        if tag is not None and tag not in tags:
            report.error(
                manifest_file,
                f"'{path}' is at {version} but tag '{tag}' does not exist. release-please "
                f"finds no release for {version}: it skips the package or proposes a bump "
                f"past it, so {version} is never released. For a new "
                f"package set the version to 0.0.0 (or drop the entry); otherwise push the "
                f"missing tag: git tag {tag} <sha> && git push origin {tag}",
            )

    expected = [t for p in packages if (t := p.tag(SAMPLE_VERSION)) is not None]
    by_workflow: dict[str, list[str]] = {}
    for wf, pattern in workflow_tag_patterns(root / workflows_dir):
        if pattern not in ignore_patterns:
            by_workflow.setdefault(wf, []).append(pattern)
    for wf, patterns in by_workflow.items():
        # A workflow may list release-please tags beside manually pushed ones.
        # It is broken only when no pattern can fire from a release.
        if not expected or any(
            github_filter_regex(p).match(t) for p in patterns for t in expected
        ):
            continue
        listed = ", ".join(f"'{p}'" for p in patterns)
        report.error(
            os.path.relpath(wf, root),
            f"no tag trigger ({listed}) matches a tag release-please creates "
            f"(it creates tags like {', '.join(sorted(expected))}). The workflow will "
            "never run from a release. Fix the pattern, or pass it in "
            "ignore-tag-patterns if the tag is pushed by something else.",
        )
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", default="release-please-config.json")
    ap.add_argument("--manifest", default=".release-please-manifest.json")
    ap.add_argument("--workflows-dir", default=".github/workflows")
    ap.add_argument("--ignore-tag-pattern", action="append", default=[])
    ap.add_argument("--no-check-tags", action="store_true")
    args = ap.parse_args(argv)

    report = lint(
        Path(args.root).resolve(),
        args.config,
        args.manifest,
        args.workflows_dir,
        [p.strip() for p in args.ignore_tag_pattern if p.strip()],
        not args.no_check_tags,
    )
    for file, msg in report.warnings:
        print(f"::warning file={file}::{msg}")
    for file, msg in report.errors:
        print(f"::error file={file}::{msg}")
    if report.errors:
        print(f"release-please-lint: {len(report.errors)} error(s)")
        return 1
    print("release-please-lint: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for release-please-lint. Each test builds a real git repo, with a bare
repo as `origin`, so tag lookup runs the same `git ls-remote` path as CI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lint  # noqa: E402


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    git(work, "remote", "add", "origin", str(origin))
    return work


def write(repo: Path, config: dict, manifest: dict, workflows: dict[str, str] | None = None):
    (repo / "release-please-config.json").write_text(json.dumps(config))
    (repo / ".release-please-manifest.json").write_text(json.dumps(manifest))
    for path in config.get("packages", {}):
        (repo / path).mkdir(parents=True, exist_ok=True)
    for name, body in (workflows or {}).items():
        wf = repo / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / name).write_text(body)


def push_tag(repo: Path, tag: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "c")
    git(repo, "tag", tag)
    git(repo, "push", "-q", "origin", tag)


def run(repo: Path, ignore: list[str] | None = None, check_tags: bool = True) -> lint.Report:
    return lint.lint(
        repo,
        "release-please-config.json",
        ".release-please-manifest.json",
        ".github/workflows",
        ignore or [],
        check_tags,
    )


def deploy_on_tags(*patterns: str) -> str:
    lines = "\n".join(f"      - '{p}'" for p in patterns)
    return f"on:\n  push:\n    tags:\n{lines}\njobs: {{}}\n"


APP = {"packages": {"apps/app": {"component": "app"}}}


def test_consistent_setup_passes(repo: Path):
    write(repo, APP, {"apps/app": "1.4.0"}, {"deploy.yml": deploy_on_tags("app-v*")})
    push_tag(repo, "app-v1.4.0")
    report = run(repo)
    assert report.errors == []


def test_new_package_at_zero_needs_no_tag(repo: Path):
    write(repo, APP, {"apps/app": "0.0.0"})
    assert run(repo).errors == []


def test_package_missing_from_manifest_is_fine(repo: Path):
    write(repo, APP, {})
    assert run(repo).errors == []


def test_manifest_version_without_tag_is_an_error(repo: Path):
    write(repo, APP, {"apps/app": "0.1.0"})
    [(file, msg)] = run(repo).errors
    assert file == ".release-please-manifest.json"
    assert "tag 'app-v0.1.0' does not exist" in msg


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"GITHUB_HEAD_REF": "release-please--branches--main--components--app"}, "c"),
        ({"GITHUB_EVENT_NAME": "push"}, "chore(main): release app 0.1.0 (#7)"),
        ({"GITHUB_EVENT_NAME": "push"}, "chore: release main"),
        (
            {"GITHUB_EVENT_NAME": "push"},
            "Merge pull request #7 from o/release-please--branches--main\n\nchore(main): release",
        ),
    ],
)
def test_release_please_changes_skip_the_tag_check(repo, monkeypatch, env, message):
    write(repo, APP, {"apps/app": "0.1.0"})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert run(repo).errors == []


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"GITHUB_HEAD_REF": "feat/add-app"}, "feat(app): add app"),
        ({"GITHUB_EVENT_NAME": "push"}, "feat(app): mention a release in passing"),
        ({"GITHUB_EVENT_NAME": "pull_request"}, "chore(main): release app 0.1.0"),
    ],
)
def test_other_changes_keep_the_tag_check(repo, monkeypatch, env, message):
    write(repo, APP, {"apps/app": "0.1.0"})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert len(run(repo).errors) == 1


def test_tag_check_can_be_disabled(repo: Path):
    write(repo, APP, {"apps/app": "0.1.0"})
    assert run(repo, check_tags=False).errors == []


def test_falls_back_to_local_tags_without_origin(repo: Path):
    write(repo, APP, {"apps/app": "2.0.0"})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    git(repo, "tag", "app-v2.0.0")
    git(repo, "remote", "remove", "origin")
    assert run(repo).errors == []


def test_trigger_with_wrong_separator_is_an_error(repo: Path):
    write(repo, APP, {"apps/app": "0.0.0"}, {"deploy.yml": deploy_on_tags("app@v*")})
    [(file, msg)] = run(repo).errors
    assert file == ".github/workflows/deploy.yml"
    assert "no tag trigger ('app@v*') matches" in msg and "app-v1.2.3" in msg


def test_manual_pattern_beside_a_release_pattern_passes(repo: Path):
    # asp-trader: manual tags use '@v', release-please tags use '-v'.
    workflows = {"deploy.yml": deploy_on_tags("app@v*", "app-v*")}
    write(repo, APP, {"apps/app": "0.0.0"}, workflows)
    assert run(repo).errors == []


def test_custom_tag_separator_is_honoured(repo: Path):
    config = {"tag-separator": "@", **APP}
    write(repo, config, {"apps/app": "1.0.0"}, {"deploy.yml": deploy_on_tags("app@v*")})
    push_tag(repo, "app@v1.0.0")
    assert run(repo).errors == []


def test_ignored_trigger_pattern_is_skipped(repo: Path):
    write(repo, APP, {"apps/app": "0.0.0"}, {"deploy.yml": deploy_on_tags("manual@v*")})
    assert run(repo, ignore=["manual@v*"]).errors == []


def test_negated_and_bare_on_triggers(repo: Path):
    body = "true: {}\n"  # a workflow with no push trigger at all
    write(
        repo,
        APP,
        {"apps/app": "0.0.0"},
        {"a.yml": deploy_on_tags("app-v*", "!app-v0.*"), "b.yaml": body},
    )
    assert run(repo).errors == []


def test_root_package_without_component_in_tag(repo: Path):
    config = {"include-component-in-tag": False, "packages": {".": {}}}
    write(repo, config, {".": "0.3.0"}, {"release.yml": deploy_on_tags("v*")})
    push_tag(repo, "v0.3.0")
    assert run(repo).errors == []


def test_include_v_false(repo: Path):
    config = {"include-v-in-tag": False, **APP}
    write(repo, config, {"apps/app": "0.0.0"}, {"d.yml": deploy_on_tags("app-v*")})
    [(_, msg)] = run(repo).errors
    assert "app-1.2.3" in msg


def test_component_comes_from_scoped_package_json(repo: Path):
    config = {"release-type": "node", "packages": {"web": {}}}
    write(repo, config, {"web": "0.0.0"}, {"d.yml": deploy_on_tags("ui-v*")})
    (repo / "web" / "package.json").write_text(json.dumps({"name": "@acme/ui"}))
    assert run(repo).errors == []


def test_component_comes_from_pyproject(repo: Path):
    config = {"release-type": "python", "packages": {"lib": {}}}
    write(repo, config, {"lib": "0.0.0"}, {"d.yml": deploy_on_tags("mylib-v*")})
    (repo / "lib" / "pyproject.toml").write_text('[project]\nname = "mylib"\n')
    assert run(repo).errors == []


def test_unknown_component_warns_and_skips_tag_checks(repo: Path):
    write(repo, {"packages": {"lib": {}}}, {"lib": "1.0.0"})
    report = run(repo)
    assert report.errors == []
    [(_, msg)] = report.warnings
    assert "set \"component\"" in msg


def test_structural_errors(repo: Path):
    write(repo, APP, {"apps/app": "1.0", "apps/gone": "1.0.0"})
    (repo / "apps" / "app").rmdir()
    messages = " | ".join(m for _, m in run(repo).errors)
    assert "'apps/gone' is in the manifest but not in" in messages
    assert "package path 'apps/app' does not exist" in messages
    assert "'apps/app' version '1.0' is not semver" in messages


def test_invalid_json_is_reported(repo: Path):
    (repo / "release-please-config.json").write_text("{nope")
    (repo / ".release-please-manifest.json").write_text("{}")
    [(file, msg)] = run(repo).errors
    assert file == "release-please-config.json" and "invalid JSON" in msg


@pytest.mark.parametrize(
    ("pattern", "tag", "matches"),
    [
        ("v*", "v1.2.3", True),
        ("v*", "app-v1.2.3", False),
        ("app-v*", "app-v1.2.3", True),
        ("app-v[0-9]+.*", "app-v12.0.1", True),
        ("app-v[0-9]+.*", "app-vx.0.1", False),
        ("*", "release/v1", False),
        ("**", "release/v1", True),
        ("app\\*", "app*", True),
        ("v1.2.3?", "v1.2.", True),
    ],
)
def test_github_filter_regex(pattern: str, tag: str, matches: bool):
    assert bool(lint.github_filter_regex(pattern).match(tag)) is matches


def test_cli_exit_codes(repo: Path, capsys):
    write(repo, APP, {"apps/app": "0.1.0"})
    assert lint.main(["--root", str(repo)]) == 1
    assert "::error file=.release-please-manifest.json::" in capsys.readouterr().out
    assert lint.main(["--root", str(repo), "--no-check-tags"]) == 0

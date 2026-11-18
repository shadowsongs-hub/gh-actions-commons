"""Tests for sops-guard.

The fixtures are real `sops 3.13.3` output (dotenv, YAML, JSON, and YAML with
`encrypted_regex`), encrypted for two throwaway age keys whose private halves
were deleted. Tampered variants are made from them, so every check runs
against the exact format SOPS writes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sops_guard as guard  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
K1 = "age1nueqd25fd8wlyfnlm0nwm0qm0l6xjpx7ekhz6cxpjf93c4jwmswq8g8sla"
K2 = "age1cdvs3wxkg22l9r7s3h53g2map476xq9t8t8auyhsp7yrm643yszqx54eac"
CONFIG = {
    "creation_rules": [
        {"path_regex": r"^secrets/", "key_groups": [{"age": [K1, K2]}]},
        {"path_regex": r"^other/", "key_groups": [{"age": [K1]}]},
    ]
}
REAL = ["app.env.encrypted", "app.enc.yaml", "app.enc.json", "partial.enc.yaml"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".sops.yaml").write_text(yaml.safe_dump(CONFIG))
    subprocess.run(["git", "-C", str(tmp_path), "add", ".sops.yaml"], check=True)
    return tmp_path


def add(repo: Path, path: str, text: str | None = None, fixture: str | None = None) -> None:
    dest = repo / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if fixture:
        shutil.copy(FIXTURES / fixture, dest)
    else:
        dest.write_text(text or "")
    subprocess.run(["git", "-C", str(repo), "add", "-f", path], check=True)


def tampered(fixture: str, old: str, new: str) -> str:
    text = (FIXTURES / fixture).read_text()
    assert old in text, f"tamper anchor {old!r} not in {fixture}"
    return text.replace(old, new, 1)


def check(repo: Path) -> list[str]:
    return guard.check_repo(repo, ".sops.yaml", guard.DEFAULT_ENCRYPTED, guard.DEFAULT_FORBIDDEN)[0]


def test_real_sops_files_pass(repo: Path):
    for name in REAL:
        add(repo, f"secrets/{name}", fixture=name)
    problems, checked = guard.check_repo(
        repo, ".sops.yaml", guard.DEFAULT_ENCRYPTED, guard.DEFAULT_FORBIDDEN
    )
    assert problems == [] and checked == 4


@pytest.mark.parametrize(
    ("fixture", "line_start", "replacement", "key"),
    [
        ("app.env.encrypted", "DB_PASSWORD=", "DB_PASSWORD=hunter2", "DB_PASSWORD"),
        ("app.enc.yaml", "    port: ", "    port: 5432", "db.port"),
        ("app.enc.json", '\t\t"port": ', '\t\t"port": 5432', "db.port"),
        ("partial.enc.yaml", "    password: ", "    password: hunter2", "db.password"),
        ("app.env.encrypted", "PORT=", "PORT=ENC[looks-encrypted]", "PORT"),
    ],
)
def test_plain_value_is_caught(repo: Path, fixture, line_start, replacement, key):
    text = (FIXTURES / fixture).read_text()
    lines = text.splitlines(keepends=True)
    [i] = [n for n, line in enumerate(lines) if line.startswith(line_start)]
    lines[i] = replacement + ("," if lines[i].rstrip().endswith(",") else "") + "\n"
    add(repo, f"secrets/{fixture}", "".join(lines))
    assert check(repo) == [f"secrets/{fixture}: value of {key} is not encrypted"]


def test_encrypted_regex_leaves_other_values_plain(repo: Path):
    # partial.enc.yaml was encrypted with --encrypted-regex '^password$', so
    # `port: 5432` and the list are plain in the real file and must pass.
    assert "port: 5432" in (FIXTURES / "partial.enc.yaml").read_text()
    add(repo, "secrets/partial.enc.yaml", fixture="partial.enc.yaml")
    assert check(repo) == []


def test_plain_dotenv_comment_is_caught(repo: Path):
    add(repo, "secrets/a.env.encrypted", tampered("app.env.encrypted", "#ENC[", "# pw is x\n#ENC["))
    [problem] = check(repo)
    assert "plain-text comment" in problem


@pytest.mark.parametrize(
    ("fixture", "old", "new", "message"),
    [
        ("app.env.encrypted", "sops_mac=ENC[", "sops_mac=[", "missing or malformed sops_mac"),
        ("app.enc.yaml", "    mac: ENC[", "    mac: [", "missing or malformed sops mac"),
        ("app.enc.yaml", "BEGIN AGE ENCRYPTED FILE", "BEGIN NOTHING", "malformed age data key"),
    ],
)
def test_broken_metadata_is_caught(repo: Path, fixture, old, new, message):
    add(repo, f"secrets/{fixture}", tampered(fixture, old, new))
    assert any(message in p for p in check(repo))


def test_file_without_data_keys_is_caught(repo: Path):
    text = "\n".join(
        line for line in (FIXTURES / "app.env.encrypted").read_text().splitlines()
        if not line.startswith("sops_age__")
    )
    add(repo, "secrets/a.env.encrypted", text + "\n")
    assert any("no data key" in p for p in check(repo))


def test_unencrypted_file_is_caught(repo: Path):
    add(repo, "secrets/plain.enc.yaml", "db:\n  password: hunter2\n")
    assert check(repo) == ["secrets/plain.enc.yaml: no SOPS metadata (encrypt it with sops)"]


def test_recipient_drift_is_caught(repo: Path):
    add(repo, "other/app.enc.json", fixture="app.enc.json")
    [problem] = check(repo)
    assert "age recipients do not match" in problem and f"unexpected ['{K2}']" in problem


def test_no_matching_rule_is_caught(repo: Path):
    add(repo, "elsewhere/app.enc.yaml", fixture="app.enc.yaml")
    assert check(repo) == ["elsewhere/app.enc.yaml: no .sops.yaml creation rule matches this path"]


def test_forbidden_plain_env_is_caught_and_examples_pass(repo: Path):
    add(repo, "svc/.env", "A=secret\n")
    add(repo, "svc/.env.example", "A=\n")
    assert check(repo) == ["svc/.env: plain-text secrets file is committed; commit it encrypted"]


def test_untracked_files_are_ignored(repo: Path):
    (repo / ".env").write_text("A=secret\n")
    assert check(repo) == []


def test_missing_config_with_encrypted_files_is_an_error(repo: Path):
    add(repo, "secrets/app.enc.yaml", fixture="app.enc.yaml")
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "-f", ".sops.yaml"], check=True)
    assert check(repo) == [".sops.yaml: not found, so recipients cannot be checked"]


def test_config_in_a_subdirectory_matches_relative_paths(repo: Path):
    add(repo, "deploy/.sops.yaml", yaml.safe_dump(CONFIG))
    add(repo, "deploy/secrets/app.enc.yaml", fixture="app.enc.yaml")
    problems, checked = guard.check_repo(
        repo, "deploy/.sops.yaml", guard.DEFAULT_ENCRYPTED, guard.DEFAULT_FORBIDDEN
    )
    assert problems == [] and checked == 1


def test_legacy_age_string_rule():
    cfg = {"creation_rules": [{"path_regex": "x", "age": f"{K1}, {K2}"}]}
    assert guard.rule_recipients(cfg, "x") == {K1, K2}


def test_cli_exit_codes(repo: Path, capsys):
    add(repo, "secrets/app.enc.yaml", fixture="app.enc.yaml")
    assert guard.main(["--root", str(repo)]) == 0
    assert "1 encrypted file(s) checked" in capsys.readouterr().out
    add(repo, "x/.env", "A=1\n")
    assert guard.main(["--root", str(repo)]) == 1
    assert "::error file=x/.env::" in capsys.readouterr().out

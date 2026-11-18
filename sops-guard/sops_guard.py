"""Guard committed secrets in a repo that uses SOPS.

Checks every file git tracks:

* A forbidden plain-text file (by default `.env`) is committed.
* A file that should be SOPS-encrypted is not, or only partly:
  - it has no SOPS metadata;
  - a value that SOPS would encrypt is plain text, or only looks encrypted;
  - the MAC or every data key is missing or malformed;
  - (dotenv) a comment is plain text.
* The file's age recipients differ from the `.sops.yaml` creation rule that
  matches its path, or no rule matches. Run `sops updatekeys` to fix drift.

dotenv, YAML and JSON files are supported. In YAML and JSON, the
`unencrypted_suffix`, `encrypted_suffix`, `unencrypted_regex` and
`encrypted_regex` settings stored in the file's metadata decide which values
must be encrypted, as SOPS does. Only age recipients are compared with the
rule; PGP and cloud KMS keys count as data keys but are not compared.

Usage: python sops_guard.py [--root DIR] [--sops-config FILE]
                       [--encrypted PATTERN ...] [--forbidden PATTERN ...]
A pattern without `/` matches the file name; one with `/` matches the path.
Exit code 1 and one line per problem if anything is wrong.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_ENCRYPTED = [
    "*.encrypted",
    *(f"*.{infix}.{ext}" for infix in ("enc", "sops") for ext in ("yaml", "yml", "json", "env")),
]
DEFAULT_FORBIDDEN = [".env"]
SOPS_CONFIG_NAMES = {".sops.yaml", ".sops.yml"}

# Shape of a real SOPS value: AES-256-GCM, 32-byte IV, 16-byte tag, base64.
ENC_VALUE = re.compile(
    r"^ENC\[AES256_GCM,data:[A-Za-z0-9+/]*={0,2},iv:[A-Za-z0-9+/]{43}=,"
    r"tag:[A-Za-z0-9+/]{22}==,type:(str|int|float|bool|bytes|comment)\]$"
)
AGE_BLOCK = "BEGIN AGE ENCRYPTED FILE"
DOTENV_AGE = re.compile(r"^sops_age__list_\d+__map_(enc|recipient)$")
DOTENV_KEY_TYPES = ("sops_age__", "sops_pgp__", "sops_kms__", "sops_gcp_kms__",
                    "sops_azure_kv__", "sops_hc_vault__")
STRUCTURED_KEY_TYPES = ("age", "pgp", "kms", "gcp_kms", "azure_kv", "hc_vault")


@dataclass
class KeyRules:
    """Which keys SOPS encrypts, from the settings stored in the file."""

    unencrypted_suffix: str | None = "_unencrypted"
    encrypted_suffix: str | None = None
    unencrypted_regex: str | None = None
    encrypted_regex: str | None = None

    @classmethod
    def from_metadata(cls, meta: dict) -> KeyRules:
        rules = cls(
            unencrypted_suffix=meta.get("unencrypted_suffix"),
            encrypted_suffix=meta.get("encrypted_suffix"),
            unencrypted_regex=meta.get("unencrypted_regex"),
            encrypted_regex=meta.get("encrypted_regex"),
        )
        # SOPS falls back to `_unencrypted` when no selector is stored at all.
        if not any(vars(rules).values()):
            rules.unencrypted_suffix = "_unencrypted"
        return rules

    @property
    def encrypt_by_default(self) -> bool:
        return not (self.encrypted_suffix or self.encrypted_regex)

    def decide(self, key: str, inherited: bool) -> bool:
        if self.unencrypted_suffix and key.endswith(self.unencrypted_suffix):
            return False
        if self.unencrypted_regex and re.search(self.unencrypted_regex, key):
            return False
        if self.encrypted_suffix and key.endswith(self.encrypted_suffix):
            return True
        if self.encrypted_regex and re.search(self.encrypted_regex, key):
            return True
        return inherited


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return [p for p in out.split("\0") if p]


def matches(path: str, patterns: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path if "/" in p else name, p) for p in patterns)


def rule_recipients(sops_config: dict, path: str) -> set[str] | None:
    """Age recipients of the first creation rule whose path_regex matches, like sops."""
    for rule in sops_config.get("creation_rules") or []:
        regex = rule.get("path_regex")
        if regex is not None and not re.search(regex, path):
            continue
        recipients: set[str] = set()
        if isinstance(rule.get("age"), str):
            recipients |= {r.strip() for r in rule["age"].split(",") if r.strip()}
        for group in rule.get("key_groups") or []:
            recipients |= set(group.get("age") or [])
        return recipients
    return None


def check_dotenv(path: str, text: str) -> tuple[list[str], set[str]]:
    problems: list[str] = []
    values: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith("#"):
            if not ENC_VALUE.match(line[1:]):
                problems.append(f"{path}:{n}: plain-text comment in an encrypted file")
            continue
        if "=" not in line:
            problems.append(f"{path}:{n}: not a dotenv line")
            continue
        key, value = line.split("=", 1)
        values[key] = value
    meta = {k.removeprefix("sops_"): v for k, v in values.items() if k.startswith("sops_")}
    rules = KeyRules.from_metadata(meta)
    for key, value in values.items():
        if key.startswith("sops_") or value == "":
            continue
        if rules.decide(key, rules.encrypt_by_default) and not ENC_VALUE.match(value):
            problems.append(f"{path}: value of {key} is not encrypted")
    if not ENC_VALUE.match(values.get("sops_mac", "")):
        problems.append(f"{path}: missing or malformed sops_mac")
    if not any(k.startswith(DOTENV_KEY_TYPES) for k in values):
        problems.append(f"{path}: no data key (age, pgp or kms) in the metadata")
    age_enc = [v for k, v in values.items() if DOTENV_AGE.match(k) and k.endswith("_enc")]
    if not all(AGE_BLOCK in v for v in age_enc):
        problems.append(f"{path}: malformed age data key")
    found = {v for k, v in values.items() if DOTENV_AGE.match(k) and k.endswith("_recipient")}
    return problems, found


def _walk(node, encrypt: bool, rules: KeyRules, where: str, path: str, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{where}.{key}" if where else str(key)
            _walk(value, rules.decide(str(key), encrypt), rules, child, path, out)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk(item, encrypt, rules, f"{where}[{i}]", path, out)
    elif encrypt and node is not None and not (isinstance(node, str) and ENC_VALUE.match(node)):
        out.append(f"{path}: value of {where} is not encrypted")


def check_structured(path: str, doc: dict) -> tuple[list[str], set[str]]:
    meta = doc.get("sops")
    if not isinstance(meta, dict):
        return [f"{path}: the sops metadata is not a mapping"], set()
    problems: list[str] = []
    rules = KeyRules.from_metadata(meta)
    data = {k: v for k, v in doc.items() if k != "sops"}
    _walk(data, rules.encrypt_by_default, rules, "", path, problems)
    if not ENC_VALUE.match(str(meta.get("mac", ""))):
        problems.append(f"{path}: missing or malformed sops mac")
    if not any(meta.get(t) for t in STRUCTURED_KEY_TYPES):
        problems.append(f"{path}: no data key (age, pgp or kms) in the metadata")
    age = meta.get("age") or []
    if not all(isinstance(a, dict) and AGE_BLOCK in str(a.get("enc", "")) for a in age):
        problems.append(f"{path}: malformed age data key")
    found = {a["recipient"] for a in age if isinstance(a, dict) and a.get("recipient")}
    return problems, found


def _load_structured(text: str):
    """JSON first: SOPS indents JSON with tabs, which YAML does not allow."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def check_encrypted_file(path: str, text: str, sops_config: dict, rule_path: str) -> list[str]:
    if re.search(r"^sops_version=", text, re.MULTILINE):
        problems, found = check_dotenv(path, text)
    else:
        doc = _load_structured(text)
        if not isinstance(doc, dict) or "sops" not in doc:
            return [f"{path}: no SOPS metadata (encrypt it with sops)"]
        problems, found = check_structured(path, doc)

    expected = rule_recipients(sops_config, rule_path)
    if expected is None:
        problems.append(f"{path}: no .sops.yaml creation rule matches this path")
    elif found != expected:
        problems.append(
            f"{path}: age recipients do not match .sops.yaml "
            f"(missing {sorted(expected - found)}, unexpected {sorted(found - expected)}); "
            f"run: sops updatekeys {path}"
        )
    return problems


def check_repo(
    root: Path, sops_config_file: str, encrypted: list[str], forbidden: list[str]
) -> tuple[list[str], int]:
    """Return (problems, number of encrypted files checked)."""
    problems: list[str] = []
    files = tracked_files(root)
    targets = []
    for path in files:
        if path.rsplit("/", 1)[-1] in SOPS_CONFIG_NAMES:
            continue
        if matches(path, forbidden):
            problems.append(f"{path}: plain-text secrets file is committed; commit it encrypted")
        elif matches(path, encrypted):
            targets.append(path)
    if not targets:
        return problems, 0

    config_path = root / sops_config_file
    if not config_path.is_file():
        return problems + [f"{sops_config_file}: not found, so recipients cannot be checked"], 0
    sops_config = yaml.safe_load(config_path.read_text()) or {}
    config_dir = os.path.dirname(sops_config_file)
    for path in targets:
        # sops matches path_regex against the path relative to the config file.
        rule_path = os.path.relpath(path, config_dir) if config_dir else path
        text = (root / path).read_text()
        problems += check_encrypted_file(path, text, sops_config, rule_path)
    return problems, len(targets)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--sops-config", default=".sops.yaml")
    ap.add_argument("--encrypted", action="append", help="default: " + " ".join(DEFAULT_ENCRYPTED))
    ap.add_argument("--forbidden", action="append", help="default: " + " ".join(DEFAULT_FORBIDDEN))
    args = ap.parse_args(argv)

    problems, checked = check_repo(
        Path(args.root).resolve(),
        args.sops_config,
        args.encrypted or DEFAULT_ENCRYPTED,
        args.forbidden or DEFAULT_FORBIDDEN,
    )
    for p in problems:
        file = p.split(":", 1)[0]
        print(f"::error file={file}::{p}")
    if problems:
        print(f"sops-guard: {len(problems)} problem(s)")
        return 1
    print(f"sops-guard: passed ({checked} encrypted file(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

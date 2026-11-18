# sops-guard

A composite action for repos that keep secrets in git with
[SOPS](https://github.com/getsops/sops). It fails when a secret could leak or a host could
lose access to it. SOPS warns about none of these at commit time.

| Check | What goes wrong without it |
|-------|----------------------------|
| A forbidden plain-text file (default `.env`) is tracked | The secret is in git history. |
| A file that should be encrypted has no SOPS metadata | Someone committed the plain file under the encrypted name. |
| A value SOPS would encrypt is plain text, or only looks like `ENC[...]` | A hand edit after decryption put a secret back in clear. |
| A dotenv comment is plain text | Comments are encrypted too. A plain one is a hand edit. |
| The MAC or every data key is missing or malformed | The file cannot be decrypted or verified. |
| The file's age recipients differ from the `.sops.yaml` rule for its path | A key was added to or removed from `.sops.yaml` but `sops updatekeys` was not run. A host that should decrypt the file cannot, or a removed key still can. |
| No `.sops.yaml` creation rule matches the path | The file was encrypted by hand with ad-hoc keys. |

Only files git tracks are checked, so local plain `.env` files on a developer machine
are fine.

Formats: dotenv, YAML and JSON. For YAML and JSON, the `unencrypted_suffix`,
`encrypted_suffix`, `unencrypted_regex` and `encrypted_regex` settings that SOPS stores in
the file decide which values must be encrypted. A file made with
`--encrypted-regex '^password$'` therefore passes with its other values in clear.

## Usage

```yaml
on:
  pull_request:

jobs:
  sops-guard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: shadowsongs-hub/gh-actions-commons/sops-guard@main
```

No keys are needed. The guard reads only the metadata and the shape of the ciphertext. It
never decrypts.

## Inputs

| Input | Default | Purpose |
|-------|---------|---------|
| `working-directory` | `.` | Repo root to check. |
| `sops-config` | `.sops.yaml` | SOPS config. Rules match paths relative to its directory, as SOPS does. |
| `encrypted-files` | see below | Newline-separated patterns for files that must be encrypted. |
| `forbidden-files` | `.env` | Newline-separated patterns for plain-text files that must never be tracked. |

A pattern without `/` matches the file name. A pattern with `/` matches the whole path.
The default `encrypted-files` are `*.encrypted`, and `*.enc.<ext>` and `*.sops.<ext>` for
`yaml`, `yml`, `json` and `env`. `.sops.yaml` itself is never checked as a secret.

Example for a repo that also bans committed key files:

```yaml
  with:
    forbidden-files: |
      .env
      *.pem
      *.agekey
```

## Requirements

`git` and Python 3 on the runner. PyYAML is used if present. If it is not, the action
installs it into a private venv under `$RUNNER_TEMP`.

## Run it locally

```bash
python sops-guard/sops_guard.py --root /path/to/repo
```

## Limits

- Only age recipients are compared with the rule. PGP and cloud KMS keys count as data
  keys, but their identities are not compared.
- YAML comments are not checked. The YAML parser drops them.
- The ciphertext check is on shape (AES-256-GCM `ENC[...]` with a valid IV and tag). The
  MAC is not verified, because that needs the data key.

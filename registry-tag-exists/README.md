# registry-tag-exists

A composite action that checks whether an image tag is already in an OCI / Docker v2
registry. Use it to publish each version once.

Inside a release cycle many commits share one version. A version tag should be
immutable, so a publish job should run only when the version is new to the registry. In
practice that is the commit that merges the release PR.

The check is one request: `HEAD /v2/<repository>/manifests/<tag>`. It does not list
tags, so it stays fast on repositories with thousands of tags.

| Registry answer | Result |
|-----------------|--------|
| 200 | `exists=true` |
| 404 | `exists=false` |
| 401 with a Bearer challenge (Docker Hub, GHCR, most cloud registries) | gets a token from the named realm, with the credentials or anonymously, and retries |
| 401 with a Basic challenge | retries with the credentials |
| anything else, or no connection | registry error, handled by `on-error` |

## Usage

```yaml
- name: Check whether this version is published
  id: published
  uses: shadowsongs-hub/gh-actions-commons/registry-tag-exists@main
  with:
    image: ghcr.io/${{ github.repository }}
    tag: ${{ steps.meta.outputs.version }}
    username: ${{ github.actor }}
    password: ${{ secrets.GITHUB_TOKEN }}

- name: Build and push
  if: steps.published.outputs.exists == 'false'
  run: |
    docker build -t "$IMAGE:$VERSION" .
    docker push "$IMAGE:$VERSION"
```

A private registry over plain HTTP:

```yaml
  with:
    image: 10.0.0.5:5000/my-app
    tag: ${{ steps.meta.outputs.version }}
    insecure: "true"
```

## Inputs

| Input | Default | Purpose |
|-------|---------|---------|
| `image` | required | Image without a tag. No host means Docker Hub (`alpine` is `library/alpine`). A digest suffix is ignored. |
| `tag` | required | Tag to look for. |
| `insecure` | `false` | Use plain HTTP. |
| `username` | empty | Registry user. Empty means anonymous. |
| `password` | empty | Registry password or token. Pass it from a secret. It reaches the script only through the environment. |
| `on-error` | `fail` | What a registry error means: `fail` fails the step, `missing` reports `exists=false` (publish anyway), `exists` reports `exists=true` (skip). |

The default is `fail` because both other choices are silent. `missing` can push over an
existing tag. `exists` can skip a release.

## Outputs

| Output | Value |
|--------|-------|
| `exists` | `true` or `false`. Not set when the step fails. |

## Requirements

Python 3 on the runner. No packages.

## Run it locally

```bash
python registry-tag-exists/check.py ghcr.io/astral-sh/uv 0.5.0
REGISTRY_USERNAME=me REGISTRY_PASSWORD=... python registry-tag-exists/check.py ghcr.io/me/private 1.0.0
```

"""Check whether an image tag already exists in an OCI / Docker v2 registry.

Use it to gate a publish job. Many commits share one version inside a release
cycle, and a version tag should be immutable. Publish only when the tag is new.

The check is one manifest request: `HEAD /v2/<repository>/manifests/<tag>`.
200 means the tag exists and 404 means it does not. A registry that answers
401 with a Bearer challenge (Docker Hub, GHCR, most cloud registries) gets a
token from the realm it names, with the given credentials or anonymously.
A Basic challenge is answered with the given credentials.

Anything else is a registry error. By default that fails the check, so an
outage never silently skips or repeats a publish. `--on-error` can map it to
"missing" or "exists" instead.

Usage: python check.py IMAGE TAG [--insecure] [--on-error fail|missing|exists]
Credentials come from REGISTRY_USERNAME and REGISTRY_PASSWORD in the
environment, never from arguments. Prints `exists=true|false`.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DOCKER_HUB = "registry-1.docker.io"
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
TIMEOUT = 20


class RegistryError(Exception):
    pass


@dataclass
class Image:
    host: str
    repository: str


def parse_image(image: str) -> Image:
    """Split `host[:port]/repo` the way docker does. No host means Docker Hub."""
    image = image.split("@", 1)[0]
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, repository = first, rest
    else:
        host, repository = DOCKER_HUB, image
    if host in ("docker.io", "index.docker.io"):
        host = DOCKER_HUB
    if host == DOCKER_HUB and "/" not in repository:
        repository = f"library/{repository}"
    if not repository or ":" in repository:
        raise ValueError(f"image {image!r} must not carry a tag; pass the tag separately")
    return Image(host, repository)


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def _challenge(header: str) -> tuple[str, dict[str, str]]:
    scheme, _, params = header.strip().partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', params))


def _request(url: str, method: str, auth: str | None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method=method, headers={"Accept": ACCEPT})
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RegistryError(f"cannot reach {url}: {e}") from e


def _bearer_token(params: dict[str, str], username: str, password: str) -> str:
    realm = params.get("realm")
    if not realm:
        raise RegistryError("Bearer challenge has no realm")
    query = {k: v for k, v in params.items() if k in ("service", "scope")}
    url = realm + ("?" + urllib.parse.urlencode(query) if query else "")
    auth = _basic(username, password) if username else None
    status, _, body = _request(url, "GET", auth)
    if status != 200:
        raise RegistryError(f"token request to {realm} returned HTTP {status}")
    data = json.loads(body or b"{}")
    token = data.get("token") or data.get("access_token")
    if not token:
        raise RegistryError(f"token response from {realm} has no token")
    return token


def tag_exists(image: str, tag: str, insecure: bool, username: str, password: str) -> bool:
    ref = parse_image(image)
    scheme = "http" if insecure else "https"
    repo = urllib.parse.quote(ref.repository, safe="/")
    url = f"{scheme}://{ref.host}/v2/{repo}/manifests/{urllib.parse.quote(tag, safe='')}"

    auth = None
    for _ in range(2):
        status, headers, _ = _request(url, "HEAD", auth)
        if status == 405:
            status, headers, _ = _request(url, "GET", auth)
        if status == 200:
            return True
        if status == 404:
            return False
        if status == 401 and auth is None:
            www = {k.lower(): v for k, v in headers.items()}.get("www-authenticate", "")
            scheme_name, params = _challenge(www)
            if scheme_name == "bearer":
                auth = "Bearer " + _bearer_token(params, username, password)
                continue
            if scheme_name == "basic" and username:
                auth = _basic(username, password)
                continue
            raise RegistryError(f"{url} needs credentials (HTTP 401, challenge {www!r})")
        if status == 401:
            break
        raise RegistryError(f"{url} returned HTTP {status}")
    raise RegistryError(f"{url} rejected the credentials (HTTP 401)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", help="host[:port]/repository, without a tag")
    ap.add_argument("tag")
    ap.add_argument("--insecure", action="store_true", help="use plain HTTP")
    ap.add_argument("--on-error", choices=["fail", "missing", "exists"], default="fail")
    args = ap.parse_args(argv)

    try:
        exists = tag_exists(
            args.image,
            args.tag,
            args.insecure,
            os.environ.get("REGISTRY_USERNAME", ""),
            os.environ.get("REGISTRY_PASSWORD", ""),
        )
    except (RegistryError, ValueError) as e:
        if args.on_error == "fail":
            print(f"::error title=Registry check failed::{e}")
            return 1
        exists = args.on_error == "exists"
        print(f"::warning title=Registry check failed::{e}; treating the tag as {args.on_error}")

    print(f"{args.image}:{args.tag} {'exists' if exists else 'does not exist'}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"exists={'true' if exists else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

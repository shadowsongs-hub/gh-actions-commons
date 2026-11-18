"""Tests for registry-tag-exists, against a fake v2 registry on localhost."""

from __future__ import annotations

import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check  # noqa: E402

TAGS = {"app": {"1.0.0"}}
CREDS = "Basic " + base64.b64encode(b"bot:s3cret").decode()


class FakeRegistry(BaseHTTPRequestHandler):
    mode = "open"  # open | bearer | basic | broken | no-head
    seen: list[tuple[str, str, str | None]] = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status: int, headers: dict[str, str] | None = None, body: bytes = b""):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _manifest(self):
        auth = self.headers.get("Authorization")
        self.seen.append((self.command, self.path, auth))
        host = f"http://{self.headers['Host']}"
        if self.mode == "broken":
            return self._send(500)
        if self.mode == "no-head" and self.command == "HEAD":
            return self._send(405)
        if self.mode == "bearer" and auth != "Bearer tok-123":
            challenge = f'Bearer realm="{host}/token",service="fake",scope="repository:app:pull"'
            return self._send(401, {"WWW-Authenticate": challenge})
        if self.mode == "basic" and auth != CREDS:
            return self._send(401, {"WWW-Authenticate": 'Basic realm="fake"'})
        _, _, repo, _, tag = self.path.split("/", 4)
        return self._send(200 if tag in TAGS.get(repo, set()) else 404)

    def _token(self):
        self.seen.append((self.command, self.path, self.headers.get("Authorization")))
        if "service=fake" not in self.path or "scope=repository%3Aapp%3Apull" not in self.path:
            return self._send(400)
        if self.headers.get("Authorization") not in (None, CREDS):
            return self._send(401)
        self._send(200, {"Content-Type": "application/json"}, json.dumps({"token": "tok-123"}).encode())

    def do_HEAD(self):
        self._manifest()

    def do_GET(self):
        if self.path.startswith("/token"):
            return self._token()
        self._manifest()


@pytest.fixture
def registry():
    FakeRegistry.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRegistry)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def exists(registry: str, tag: str, mode: str, user: str = "", pw: str = "") -> bool:
    FakeRegistry.mode = mode
    return check.tag_exists(f"{registry}/app", tag, True, user, pw)


@pytest.mark.parametrize("mode", ["open", "bearer", "no-head"])
def test_existing_and_missing_tags(registry: str, mode: str):
    assert exists(registry, "1.0.0", mode) is True
    assert exists(registry, "2.0.0", mode) is False


def test_bearer_flow_sends_credentials_to_the_realm(registry: str):
    assert exists(registry, "1.0.0", "bearer", "bot", "s3cret") is True
    token_calls = [s for s in FakeRegistry.seen if s[1].startswith("/token")]
    assert [auth for _, _, auth in token_calls] == [CREDS]
    assert FakeRegistry.seen[-1][2] == "Bearer tok-123"


def test_basic_challenge_uses_credentials(registry: str):
    assert exists(registry, "1.0.0", "basic", "bot", "s3cret") is True


def test_basic_challenge_without_credentials_is_an_error(registry: str):
    with pytest.raises(check.RegistryError, match="needs credentials"):
        exists(registry, "1.0.0", "basic")


def test_rejected_credentials_are_an_error(registry: str):
    with pytest.raises(check.RegistryError, match="rejected the credentials"):
        exists(registry, "1.0.0", "basic", "bot", "wrong")


def test_server_error_is_an_error(registry: str):
    with pytest.raises(check.RegistryError, match="HTTP 500"):
        exists(registry, "1.0.0", "broken")


def test_unreachable_registry_is_an_error():
    with pytest.raises(check.RegistryError, match="cannot reach"):
        check.tag_exists("127.0.0.1:9/app", "1.0.0", True, "", "")


@pytest.mark.parametrize(
    ("on_error", "code", "output"),
    [("fail", 1, ""), ("missing", 0, "exists=false\n"), ("exists", 0, "exists=true\n")],
)
def test_on_error_modes(registry, tmp_path, monkeypatch, on_error, code, output):
    out = tmp_path / "out"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    FakeRegistry.mode = "broken"
    args = [f"{registry}/app", "1.0.0", "--insecure", "--on-error", on_error]
    assert check.main(args) == code
    assert out.read_text() == output


def test_cli_reads_credentials_from_env(registry, tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REGISTRY_USERNAME", "bot")
    monkeypatch.setenv("REGISTRY_PASSWORD", "s3cret")
    FakeRegistry.mode = "basic"
    assert check.main([f"{registry}/app", "1.0.0", "--insecure"]) == 0
    assert out.read_text() == "exists=true\n"


@pytest.mark.parametrize(
    ("image", "host", "repository"),
    [
        ("alpine", check.DOCKER_HUB, "library/alpine"),
        ("docker.io/alpine", check.DOCKER_HUB, "library/alpine"),
        ("bitnami/redis", check.DOCKER_HUB, "bitnami/redis"),
        ("ghcr.io/org/app", "ghcr.io", "org/app"),
        ("10.0.0.1:5000/app", "10.0.0.1:5000", "app"),
        ("localhost/team/app", "localhost", "team/app"),
        ("ghcr.io/org/app@sha256:abc", "ghcr.io", "org/app"),
    ],
)
def test_parse_image(image: str, host: str, repository: str):
    assert check.parse_image(image) == check.Image(host, repository)


@pytest.mark.parametrize("image", ["alpine:3.20", "ghcr.io/org/app:1.0"])
def test_parse_image_rejects_a_tag(image: str):
    with pytest.raises(ValueError, match="must not carry a tag"):
        check.parse_image(image)

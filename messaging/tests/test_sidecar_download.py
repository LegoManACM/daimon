import asyncio
import hashlib

import httpx

from daimon_messages import sidecar
from daimon_messages.sidecar import asset_name, download, parse_sums

BINARY = b"\x7fELF fake sidecar"
NAME = "daimon-messages-sidecar-linux-amd64"


def release(sums: str, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        path = request.url.path
        if path == "/LegoManACM/daimon/releases/download/v1.2.3/SHA256SUMS":
            return httpx.Response(200, text=sums)
        if path == f"/LegoManACM/daimon/releases/download/v1.2.3/{NAME}":
            return httpx.Response(200, content=BINARY)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_asset_names():
    assert asset_name("Windows", "AMD64") == "daimon-messages-sidecar-windows-amd64.exe"
    assert asset_name("Windows", "ARM64") == "daimon-messages-sidecar-windows-arm64.exe"
    assert asset_name("Darwin", "arm64") == "daimon-messages-sidecar-darwin-arm64"
    assert asset_name("Darwin", "x86_64") == "daimon-messages-sidecar-darwin-amd64"
    assert asset_name("Linux", "aarch64") == "daimon-messages-sidecar-linux-arm64"
    assert asset_name("Linux", "x86_64") == "daimon-messages-sidecar-linux-amd64"
    assert asset_name("Linux", "armv7l") == ""
    assert asset_name("FreeBSD", "amd64") == ""


def test_parse_sums():
    sums = parse_sums("abc123  daimon-0.1.0-py3-none-any.whl\nDEF456 *install.sh\n\n")
    assert sums == {"daimon-0.1.0-py3-none-any.whl": "abc123", "install.sh": "def456"}


def test_download_installs_a_verified_binary(tmp_path):
    seen = []
    sums = f"{'0' * 64}  other\n{hashlib.sha256(BINARY).hexdigest()}  {NAME}\n"
    binary = tmp_path / "bin" / "daimon-messages-sidecar"
    err = asyncio.run(download(binary, "1.2.3", NAME, transport=release(sums, seen)))
    assert err == ""
    assert binary.read_bytes() == BINARY
    assert not (tmp_path / "bin" / "daimon-messages-sidecar.part").exists()
    assert seen[0] == "https://github.com/LegoManACM/daimon/releases/download/v1.2.3/SHA256SUMS"


def test_download_rejects_a_checksum_mismatch(tmp_path):
    binary = tmp_path / "bin" / "daimon-messages-sidecar"
    err = asyncio.run(download(binary, "1.2.3", NAME, transport=release(f"{'a' * 64}  {NAME}\n")))
    assert "checksum" in err
    assert not binary.exists()
    assert not list((tmp_path / "bin").iterdir())


def test_download_needs_a_listed_checksum_and_a_release(tmp_path):
    binary = tmp_path / "daimon-messages-sidecar"
    assert "no checksum" in asyncio.run(download(binary, "1.2.3", NAME, transport=release("")))
    assert "Couldn't download" in asyncio.run(download(binary, "9.9.9", NAME, transport=release("")))
    assert "no prebuilt" in asyncio.run(download(binary, "1.2.3", "", transport=release("")))
    assert not binary.exists()


def test_install_builds_when_the_download_fails_and_go_is_there(tmp_path, monkeypatch):
    calls = []

    async def failed(binary):
        calls.append("download")
        return "Couldn't download it."

    async def built(binary):
        calls.append("build")
        return ""

    monkeypatch.setattr(sidecar, "CHECKOUT_SOURCE", tmp_path / "no-checkout")
    monkeypatch.setattr(sidecar, "SOURCE", tmp_path)
    monkeypatch.setattr(sidecar, "download", failed)
    monkeypatch.setattr(sidecar, "build", built)
    monkeypatch.setattr(sidecar, "find_go", lambda: "go")
    assert asyncio.run(sidecar.install(tmp_path / "x")) == ""
    assert calls == ["download", "build"]

    calls.clear()
    monkeypatch.setattr(sidecar, "find_go", lambda: "")
    err = asyncio.run(sidecar.install(tmp_path / "x"))
    assert "Couldn't download it." in err and "go.dev" in err
    assert calls == ["download"]

    calls.clear()
    monkeypatch.setattr(sidecar, "CHECKOUT_SOURCE", tmp_path)
    monkeypatch.setattr(sidecar, "find_go", lambda: "go")
    assert asyncio.run(sidecar.install(tmp_path / "x")) == ""
    assert calls == ["build"]

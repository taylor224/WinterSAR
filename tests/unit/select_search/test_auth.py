"""KB-AUTH-001: Earthdata credentials (token / .netrc) and session creation."""

from __future__ import annotations

from pathlib import Path

import pytest

from wintersar.i18n import t
from wintersar.select import auth

TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJvcmlnaW4iOiJFYXJ0aGRhdGEgTG9naW4i.eyJ0eXBlIjoiVXNlciJ9.signaturesignature"
)


@pytest.fixture
def no_home_netrc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    return tmp_path


def _write_netrc(path: Path, login: str = "alice", password: str = "s3cret-pass") -> Path:
    path.write_text(
        f"machine urs.earthdata.nasa.gov login {login} password {password}\n", encoding="utf-8"
    )
    path.chmod(0o600)
    return path


def test_find_credentials_none(no_home_netrc: Path) -> None:
    assert auth.find_credentials() is None
    assert auth.credentials_finding() is not None


def test_find_credentials_token(no_home_netrc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARTHDATA_TOKEN", TOKEN)
    creds = auth.find_credentials()
    assert creds is not None and creds.method == "token" and creds.token == TOKEN
    assert creds.source == "env:EARTHDATA_TOKEN"
    assert TOKEN not in str(creds.masked())
    monkeypatch.setenv("MY_TOKEN", "abc")
    assert auth.find_credentials("env:MY_TOKEN").token == "abc"
    assert auth.find_credentials("netrc") is None  # 'netrc' ignores env tokens


def test_find_credentials_netrc_preferred(
    no_home_netrc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_netrc(no_home_netrc / ".netrc")
    monkeypatch.setenv("EARTHDATA_TOKEN", TOKEN)
    creds = auth.find_credentials()
    assert creds is not None and creds.method == "netrc" and creds.username == "alice"
    assert auth.find_credentials(prefer="token").method == "token"
    other = _write_netrc(no_home_netrc / "other_netrc", login="bob")
    assert auth.find_credentials("netrc", netrc_path=other).username == "bob"


def test_find_credentials_netrc_without_edl_entry(no_home_netrc: Path) -> None:
    (no_home_netrc / ".netrc").write_text(
        "machine example.com login x password y\n", encoding="utf-8"
    )
    assert auth.find_credentials() is None
    (no_home_netrc / ".netrc").write_text(
        "machine urs.earthdata.nasa.gov login\n", encoding="utf-8"
    )  # unparsable
    assert auth.find_credentials() is None


def test_auth_finding_keys_and_masking() -> None:
    f = auth.auth_finding("invalid_token", error=f"Bearer {TOKEN} rejected")
    assert f.rule_id == "KB-AUTH-001" and f.severity == "FAIL"
    assert f.message_key == "select_search.KB-AUTH-001.cause_invalid_token"
    assert f.fix_key == "select_search.KB-AUTH-001.fix_invalid_token"
    assert TOKEN not in f.params["error"]
    rendered = t(f.message_key, "ko", **f.params)
    assert "EARTHDATA_TOKEN" in rendered and TOKEN not in rendered
    assert auth.auth_finding("missing").severity == "WARN"
    assert auth.auth_finding("network").severity == "WARN"
    assert auth.auth_finding("netrc_error").severity == "FAIL"


def test_earthdata_session_missing(no_home_netrc: Path) -> None:
    findings = []
    assert auth.earthdata_session(findings=findings) is None
    assert [f.rule_id for f in findings] == ["KB-AUTH-001"] and findings[0].params[
        "reason"
    ] == "missing"
    assert auth.earthdata_session() is None  # contract: bare call returns None, never raises


def test_earthdata_session_token_success(
    no_home_netrc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asf_search

    monkeypatch.setenv("EARTHDATA_TOKEN", TOKEN)
    calls: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        asf_search.ASFSession,
        "auth_with_token",
        lambda self, token: calls.append(("token", (token,))) or self,
    )
    monkeypatch.setattr(
        asf_search.ASFSession,
        "auth_with_creds",
        lambda self, u, p: calls.append(("creds", (u, p))) or self,
    )
    findings = []
    session = auth.earthdata_session(findings=findings)
    assert isinstance(session, asf_search.ASFSession) and findings == []
    assert calls == [("token", (TOKEN,))]


def test_earthdata_session_netrc_uses_creds(
    no_home_netrc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asf_search

    _write_netrc(no_home_netrc / ".netrc")
    calls: list[tuple] = []
    monkeypatch.setattr(
        asf_search.ASFSession, "auth_with_creds", lambda self, u, p: calls.append((u, p)) or self
    )
    session = auth.earthdata_session()
    assert session is not None and calls == [("alice", "s3cret-pass")]


def test_earthdata_session_invalid_token(
    no_home_netrc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asf_search

    monkeypatch.setenv("EARTHDATA_TOKEN", TOKEN)

    def _reject(self, token):
        raise asf_search.ASFAuthenticationError("Invalid/Expired token passed")

    monkeypatch.setattr(asf_search.ASFSession, "auth_with_token", _reject)
    findings = []
    assert auth.earthdata_session(findings=findings) is None
    assert findings[0].params["reason"] == "invalid_token" and findings[0].severity == "FAIL"
    assert "Invalid/Expired" in findings[0].params["error"]


def test_earthdata_session_network_error(
    no_home_netrc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asf_search
    import requests

    _write_netrc(no_home_netrc / ".netrc")

    def _down(self, u, p):
        raise requests.ConnectionError("urs.earthdata.nasa.gov unreachable")

    monkeypatch.setattr(asf_search.ASFSession, "auth_with_creds", _down)
    findings = []
    assert auth.earthdata_session(findings=findings) is None
    assert findings[0].params["reason"] == "network" and findings[0].severity == "WARN"

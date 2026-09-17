"""Earthdata Login credentials for ``asf_search`` (plan §5.1.1, KB-AUTH-001).

Two credential sources are supported, in this order of preference:

1. ``~/.netrc`` entry for ``urs.earthdata.nasa.gov`` (username/password). asf_search's
   ``auth_with_creds`` also sets the ``asf-urs`` cookie that the SLC-BURST extractor needs.
2. An Earthdata *user token* in an environment variable (``EARTHDATA_TOKEN`` by default,
   configurable via ``data.credentials: env:VAR``). ``auth_with_token`` does **not** set the
   ``asf-urs`` cookie (see the docstring of ``ASFSession.auth_with_token``), so burst
   downloads may still fail with token-only auth (open-questions #26, ADR-0013).

Searching CMR needs no credentials at all; a session is only required for downloads.
:func:`find_credentials` is therefore side-effect free (no network) and is what the search
command uses to warn early, while :func:`earthdata_session` performs the real login.

Every failure is reported as a ``Finding`` with ``rule_id='KB-AUTH-001'`` whose message keys
live under the ``select_search`` i18n namespace (cause -> fix order).
"""

from __future__ import annotations

import netrc
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from wintersar.io.schemas import Finding
from wintersar.util.masking import mask_text

if TYPE_CHECKING:  # pragma: no cover - typing only (asf_search is untyped)
    from asf_search import ASFSession

# source: .venv/lib/python3.11/site-packages/asf_search/constants/INTERNAL.py (EDL_HOST)
EDL_HOST = "urs.earthdata.nasa.gov"
DEFAULT_CREDENTIALS = "env:EARTHDATA_TOKEN"
RULE_ID = "KB-AUTH-001"
_NS = "select_search"

AuthMethod = Literal["token", "netrc"]
AuthFailure = Literal["missing", "invalid_token", "netrc_error", "network"]


@dataclass(frozen=True)
class Credentials:
    """Resolved (but not yet verified) Earthdata credentials."""

    method: AuthMethod
    source: str
    token: str | None = None
    username: str | None = None
    password: str | None = None

    def masked(self) -> dict[str, str]:
        """Loggable description without secrets (rule 11.11)."""
        return {"method": self.method, "source": mask_text(self.source)}


def token_env_name(credentials: str = DEFAULT_CREDENTIALS) -> str | None:
    """``'env:VAR'`` -> ``'VAR'``; ``'netrc'`` (or anything else) -> ``None``."""
    if credentials.startswith("env:") and len(credentials) > 4:
        return credentials[4:]
    return None


def _netrc_credentials(netrc_path: Path | None) -> tuple[str, str] | None:
    """Return ``(login, password)`` for :data:`EDL_HOST` from ``.netrc`` or ``None``.

    A missing or unparsable file is treated as "no netrc credentials"; the caller decides
    whether that is a problem.
    """
    path = netrc_path if netrc_path is not None else Path.home() / ".netrc"
    if not path.is_file():
        # Windows convention (requests also checks ``_netrc``)
        alt = path.with_name("_netrc")
        if not alt.is_file():
            return None
        path = alt
    try:
        auth = netrc.netrc(str(path)).authenticators(EDL_HOST)
    except (netrc.NetrcParseError, OSError):
        return None
    if auth is None:
        return None
    login, _account, password = auth
    if not login or not password:
        return None
    return login, password


def find_credentials(
    credentials: str = DEFAULT_CREDENTIALS,
    netrc_path: Path | None = None,
    prefer: AuthMethod = "netrc",
) -> Credentials | None:
    """Locate credentials without touching the network.

    Parameters
    ----------
    credentials:
        ``Config.data.credentials``: ``'env:VAR'`` names the token variable (the ``.netrc``
        file is still consulted), ``'netrc'`` restricts lookup to ``.netrc``.
    netrc_path:
        Override for ``~/.netrc`` (tests).
    prefer:
        Which method wins when both are available. ``'netrc'`` by default because only the
        username/password login sets the ``asf-urs`` cookie needed for burst downloads.
    """
    found: dict[str, Credentials] = {}
    env_var = token_env_name(credentials)
    if env_var is not None:
        token = os.environ.get(env_var, "").strip()
        if token:
            found["token"] = Credentials(method="token", source=f"env:{env_var}", token=token)
    netrc_auth = _netrc_credentials(netrc_path)
    if netrc_auth is not None:
        login, password = netrc_auth
        found["netrc"] = Credentials(
            method="netrc", source="~/.netrc", username=login, password=password
        )
    order: tuple[AuthMethod, ...] = ("netrc", "token") if prefer == "netrc" else ("token", "netrc")
    for method in order:
        if method in found:
            return found[method]
    return None


def auth_finding(
    reason: AuthFailure,
    credentials: str = DEFAULT_CREDENTIALS,
    error: str | None = None,
) -> Finding:
    """Build the KB-AUTH-001 finding for ``reason`` (cause -> fix keys, rule 11.6)."""
    env_var = token_env_name(credentials) or "EARTHDATA_TOKEN"
    severity: Literal["FAIL", "WARN"] = "WARN" if reason in ("missing", "network") else "FAIL"
    fix_key = {
        "missing": f"{_NS}.{RULE_ID}.fix",
        "invalid_token": f"{_NS}.{RULE_ID}.fix_invalid_token",
        "netrc_error": f"{_NS}.{RULE_ID}.fix",
        "network": f"{_NS}.{RULE_ID}.fix_network",
    }[reason]
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        message_key=f"{_NS}.{RULE_ID}.cause_{reason}",
        params={
            "reason": reason,
            "env_var": env_var,
            "host": EDL_HOST,
            "error": mask_text(error or ""),
        },
        evidence={"reason": reason, "credentials": mask_text(credentials)},
        fix_key=fix_key,
        refs=["https://urs.earthdata.nasa.gov/documentation/faq"],
        scope="auth",
    )


def credentials_finding(
    credentials: str = DEFAULT_CREDENTIALS, netrc_path: Path | None = None
) -> Finding | None:
    """Offline check used by ``search``: ``None`` when credentials exist, else KB-AUTH-001."""
    if find_credentials(credentials, netrc_path) is not None:
        return None
    return auth_finding("missing", credentials)


def earthdata_session(
    credentials: str = DEFAULT_CREDENTIALS,
    netrc_path: Path | None = None,
    findings: list[Finding] | None = None,
) -> ASFSession | None:
    """Return an authenticated ``asf_search.ASFSession`` or ``None``.

    On failure a KB-AUTH-001 finding is appended to ``findings`` (when given) and ``None``
    is returned; nothing is raised so that callers can continue with search-only work.
    """
    creds = find_credentials(credentials, netrc_path)
    if creds is None:
        if findings is not None:
            findings.append(auth_finding("missing", credentials))
        return None

    import asf_search  # lazy: importing asf_search pulls numpy/shapely/dateparser
    import requests

    session: Any = asf_search.ASFSession()
    try:
        if creds.method == "token":
            # source: asf_search/ASFSession.py  ASFSession.auth_with_token(token)
            session.auth_with_token(creds.token)
        else:
            # source: asf_search/ASFSession.py  ASFSession.auth_with_creds(username, password)
            session.auth_with_creds(creds.username, creds.password)
    except asf_search.ASFAuthenticationError as exc:  # source: asf_search/exceptions.py
        reason: AuthFailure = "invalid_token" if creds.method == "token" else "netrc_error"
        if findings is not None:
            findings.append(auth_finding(reason, credentials, error=str(exc)))
        return None
    except requests.RequestException as exc:
        if findings is not None:
            findings.append(auth_finding("network", credentials, error=str(exc)))
        return None
    return session


__all__ = [
    "DEFAULT_CREDENTIALS",
    "EDL_HOST",
    "RULE_ID",
    "AuthFailure",
    "AuthMethod",
    "Credentials",
    "auth_finding",
    "credentials_finding",
    "earthdata_session",
    "find_credentials",
    "token_env_name",
]

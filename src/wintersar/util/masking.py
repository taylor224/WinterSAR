"""Mask personal paths and secrets in logs/reports (rule 11.11).

Use :func:`mask_text` on any string that ends up in a report, a log excerpt, a Finding
``evidence`` value or a JSON output. Use :func:`mask_mapping` for nested dicts.

What is masked
--------------
* **Secret-looking ``key = value`` pairs**, where the key *contains* one of the secret
  words: ``EARTHDATA_TOKEN=…``, ``AWS_SECRET_ACCESS_KEY=…``, ``HYP3_TOKEN: …``,
  ``{"EARTHDATA_TOKEN": "…"}``, ``os.environ['HYP3_TOKEN'] = '…'``. Engine wrappers and
  SDK debug dumps print these forms (``set -x``, ``env``), so matching only the bare
  ``token=`` form is not enough.
* **Command-line flags**: ``--token X``, ``--password=X``.
* ``Bearer <token>``, JWTs, ``netrc`` ``password`` lines and URL credentials.
* **Values of secret-looking environment variables** of the *current* process (the name,
  not a fixed list, decides: ``*TOKEN*``, ``*SECRET*``, ``*PASSWORD*``, ``*_KEY``…).
  This only helps when the masking process has the same variables set, which is why the
  pattern-based rules above must stand on their own.
* Home directories (``/Users/<name>`` → ``~``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

MASK = "***"

# Words that make a key (or a flag, or an environment variable name) secret-looking.
_SECRET_WORD = (
    r"(?:tokens?|passwords?|passwd|secrets?|api[_-]?keys?|access[_-]?keys?"
    r"|private[_-]?keys?|authorization|credentials?|passphrases?)"
)
# Key names such as EARTHDATA_TOKEN / aws_secret_access_key / my.api.key: the secret word
# may sit anywhere inside the name, so no \b before it (``_`` is a word character).
_SECRET_KEY = rf"[\w.-]*{_SECRET_WORD}[\w.-]*"
# Values stop at whitespace/quote/list separators so that surrounding syntax survives.
_VALUE = r'(?P<q>["\']?)(?P<val>[^\s"\',;)\]}]+)'
# 'Bearer <token>' / 'token <token>' after an Authorization-style key stays visible.
_SCHEME = r"(?P<scheme>(?:[Bb]earer|BEARER|[Bb]asic|[Tt]oken)\s+)?"

_KEY_VALUE = re.compile(
    rf"(?i)(?<![\w.])(?P<key>{_SECRET_KEY})"
    rf"""(?P<sep>["']?\s*\]?\s*[=:]\s*){_SCHEME}{_VALUE}"""
)
_FLAG_VALUE = re.compile(rf"(?i)(?P<key>--?[\w-]*{_SECRET_WORD}[\w-]*)(?P<sep>\s+){_VALUE}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")
_NETRC = re.compile(r"(?i)(password\s+)\S+")
_URL_CREDS = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")


def _mask_key_value(m: re.Match[str]) -> str:
    return f"{m.group('key')}{m.group('sep')}{m.group('scheme') or ''}{m.group('q')}{MASK}"


def _mask_flag(m: re.Match[str]) -> str:
    return f"{m.group('key')}{m.group('sep')}{m.group('q')}{MASK}"


_PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (_KEY_VALUE, _mask_key_value),
    (_FLAG_VALUE, _mask_flag),
    (_BEARER, rf"\1{MASK}"),
    (_JWT, MASK),
    (_NETRC, rf"\1{MASK}"),
    (_URL_CREDS, rf"\1{MASK}:{MASK}@"),
)

# Cheap pre-filter: skip the substitutions when no pattern can possibly match.
_TRIGGER = re.compile(
    r"(?i)token|password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|authorization|credential|passphrase|bearer|eyJ|://"
)

# A mapping key that is itself a secret name (``{"token": "…"}``).
_SECRET_KEY_NAME = re.compile(rf"(?i)\A{_SECRET_KEY}\Z")

# Environment variables whose *name* looks secret; their values are masked wherever they
# appear (an engine may echo them without the name, e.g. `curl -H "<value>"`).
_SECRET_ENV_NAME = re.compile(
    r"(?i)token|secret|password|passwd|passphrase|credential|(?:^|[_-])key(?:s)?(?:$|[_-])"
)
_MIN_SECRET_LEN = 8

_secret_env_names: tuple[str, ...] = ()
_secret_env_size = -1


def _secret_env_values() -> list[str]:
    """Values of secret-looking environment variables, longest first.

    The *names* are cached and only rescanned when the number of environment variables
    changes (scanning every name on every call dominates :func:`mask_text` when a whole
    log file is masked line by line); the values are read on every call so that a
    changed token is still masked.
    """
    global _secret_env_names, _secret_env_size
    if len(os.environ) != _secret_env_size:
        _secret_env_size = len(os.environ)
        _secret_env_names = tuple(n for n in os.environ if _SECRET_ENV_NAME.search(n))
    values = {
        v for n in _secret_env_names if (v := os.environ.get(n)) and len(v) >= _MIN_SECRET_LEN
    }
    return sorted(values, key=len, reverse=True)


@lru_cache(maxsize=8)
def _home_dirs_for(home: str | None, userprofile: str | None, real_home: str) -> tuple[str, ...]:
    homes = {real_home}
    for v in (home, userprofile):
        if v:
            homes.add(v)
    return tuple(sorted(homes, key=len, reverse=True))


def _home_dirs() -> tuple[str, ...]:
    return _home_dirs_for(os.environ.get("HOME"), os.environ.get("USERPROFILE"), str(Path.home()))


def mask_text(text: str, extra_secrets: list[str] | None = None) -> str:
    """Replace secrets and home-directory prefixes in ``text``."""
    if not text:
        return text
    out = text
    for v in _secret_env_values():
        out = out.replace(v, MASK)
    for s in extra_secrets or []:
        if s and len(s) >= 4:
            out = out.replace(s, MASK)
    if _TRIGGER.search(out):
        for pat, repl in _PATTERNS:
            out = pat.sub(repl, out)
    for home in _home_dirs():
        out = out.replace(home, "~")
    return out


def mask_mapping(obj: Any, extra_secrets: list[str] | None = None) -> Any:
    """Recursively mask strings inside dicts/lists (returns a new object).

    A string value stored under a secret-looking *key* (``{"token": "…"}``) is replaced
    wholesale: there is no ``key=value`` syntax left in the value for the patterns above
    to recognise.
    """
    if isinstance(obj, str):
        return mask_text(obj, extra_secrets)
    if isinstance(obj, Path):
        return mask_text(str(obj), extra_secrets)
    if isinstance(obj, Mapping):
        return {
            k: MASK
            if isinstance(k, str) and isinstance(v, str) and v and _SECRET_KEY_NAME.match(k)
            else mask_mapping(v, extra_secrets)
            for k, v in obj.items()
        }
    if isinstance(obj, list | tuple):
        return [mask_mapping(v, extra_secrets) for v in obj]
    return obj

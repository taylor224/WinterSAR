"""Global CLI state (``--json``, ``--lang``, ``--verbose``) shared by all sub-commands.

Module CLIs import :data:`state` instead of ``wintersar.cli`` to avoid circular imports.

``lang_explicit`` records whether the language was *chosen* (``--lang``) or merely
defaulted from ``WINTERSAR_LANG``/``ko``; report writers that otherwise follow
``project.language`` use it to let an explicit ``--lang`` win (ADR-0090).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from wintersar.i18n import DEFAULT_LANG, SUPPORTED, current_lang


@dataclass
class CliState:
    json: bool = False
    lang: str = field(default_factory=lambda: os.environ.get("WINTERSAR_LANG", DEFAULT_LANG))
    verbose: int = 0
    lang_explicit: bool = False

    def set_lang(self, value: str | None) -> None:
        """Adopt ``--lang VALUE`` (``None`` = not given: ``WINTERSAR_LANG`` or ``ko``).

        An unsupported value falls back to the default instead of failing, as before.
        """
        if value is not None and value.lower() in SUPPORTED:
            self.lang = value.lower()
            self.lang_explicit = True
        else:
            self.lang = current_lang()
            self.lang_explicit = False

    def apply(self) -> None:
        os.environ["WINTERSAR_LANG"] = self.lang


state = CliState()

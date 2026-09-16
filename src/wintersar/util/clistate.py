"""Global CLI state (``--json``, ``--lang``, ``--verbose``) shared by all sub-commands.

Module CLIs import :data:`state` instead of ``wintersar.cli`` to avoid circular imports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class CliState:
    json: bool = False
    lang: str = field(default_factory=lambda: os.environ.get("WINTERSAR_LANG", "ko"))
    verbose: int = 0

    def apply(self) -> None:
        os.environ["WINTERSAR_LANG"] = self.lang


state = CliState()

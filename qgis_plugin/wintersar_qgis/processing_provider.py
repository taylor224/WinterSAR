"""Processing provider exposing the wintersar algorithms (plan §5.8 "Processing Provider").

# source: https://qgis.org/pyqgis/3.44/core/QgsProcessingProvider.html
#   abstract id() -> str, name() -> str, loadAlgorithms(); addAlgorithm(algorithm) -> bool;
#   virtual longName(), icon()
# source: https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/processing.html
#   QgsApplication.processingRegistry().addProvider(provider) / removeProvider(provider);
#   metadata.txt: hasProcessingProvider=yes
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .algorithms import make_algorithm_classes
from .cli_client import WintersarClient
from .i18n import t

PROVIDER_ID = "wintersar"


def make_provider_class() -> type:
    """``QgsProcessingProvider`` subclass (created lazily: needs the qgis module)."""
    from qgis.core import QgsProcessingProvider

    class WintersarProvider(QgsProcessingProvider):  # type: ignore[misc]
        def __init__(self, client_factory: Callable[[], WintersarClient]) -> None:
            super().__init__()
            self._client_factory = client_factory

        def id(self) -> str:
            return PROVIDER_ID

        def name(self) -> str:
            return t("qgis.plugin.provider_name")

        def longName(self) -> str:  # noqa: N802
            return self.name()

        def loadAlgorithms(self) -> None:  # noqa: N802
            for cls in make_algorithm_classes(self._client_factory):
                self.addAlgorithm(cls())

    return WintersarProvider


def register_provider(client_factory: Callable[[], WintersarClient]) -> Any:
    """Create the provider and add it to the processing registry; returns the provider."""
    from qgis.core import QgsApplication

    provider = make_provider_class()(client_factory)
    QgsApplication.processingRegistry().addProvider(provider)
    return provider


def unregister_provider(provider: Any) -> None:
    if provider is None:
        return
    from qgis.core import QgsApplication

    QgsApplication.processingRegistry().removeProvider(provider)

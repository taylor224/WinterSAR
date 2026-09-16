"""wintersar QGIS plugin package (plan §5.8, R-12, ADR-0070).

QGIS loads the plugin through :func:`classFactory`; everything that touches ``qgis``/Qt is
imported lazily inside functions so that the pure-python parts (``cli_client``,
``settings``, ``algorithms`` parameter mapping, ``dock_widget`` row helpers) can be unit
tested without a QGIS installation.

# source: https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/plugins/plugins.html
#   "__init__.py ... has to have the classFactory() method"
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"


def classFactory(iface: Any) -> Any:  # noqa: N802  (name mandated by QGIS)
    """Entry point called by QGIS with the :class:`QgisInterface` instance."""
    from .plugin import WintersarPlugin

    return WintersarPlugin(iface)

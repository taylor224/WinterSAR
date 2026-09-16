"""QGIS plugin entry class: dock widget + Processing provider (plan §5.8, R-12, ADR-0070).

# source: https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/plugins/plugins.html
#   __init__(self, iface); initGui() "called when the plugin is loaded";
#   unload() "called when the plugin is unloaded"
# source: https://qgis.org/pyqgis/3.44/gui/QgisInterface.html
#   addDockWidget(area, dockwidget), removeDockWidget(dockwidget), addPluginToMenu(name, action),
#   removePluginMenu(name, action), addToolBarIcon(action), removeToolBarIcon(action)
# source: https://github.com/qgis/QGIS/wiki/Plugin-migration-to-be-compatible-with-Qt5-and-Qt6
#   fully qualified enums (Qt.DockWidgetArea.RightDockWidgetArea) work on PyQt5 and PyQt6;
#   Qt classes imported through the qgis.PyQt shim
"""

from __future__ import annotations

from typing import Any

from .cli_client import WintersarClient
from .i18n import set_lang, t
from .settings import PluginSettings, load_settings, save_settings


class WintersarPlugin:
    """Owns the dock, the Processing provider and the shared settings/client."""

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        self.settings: PluginSettings = load_settings()
        set_lang(self.settings.lang)
        self.dock: Any = None
        self.dock_controller: Any = None
        self.provider: Any = None
        self.action: Any = None

    # ------------------------------------------------------------------ client
    def make_client(self) -> WintersarClient:
        s = self.settings
        return WintersarClient(
            python_exe=s.python_exe, env_activate=s.env_hint, lang=s.lang, timeout=s.timeout_s
        )

    def update_settings(self, settings: PluginSettings) -> None:
        self.settings = settings
        set_lang(settings.lang)
        save_settings(settings)

    # ------------------------------------------------------------------ QGIS hooks
    def initGui(self) -> None:  # noqa: N802
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtWidgets import QAction

        from .dock_widget import WintersarDock
        from .processing_provider import register_provider

        self.action = QAction(t("qgis.plugin.menu_open"), self.iface.mainWindow())
        self.action.triggered.connect(self.show_dock)
        self.iface.addPluginToMenu(t("qgis.plugin.name"), self.action)
        self.iface.addToolBarIcon(self.action)

        self.dock_controller = WintersarDock(
            self.iface, self.make_client, self.settings, on_settings_changed=self.update_settings
        )
        self.dock = self.dock_controller.build()
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.hide()

        self.provider = register_provider(self.make_client)

    def unload(self) -> None:
        from .processing_provider import unregister_provider

        unregister_provider(self.provider)
        self.provider = None
        if self.dock is not None:
            if self.dock_controller is not None:
                self.dock_controller.shutdown()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginMenu(t("qgis.plugin.name"), self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    def show_dock(self) -> None:
        if self.dock is not None:
            self.dock.show()
            self.dock.raise_()

# controller/theme.py
"""
Dark mode for the Controller (2026-09-25, Craig: "can we give it a dark
mode?"). One palette on the QApplication, Fusion style so every widget
honours it, and the row tints (common.TINTS) swapped for a stronger set
that reads on a dark base. The choice is `theme` in
config/controller_settings.json ("dark" is the default); the Run view
has the switch and applies it live.
"""
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from controller.common import load_controller_settings, save_controller_settings, set_dark_tints


def is_dark() -> bool:
    return (load_controller_settings().get("theme") or "dark") == "dark"


def remember(dark: bool):
    settings = load_controller_settings()
    settings["theme"] = "dark" if dark else "light"
    save_controller_settings(settings)


def _dark_palette() -> QPalette:
    p = QPalette()
    window = QColor(43, 43, 43)
    base = QColor(30, 30, 30)
    text = QColor(224, 224, 224)
    dim = QColor(128, 128, 128)
    button = QColor(58, 58, 58)
    highlight = QColor(61, 111, 181)
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, window)
    p.setColor(QPalette.ToolTipBase, QColor(50, 50, 50))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.PlaceholderText, dim)
    p.setColor(QPalette.Button, button)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(255, 84, 84))
    p.setColor(QPalette.Link, QColor(90, 160, 240))
    p.setColor(QPalette.Highlight, highlight)
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, dim)
    p.setColor(QPalette.Disabled, QPalette.Base, QColor(38, 38, 38))
    p.setColor(QPalette.Disabled, QPalette.Button, QColor(48, 48, 48))
    return p


def apply(app: QApplication, dark: bool):
    """Fusion in both modes, so the switch changes colours and nothing
    else about the layout."""
    app.setStyle("Fusion")
    if dark:
        app.setPalette(_dark_palette())
        # Fusion draws QTextEdit/QTableWidget from Base; a slightly lighter
        # scrollbar and a visible grid line help on a dark base.
        app.setStyleSheet("QTableWidget { gridline-color: #3c3c3c; } QToolTip { color: #e0e0e0; background: #323232; border: 1px solid #555; }")
    else:
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")
    set_dark_tints(dark)

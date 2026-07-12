"""Local browser GUI for code-analyzer (``code-analyzer gui``).

Pure standard-library implementation (``http.server`` + ``webbrowser`` +
``threading`` + ``json``): no third-party web framework, no external network
resources. See :mod:`codeanalyzer.gui.server` for the entry points
(``run_gui`` / ``create_server``).
"""
from codeanalyzer.gui.server import create_server, run_gui

__all__ = ["run_gui", "create_server"]

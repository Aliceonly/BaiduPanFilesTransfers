import sys
import types

import pytest

# Keep the business logic testable on hosts without Tk; no windows are created.
try:
    import tkinter
except ImportError:
    ttk = types.ModuleType("ttkbootstrap")
    ttk.END = "end"
    sys.modules["ttkbootstrap"] = ttk
    ui = types.ModuleType("src.ui")
    ui.CustomDialog = object
    sys.modules["src.ui"] = ui


@pytest.fixture(autouse=True)
def isolate_config_and_network(tmp_path, monkeypatch):
    monkeypatch.setattr("src.utils.CONFIG_PATH", str(tmp_path / "config.ini"))

    def reject_request(*_args, **_kwargs):
        raise AssertionError("Tests must not contact the live Baidu API")

    monkeypatch.setattr("requests.Session.request", reject_request)

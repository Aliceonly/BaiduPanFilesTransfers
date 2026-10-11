import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")

from scripts.smoke_gui import main
from src.ui import TextEditor


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows GUI regression")
def test_main_window_and_share_dialog_open():
    main()


@pytest.mark.parametrize("value", ["real links", "placeholder"])
def test_focus_in_resets_a_stale_placeholder_flag(value):
    deleted = []
    colors = []
    text = SimpleNamespace(
        get=lambda *_args: value,
        delete=lambda *_args: deleted.append(True),
        config=lambda **kwargs: colors.append(kwargs),
        is_placeholder=True,
    )

    TextEditor._on_focus_in(text, "placeholder")

    assert text.is_placeholder is False
    assert bool(deleted) == (value == "placeholder")
    assert colors == [{"fg": "black"}]


def test_focus_out_with_real_content_resets_placeholder_state():
    colors = []
    text = SimpleNamespace(
        get=lambda *_args: "real links",
        config=lambda **kwargs: colors.append(kwargs),
        is_placeholder=True,
    )

    TextEditor._on_focus_out(text, "placeholder")

    assert text.is_placeholder is False
    assert colors == [{"fg": "black"}]

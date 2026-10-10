import sys

import pytest

pytest.importorskip("tkinter")

from scripts.smoke_gui import main


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows GUI regression")
def test_main_window_and_share_dialog_open():
    main()

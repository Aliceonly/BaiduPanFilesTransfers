"""Check native Tk windows without credentials or calls to the Baidu API."""

from src.ui import CustomDialog, MainWindow


def main():
    root = MainWindow(None)
    try:
        root.withdraw()
        root.update()
        closed_dialog = []

        def close_dialog():
            for child in root.winfo_children():
                if isinstance(child, CustomDialog):
                    closed_dialog.append(True)
                    child.destroy()

        root.after(250, close_dialog)
        CustomDialog(root)
        assert closed_dialog, "Share settings dialog did not open"
    finally:
        root.destroy()
    print("Main window and share settings dialog opened successfully")


if __name__ == "__main__":
    main()

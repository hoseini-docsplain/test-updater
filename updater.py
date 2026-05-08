import ctypes
import os
import sys
import threading
import time

import customtkinter as ctk
import git
import requests


README_URL = (
    "https://raw.githubusercontent.com/hoseini-docsplain/test-updater/main/README.md"
)
APP_EXE_NAME = "app.exe"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def get_latest_version() -> str:
    try:
        response = requests.get(README_URL, timeout=10)
        response.raise_for_status()
        content = response.text
    except Exception:
        return "unknown"

    for line in content.splitlines():
        if line.strip().lower().startswith("version"):
            return line.replace("Version", "", 1).replace("version", "", 1).strip()
    return "unknown"


class UpdaterApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("VTLab Updater")
        self.geometry("430x220")
        self.resizable(False, False)

        self.status_label = ctk.CTkLabel(
            self,
            text="Please wait while VTLab is being updated...",
            wraplength=380,
            justify="center",
        )
        self.status_label.pack(padx=20, pady=(25, 10))

        self.progress = ctk.CTkProgressBar(self, mode="indeterminate", width=320)
        self.progress.pack(padx=20, pady=10)
        self.progress.start()

        self.action_button = ctk.CTkButton(
            self, text="Updating...", state="disabled", command=self.finish
        )
        self.action_button.pack(padx=20, pady=(15, 20))

        self.after(100, self.start_update)

    def start_update(self) -> None:
        threading.Thread(target=self.run_update, daemon=True).start()

    def run_update(self) -> None:
        try:
            repo = git.Repo(os.getcwd())
            remote = repo.remote("origin")

            # Keep current updater behavior: drop local edits before pulling.
            repo.git.reset("--hard")
            remote.pull()
        except Exception as exc:
            error_text = str(exc)
            self.after(0, lambda msg=error_text: self.show_result(False, msg))
            return

        version = get_latest_version()
        self.after(0, lambda: self.show_result(True, version))

    def show_result(self, success: bool, details: str) -> None:
        self.progress.stop()
        self.progress.pack_forget()

        if success:
            self.status_label.configure(text=f"VTLab was updated successfully.\nVersion: {details}")
            self.action_button.configure(text="Done", state="normal")
        else:
            self.status_label.configure(
                text=(
                    "Update failed.\n"
                    "Please upgrade manually from Help > Check for updates.\n\n"
                    f"Details: {details}"
                )
            )
            self.action_button.configure(text="Close", state="normal")

    def finish(self) -> None:
        try:
            os.startfile(APP_EXE_NAME)
            time.sleep(0.5)
        except OSError:
            pass
        self.quit()
        self.destroy()


def elevate_if_needed() -> None:
    if is_admin():
        return
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, " ".join(sys.argv), None, 1
    )
    sys.exit(0)


if __name__ == "__main__":
    elevate_if_needed()
    app = UpdaterApp()
    app.mainloop()

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
import requests

from version import APP_VERSION


APP_EXE_NAME = "app.exe"
UPDATER_EXE_NAME = "updater.exe"
UPDATER_SCRIPT_NAME = "updater.py"
UPDATE_FEED_URL = os.environ.get(
    "APP_UPDATE_FEED_URL",
    "https://raw.githubusercontent.com/hoseini-docsplain/patch-updater/main/latest.json",
)
REQUEST_TIMEOUT = 8


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def read_current_version() -> str:
    return APP_VERSION


def version_key(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(part) for part in parts) if parts else (0,)


def fetch_update_feed() -> dict:
    response = requests.get(UPDATE_FEED_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_patch_for_version(feed: dict, current_version: str) -> dict | None:
    patches = feed.get("patches", {})
    if isinstance(patches, dict):
        patch = patches.get(current_version)
        return patch if isinstance(patch, dict) else None

    if isinstance(patches, list):
        for patch in patches:
            if isinstance(patch, dict) and patch.get("from_version") == current_version:
                return patch

    return None


def is_update_available(feed: dict, current_version: str) -> bool:
    latest_version = str(feed.get("version", "0.0.0"))
    return version_key(latest_version) > version_key(current_version)


def updater_command(current_version: str) -> list[str]:
    root = app_root()
    updater_exe = root / UPDATER_EXE_NAME
    if getattr(sys, "frozen", False) and updater_exe.exists():
        return [
            str(updater_exe),
            "--install-root",
            str(root),
            "--current-version",
            current_version,
            "--feed-url",
            UPDATE_FEED_URL,
            "--restart-exe",
            APP_EXE_NAME,
        ]

    return [
        sys.executable,
        str(root / UPDATER_SCRIPT_NAME),
        "--install-root",
        str(root),
        "--current-version",
        current_version,
        "--feed-url",
        UPDATE_FEED_URL,
        "--restart-exe",
        APP_EXE_NAME,
    ]


class MainApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.current_version = read_current_version()

        self.title("Simple App")
        self.geometry("440x300")
        self.resizable(False, False)

        frame = ctk.CTkFrame(self)
        frame.pack(expand=True, fill="both", padx=24, pady=24)

        label = ctk.CTkLabel(
            frame,
            text=f"Version {self.current_version}",
            font=("Arial", 24),
        )
        label.pack(expand=True)

        self.update_label = ctk.CTkLabel(frame, text="Checking for updates...")
        self.update_label.pack(pady=(0, 12))

        self.after(500, self.check_for_updates_async)

    def check_for_updates_async(self) -> None:
        threading.Thread(target=self.check_for_updates, daemon=True).start()

    def check_for_updates(self) -> None:
        try:
            feed = fetch_update_feed()
            if not is_update_available(feed, self.current_version):
                self.after(0, lambda: self.update_label.configure(text="App is up to date."))
                return

            patch = get_patch_for_version(feed, self.current_version)
            if not patch or not patch.get("url"):
                self.after(
                    0,
                    lambda: self.update_label.configure(
                        text="Update available, but no patch for this version."
                    ),
                )
                return

            latest_version = str(feed.get("version", "unknown"))
            self.after(0, lambda: self.prompt_update(latest_version))
        except Exception:
            self.after(0, lambda: self.update_label.configure(text="Update check unavailable."))

    def prompt_update(self, latest_version: str) -> None:
        self.update_label.configure(text=f"Update available: {latest_version}")
        should_update = messagebox.askyesno(
            "Update available",
            f"Version {latest_version} is available.\n\nUpdate now?",
            parent=self,
        )
        if should_update:
            self.launch_updater_and_exit()

    def launch_updater_and_exit(self) -> None:
        try:
            subprocess.Popen(updater_command(self.current_version), cwd=str(app_root()))
        except OSError as exc:
            messagebox.showerror("Update failed", f"Could not start updater:\n{exc}", parent=self)
            return

        self.destroy()


if __name__ == "__main__":
    app = MainApp()
    app.mainloop()

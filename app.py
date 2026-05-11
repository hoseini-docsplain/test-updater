import os
import re
import sys
import threading
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
import requests

from updater import UpdaterApp
from version import APP_VERSION


APP_EXE_NAME = "app.exe"
UPDATE_FEED_URL = os.environ.get(
    "APP_UPDATE_FEED_URL",
    "https://raw.githubusercontent.com/hoseini-docsplain/patch-updater/main/latest.json",
)
REQUEST_TIMEOUT = 15
UPDATE_STATUS_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000


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


def get_full_update(feed: dict) -> dict | None:
    full = feed.get("full")
    return full if isinstance(full, dict) and full.get("url") else None


def is_update_available(feed: dict, current_version: str) -> bool:
    latest_version = str(feed.get("version", "0.0.0"))
    return version_key(latest_version) > version_key(current_version)


class MainApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.current_version = read_current_version()
        self.run_updater_after_close = False
        self.is_status_check_running = False

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
        self.after(UPDATE_STATUS_CHECK_INTERVAL_MS, self.check_update_status_async)

    def check_for_updates_async(self) -> None:
        threading.Thread(target=self.check_for_updates, daemon=True).start()

    def check_update_status_async(self) -> None:
        if not self.is_status_check_running:
            self.is_status_check_running = True
            threading.Thread(target=self.check_update_status, daemon=True).start()
        self.after(UPDATE_STATUS_CHECK_INTERVAL_MS, self.check_update_status_async)

    def check_for_updates(self) -> None:
        try:
            feed = fetch_update_feed()
            if not is_update_available(feed, self.current_version):
                self.after(0, lambda: self.update_label.configure(text="App is up to date."))
                return

            patch = get_patch_for_version(feed, self.current_version)
            full = get_full_update(feed)
            if not patch and not full:
                self.after(
                    0,
                    lambda: self.update_label.configure(
                        text="Update available, but no package for this version."
                    ),
                )
                return

            latest_version = str(feed.get("version", "unknown"))
            self.after(0, lambda: self.prompt_update(latest_version))
        except Exception:
            self.after(0, lambda: self.update_label.configure(text="Update check unavailable."))

    def check_update_status(self) -> None:
        try:
            feed = fetch_update_feed()
            if not is_update_available(feed, self.current_version):
                self.after(0, lambda: self.update_label.configure(text="App is up to date."))
                return

            latest_version = str(feed.get("version", "unknown"))
            self.after(
                0,
                lambda: self.update_label.configure(text=f"Update available: {latest_version}"),
            )
        except Exception:
            self.after(0, lambda: self.update_label.configure(text="Update check unavailable."))
        finally:
            self.is_status_check_running = False

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
        self.run_updater_after_close = True
        self.destroy()


def run_updater_window(current_version: str) -> None:
    app = UpdaterApp(
        install_root=app_root(),
        current_version=current_version,
        feed_url=UPDATE_FEED_URL,
        restart_exe=APP_EXE_NAME,
    )
    app.mainloop()


if __name__ == "__main__":
    app = MainApp()
    app.mainloop()
    if app.run_updater_after_close:
        run_updater_window(app.current_version)

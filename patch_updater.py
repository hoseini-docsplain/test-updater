import argparse
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from uuid import uuid4

try:
    import requests
except ImportError:
    requests = None

try:
    import customtkinter as ctk
except ImportError:
    ctk = None


CONTROL_FILES = {"patch_meta.json", "deleted_files.txt"}
REQUEST_TIMEOUT = 15
CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def normalize_rel_path(rel: str) -> str:
    rel = rel.replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def is_safe_path(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def allowed_delete(rel_path: str, delete_prefixes: tuple[str, ...]) -> bool:
    return any(rel_path == prefix or rel_path.startswith(prefix + "/") for prefix in delete_prefixes)


def safe_extract(zip_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            target = destination / normalize_rel_path(info.filename)
            if not is_safe_path(destination, target):
                raise RuntimeError(f"Unsafe path in patch zip: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)


def apply_patch(
    install_root: Path,
    patch_zip_path: Path,
    delete_prefixes: tuple[str, ...] = ("app.exe", "patch_updater.exe", "updater.exe", "_internal"),
) -> None:
    install_root = install_root.resolve()
    patch_zip_path = patch_zip_path.resolve()

    if not install_root.is_dir():
        raise RuntimeError(f"Install root does not exist: {install_root}")
    if not patch_zip_path.is_file():
        raise RuntimeError(f"Patch file does not exist: {patch_zip_path}")

    work_dir = install_root / f".app_patch_{uuid4().hex}"
    extract_dir = work_dir / "extract"
    backup_dir = work_dir / "backup"
    extract_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    created_paths: list[Path] = []
    backup_paths: list[str] = []

    try:
        safe_extract(patch_zip_path, extract_dir)

        meta_path = extract_dir / "patch_meta.json"
        patch_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

        deleted_file_list = []
        deleted_path = extract_dir / "deleted_files.txt"
        if deleted_path.exists():
            deleted_file_list = [
                normalize_rel_path(line)
                for line in deleted_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        changed_rel_paths = []
        for path in extract_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = normalize_rel_path(path.relative_to(extract_dir).as_posix())
            if rel in CONTROL_FILES:
                continue
            changed_rel_paths.append(rel)

        changed_rel_paths.sort()

        def backup_if_exists(rel: str) -> None:
            target = install_root / rel
            if target.exists() and target.is_file() and rel not in backup_paths:
                dest = backup_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)
                backup_paths.append(rel)

        for rel in changed_rel_paths:
            target = install_root / rel
            source = extract_dir / rel
            if not is_safe_path(install_root, target):
                raise RuntimeError(f"Unsafe target path in patch: {rel}")

            backup_if_exists(rel)
            if not target.exists():
                created_paths.append(target)

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        for rel in deleted_file_list:
            if not allowed_delete(rel, delete_prefixes):
                continue

            target = install_root / rel
            if not is_safe_path(install_root, target):
                raise RuntimeError(f"Unsafe delete target in patch: {rel}")

            if target.exists() and target.is_file():
                backup_if_exists(rel)
                target.unlink()

        expected = patch_meta.get("changed_files", {})
        for rel, meta in expected.items():
            normalized_rel = normalize_rel_path(rel)
            target = install_root / normalized_rel
            if not target.exists() or not target.is_file():
                raise RuntimeError(f"Patched file missing after apply: {normalized_rel}")

            expected_hash = meta.get("sha256")
            if expected_hash and sha256_file(target) != expected_hash:
                raise RuntimeError(f"Hash mismatch after patch apply: {normalized_rel}")

    except Exception:
        for path in created_paths:
            if path.exists() and path.is_file():
                path.unlink()

        for rel in backup_paths:
            backup_file = backup_dir / rel
            target = install_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_file, target)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def fetch_update_feed(feed_url: str) -> dict:
    if requests is None:
        raise RuntimeError("requests is required to fetch update metadata.")

    response = requests.get(feed_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_patch_for_version(feed: dict, current_version: str) -> dict:
    patches = feed.get("patches", {})
    if isinstance(patches, dict):
        patch = patches.get(current_version)
        if isinstance(patch, dict):
            return patch

    if isinstance(patches, list):
        for patch in patches:
            if isinstance(patch, dict) and patch.get("from_version") == current_version:
                return patch

    raise RuntimeError(f"No patch is available from version {current_version}.")


def download_file(url: str, output_path: Path, expected_sha256: str | None, progress_callback) -> None:
    if requests is None:
        raise RuntimeError("requests is required to download updates.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", "0") or 0)
        downloaded = 0
        hasher = hashlib.sha256()

        with output_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                f.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                progress_callback(downloaded, total)

    actual_hash = hasher.hexdigest()
    if expected_sha256 and actual_hash.lower() != expected_sha256.lower():
        output_path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded patch failed SHA256 verification.")


class PatchUpdaterApp(ctk.CTk if ctk else object):
    def __init__(
        self,
        install_root: Path,
        current_version: str,
        feed_url: str,
        restart_exe: str,
    ) -> None:
        super().__init__()
        self.install_root = install_root.resolve()
        self.current_version = current_version
        self.feed_url = feed_url
        self.restart_exe = restart_exe
        self.failed = False

        self.title("Updating")
        self.geometry("460x230")
        self.resizable(False, False)

        self.status_label = ctk.CTkLabel(
            self,
            text="Preparing update...",
            wraplength=400,
            justify="center",
        )
        self.status_label.pack(padx=24, pady=(28, 12))

        self.progress = ctk.CTkProgressBar(self, mode="indeterminate", width=340)
        self.progress.pack(padx=24, pady=10)
        self.progress.start()

        self.action_button = ctk.CTkButton(
            self,
            text="Working...",
            state="disabled",
            command=self.close_or_restart,
        )
        self.action_button.pack(padx=24, pady=(18, 24))

        self.after(250, self.start_update)

    def set_status(self, text: str) -> None:
        self.after(0, lambda: self.status_label.configure(text=text))

    def set_download_progress(self, downloaded: int, total: int) -> None:
        if total <= 0:
            return

        progress_value = min(downloaded / total, 1)

        def update() -> None:
            self.progress.configure(mode="determinate")
            self.progress.set(progress_value)

        self.after(0, update)

    def start_update(self) -> None:
        threading.Thread(target=self.run_update, daemon=True).start()

    def run_update(self) -> None:
        try:
            self.set_status("Checking latest release...")
            feed = fetch_update_feed(self.feed_url)
            latest_version = str(feed.get("version", "unknown"))
            patch = get_patch_for_version(feed, self.current_version)

            patch_url = patch.get("url")
            if not patch_url:
                raise RuntimeError("Patch metadata does not include a URL.")

            patch_hash = patch.get("sha256")
            patch_path = self.install_root / f".downloaded_patch_{uuid4().hex}.zip"

            self.set_status(f"Downloading update {self.current_version} to {latest_version}...")
            download_file(patch_url, patch_path, patch_hash, self.set_download_progress)

            self.set_status("Applying update...")
            self.after(0, lambda: self.progress.configure(mode="indeterminate"))
            self.after(0, self.progress.start)
            apply_patch(self.install_root, patch_path)
            patch_path.unlink(missing_ok=True)

            self.set_status(f"Update completed successfully.\nVersion: {latest_version}")
            self.after(0, self.finish_success)
        except Exception as exc:
            self.failed = True
            self.set_status(
                "Update failed.\n"
                "Your installed files were restored when needed.\n\n"
                f"Details: {exc}"
            )
            self.after(0, self.finish_failure)

    def finish_success(self) -> None:
        self.progress.stop()
        self.progress.pack_forget()
        self.action_button.configure(text="Restart app", state="normal")

    def finish_failure(self) -> None:
        self.progress.stop()
        self.progress.pack_forget()
        self.action_button.configure(text="Close", state="normal")

    def close_or_restart(self) -> None:
        if not self.failed:
            restart_path = self.install_root / self.restart_exe
            if restart_path.exists():
                try:
                    subprocess.Popen([str(restart_path)], cwd=str(self.install_root))
                    time.sleep(0.2)
                except OSError:
                    pass
        self.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download and apply a patch update.")
    parser.add_argument("--install-root", required=True, help="Installed app root.")
    parser.add_argument("--current-version", required=True, help="Currently installed version.")
    parser.add_argument("--feed-url", required=True, help="URL to latest.json update feed.")
    parser.add_argument("--restart-exe", default="app.exe", help="Executable to start after update.")
    return parser.parse_args()


if __name__ == "__main__":
    if ctk is None:
        raise SystemExit("customtkinter is required to run the updater GUI.")

    args = parse_args()
    os.chdir(args.install_root)
    app = PatchUpdaterApp(
        install_root=Path(args.install_root),
        current_version=args.current_version,
        feed_url=args.feed_url,
        restart_exe=args.restart_exe,
    )
    app.mainloop()

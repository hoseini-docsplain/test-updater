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
PROTECTED_FILES = {"updater.exe"}
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


def ensure_patch_does_not_update_protected_files(rel_paths: list[str]) -> None:
    protected = sorted(rel for rel in rel_paths if rel in PROTECTED_FILES)
    if protected:
        joined = ", ".join(protected)
        raise RuntimeError(
            f"Patch includes updater executable(s): {joined}. "
            "Rebuild the patch excluding updater executables."
        )


def is_protected_file(rel_path: str) -> bool:
    return rel_path in PROTECTED_FILES


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
    delete_prefixes: tuple[str, ...] = ("app.exe", "updater.exe", "_internal"),
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
        ensure_patch_does_not_update_protected_files(changed_rel_paths)
        ensure_patch_does_not_update_protected_files(deleted_file_list)

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


def find_full_bundle_root(extract_dir: Path) -> Path:
    if (extract_dir / "app.exe").exists() or (extract_dir / "_internal").is_dir():
        return extract_dir

    candidates = [
        path
        for path in extract_dir.iterdir()
        if path.is_dir() and ((path / "app.exe").exists() or (path / "_internal").is_dir())
    ]
    if len(candidates) == 1:
        return candidates[0]

    raise RuntimeError("Full update zip must contain app.exe and _internal at its root.")


def collect_full_bundle_files(bundle_root: Path) -> list[str]:
    rel_paths = []
    for path in bundle_root.rglob("*"):
        if path.is_file():
            rel_paths.append(normalize_rel_path(path.relative_to(bundle_root).as_posix()))
    return sorted(rel_paths)


def collect_managed_install_files(install_root: Path) -> list[str]:
    rel_paths = []
    app_exe = install_root / "app.exe"
    if app_exe.is_file():
        rel_paths.append("app.exe")

    internal_root = install_root / "_internal"
    if internal_root.is_dir():
        for path in internal_root.rglob("*"):
            if path.is_file():
                rel_paths.append(normalize_rel_path(path.relative_to(install_root).as_posix()))

    return sorted(rel_paths)


def write_deferred_apply_script(
    work_dir: Path,
    install_root: Path,
    source_root: Path,
    copy_rel_paths: list[str],
    delete_rel_paths: list[str],
    restart_exe: str,
) -> Path:
    plan_path = work_dir / "apply_plan.json"
    script_path = work_dir / "apply_update.ps1"
    backup_dir = work_dir / "backup"
    log_path = work_dir / "apply_update.log"

    plan = {
        "pid": os.getpid(),
        "install_root": str(install_root),
        "source_root": str(source_root),
        "backup_root": str(backup_dir),
        "restart_path": str(install_root / restart_exe),
        "copy_files": copy_rel_paths,
        "delete_files": delete_rel_paths,
        "work_dir": str(work_dir),
        "log_path": str(log_path),
    }
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    script_path.write_text(
        r'''
$ErrorActionPreference = "Stop"
$plan = Get-Content -LiteralPath $args[0] -Raw | ConvertFrom-Json

function Write-UpdateLog($message) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $plan.log_path -Value "[$timestamp] $message"
}

function Join-RelativePath($root, $relative) {
    $parts = $relative -split "/"
    return Join-Path -Path $root -ChildPath ([System.IO.Path]::Combine($parts))
}

try {
    Write-UpdateLog "Waiting for updater process $($plan.pid) to exit."
    Wait-Process -Id $plan.pid -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500

    New-Item -ItemType Directory -Path $plan.backup_root -Force | Out-Null

    foreach ($relative in $plan.copy_files) {
        $target = Join-RelativePath $plan.install_root $relative
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            $backup = Join-RelativePath $plan.backup_root $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $target -Destination $backup -Force
        }
    }

    foreach ($relative in $plan.delete_files) {
        $target = Join-RelativePath $plan.install_root $relative
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            $backup = Join-RelativePath $plan.backup_root $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $target -Destination $backup -Force
            Remove-Item -LiteralPath $target -Force
        }
    }

    foreach ($relative in $plan.copy_files) {
        $source = Join-RelativePath $plan.source_root $relative
        $target = Join-RelativePath $plan.install_root $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $target -Force
    }

    Write-UpdateLog "Update applied successfully."
    if (Test-Path -LiteralPath $plan.restart_path -PathType Leaf) {
        Start-Process -FilePath $plan.restart_path -WorkingDirectory $plan.install_root
    }
}
catch {
    Write-UpdateLog "Update failed: $($_.Exception.Message)"
    try {
        foreach ($relative in $plan.copy_files) {
            $backup = Join-RelativePath $plan.backup_root $relative
            if (Test-Path -LiteralPath $backup -PathType Leaf) {
                $target = Join-RelativePath $plan.install_root $relative
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                Copy-Item -LiteralPath $backup -Destination $target -Force
            }
        }
        foreach ($relative in $plan.delete_files) {
            $backup = Join-RelativePath $plan.backup_root $relative
            if (Test-Path -LiteralPath $backup -PathType Leaf) {
                $target = Join-RelativePath $plan.install_root $relative
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                Copy-Item -LiteralPath $backup -Destination $target -Force
            }
        }
    }
    catch {
        Write-UpdateLog "Rollback failed: $($_.Exception.Message)"
    }
}
'''.strip(),
        encoding="utf-8",
    )
    return script_path


def launch_deferred_apply(script_path: Path, plan_path: Path) -> None:
    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            str(plan_path),
        ],
        cwd=str(script_path.parent),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def stage_patch_update(install_root: Path, patch_zip_path: Path, restart_exe: str) -> Path:
    install_root = install_root.resolve()
    work_dir = install_root / f".pending_patch_{uuid4().hex}"
    extract_dir = work_dir / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    safe_extract(patch_zip_path, extract_dir)

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
    ensure_patch_does_not_update_protected_files(changed_rel_paths)
    ensure_patch_does_not_update_protected_files(deleted_file_list)

    return write_deferred_apply_script(
        work_dir=work_dir,
        install_root=install_root,
        source_root=extract_dir,
        copy_rel_paths=changed_rel_paths,
        delete_rel_paths=deleted_file_list,
        restart_exe=restart_exe,
    )


def stage_full_update(install_root: Path, full_zip_path: Path, restart_exe: str) -> Path:
    install_root = install_root.resolve()
    work_dir = install_root / f".pending_full_update_{uuid4().hex}"
    extract_dir = work_dir / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    safe_extract(full_zip_path, extract_dir)
    bundle_root = find_full_bundle_root(extract_dir)
    copy_rel_paths = collect_full_bundle_files(bundle_root)
    source_set = set(copy_rel_paths)
    delete_rel_paths = [
        rel
        for rel in collect_managed_install_files(install_root)
        if rel not in source_set
    ]

    return write_deferred_apply_script(
        work_dir=work_dir,
        install_root=install_root,
        source_root=bundle_root,
        copy_rel_paths=copy_rel_paths,
        delete_rel_paths=delete_rel_paths,
        restart_exe=restart_exe,
    )


def apply_full_update(install_root: Path, full_zip_path: Path) -> None:
    install_root = install_root.resolve()
    full_zip_path = full_zip_path.resolve()

    if not install_root.is_dir():
        raise RuntimeError(f"Install root does not exist: {install_root}")
    if not full_zip_path.is_file():
        raise RuntimeError(f"Full update file does not exist: {full_zip_path}")

    work_dir = install_root / f".app_full_update_{uuid4().hex}"
    extract_dir = work_dir / "extract"
    backup_dir = work_dir / "backup"
    extract_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    created_paths: list[Path] = []
    backup_paths: list[str] = []

    try:
        safe_extract(full_zip_path, extract_dir)
        bundle_root = find_full_bundle_root(extract_dir)
        source_rel_paths = collect_full_bundle_files(bundle_root)
        copied_rel_paths = [rel for rel in source_rel_paths if not is_protected_file(rel)]
        source_set = set(copied_rel_paths)
        delete_rel_paths = [
            rel
            for rel in collect_managed_install_files(install_root)
            if rel not in source_set and not is_protected_file(rel)
        ]

        def backup_if_exists(rel: str) -> None:
            target = install_root / rel
            if target.exists() and target.is_file() and rel not in backup_paths:
                dest = backup_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)
                backup_paths.append(rel)

        for rel in copied_rel_paths:
            source = bundle_root / rel
            target = install_root / rel
            if not is_safe_path(install_root, target):
                raise RuntimeError(f"Unsafe target path in full update: {rel}")

            backup_if_exists(rel)
            if not target.exists():
                created_paths.append(target)

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        for rel in delete_rel_paths:
            target = install_root / rel
            if not is_safe_path(install_root, target):
                raise RuntimeError(f"Unsafe delete target in full update: {rel}")

            if target.exists() and target.is_file():
                backup_if_exists(rel)
                target.unlink()

        for rel in copied_rel_paths:
            source = bundle_root / rel
            target = install_root / rel
            if not target.exists() or sha256_file(source) != sha256_file(target):
                raise RuntimeError(f"Hash mismatch after full update apply: {rel}")

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


def get_patch_for_version(feed: dict, current_version: str) -> dict | None:
    patches = feed.get("patches", {})
    if isinstance(patches, dict):
        patch = patches.get(current_version)
        if isinstance(patch, dict):
            return patch

    if isinstance(patches, list):
        for patch in patches:
            if isinstance(patch, dict) and patch.get("from_version") == current_version:
                return patch

    return None


def get_full_update(feed: dict) -> dict | None:
    full = feed.get("full")
    return full if isinstance(full, dict) and full.get("url") else None


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


class UpdaterApp(ctk.CTk if ctk else object):
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
            full = get_full_update(feed)

            update_package = patch or full
            update_kind = "patch" if patch else "full update"
            if not update_package:
                raise RuntimeError(f"No patch or full update is available from version {self.current_version}.")

            package_url = update_package.get("url")
            if not package_url:
                raise RuntimeError("Update metadata does not include a URL.")

            package_hash = update_package.get("sha256")
            package_path = self.install_root / f".downloaded_update_{uuid4().hex}.zip"

            self.set_status(f"Downloading {update_kind} {self.current_version} to {latest_version}...")
            download_file(package_url, package_path, package_hash, self.set_download_progress)

            self.set_status(f"Preparing {update_kind}...")
            self.after(0, lambda: self.progress.configure(mode="indeterminate"))
            self.after(0, self.progress.start)
            if patch:
                script_path = stage_patch_update(self.install_root, package_path, self.restart_exe)
            else:
                script_path = stage_full_update(self.install_root, package_path, self.restart_exe)
            package_path.unlink(missing_ok=True)

            self.set_status(f"Finishing update to {latest_version}...")
            launch_deferred_apply(script_path, script_path.parent / "apply_plan.json")
            self.after(500, self.destroy)
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
    app = UpdaterApp(
        install_root=Path(args.install_root),
        current_version=args.current_version,
        feed_url=args.feed_url,
        restart_exe=args.restart_exe,
    )
    app.mainloop()

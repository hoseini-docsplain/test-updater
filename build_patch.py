import argparse
import datetime as dt
import hashlib
import json
import time
import urllib.parse
import zipfile
from pathlib import Path


DEFAULT_EXCLUDES = {
    "manifest.json",
    "updater.exe",
}


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def collect_file_map(root: Path, excludes: set[str]) -> dict[str, dict[str, int | str]]:
    file_map: dict[str, dict[str, int | str]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel in excludes:
            continue
        file_map[rel] = {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
    return file_map


def build_patch(
    old_root: Path,
    new_root: Path,
    from_version: str,
    to_version: str,
    output: Path,
    excludes: set[str],
) -> dict:
    old_files = collect_file_map(old_root, excludes)
    new_files = collect_file_map(new_root, excludes)

    changed_or_new = [
        rel for rel, meta in new_files.items() if rel not in old_files or old_files[rel]["sha256"] != meta["sha256"]
    ]
    deleted = [rel for rel in old_files if rel not in new_files]

    patch_meta = {
        "from_version": from_version,
        "to_version": to_version,
        "generated_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "changed_files": {
            rel: {
                "sha256": new_files[rel]["sha256"],
                "size": new_files[rel]["size"],
            }
            for rel in changed_or_new
        },
        "deleted_files_count": len(deleted),
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in changed_or_new:
            zf.write(new_root / rel, arcname=rel)

        zf.writestr("deleted_files.txt", "\n".join(sorted(deleted)) + ("\n" if deleted else ""))
        zf.writestr("patch_meta.json", json.dumps(patch_meta, indent=2))

    return {
        "changed_files": len(changed_or_new),
        "deleted_files": len(deleted),
        "output": str(output),
        "sha256": sha256_file(output),
    }


def build_zip_from_root(root: Path, zip_output: Path) -> None:
    zip_output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            zf.write(path, arcname=rel)


def combine_base_url(base_url: str, file_name: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", file_name)


def with_optional_suffix(base: str, suffix: str | None) -> str:
    if suffix:
        return f"{base}-{suffix}"
    return base


def write_latest_json(
    output_path: Path,
    to_version: str,
    from_version: str,
    base_url: str,
    full_zip_name: str,
    full_sha256: str,
    patch_zip_name: str,
    patch_sha256: str,
) -> None:
    latest_payload = {
        "version": to_version,
        "full": {
            "url": combine_base_url(base_url, full_zip_name),
            "sha256": full_sha256,
        },
        "patches": {
            from_version: {
                "url": combine_base_url(base_url, patch_zip_name),
                "sha256": patch_sha256,
            }
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(latest_payload, indent=2), encoding="utf-8")


def main() -> None:
    start_time = time.perf_counter()
    parser = argparse.ArgumentParser(description="Build full/patch packages and latest.json from release folders.")
    parser.add_argument("--root-dir", required=True, help="Release root directory containing full-<version>-<suffix> folders")
    parser.add_argument("--from-version", required=True, help="Old version label (example: 0.0.1)")
    parser.add_argument("--to-version", required=True, help="New version label (example: 0.0.2)")
    parser.add_argument("--suffix", help="Optional folder/file suffix (example: local)")
    parser.add_argument("--base-url", required=True, help="Base URL used to construct full and patch URLs")
    parser.add_argument(
        "--exclude",
        action="append",
        default=sorted(DEFAULT_EXCLUDES),
        help="Relative path to exclude from compare/patch. Can be passed multiple times.",
    )
    args = parser.parse_args()

    root_dir = Path(args.root_dir).resolve()
    old_root = root_dir / with_optional_suffix(f"full-{args.from_version}", args.suffix)
    new_root = root_dir / with_optional_suffix(f"full-{args.to_version}", args.suffix)
    output_name = with_optional_suffix(
        f"patch-{args.from_version}-to-{args.to_version}",
        args.suffix,
    )
    output = root_dir / f"{output_name}.zip"
    latest_output = root_dir / "latest.json"

    if not old_root.is_dir():
        raise SystemExit(f"Old root does not exist or is not a directory: {old_root}")
    if not new_root.is_dir():
        raise SystemExit(f"New root does not exist or is not a directory: {new_root}")

    # Build the full package zip first from new-root.
    full_zip_output = output.parent / f"{new_root.name}.zip"
    build_zip_from_root(new_root, full_zip_output)
    full_zip_sha256 = sha256_file(full_zip_output)

    excludes = {x.replace("\\", "/") for x in args.exclude}
    result = build_patch(
        old_root=old_root,
        new_root=new_root,
        from_version=args.from_version,
        to_version=args.to_version,
        output=output,
        excludes=excludes,
    )

    print(f"Patch written: {result['output']}")
    print(f"Changed/new files: {result['changed_files']}")
    print(f"Deleted files: {result['deleted_files']}")
    print(f"SHA256: {result['sha256']}")
    print(f"Full zip written: {full_zip_output}")
    print(f"Full zip SHA256: {full_zip_sha256}")
    write_latest_json(
        output_path=latest_output,
        to_version=args.to_version,
        from_version=args.from_version,
        base_url=args.base_url,
        full_zip_name=full_zip_output.name,
        full_sha256=full_zip_sha256,
        patch_zip_name=output.name,
        patch_sha256=result["sha256"],
    )
    print(f"latest.json written: {latest_output}")

    print(f"Execution time: {time.perf_counter() - start_time:.3f} seconds")


if __name__ == "__main__":
    main()

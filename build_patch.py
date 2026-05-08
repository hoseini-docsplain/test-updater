import argparse
import datetime as dt
import hashlib
import json
import zipfile
from pathlib import Path


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a patch zip between two app build folders.")
    parser.add_argument("--old-root", required=True, help="Old build root (example: releases/0.0.1/bundle)")
    parser.add_argument("--new-root", required=True, help="New build root (example: releases/0.0.2/bundle)")
    parser.add_argument("--from-version", required=True, help="Old version label (example: 0.0.1)")
    parser.add_argument("--to-version", required=True, help="New version label (example: 0.0.2)")
    parser.add_argument("--output", default="patch.zip", help="Patch zip output path")
    parser.add_argument(
        "--exclude",
        action="append",
        default=["manifest.json"],
        help="Relative path to exclude from compare/patch. Can be passed multiple times.",
    )
    args = parser.parse_args()

    old_root = Path(args.old_root).resolve()
    new_root = Path(args.new_root).resolve()
    output = Path(args.output).resolve()

    if not old_root.is_dir():
        raise SystemExit(f"Old root does not exist or is not a directory: {old_root}")
    if not new_root.is_dir():
        raise SystemExit(f"New root does not exist or is not a directory: {new_root}")

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


if __name__ == "__main__":
    main()

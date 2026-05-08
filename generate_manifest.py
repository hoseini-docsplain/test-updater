import argparse
import datetime as dt
import hashlib
import json
import time
from pathlib import Path


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_manifest(root: Path, version: str, excludes: set[str]) -> dict:
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel in excludes:
            continue
        files.append(
            {
                "path": rel,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )

    return {
        "version": version,
        "generated_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "files": files,
    }


def main() -> None:
    start_time = time.perf_counter()
    parser = argparse.ArgumentParser(
        description="Generate a file-hash manifest for app updates."
    )
    parser.add_argument("--version", required=True, help="Release version (example: 1.2.0)")
    parser.add_argument(
        "--root",
        default=".",
        help="Root directory to scan (example: dist/bundle)",
    )
    parser.add_argument(
        "--output",
        default="manifest.json",
        help="Where to write manifest JSON (example: dist/bundle/manifest.json)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=["manifest.json"],
        help="Relative file path to exclude. Can be passed multiple times.",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output = Path(args.output).resolve()

    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Root directory does not exist or is not a directory: {root}")

    excludes = {p.replace("\\", "/") for p in args.exclude}
    manifest = build_manifest(root=root, version=args.version, excludes=excludes)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    elapsed = time.perf_counter() - start_time
    print(f"Wrote manifest with {len(manifest['files'])} files: {output}")
    print(f"Execution time: {elapsed:.3f} seconds")


if __name__ == "__main__":
    main()

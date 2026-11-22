"""Checksum lists and verification."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from .common import iter_files

CHUNK = 1024 * 1024


def _digest(path: Path, algo: str) -> str:
    digest = hashlib.new(algo)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_checksum(args) -> int:
    lines: list[str] = []
    for raw in args.paths:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            print(f"checksum: no such path: {path}", file=sys.stderr)
            return 1
        files = [path] if path.is_file() else iter_files(path)
        for candidate in files:
            lines.append(f"{_digest(candidate, args.algo)}  {candidate}")
    output = f"# algo: {args.algo}\n" + "\n".join(lines) + ("\n" if lines else "")
    if args.output:
        target = Path(args.output).expanduser().resolve()
        target.write_text(output, encoding="utf-8")
        print(f"Wrote {len(lines)} checksum(s) to {target}")
    else:
        sys.stdout.write(output)
    return 0


def cmd_verify(args) -> int:
    manifest = Path(args.manifest).expanduser().resolve()
    if not manifest.is_file():
        print(f"verify: no such manifest: {manifest}", file=sys.stderr)
        return 1
    lines = manifest.read_text(encoding="utf-8").splitlines()
    algo = "md5"
    if lines and lines[0].startswith("# algo: "):
        algo = lines[0][8:].strip()
        lines = lines[1:]
    failures = 0
    checked = 0
    for line in lines:
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            print(f"verify: malformed line: {line}")
            failures += 1
            continue
        expected, raw_path = parts
        path = Path(raw_path.strip())
        checked += 1
        if not path.is_file():
            print(f"MISSING  {path}")
            failures += 1
            continue
        actual = _digest(path, algo)
        status = "OK" if actual == expected.lower() else "MISMATCH"
        print(f"{status:8}  {path}")
        if status != "OK":
            failures += 1
    print(f"{checked - failures}/{checked} verified" if checked else "Nothing to verify")
    return 1 if failures else 0

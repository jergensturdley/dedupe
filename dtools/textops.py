"""Tab removal and line-ending conversion for text files."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .common import is_binary


def _write_result(path: Path, data: bytes, in_place: bool) -> None:
    if not in_place:
        sys.stdout.buffer.write(data)
        return
    temp = path.with_name(path.name + ".dedupe-tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def _process(paths: list[str], transform, in_place: bool) -> int:
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            print(f"textops: no such file: {path}", file=sys.stderr)
            return 1
        data = path.read_bytes()
        if is_binary(data):
            print(f"skip binary file: {path}")
            continue
        text = data.decode("utf-8", errors="replace")
        result = transform(text).encode("utf-8")
        if result != data:
            _write_result(path, result, in_place)
    return 0


def cmd_detab(args) -> int:
    width = max(1, args.tabs)
    return _process(args.paths, lambda text: text.replace("\t", " " * width), args.in_place)


def _convert(text: str, target: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "crlf":
        return normalized.replace("\n", "\r\n")
    return normalized


def cmd_crlf(args) -> int:
    if args.to not in ("lf", "crlf"):
        print(f"crlf: invalid target: {args.to}", file=sys.stderr)
        return 1
    return _process(args.paths, lambda text: _convert(text, args.to), args.in_place)

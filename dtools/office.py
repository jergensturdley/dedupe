"""Search text inside zip-based office documents."""

from __future__ import annotations

import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .common import iter_files

OFFICE_MEMBERS: dict[str, list[str]] = {
    ".docx": ["word/document.xml"],
    ".xlsx": ["xl/sharedStrings.xml"],
    ".pptx": ["ppt/slides/"],
    ".odt": ["content.xml"],
    ".ods": ["content.xml"],
}
LEGACY_SUFFIXES = (".doc", ".xls", ".ppt")


def _text_from_xml(data: bytes) -> str:
    root = ET.fromstring(data)
    pieces: list[str] = []
    for element in root.iter():
        if element.text:
            pieces.append(element.text)
        if element.tail:
            pieces.append(element.tail)
    return " ".join(pieces)


def _snippets(text: str, pattern: re.Pattern[str], limit: int) -> list[str]:
    results: list[str] = []
    for match in pattern.finditer(text):
        start = max(0, match.start() - 45)
        end = min(len(text), match.end() + 75)
        snippet = " ".join(text[start:end].split())
        results.append(snippet)
        if len(results) >= limit:
            break
    return results


def _search_file(path: Path, pattern: re.Pattern[str]) -> int:
    suffix = path.suffix.lower()
    if suffix in LEGACY_SUFFIXES:
        print(f"skip legacy format {path} (needs a converter)")
        return 0
    wanted = OFFICE_MEMBERS.get(suffix)
    if wanted is None:
        return 0
    documents: list[tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not any(name.lower().startswith(w.lower()) for w in wanted):
                    continue
                if zf.getinfo(name).file_size > 50 * 1024 * 1024:
                    continue
                documents.append((name, zf.read(name)))
    except (zipfile.BadZipFile, OSError) as error:
        print(f"office: {error}: {path}", file=sys.stderr)
        return 0
    hits = 0
    for name, data in documents:
        try:
            text = _text_from_xml(data)
        except ET.ParseError:
            continue
        label = name.split("/")[-1]
        for snippet in _snippets(text, pattern, 20):
            print(f"{path}:{label}: {snippet}")
            hits += 1
    return hits


def cmd_office(args) -> int:
    pattern = re.compile(args.pattern, re.IGNORECASE)
    total = 0
    for raw in args.paths:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            print(f"office: no such path: {path}", file=sys.stderr)
            continue
        files = [path] if path.is_file() else iter_files(path)
        for candidate in files:
            if candidate.suffix.lower() in OFFICE_MEMBERS or candidate.suffix.lower() in LEGACY_SUFFIXES:
                total += _search_file(candidate, pattern)
    print(f"{total} match(es) found.")
    return 0

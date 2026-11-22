# dedupe

A duplicate file finder with a terminal UI, built on
[Textual](https://github.com/Textualize/textual). Scan a directory, review the
groups of identical files, delete the copies you don't need. Deleted files move
to `~/.dedupe_trash` rather than being unlinked, and `dedupe --restore` brings
back the most recent batch.

Detection is by content: group by size, sample the first and last 64 KiB, then
run a full BLAKE2b hash only on files that still match. Hashing is parallelized
across cores. Symlinks are never followed or hashed.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/jergensturdley/dedupe.git
cd dedupe
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./dedupe
```

The `./dedupe` launcher picks up `.venv` automatically. For development, also
install `requirements-dev.txt` and run the tests:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

## Usage

```
dedupe [PATH]           TUI (defaults to the current directory)
dedupe --scan [PATH]    print a report; read-only
dedupe --restore        restore the most recent trash batch
dedupe --keep STRATEGY  shortest | oldest | newest (default: shortest)
dedupe --sort ORDER     waste | name (default: waste)
dedupe --min-size N     ignore files smaller than N bytes
```

Every action has both a button and a key. The buttons cover the common
operations: mark, keep this, open (platform default app), reveal (file
manager), copy path, info, preview, filter, delete marked, trash. The keys:
`space` mark, `a` select all in group, `d` delete marked, `r` rescan, `s`
re-sort, `g` cycle keeper strategy, `f` jump to the filter box, `v` open the
trash browser, `o`/`c`/`i`/`p` open, copy path, info, preview, `t` tools,
`q` quit.

The trash browser lists every deleted batch with its files and restores any
batch (or the latest); the main view rescans afterwards.

## Safety

- Deleted files go to `~/.dedupe_trash/<batch>/`, relative paths preserved,
  with a manifest per batch. Nothing is unlinked outright.
- `--scan` is read-only.
- `unzip` and `arclist` reject path traversal and absolute paths in archives.

## File tools

The same launcher carries a set of small file tools as subcommands:

| Command | Description |
| --- | --- |
| `dedupe zip` | Create a zip archive |
| `dedupe unzip` | Extract a zip or tar archive |
| `dedupe arclist` | List archive contents, including nested archives |
| `dedupe backup` | Copy a tree with exclusions |
| `dedupe compare` | List added, removed and changed files between two trees |
| `dedupe treesize` | Show cumulative sizes of subfolders |
| `dedupe office` | Search text inside docx/xlsx/pptx/odt/ods files |
| `dedupe detab` | Replace tabs with spaces (skips binary files) |
| `dedupe crlf` | Convert line endings (lf/crlf) |
| `dedupe checksum` | Write a checksum list (md5/sha256/...) |
| `dedupe verify` | Verify a checksum list |
| `dedupe serve` | Share a file or folder over HTTP with a QR link |

Each takes `--help` for its options. The share server accepts a password via
`--password`, `--auth` (randomly generated), or the `DEDUPE_SHARE_PASSWORD`
environment variable.

## Out of scope

Secure delete, file split/join, two-way folder sync, and near-duplicate
image/audio detection. The first three work against the trash-first model or
duplicate the existing `backup`/`compare` tools; the last would pull in heavy
ML dependencies.

## Layout

```
dedupe.py       TUI application and entry point
dedupe          launcher script (uses .venv if present)
dtools/         subcommand implementations
  archives.py   zip/unzip/arclist
  checksum.py   checksum/verify
  common.py     shared helpers (walking, hashing, formatting)
  compare.py    backup/compare/treesize
  office.py     office document text search
  server.py     HTTP share server with QR
  textops.py    detab/crlf
tests/          TUI tests (pytest)
docs/           screenshots
```

## License

[MIT](LICENSE)

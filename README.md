# dedupe

Find and remove duplicate files through an interactive terminal UI, backed by a set of
small, dependency-light file tools.

`dedupe` walks a directory tree, groups files by content (size + sampled head/tail,
then a full BLAKE2b hash), and presents the duplicate groups in a keyboard-driven
[Textual](https://github.com/Textualize/textual) TUI where you mark and delete the
redundant copies. Deletions are never permanent: files move to `~/.dedupe_trash` and
the most recent batch can be restored with one command.

## Features

- **Content-based detection** — size pre-filter, 64 KiB head/tail sampling, full
  BLAKE2b hashing only for real candidates; hashing parallelized across cores.
- **Safe by default** — deleted files go to a trash folder, not the void;
  `dedupe --restore` brings back the last batch.
- **Interactive TUI** — progress bar during scan, group navigation, keeper
  strategies (shortest path / oldest / newest), per-group wasted-space totals.
- **Mouse-first file operations** — every common action has a button: a top
  action bar (Rescan / Sort / Select all / Delete marked / Trash / Tools /
  Quit), a group filter box with Filter and Clear buttons, per-group keeper
  strategy buttons, and per-file buttons: Mark, Keep this, Open (opens the
  file with the platform default app), Reveal (shows it in the file
  manager), Copy path, Info, Preview and Hide info.
- **Trash browser** — press `v` or the Trash button to list every deleted
  batch with its files, and restore any batch (or the latest one) with a
  button; the main view rescans automatically afterwards.
- **Text preview & file info** — preview the first lines of any duplicate
  (binary-safe) and see its full path, size and modification time.
- **Headless mode** — `dedupe --scan` prints a report and touches nothing; safe
  for scripts and cron.
- **Bundled file tools** — one binary, twelve subcommands (see below).

## Installation

Requires Python 3.10+.

```bash
git clone https://github.com/jergensturdley/dedupe.git
cd dedupe
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then run via the launcher (uses the local venv automatically):

```bash
./dedupe [PATH]
```

Or directly: `.venv/bin/python dedupe.py [PATH]`

## Usage

```
dedupe [PATH]           launch the TUI (defaults to the current directory)
dedupe --scan [PATH]    headless report; no UI and no changes
dedupe --restore        restore the most recent trash batch
dedupe --keep STRATEGY  shortest | oldest | newest  (default: shortest)
dedupe --sort ORDER     waste | name                (default: waste)
dedupe --min-size N     ignore files smaller than N bytes
```

Keybindings are shown in the TUI footer: `space` marks a file, `a` selects
all in the group, `d` deletes the marked files to trash, `r` rescans,
`s` re-sorts, `g` cycles the keeper strategy, `f` jumps to the filter box,
`v` opens the trash browser, `o`/`c`/`i`/`p` open, copy, inspect or preview
the selected file, `t` opens the tools screen, `q` quits. Every one of these
actions also has a clickable button.

### Skipped features (with reasons)

In the spirit of Swiss File Knife the following ideas were considered and
deliberately left out:

- **Secure/irrecoverable delete** — unverifiable on modern filesystems and
  contrary to the trash-first safety model.
- **File split/join and base64/hexdump converters** — niche utilities with
  questionable value inside a dedupe workflow.
- **Two-way folder sync / mirror** — risk of destructive overwrite; the safe
  one-way `backup` and `compare` tools already cover the common cases.
- **Similar-image/audio detection** — requires heavy ML dependencies;
  skipped per the "no dependency hell" rule.

## File tools

| Command | Description |
| --- | --- |
| `dedupe zip` | Create a zip archive |
| `dedupe unzip` | Extract a zip or tar archive |
| `dedupe arclist` | List archive contents, including nested archives |
| `dedupe backup` | Copy a tree with exclusions |
| `dedupe compare` | List added, removed and changed files between two trees |
| `dedupe treesize` | Show cumulative sizes of subfolders |
| `dedupe office` | Search text inside docx/xlsx/pptx/odt/ods files |
| `dedupe detab` | Replace tabs with spaces (binary-safe skip) |
| `dedupe crlf` | Convert line endings (lf/crlf) |
| `dedupe checksum` | Write a checksum list (md5/sha256/...) |
| `dedupe verify` | Verify a checksum list |
| `dedupe serve` | Share a file or folder over HTTP with a QR link |

Each tool takes `--help` for its full options, e.g. `dedupe serve --help`.
The share server can require a password via `--password`, `--auth`
(random generated), or the `DEDUPE_SHARE_PASSWORD` environment variable.

## Safety model

- Nothing is ever unlinked outright; deletions move files into
  `~/.dedupe_trash/<batch>/` preserving relative paths.
- `--scan` is strictly read-only.
- Symlinks are never followed or hashed during scans.
- Archive extraction (`unzip`/`arclist`) rejects path traversal and absolute paths.

## Project layout

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
```

## License

[MIT](LICENSE)

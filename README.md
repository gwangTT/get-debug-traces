# get-debug-traces

Consumer-side bootstrap for [`tenstorrent/bit_sculpt`](https://github.com/tenstorrent/bit_sculpt)
debug-trace releases.

This repo holds **only `get.py`** — a stdlib-only Python script that
downloads, SHA-verifies, extracts, and self-checks a release bundle.
The traces themselves live as private GitHub Release assets in
`tenstorrent/bit_sculpt`; this public sidecar exists so the
download-script bootstrap is `curl`-able with no auth, even though
the repo holding the assets isn't.

## Usage

Browse + pick interactively (release tag → traces → dest):

```bash
curl -fsSL https://raw.githubusercontent.com/gwangTT/get-debug-traces/main/get.py \
  | python3 -
```

List all release tags + summary, no download:

```bash
curl -fsSL https://raw.githubusercontent.com/gwangTT/get-debug-traces/main/get.py \
  | python3 - list
```

Non-interactive download of every trace in a tag:

```bash
curl -fsSL https://raw.githubusercontent.com/gwangTT/get-debug-traces/main/get.py \
  | python3 - <tag> <dest>
```

Example — fetch the GLM-5.1 8K pair:

```bash
curl -fsSL https://raw.githubusercontent.com/gwangTT/get-debug-traces/main/get.py \
  | python3 - zai-org/glm51 ~/glm51-traces
```

## Prerequisites

- `python3` (stdlib only — no `pip install`)
- `tar` with `--zstd` support (any modern coreutils; script falls back
  to `zstd -dc | tar` if your tar predates 2019)
- **`gh` CLI authenticated against `tenstorrent/bit_sculpt`.** `get.py`
  shells out to `gh release download` to fetch the (private) assets.
  If you see "gh CLI not found" or "not authenticated," set it up:

  ```bash
  # macOS
  brew install gh
  # Debian/Ubuntu
  sudo apt install gh
  gh auth login   # follow the device-code prompt
  ```

External consumers without bit_sculpt access can't fetch the trace
bytes even though this sidecar is public — auth is required for the
actual release assets.

## What the script does

```
curl -fsSL .../get.py  →  python3 reads stdin
  → /dev/tty re-attach (for interactive curl|python3 flows)
  → `gh release list` to enumerate available tags
  → user picks a tag (interactive) or supplies one (non-interactive)
  → `gh release download` fetches assets (parallel, auth handled by gh)
  → SHA256SUMS verified post-download
  → `tar --zstd -xf` per (stream, layer) tarball → per-trace subdirs
  → `validate.py --quick` self-check
  → bundle ready at <dest>
```

`get.py` itself is ~380 lines of stdlib-only Python. Source at
[`tenstorrent/bit_sculpt:scripts/model_traces/get.py`](https://github.com/tenstorrent/bit_sculpt/blob/main/scripts/model_traces/get.py).

## Source of truth + sync

`get.py` here is auto-mirrored from
[`tenstorrent/bit_sculpt:scripts/model_traces/get.py`](https://github.com/tenstorrent/bit_sculpt/blob/main/scripts/model_traces/get.py)
via a GitHub Actions workflow
([`sync-get-py.yml`](https://github.com/tenstorrent/bit_sculpt/blob/main/.github/workflows/sync-get-py.yml))
on every push to bit_sculpt's `main`. **Don't edit `get.py` directly
here** — changes get overwritten on the next sync.

Bug reports / PRs / feature requests: open them on
[`tenstorrent/bit_sculpt`](https://github.com/tenstorrent/bit_sculpt).
The bit_sculpt-side helper
[`scripts/model_traces/sync_get_py.py`](https://github.com/tenstorrent/bit_sculpt/blob/main/scripts/model_traces/sync_get_py.py)
can also be invoked manually with `--push` to force a sync without
waiting for the next bit_sculpt merge.

#!/usr/bin/env python3
"""Consumer entry point for ``tenstorrent/bit_sculpt`` debug-trace releases.

Usage::

    # Interactive (browse model → pick traces → pick dest):
    curl -fsSL https://raw.githubusercontent.com/gwangTT/get-debug-traces/main/get.py \\
      | python3 -

    # List all model tags + summary:
    curl -fsSL .../get.py | python3 - list

    # Non-interactive download of every trace in a tag:
    curl -fsSL .../get.py | python3 - <tag> <dest>

This script is stdlib-only on the Python side, but **requires ``gh``
CLI** to be installed and authenticated against
``tenstorrent/bit_sculpt`` (private). Every Tenstorrent engineer
already has ``gh auth login`` set up; the script fails fast with a
clear message if not. ``gh`` handles all asset fetching + auth +
retries; this script orchestrates the picker UI, selective download,
extract, and ``validate.py --quick`` self-check.

Source of truth: ``scripts/model_traces/get.py`` in
``tenstorrent/bit_sculpt``. The sister script
``sync_get_py.py`` keeps the public sidecar
(``gwangTT/get-debug-traces``) byte-identical, so the ``curl`` URL
above always serves the bit_sculpt canonical version.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_RELEASE_REPO = "tenstorrent/bit_sculpt"


# ---------- TTY / TUI primitives ----------

def _ensure_tty() -> None:
    """Re-attach stdin to /dev/tty for `curl | python3 -` invocations."""
    if sys.stdin.isatty():
        return
    if os.path.exists("/dev/tty"):
        sys.stdin = open("/dev/tty", "r")
        return
    print(
        "No terminal available; pass <tag> <dest> for non-interactive use.",
        file=sys.stderr,
    )
    sys.exit(1)


def pick_one(prompt: str, options: list[tuple[str, str]]) -> int:
    """Numbered menu; returns chosen index. options = [(label, desc), ...]."""
    while True:
        print()
        for i, (label, desc) in enumerate(options, start=1):
            line = f"{i:>3}) {label}"
            if desc:
                line += f"   {desc}"
            print(line)
        raw = input(f"{prompt} ").strip()
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(options):
                return n - 1
        print(f"  → invalid (expected 1-{len(options)})")


def pick_many(prompt: str, options: list[tuple[str, str]]) -> list[int]:
    """Toggle-loop multi-select. Default all-on so Enter = take everything."""
    selected = [True] * len(options)
    while True:
        print()
        print(prompt)
        for i, ((label, desc), on) in enumerate(zip(options, selected), start=1):
            mark = "[✓]" if on else "[ ]"
            line = f"  {mark} {i:>2}) {label}"
            if desc:
                line += f"   {desc}"
            print(line)
        raw = input(
            "Toggle/action (number, 'a'=all, 'n'=none, Enter=confirm): "
        ).strip().lower()
        if raw == "":
            chosen = [i for i, on in enumerate(selected) if on]
            if not chosen:
                print("  → must select at least one")
                continue
            return chosen
        if raw == "a":
            selected = [True] * len(options)
            continue
        if raw == "n":
            selected = [False] * len(options)
            continue
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(options):
                selected[n - 1] = not selected[n - 1]
                continue
        print(f"  → invalid (number 1-{len(options)}, 'a', 'n', or Enter)")


def prompt_dest(default: str) -> str:
    raw = input(f"Destination [{default}]: ").strip()
    return os.path.expanduser(raw or default)


# ---------- gh CLI wrappers ----------

def _check_gh() -> None:
    """Fail fast if `gh` isn't installed or isn't authed for the repo."""
    if shutil.which("gh") is None:
        raise SystemExit(
            "gh CLI not found. Install from https://cli.github.com/ "
            "and run `gh auth login` (this script needs read access to "
            f"{_RELEASE_REPO})."
        )
    r = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True
    )
    if r.returncode != 0:
        raise SystemExit(
            "gh CLI is installed but not authenticated. "
            "Run `gh auth login` and retry."
        )
    r = subprocess.run(
        ["gh", "api", f"repos/{_RELEASE_REPO}"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise SystemExit(
            f"gh CLI can't reach {_RELEASE_REPO}. Check your auth scopes "
            f"(needs `repo` scope for private repo access)."
        )


def gh_release_list() -> list[dict]:
    r = subprocess.run(
        [
            "gh", "release", "list",
            "--repo", _RELEASE_REPO,
            "--limit", "100",
            "--json", "tagName,name,publishedAt,isDraft,isPrerelease",
        ],
        capture_output=True, text=True, check=True,
    )
    items = json.loads(r.stdout)
    return [x for x in items if not x.get("isDraft")]


def gh_release_view(tag: str) -> dict:
    r = subprocess.run(
        [
            "gh", "release", "view", tag,
            "--repo", _RELEASE_REPO,
            "--json", "tagName,name,publishedAt,assets,body",
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)


def gh_release_download(
    tag: str, dest: Path, patterns: list[str] | None = None
) -> None:
    """Fetch release assets via gh (auth + retries handled by gh)."""
    cmd = [
        "gh", "release", "download", tag,
        "--repo", _RELEASE_REPO,
        "--dir", str(dest),
        "--skip-existing",
    ]
    if patterns:
        for p in patterns:
            cmd += ["--pattern", p]
    subprocess.run(cmd, check=True)


def fetch_manifest(tag: str) -> dict:
    """gh-download manifest.json into a temp dir and parse it."""
    with tempfile.TemporaryDirectory(prefix="get-manifest-") as td:
        td_path = Path(td)
        gh_release_download(tag, td_path, patterns=["manifest.json"])
        return json.loads((td_path / "manifest.json").read_text())


# ---------- Download + extract ----------

def _have_tar_zstd() -> bool:
    try:
        r = subprocess.run(
            ["tar", "--help"], capture_output=True, text=True, timeout=10
        )
        return "--zstd" in (r.stdout + r.stderr)
    except Exception:
        return False


def _extract_tarball(tarball: Path, dest: Path, use_native_zstd: bool) -> None:
    if use_native_zstd:
        subprocess.run(
            ["tar", "--zstd", "-xf", str(tarball), "-C", str(dest)],
            check=True,
        )
        return
    with subprocess.Popen(
        ["zstd", "-dc", str(tarball)], stdout=subprocess.PIPE
    ) as zproc:
        subprocess.run(
            ["tar", "-xf", "-", "-C", str(dest)],
            stdin=zproc.stdout, check=True,
        )
        zproc.stdout.close()  # type: ignore[union-attr]
        if zproc.wait() != 0:
            raise RuntimeError(f"zstd -dc failed on {tarball}")


def _verify_sha256sums(root: Path, sums_path: Path | None = None, *, label: str = "") -> int:
    """Verify files under `root` against their SHA256SUMS lines.

    Lines whose file is absent under `root` are skipped (the
    `sha256sum -c --ignore-missing` equivalent), so this works in BOTH
    contexts against the single dual-namespace SHA256SUMS:
      - pre-extract over the staging dir: the asset-namespace lines
        (`<tag>--*.tar.zst`, flat files) resolve; the extracted-namespace
        lines (`<tag>/...`) are absent → skipped.
      - post-extract over the dest bundle: the extracted-namespace lines
        (`<tag>/ffn_context/...`, `<tag>/kv_cache_layer_*.safetensors`, …)
        resolve; the tarball-namespace lines are gone → skipped.

    `sums_path` defaults to `root/SHA256SUMS`. Returns the number of files
    actually verified (non-skipped). Raises on any content mismatch.
    """
    import hashlib
    sums_file = sums_path if sums_path is not None else (root / "SHA256SUMS")
    if not sums_file.is_file():
        print("[get] WARN: SHA256SUMS not present — skipping verify")
        return 0
    failed = []
    checked = 0
    for line in sums_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        sha, name = line.split(maxsplit=1)
        target = root / name
        if not target.is_file():
            continue  # absent in this context (selective download / namespace) — ok
        h = hashlib.sha256()
        with open(target, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != sha:
            failed.append(name)
        else:
            checked += 1
    if failed:
        raise RuntimeError(
            f"sha256 mismatch on {len(failed)} files: "
            f"{failed[:3]}{' ...' if len(failed) > 3 else ''}"
        )
    tag = f" ({label})" if label else ""
    print(f"[get] SHA256SUMS verified{tag}: {checked} file(s)")
    return checked


def download_bundle(
    tag: str, trace_tags: list[str] | None, dest: Path
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"get-{tag.replace('/', '_')}-"))
    print(f"[get] staging: {staging}")

    # Always fetch release-level + manifest + SHA256SUMS. Trace-specific
    # assets are filtered via --pattern <tag>--*.
    patterns: list[str] | None
    if trace_tags is None:
        patterns = None  # all
    else:
        patterns = ["manifest.json", "SHA256SUMS", "REPORT.md",
                    "debug_trace_io.py", "validate.py"]
        for tt in trace_tags:
            patterns.append(f"{tt}--*")

    print(f"[get] gh release download {tag} (patterns: {patterns or 'all'}) ...")
    gh_release_download(tag, staging, patterns=patterns)

    # Pre-extract: verify the downloaded assets (tarball + flat namespace).
    _verify_sha256sums(staging, label="pre-extract assets")

    use_native = _have_tar_zstd()
    print(f"[get] extracting tarballs (tar --zstd: {use_native}) ...")
    for tarball in sorted(staging.glob("*.tar.zst")):
        _extract_tarball(tarball, dest, use_native_zstd=use_native)

    print(f"[get] placing flat per-trace + release-level files ...")
    for src in sorted(staging.iterdir()):
        if not src.is_file():
            continue
        name = src.name
        if name.endswith(".tar.zst"):
            continue
        if "--" in name:
            trace_tag, flat = name.split("--", 1)
            target = dest / trace_tag / flat
        else:
            target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))

    # Post-extract: verify the extracted .safetensors against the
    # extracted-namespace lines in SHA256SUMS (manifest v3 extracted_files).
    # SHA256SUMS was moved into dest by the flat-file loop above. This
    # realizes the per-extracted-file integrity guarantee in the default
    # consumer flow (not just a manual `sha256sum -c`). On a v2 bundle
    # (no extracted-namespace lines) this verifies 0 extra files — harmless.
    sums_in_dest = dest / "SHA256SUMS"
    if sums_in_dest.is_file():
        _verify_sha256sums(dest, sums_in_dest, label="post-extract .safetensors")

    validate = dest / "validate.py"
    if validate.is_file():
        print(f"[get] running validate.py --quick ...")
        subprocess.run(
            [sys.executable, str(validate), str(dest), "--quick"],
            check=True,
        )
    else:
        print(f"[get] (validate.py absent — skipping self-check)")

    shutil.rmtree(staging, ignore_errors=True)
    print(f"[get] bundle ready at: {dest}")


# ---------- Subcommands ----------

def _release_summary(r: dict) -> str:
    date = (r.get("publishedAt") or "")[:10]
    name = r.get("name") or ""
    return f"updated {date}" + (f"  — {name}" if name else "")


def cmd_list() -> int:
    _check_gh()
    releases = gh_release_list()
    if not releases:
        print("(no releases)")
        return 0
    print()
    for r in releases:
        tag = r["tagName"]
        print(f"  {tag:<48}  {_release_summary(r)}")
    return 0


def _trace_summary(t: dict) -> str:
    prompt = (t.get("prompt") or "").replace("\n", " ").strip()
    if len(prompt) > 40:
        prompt = prompt[:37] + "..."
    n_tok = t.get("n_prompt_tokens", "?")
    decode = t.get("decode_steps", "?")
    n_layers = t.get("n_layers", "?")
    return f"{prompt!r:<44}  prompt+decode={n_tok}+{decode}  n_layers={n_layers}"


def _interactive_trace_picker(tag: str) -> int:
    print(f"[get] fetching manifest for {tag} ...")
    manifest = fetch_manifest(tag)
    traces = manifest.get("traces", [])
    if not traces:
        print("(no traces in this release)")
        return 1
    trace_tags = [t["tag"] for t in traces]
    options = [(t["tag"], _trace_summary(t)) for t in traces]
    if len(options) == 1:
        chosen_indices = [0]
        print(f"\n(only one trace: {options[0][0]})")
    else:
        chosen_indices = pick_many("Select traces:", options)
    chosen = [trace_tags[i] for i in chosen_indices]
    default = f"~/{tag.replace('/', '_')}"
    dest = prompt_dest(default)
    download_bundle(tag, trace_tags=chosen, dest=Path(dest))
    return 0


def cmd_interactive() -> int:
    _check_gh()
    _ensure_tty()
    releases = gh_release_list()
    if not releases:
        print("(no releases available)")
        return 1
    options: list[tuple[str, str]] = [
        (r["tagName"], _release_summary(r)) for r in releases
    ]
    options.append(("Quit", ""))
    idx = pick_one("Pick a release tag:", options)
    if idx == len(options) - 1:
        return 0
    return _interactive_trace_picker(releases[idx]["tagName"])


def cmd_with_tag(tag: str, dest: str | None) -> int:
    _check_gh()
    # Verify tag exists (gh release view will error otherwise).
    try:
        gh_release_view(tag)
    except subprocess.CalledProcessError:
        raise SystemExit(
            f"tag {tag!r} not found in {_RELEASE_REPO}. "
            f"Run `gh release list --repo {_RELEASE_REPO}` to see all tags."
        )
    if dest is None:
        _ensure_tty()
        return _interactive_trace_picker(tag)
    download_bundle(tag, trace_tags=None, dest=Path(os.path.expanduser(dest)))
    return 0


# ---------- Entry ----------

def main() -> int:
    p = argparse.ArgumentParser(
        prog="get.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "tag",
        nargs="?",
        help="release tag (e.g. zai-org/glm51/vllm-ef537f25-8193tok), "
        "or 'list'. Omit for full interactive TUI.",
    )
    p.add_argument(
        "dest",
        nargs="?",
        help="destination dir. Omit for interactive trace picker + dest prompt.",
    )
    args = p.parse_args()

    if args.tag is None:
        return cmd_interactive()
    if args.tag == "list":
        return cmd_list()
    return cmd_with_tag(args.tag, args.dest)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[get] interrupted", file=sys.stderr)
        sys.exit(130)
    except subprocess.CalledProcessError as e:
        print(f"[get] subprocess failed: {e}", file=sys.stderr)
        sys.exit(e.returncode or 1)
    except Exception as e:
        print(f"[get] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

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
extract, post-extract SHA verify, and a FULL ``validate.py`` self-check
whose reader-contract gates open every downloaded trace with the
bundle's shipped ``debug_trace_io`` reader (the consumer-side test of
that reader against what was downloaded).

Source of truth: ``scripts/model_traces/get.py`` in
``tenstorrent/bit_sculpt``. The sister script
``sync_get_py.py`` keeps the public sidecar
(``gwangTT/get-debug-traces``) byte-identical, so the ``curl`` URL
above always serves the bit_sculpt canonical version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_RELEASE_REPO = "tenstorrent/bit_sculpt"
_MANIFEST_NAME = "manifest.json"
_SHA256SUMS_NAME = "SHA256SUMS"

# Set from --verbose in main(); gates the extra per-file/per-component logging.
_VERBOSE = False


def _vprint(msg: str) -> None:
    if _VERBOSE:
        print(msg)


# ---------- TTY / TUI primitives ----------


def _ensure_tty() -> None:
    """Re-attach stdin to /dev/tty for `curl | python3 -` invocations."""
    if sys.stdin.isatty():
        return
    if os.path.exists("/dev/tty"):
        sys.stdin = open("/dev/tty")
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
        raw = input("Toggle/action (number, 'a'=all, 'n'=none, Enter=confirm): ").strip().lower()
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
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            "gh CLI is installed but not authenticated. Run `gh auth login` and retry."
        )
    r = subprocess.run(["gh", "api", f"repos/{_RELEASE_REPO}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            f"gh CLI can't reach {_RELEASE_REPO}. Check your auth scopes "
            f"(needs `repo` scope for private repo access)."
        )


def gh_release_list() -> list[dict]:
    r = subprocess.run(
        [
            "gh",
            "release",
            "list",
            "--repo",
            _RELEASE_REPO,
            "--limit",
            "100",
            "--json",
            "tagName,name,publishedAt,isDraft,isPrerelease",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    items = json.loads(r.stdout)
    return [x for x in items if not x.get("isDraft")]


def gh_release_view(tag: str) -> dict:
    r = subprocess.run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            _RELEASE_REPO,
            "--json",
            "tagName,name,publishedAt,assets,body",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(r.stdout)


def gh_release_download(tag: str, dest: Path, patterns: list[str] | None = None) -> None:
    """Fetch release assets via gh (auth + retries handled by gh).

    `--skip-existing` is deliberately NOT used: it was added in a recent gh
    and is absent on older clients (e.g. gh 2.4.0), so it breaks consumers.
    Every caller downloads into a freshly-created empty dir, so gh never hits
    an existing file (the condition --skip-existing / --clobber would guard).
    """
    cmd = [
        "gh",
        "release",
        "download",
        tag,
        "--repo",
        _RELEASE_REPO,
        "--dir",
        str(dest),
    ]
    if patterns:
        for p in patterns:
            cmd += ["--pattern", p]
    subprocess.run(cmd, check=True)


def _fetch_release_metadata(tag: str, dest: Path) -> tuple[dict, bytes, bytes]:
    """gh-download manifest.json + SHA256SUMS into a probe dir under `dest`,
    returning (manifest dict, manifest.json bytes, SHA256SUMS bytes).

    These two tiny files describe the whole release, so we fetch them first —
    cheaply — to (a) enumerate every asset + its sha for a selective per-asset
    download and (b) detect which files `dest` already holds so the heavy asset
    download can be skipped. The raw bytes are returned so the caller can persist
    a BYTE-IDENTICAL index to `dest` (the released SHA256SUMS has a line covering
    manifest.json, so a re-serialized copy would fail the post-extract verify).

    Contract: get.py must confine all filesystem activity to `dest` (no /tmp
    side effects), so the probe lives in a hidden subdir of dest that is
    created fresh and removed once parsed.
    """
    dest.mkdir(parents=True, exist_ok=True)
    probe = dest / ".get_metadata_probe"
    if probe.exists():
        shutil.rmtree(probe)
    probe.mkdir()
    try:
        gh_release_download(tag, probe, patterns=[_MANIFEST_NAME, _SHA256SUMS_NAME])
        manifest_bytes = (probe / _MANIFEST_NAME).read_bytes()
        sums_bytes = (probe / _SHA256SUMS_NAME).read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        return manifest, manifest_bytes, sums_bytes
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def fetch_manifest(tag: str, dest: Path) -> dict:
    """Just the manifest (for the interactive picker)."""
    return _fetch_release_metadata(tag, dest)[0]


# ---------- Download + extract ----------


def _python_has_torch() -> bool:
    """Whether the interpreter that runs validate.py can import torch.

    validate.py's reader-contract gates open traces via debug_trace_io, which
    imports torch. get.py and validate.py run under the same interpreter
    (sys.executable), so checking here is accurate. find_spec only checks
    installability — it does not pay the (slow) cost of importing torch.
    """
    try:
        import importlib.util

        return importlib.util.find_spec("torch") is not None
    except Exception:
        return False


def _have_tar_zstd() -> bool:
    try:
        r = subprocess.run(["tar", "--help"], capture_output=True, text=True, timeout=10)
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
    with subprocess.Popen(["zstd", "-dc", str(tarball)], stdout=subprocess.PIPE) as zproc:
        subprocess.run(
            ["tar", "-xf", "-", "-C", str(dest)],
            stdin=zproc.stdout,
            check=True,
        )
        zproc.stdout.close()  # type: ignore[union-attr]
        if zproc.wait() != 0:
            raise RuntimeError(f"zstd -dc failed on {tarball}")


# ---------- Per-asset reuse: hashing, path resolution, fetch planning ----------
#
# A release bundle carries TWO on-disk namespaces (see write_release_manifest.py):
#   - assets        : the downloadable files (tarballs + flat files), keyed by
#                     asset name, each with its own sha256 (manifest["assets"]).
#   - extracted_files: every .safetensors AS IT LANDS post-extract, keyed by its
#                     dest-relative path "<tag>/<rel>" with sha256 (v3 only).
# Per-asset reuse hashes what's already in `dest` against these structured hashes
# and downloads only the assets whose post-conditions aren't already satisfied.
# SHA256SUMS stays the flat-text mirror used by the final _verify_sha256sums gate.


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _flat_asset_relpath(name: str, trace_tag: str | None) -> str:
    """Dest-relative path a FLAT (non-tarball) asset lands at after placement.

    Release-level flat files (REPORT.md, debug_trace_io.py, validate.py) move
    verbatim to ``<name>``; per-trace flat files (``<tag>--<rest>``, e.g.
    ``<tag>--kv_cache_layer_0.safetensors``, index.json, metadata.json) move to
    ``<tag>/<rest>``.
    """
    if trace_tag is None:
        return name
    return f"{trace_tag}/{name[len(trace_tag) + 2 :]}"  # strip "<tag>--"


# Per-(stream, layer) tarballs extract into a NESTED family subdir the tarball
# stem alone doesn't encode. This MIRRORS validate.py's resolver (_NESTED_FAMILY
# / _NESTED_PREFIX) and the release skill's tar layout — keep the three in sync
# when a model adds a new nested stream. Whole-family tarballs (topk, routing,
# dsa, …) fall through to the generic ``<tag>/<stem>`` form, so a new flat family
# needs no edit here. Mis-resolution only ever costs a redundant re-fetch (the
# attribution check in _plan_fetch falls back to per-tag), never a wrong skip.
_NESTED_FAMILY = {"decoder_input_layer_0": "decoder_io"}
_NESTED_PREFIX = (
    ("decoder_output_layer_", "decoder_io"),
    ("post_attn_residual_layer_", "ffn_context"),
    ("gate_logits_layer_", "ffn_context"),
    ("gate_scores_layer_", "ffn_context"),
)


def _tarball_extract_prefix(name: str, trace_tag: str | None) -> str:
    """Dest-relative DIR a tarball asset extracts into (no trailing slash).

    release-level ``<stem>.tar.zst``    -> ``<stem>``
    per-trace ``<tag>--<stem>.tar.zst`` -> ``<tag>/<family>/<stem>`` (nested
                                           streams) or ``<tag>/<stem>``.
    """
    stem_full = name[: -len(".tar.zst")]
    if trace_tag is None:
        return stem_full
    stem = stem_full[len(trace_tag) + 2 :]  # strip "<tag>--"
    family = _NESTED_FAMILY.get(stem)
    if family is None:
        for pfx, fam in _NESTED_PREFIX:
            if stem.startswith(pfx):
                family = fam
                break
    return f"{trace_tag}/{family}/{stem}" if family else f"{trace_tag}/{stem}"


def _owned_paths(manifest: dict, scope_tags: set[str] | None = None) -> tuple[set[str], set[str]]:
    """``(flat/extracted dest-relpaths, release-level-tarball stems)`` a v3
    manifest owns, optionally restricted to `scope_tags`.

    Used for cross-release orphan cleanup: a path a PRIOR release owned but the
    new one does not is safe to delete. Files unknown to both manifests (user
    data, unregistered tarball contents) are in neither set, so never deleted.
    `scope_tags` (None = all) confines per-trace paths to the tags the user
    actually requested, so a selective download never deletes an unrelated trace
    the user fetched earlier. Release-level paths/stems are always included.
    """

    def keep(tag: str | None) -> bool:
        return tag is None or scope_tags is None or tag in scope_tags

    files: set[str] = {_MANIFEST_NAME, _SHA256SUMS_NAME}
    stems: set[str] = set()
    for a in manifest.get("assets", []):
        name, tag = a["name"], a.get("trace_tag")
        if name.endswith(".tar.zst"):
            if tag is None:
                stems.add(name[: -len(".tar.zst")])
            # per-trace tarball contents are .safetensors -> covered via extracted_files
        elif keep(tag):
            files.add(_flat_asset_relpath(name, tag))
    for e in manifest.get("extracted_files", []):
        path = e.get("path")
        if path and keep(e.get("trace_tag") or path.split("/", 1)[0]):
            files.add(path)
    return files, stems


def _plan_fetch(
    dest: Path,
    manifest: dict,
    wanted_tags: set[str] | None,
    *,
    refetch_release_tarballs: bool = False,
) -> tuple[list[str], set[str]] | None:
    """Decide the minimal asset set to download (v3 only).

    Returns ``(patterns, verified)`` where ``patterns`` are EXACT asset names to
    fetch and ``verified`` are dest-relpaths confirmed byte-correct against the
    manifest this pass (so the post-extract verify can skip re-hashing them).
    Returns ``None`` for non-v3 manifests (caller fetches everything — graceful
    degradation per the get.py distribution contract).

    Scope: release-level assets (``trace_tag`` None) always; per-trace assets
    only for tags in ``wanted_tags`` (None = all). A FLAT asset is satisfied iff
    its dest file hashes to the manifest sha. A per-trace TARBALL is satisfied
    iff every extracted_files entry it produces is present + hash-correct; a
    tarball with NO extracted_files evidence (unattributed) can't be confirmed,
    so it is always fetched. When a tag's attributed tarballs don't cleanly
    partition its extracted files (convention drift), the attributed set falls
    back to one all-or-nothing group. Release-level tarballs (contents not in the
    registry) use an existence check, except on a cross-release reconcile
    (`refetch_release_tarballs`) where their bytes can't be confirmed and they
    are re-fetched.
    """
    if manifest.get("format_version") != 3:
        return None

    extracted = {
        e["path"]: e["sha256"] for e in manifest.get("extracted_files", []) if e.get("path")
    }
    assets = manifest.get("assets", [])

    def in_scope(tag: str | None) -> bool:
        return tag is None or wanted_tags is None or tag in wanted_tags

    # Flat .safetensors also appear in extracted_files (writer priority (a)) —
    # exclude them from tarball attribution so a present flat file isn't counted
    # toward a tarball's completeness.
    flat_extracted = {
        _flat_asset_relpath(a["name"], a["trace_tag"])
        for a in assets
        if a.get("trace_tag") and a["name"].endswith(".safetensors")
    }

    patterns: list[str] = []
    verified: set[str] = set()

    def group_ok(paths: set[str]) -> bool:
        """All paths present in dest AND hash-match? Records matches in `verified`
        only on full success (a partial group is re-fetched whole)."""
        matched: list[str] = []
        for p in sorted(paths):
            fp = dest / p
            if fp.is_file() and _sha256_file(fp) == extracted.get(p):
                matched.append(p)
            else:
                return False
        verified.update(matched)
        return True

    # --- Flat assets: per-asset decision (the main per-file reuse win) ---
    for a in assets:
        name, tag = a["name"], a.get("trace_tag")
        if not in_scope(tag) or name.endswith(".tar.zst"):
            continue
        rel = _flat_asset_relpath(name, tag)
        fp = dest / rel
        if fp.is_file() and _sha256_file(fp) == a["sha256"]:
            verified.add(rel)
        else:
            patterns.append(name)

    # --- Release-level tarballs: existence-only, unless a cross-release reconcile
    #     (their contents aren't in the registry, so changed bytes under the same
    #     stem can't be detected — re-fetch to be safe). ---
    for a in assets:
        name, tag = a["name"], a.get("trace_tag")
        if tag is not None or not name.endswith(".tar.zst"):
            continue
        d = dest / name[: -len(".tar.zst")]
        if refetch_release_tarballs or not (d.is_dir() and any(d.iterdir())):
            patterns.append(name)

    # --- Per-trace tarballs: per-(stream, layer) when attribution is clean ---
    tarball_tags = sorted(
        {
            a["trace_tag"]
            for a in assets
            if a.get("trace_tag") and a["name"].endswith(".tar.zst") and in_scope(a["trace_tag"])
        }
    )
    for tag in tarball_tags:
        tballs = [
            a["name"]
            for a in assets
            if a.get("trace_tag") == tag and a["name"].endswith(".tar.zst")
        ]
        attributed = {
            n: {
                p
                for p in extracted
                if p == _tarball_extract_prefix(n, tag)
                or p.startswith(_tarball_extract_prefix(n, tag) + "/")
            }
            for n in tballs
        }
        # A tarball with no extracted_files evidence can't be confirmed present →
        # always fetch it. Decide the rest from the attributed tarballs only.
        patterns.extend(n for n in tballs if not attributed[n])
        attr = [n for n in tballs if attributed[n]]
        union: set[str] = set().union(*(attributed[n] for n in attr)) if attr else set()
        tag_tarball_files = {p for p in extracted if p.split("/", 1)[0] == tag} - flat_extracted
        if union == tag_tarball_files:
            for n in attr:  # clean partition -> per-tarball
                if not group_ok(attributed[n]):
                    patterns.append(n)
        elif not group_ok(tag_tarball_files):  # drift -> all-or-nothing per tag
            patterns.extend(attr)

    return list(dict.fromkeys(patterns)), verified


def _read_prior_manifest(dest: Path) -> dict | None:
    """Parse the release currently in `dest` (if any), for cross-release cleanup.

    Returns None only when there is no readable manifest object. A present file
    that isn't a JSON object (corrupt scalar/array, truncated) yields None too,
    so the caller's present-but-unreadable refuse-to-mix guard fires instead of a
    later AttributeError on `.get(...)`.
    """
    p = dest / _MANIFEST_NAME
    if not p.is_file():
        return None
    try:
        parsed = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically (temp file in the same dir + os.replace),
    so a crash mid-write never leaves a half-written index that a later run would
    mistake for a corrupt prior bundle."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _orphan_cleanup(dest: Path, prior: dict, new: dict, scope_tags: set[str] | None = None) -> None:
    """Delete files/dirs the PRIOR release owned that the new one doesn't, then
    prune the dirs THIS cleanup emptied.

    Only paths the prior manifest explicitly owned (within `scope_tags`) are
    removed, so user files, unregistered content, and out-of-scope traces the
    user fetched earlier are never touched. Symlinks are skipped and every target
    is confirmed inside `dest`, and a problematic entry can't abort the run.
    """
    prior_files, prior_stems = _owned_paths(prior, scope_tags)
    new_files, new_stems = _owned_paths(new, scope_tags)
    dest_resolved = dest.resolve()
    removed = 0
    touched: set[Path] = set()

    def _within(p: Path) -> bool:
        try:
            p.resolve().relative_to(dest_resolved)
            return True
        except (ValueError, OSError):
            return False

    for rel in sorted(prior_files - new_files):
        fp = dest / rel
        if fp.is_file() and not fp.is_symlink() and _within(fp):
            try:
                fp.unlink()
            except OSError:
                continue
            removed += 1
            touched.add(fp.parent)
            _vprint(f"[get]   - removed stale {rel}")
    for stem in sorted(prior_stems - new_stems):
        d = dest / stem
        if d.is_dir() and not d.is_symlink() and _within(d):
            try:
                shutil.rmtree(d)
            except OSError:
                continue
            removed += 1
            touched.add(d.parent)
            _vprint(f"[get]   - removed stale dir {stem}/")
    # Prune ONLY the dirs this cleanup emptied (each touched parent + its
    # ancestors up to, but not including, dest) — never blanket-walk the tree, so
    # the user's own empty dirs are left alone.
    for start in sorted(touched, key=lambda p: len(p.parts), reverse=True):
        d = start
        while d != dest and d.is_relative_to(dest):
            try:
                if any(d.iterdir()):
                    break
                d.rmdir()
            except OSError:
                break
            d = d.parent
    if removed:
        print(f"[get] reconciled prior release: removed {removed} stale file(s)/dir(s)")


def _verify_sha256sums(
    root: Path,
    sums_path: Path | None = None,
    *,
    label: str = "",
    skip: set[str] | None = None,
    scope_tags: set[str] | None = None,
    known_tags: set[str] | None = None,
) -> int:
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

    `skip` names (dest-relative paths) already hash-verified during fetch
    planning are not re-hashed — each file is hashed at most once per run. When
    `scope_tags` is set (a selective download), per-trace lines whose head
    segment is a `known_tags` trace NOT in `scope_tags` are skipped — so a
    selective download doesn't fail on an out-of-scope trace the user fetched
    from a different release (mirrors validate.py's --downloaded-traces scoping).
    `sums_path` defaults to `root/SHA256SUMS`. Returns the number of files
    actually verified (non-skipped). Raises on any content mismatch.
    """
    sums_file = sums_path if sums_path is not None else (root / _SHA256SUMS_NAME)
    if not sums_file.is_file():
        print("[get] WARN: SHA256SUMS not present — skipping verify")
        return 0
    skip = skip or set()
    known_tags = known_tags or set()
    failed = []
    checked = 0
    reused = 0
    for line in sums_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        sha, name = line.split(maxsplit=1)
        name = name.lstrip("*")  # BSD-style checksum marker parity with validate.py
        if name in skip:
            reused += 1
            continue
        if scope_tags is not None:
            head = name.split("/", 1)[0]
            if head in known_tags and head not in scope_tags:
                continue  # out-of-scope per-trace line (selective download)
        target = root / name
        if not target.is_file():
            continue  # absent in this context (selective download / namespace) — ok
        actual = _sha256_file(target)
        if actual != sha:
            failed.append(name)
            _vprint(f"[get]   ✗ {name}  (expected {sha[:12]}, got {actual[:12]})")
        else:
            checked += 1
            _vprint(f"[get]   ✓ {name}  ({sha[:12]})")
    if failed:
        raise RuntimeError(
            f"sha256 mismatch on {len(failed)} file(s): "
            f"{failed[:3]}{' ...' if len(failed) > 3 else ''}. "
            f"These files are corrupt — delete them (or the whole destination "
            f"dir) and re-run get.py to re-download."
        )
    tag = f" ({label})" if label else ""
    extra = f", {reused} reused from prior run" if reused else ""
    print(f"[get] SHA256SUMS verified{tag}: {checked} file(s){extra}")
    return checked


def download_bundle(tag: str, trace_tags: list[str] | None, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)

    # Never let a stale staging dir from an interrupted prior run survive into
    # this one (even the skip path) — clean it unconditionally up front.
    staging = dest / ".get_staging"
    if staging.exists():
        shutil.rmtree(staging)

    # Fetch the two tiny index files first (manifest.json + SHA256SUMS), as exact
    # bytes. They carry every asset + its sha, so we hash whatever is ALREADY in
    # dest against them and fetch ONLY the assets whose bytes aren't present. We
    # do NOT rely on `gh release download --skip-existing` (absent on older gh):
    # we fetch the missing assets into a FRESH staging dir, so gh never meets a
    # pre-existing file.
    manifest, manifest_bytes, sums_bytes = _fetch_release_metadata(tag, dest)

    # Capture the release CURRENTLY in dest (if any) BEFORE we overwrite its
    # index, so we can reconcile across releases below.
    prior_manifest = _read_prior_manifest(dest)
    fmt = manifest.get("format_version")
    prior_fmt = prior_manifest.get("format_version") if prior_manifest else None
    prior_commit = prior_manifest.get("release_commit") if prior_manifest else None
    new_commit = manifest.get("release_commit")

    # A present-but-unparseable index means a corrupt/partial prior bundle: we
    # can't enumerate what it owns to reconcile it, so refuse to mix rather than
    # extract on top (the index is written atomically below, so this never fires
    # on our own interrupted write). Empty dirs (no index) fall through.
    if prior_manifest is None and (dest / _MANIFEST_NAME).is_file():
        raise SystemExit(
            f"{dest} holds an unreadable {_MANIFEST_NAME} (corrupt or partial prior "
            f"download). Delete the directory (or use a fresh one) and re-run."
        )

    # Cross-release policy (decided up front, before any download):
    #   - both v3: per-asset content-hash reuse + orphan cleanup is safe, so
    #     reuse byte-identical files from the prior release and reconcile after.
    #   - any pre-v3 bundle involved: no per-file registry to enumerate the prior
    #     release's extracted files, so refuse to mix (original guard) rather than
    #     risk leaving its stale files behind.
    cross_release = (
        prior_manifest is not None
        and fmt == 3
        and prior_fmt == 3
        and bool(prior_commit)
        and bool(new_commit)
        and prior_commit != new_commit
    )
    do_orphan_cleanup = False
    if prior_manifest is not None and prior_commit and new_commit and prior_commit != new_commit:
        if fmt == 3 and prior_fmt == 3:
            do_orphan_cleanup = True
        else:
            raise SystemExit(
                f"{dest} already holds a different release (commit {prior_commit[:12]}; "
                f"you requested {new_commit[:12]}) that can't be safely reconciled "
                f"(pre-v3 bundle, no per-file registry). Delete the directory (or use "
                f"a fresh one) and re-run."
            )
    elif fmt == 3 and prior_fmt == 3:
        # Same release re-run: orphan cleanup is a no-op but harmless, and prunes
        # leftovers from an interrupted prior run of this same release.
        do_orphan_cleanup = prior_manifest is not None

    # Persist the authoritative index now (exact released bytes, atomically), so
    # dest always holds manifest.json + SHA256SUMS even when every other asset is
    # skipped, and the pre/post-extract verify can read dest/SHA256SUMS. Done
    # AFTER capturing prior_manifest above.
    _atomic_write_bytes(dest / _MANIFEST_NAME, manifest_bytes)
    _atomic_write_bytes(dest / _SHA256SUMS_NAME, sums_bytes)

    known_tags = {t["tag"] for t in manifest.get("traces", []) if "tag" in t}
    wanted_tags = None if trace_tags is None else (set(trace_tags) & known_tags)

    # On a cross-release reconcile, release-level tarball bytes can't be confirmed
    # from the per-file registry, so force a re-fetch (a same-stem dir may hold the
    # prior release's contents).
    plan = _plan_fetch(dest, manifest, wanted_tags, refetch_release_tarballs=cross_release)
    if plan is None:
        # Pre-v3 fallback (no extracted_files registry): no per-asset reuse —
        # fetch every in-scope asset. Exact release-level names + per-tag globs.
        patterns = [
            _MANIFEST_NAME,
            _SHA256SUMS_NAME,
            "REPORT.md",
            "debug_trace_io.py",
            "validate.py",
        ]
        patterns += [a["name"] for a in manifest.get("assets", []) if a.get("trace_tag") is None]
        patterns += [f"{t}--*" for t in sorted(known_tags if wanted_tags is None else wanted_tags)]
        patterns = list(dict.fromkeys(patterns))
        verified: set[str] = set()
    else:
        patterns, verified = plan

    if not patterns:
        print(
            f"[get] release already present in {dest} — nothing to download "
            f"({len(verified)} file(s) verified against the manifest)"
        )
    else:
        if verified:
            print(
                f"[get] reusing {len(verified)} already-present file(s); "
                f"fetching {len(patterns)} asset(s)"
            )
        else:
            print(f"[get] fetching {len(patterns)} asset(s)")
        # Contract: confine ALL activity to dest — stage inside it, never /tmp.
        # Fresh staging => gh never sees a pre-existing file (no --skip-existing).
        staging.mkdir(parents=True)
        print(f"[get] staging: {staging}")
        _vprint(f"[get] gh release download {tag} (patterns: {patterns}) ...")
        gh_release_download(tag, staging, patterns=patterns)

        # Pre-extract: verify the freshly-downloaded assets against the
        # asset-namespace lines of SHA256SUMS (the authoritative dest copy).
        _verify_sha256sums(staging, sums_path=dest / _SHA256SUMS_NAME, label="pre-extract assets")

        use_native = _have_tar_zstd()
        print(f"[get] extracting tarballs (tar --zstd: {use_native}) ...")
        for tarball in sorted(staging.glob("*.tar.zst")):
            if "--" in tarball.name:
                # Per-trace tarball -> dest/<tag>/... . A partial extract leaves
                # missing extracted_files rows, so the tarball is detected
                # incomplete next run and re-fetched; direct extraction is safe.
                _extract_tarball(tarball, dest, use_native_zstd=use_native)
            else:
                # Release-level tarball (e.g. diversity_plots): its files aren't
                # in the extracted_files registry, so a PARTIAL dir would be
                # wrongly trusted as complete on a later run. Extract atomically
                # into a temp dir, then swap the finished dir into place so dest
                # never holds a half-extracted release dir.
                stem = tarball.name[: -len(".tar.zst")]
                tmp = staging / f".extract_{stem}"
                tmp.mkdir(parents=True, exist_ok=True)
                _extract_tarball(tarball, tmp, use_native_zstd=use_native)
                final = dest / stem
                if final.exists():
                    shutil.rmtree(final)
                shutil.move(str(tmp / stem), str(final))

        print("[get] placing flat per-trace + release-level files ...")
        for src in sorted(staging.iterdir()):
            if not src.is_file() or src.name.endswith(".tar.zst"):
                continue
            if "--" in src.name:
                trace_tag, flat = src.name.split("--", 1)
                target = dest / trace_tag / flat
            else:
                target = dest / src.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))

        # Staging fully consumed (tarballs extracted, flat files moved). Remove
        # it so the orphan cleanup + verify + validate.py see a clean dest.
        shutil.rmtree(staging, ignore_errors=True)

    # Cross-release reconcile (v3↔v3 only): remove files the PRIOR release owned
    # but this one doesn't, so dest holds exactly one release with no stale
    # leftovers. Scoped to wanted_tags so a selective download never deletes an
    # out-of-scope trace the user fetched earlier. Replaces the old refuse-to-mix
    # guard for the v3 path.
    if do_orphan_cleanup:
        _orphan_cleanup(dest, prior_manifest, manifest, scope_tags=wanted_tags)

    # Post-extract: verify the extracted .safetensors against the
    # extracted-namespace lines in SHA256SUMS (manifest v3 extracted_files),
    # skipping any file already hash-confirmed during fetch planning so each file
    # is hashed at most once. Scoped to wanted_tags so a selective download
    # doesn't fail on an out-of-scope trace the user holds from another release.
    # On a v2 bundle (no extracted-namespace lines) this verifies 0 extra files.
    _verify_sha256sums(
        dest,
        dest / _SHA256SUMS_NAME,
        label="post-extract .safetensors",
        skip=verified,
        scope_tags=wanted_tags,
        known_tags=known_tags,
    )

    # Full validate.py (NOT --quick): the reader-contract gates (8-14) open
    # each downloaded trace with the bundle's shipped debug_trace_io
    # ChunkedTraceReader and check per-layer shapes / dtypes / KV layout /
    # alias rule / routing / top-K. This is the consumer-side test of the
    # SHIPPED reader against what was actually downloaded — the whole point
    # of bundling debug_trace_io.py. (--quick would skip exactly these gates,
    # leaving the reader shipped-but-never-exercised.) validate.py skips the
    # reader gates for any trace dir absent from a selective download.
    validate = dest / "validate.py"
    if not validate.is_file():
        print("[get] (validate.py absent — skipping self-check)")
    elif not _python_has_torch():
        # validate.py's reader-contract gates open traces with debug_trace_io,
        # which imports torch. A consumer who only wants the trace files need
        # not have torch installed — and the download is already integrity-
        # verified above via SHA256SUMS (the release contract's core
        # guarantee). So skip the reader self-check rather than hard-fail.
        print(
            "[get] torch not installed — skipping the validate.py reader "
            "self-check (download integrity already verified via SHA256SUMS). "
            f"Install torch and run `python3 {validate} {dest}` to exercise "
            "the bundled debug_trace_io reader."
        )
    else:
        print("[get] running validate.py (full: reader-contract gates exercise debug_trace_io) ...")
        cmd = [sys.executable, str(validate), str(dest)]
        if trace_tags is not None:
            # Selective download: tell validate.py which traces are actually
            # present so per-trace gates (asset presence, schema, reader
            # contract) scope to them instead of failing on the absent ones.
            cmd += ["--downloaded-traces", ",".join(trace_tags)]
        subprocess.run(cmd, check=True)

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
    # Ask for the destination first: the manifest probe (and all later
    # activity) must live inside dest per the release contract — no /tmp.
    default = f"~/{tag.replace('/', '_')}"
    dest = Path(prompt_dest(default))  # prompt_dest already expands ~
    print(f"[get] fetching manifest for {tag} ...")
    manifest = fetch_manifest(tag, dest)
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
    download_bundle(tag, trace_tags=chosen, dest=dest)
    return 0


def cmd_interactive() -> int:
    _check_gh()
    _ensure_tty()
    releases = gh_release_list()
    if not releases:
        print("(no releases available)")
        return 1
    options: list[tuple[str, str]] = [(r["tagName"], _release_summary(r)) for r in releases]
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
        ) from None
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
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose: log per-component completeness (which files are found "
        "and will be checksum-validated vs. which are missing and will be "
        "downloaded) and each file as its SHA256 is verified.",
    )
    args = p.parse_args()

    global _VERBOSE
    _VERBOSE = args.verbose

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

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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_RELEASE_REPO = "tenstorrent/bit_sculpt"

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
    """Fetch release assets via gh (auth + retries handled by gh).

    `--skip-existing` is deliberately NOT used: it was added in a recent gh
    and is absent on older clients (e.g. gh 2.4.0), so it breaks consumers.
    Every caller downloads into a freshly-created empty dir, so gh never hits
    an existing file (the condition --skip-existing / --clobber would guard).
    """
    cmd = [
        "gh", "release", "download", tag,
        "--repo", _RELEASE_REPO,
        "--dir", str(dest),
    ]
    if patterns:
        for p in patterns:
            cmd += ["--pattern", p]
    subprocess.run(cmd, check=True)


def _fetch_release_metadata(tag: str, dest: Path) -> tuple[dict, str]:
    """gh-download manifest.json + SHA256SUMS into a probe dir under `dest`,
    returning (manifest dict, SHA256SUMS text).

    These two tiny files describe the whole release, so we fetch them first —
    cheaply — to (a) enumerate release-level assets for a selective download and
    (b) detect whether `dest` already holds this release so the heavy asset
    download can be skipped.

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
        gh_release_download(tag, probe, patterns=["manifest.json", "SHA256SUMS"])
        manifest = json.loads((probe / "manifest.json").read_text())
        sums_text = (probe / "SHA256SUMS").read_text()
        return manifest, sums_text
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


def _expected_files_by_component(manifest: dict) -> tuple[dict[str, set[str]], list[str]]:
    """Map each release COMPONENT to the post-extract files it should produce.

    A component is a trace tag (e.g. ``vllm-ef537f25-8193tok``) or the special
    ``"__release__"`` group (top-level files: REPORT.md, debug_trace_io.py,
    validate.py). Returns ``(expected, release_tar_dirs)`` where:
      - ``expected[component]`` is the set of dest-relative paths that must
        exist for that component to be considered fully present;
      - ``release_tar_dirs`` are extracted-dir names for release-level tarballs
        (e.g. ``diversity_plots``) whose individual files aren't in the
        ``extracted_files`` registry — checked as a non-empty dir instead.

    Built from ``assets`` (flat files map by stripping the ``<tag>--`` prefix)
    + the v3 ``extracted_files`` registry (every per-trace ``.safetensors``,
    incl. tarball-extracted rows). Empty if the manifest isn't v3 (no registry
    to confirm completeness → caller must download).
    """
    expected: dict[str, set[str]] = {}
    release_tar_dirs: list[str] = []
    if manifest.get("format_version") != 3:
        return expected, release_tar_dirs
    for a in manifest.get("assets", []):
        name = a["name"]
        tag = a.get("trace_tag")
        if tag is None:
            if name.endswith(".tar.zst"):
                release_tar_dirs.append(name[: -len(".tar.zst")])
            else:
                expected.setdefault("__release__", set()).add(name)
        elif not name.endswith(".tar.zst"):
            # Per-trace flat file (kv_cache_*.safetensors, index.json, metadata.json)
            # extracts to <tag>/<stripped>.
            expected.setdefault(tag, set()).add(f"{tag}/{name.split('--', 1)[1]}")
    for e in manifest.get("extracted_files", []):
        path = e["path"]
        expected.setdefault(path.split("/", 1)[0], set()).add(path)
    return expected, release_tar_dirs


def _components_present(dest: Path, manifest: dict, sums_text: str) -> set[str]:
    """Components ('__release__' / trace tags) already fully present in `dest`.

    Existence-only (the post-extract SHA verify is authoritative afterward).
    Gated on a same-release check: dest must hold this exact release's
    manifest.json (matching release_commit) + SHA256SUMS, else nothing is
    trusted (a stale/different release in dest → re-download everything).
    """
    expected, release_tar_dirs = _expected_files_by_component(manifest)
    if not expected:
        _vprint("[get] manifest has no extracted_files registry (pre-v3) — "
                "can't confirm existing files; will download everything")
        return set()  # not v3 / no registry — can't confirm anything
    dm = dest / "manifest.json"
    ds = dest / "SHA256SUMS"
    if not dm.is_file() or not ds.is_file():
        _vprint(f"[get] no prior manifest.json/SHA256SUMS in {dest} — fresh download")
        return set()
    try:
        if json.loads(dm.read_text()).get("release_commit") != manifest.get("release_commit"):
            _vprint("[get] dest manifest.json is for a different release_commit — "
                    "nothing reused")
            return set()  # different release cached in dest
        if ds.read_text() != sums_text:
            _vprint("[get] dest SHA256SUMS differs from this release — nothing reused")
            return set()  # integrity manifest changed
    except (json.JSONDecodeError, OSError):
        return set()
    _vprint(f"[get] scanning {dest} for existing components ...")
    present: set[str] = set()
    for comp, files in expected.items():
        missing = sorted(p for p in files if not (dest / p).is_file())
        if not missing:
            present.add(comp)
            _vprint(f"[get]   ✓ {comp}: all {len(files)} file(s) present "
                    f"(will be checksum-validated)")
        else:
            _vprint(f"[get]   ✗ {comp}: {len(files) - len(missing)}/{len(files)} "
                    f"present, {len(missing)} missing → will download:")
            for p in missing[:12]:
                _vprint(f"[get]        - {p}")
            if len(missing) > 12:
                _vprint(f"[get]        ... (+{len(missing) - 12} more)")
    # The release component also needs its release-level tarball dirs (plots).
    if "__release__" in present:
        for d in release_tar_dirs:
            p = dest / d
            if not (p.is_dir() and any(p.iterdir())):
                _vprint(f"[get]   ✗ __release__: tarball dir {d}/ missing/empty "
                        f"→ will re-download")
                present.discard("__release__")
                break
    return present


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
        actual = h.hexdigest()
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
    print(f"[get] SHA256SUMS verified{tag}: {checked} file(s)")
    return checked


def download_bundle(
    tag: str, trace_tags: list[str] | None, dest: Path
) -> None:
    dest.mkdir(parents=True, exist_ok=True)

    # Never let a stale staging dir from an interrupted prior run survive into
    # this one (even the skip path) — clean it unconditionally up front.
    staging = dest / ".get_staging"
    if staging.exists():
        shutil.rmtree(staging)

    # Fetch the two tiny metadata files first (manifest.json + SHA256SUMS).
    # They let us (a) enumerate release-level assets and (b) detect which
    # components are ALREADY in dest so we can skip re-downloading them. We do
    # NOT rely on `gh release download --skip-existing` (absent on older gh):
    # we detect present components ourselves and fetch only the missing ones
    # into a FRESH staging dir, so gh never meets a pre-existing file.
    manifest, sums_text = _fetch_release_metadata(tag, dest)

    # Refuse to mix releases: if dest already holds a DIFFERENT release, the
    # extract/move below would only overwrite same-named paths and silently
    # leave the other release's files behind. Make the user start from clean.
    dm = dest / "manifest.json"
    if dm.is_file():
        try:
            prior_commit = json.loads(dm.read_text()).get("release_commit")
        except (json.JSONDecodeError, OSError):
            prior_commit = None
        new_commit = manifest.get("release_commit")
        if prior_commit and new_commit and prior_commit != new_commit:
            raise SystemExit(
                f"{dest} already holds a different release of {tag} "
                f"(commit {prior_commit[:12]}; you requested {new_commit[:12]}). "
                f"Delete the directory (or use a fresh one) and re-run — get.py "
                f"won't mix two releases in one dir."
            )

    all_trace_tags = [t["tag"] for t in manifest.get("traces", []) if "tag" in t]
    known_components = set(_expected_files_by_component(manifest)[0])
    wanted = {"__release__"} | set(all_trace_tags if trace_tags is None else trace_tags)
    # v3 only: drop any wanted component the manifest ships nothing for. A
    # degenerate empty trace tag would otherwise yield an unsatisfiable
    # "<tag>--*" pattern that makes gh exit nonzero and abort the whole bundle.
    if known_components:
        wanted &= known_components | {"__release__"}
    present = _components_present(dest, manifest, sums_text)
    to_fetch = sorted(wanted - present)
    reused = sorted(wanted & present)

    if not to_fetch:
        print(f"[get] release already present in {dest} — skipping download "
              f"({len(wanted)} component(s) verified against manifest)")
    else:
        if reused:
            print(f"[get] reusing {len(reused)} already-present component(s); "
                  f"fetching {len(to_fetch)}: {', '.join(to_fetch)}")
        # --pattern list for ONLY the components we still need. A trace tag's
        # assets all begin "<tag>--"; release-level assets have no trace_tag.
        patterns: list[str] = []
        if "__release__" in to_fetch:
            release_level = [a["name"] for a in manifest.get("assets", [])
                             if a.get("trace_tag") is None]
            patterns += ["manifest.json", "SHA256SUMS", "REPORT.md",
                         "debug_trace_io.py", "validate.py", *release_level]
        patterns += [f"{c}--*" for c in to_fetch if c != "__release__"]
        patterns = list(dict.fromkeys(patterns))

        # Contract: confine ALL activity to dest — stage inside it, never /tmp.
        # Fresh staging => gh never sees a pre-existing file (no --skip-existing).
        staging.mkdir(parents=True)
        print(f"[get] staging: {staging}")
        print(f"[get] gh release download {tag} (patterns: {patterns}) ...")
        gh_release_download(tag, staging, patterns=patterns)

        # Pre-extract: verify the downloaded tarballs/flat files against
        # SHA256SUMS. Use the freshly-staged copy if we fetched the release
        # component, else the already-present dest copy (partial tag fetch).
        sums_for_verify = staging / "SHA256SUMS"
        if not sums_for_verify.is_file():
            sums_for_verify = dest / "SHA256SUMS"
        _verify_sha256sums(staging, sums_path=sums_for_verify, label="pre-extract assets")

        use_native = _have_tar_zstd()
        print(f"[get] extracting tarballs (tar --zstd: {use_native}) ...")
        for tarball in sorted(staging.glob("*.tar.zst")):
            if "--" in tarball.name:
                # Per-trace tarball -> dest/<tag>/... . A partial extract leaves
                # missing extracted_files rows, so the tag is detected incomplete
                # next run and re-fetched; direct extraction is safe.
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

        # Staging fully consumed (tarballs extracted, flat files moved). Remove
        # it so the post-extract verify + validate.py see a clean dest.
        shutil.rmtree(staging, ignore_errors=True)

    # Post-extract: verify the extracted .safetensors against the
    # extracted-namespace lines in SHA256SUMS (manifest v3 extracted_files).
    # SHA256SUMS was moved into dest by the flat-file loop above. This
    # realizes the per-extracted-file integrity guarantee in the default
    # consumer flow (not just a manual `sha256sum -c`). On a v2 bundle
    # (no extracted-namespace lines) this verifies 0 extra files — harmless.
    sums_in_dest = dest / "SHA256SUMS"
    if sums_in_dest.is_file():
        _verify_sha256sums(dest, sums_in_dest, label="post-extract .safetensors")

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
    p.add_argument(
        "-v", "--verbose", action="store_true",
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

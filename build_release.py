"""Build and package tai_backtest EXE for release.

Usage:
    python build_release.py          # build + zip
    python build_release.py --skip-build  # zip only (reuse existing dist/)
    python build_release.py --allow-dirty # permit uncommitted source

Build-integrity guard (added after the v2.13.0 mismatch, where the
published EXE was built from a checkout lacking the regime code):

  1. Refuses to package when tracked SOURCE (src/, run_backtest.py,
     version.py) has uncommitted changes — a dirty tree can't be traced
     back to a commit. Override with --allow-dirty.
  2. Refuses to package when the bundled ``_internal/src`` does not match
     the working-tree ``src`` — this is exactly the drift that shipped in
     v2.13.0, and it catches a stale ``dist/`` reused via --skip-build.
  3. Stamps ``BUILD_INFO.json`` (version + commit SHA + dirty flag +
     build time) into the zip so every artifact is traceable to a commit.
"""

import argparse
import filecmp
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

import yaml

from version import APP_VERSION as VERSION
DIST_DIR = os.path.join("dist", "tai_backtest")
ZIP_NAME = f"tai_backtest_v{VERSION}_win_x64.zip"

# Tracked paths whose uncommitted state would make the build untraceable.
SOURCE_PATHS = ["src", "run_backtest.py", "version.py"]

# Files and directories to EXCLUDE from the release zip
EXCLUDE_FILES = set()  # settings.yaml is now included with default values
EXCLUDE_DIRS = {"CapitalLog_Backtest", "_comtypes_cache", "__pycache__", "data", "live"}


def _git(*args) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def git_commit_info() -> tuple[str, bool]:
    """Return (commit_sha, source_is_dirty) for the current checkout.

    ``source_is_dirty`` is True only when a tracked SOURCE_PATH has
    uncommitted changes — unrelated dirt (e.g. CLAUDE.md) is ignored so
    the guard doesn't block on docs edits.
    """
    try:
        sha = _git("rev-parse", "HEAD")
        porcelain = _git("status", "--porcelain", "--", *SOURCE_PATHS)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("WARNING: git unavailable — cannot verify build provenance")
        return "unknown", False
    return sha, bool(porcelain)


def _iter_rel_files(root: str):
    """Yield paths relative to ``root``, skipping __pycache__ and .pyc."""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in files:
            if fname.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, fname)
            yield os.path.relpath(full, root)


def verify_bundle_matches_source() -> None:
    """Refuse to package if the bundled ``src`` differs from the working
    tree — the guard that would have caught the v2.13.0 stale-EXE ship.
    """
    bundled = os.path.join(DIST_DIR, "_internal", "src")
    if not os.path.isdir(bundled):
        print(f"ERROR: bundled source not found at {bundled} — build first.")
        sys.exit(1)

    src_files = set(_iter_rel_files("src"))
    bnd_files = set(_iter_rel_files(bundled))
    missing = sorted(src_files - bnd_files)
    extra = sorted(bnd_files - src_files)
    differ = [rel for rel in sorted(src_files & bnd_files)
              if not filecmp.cmp(os.path.join("src", rel),
                                 os.path.join(bundled, rel), shallow=False)]

    if missing or extra or differ:
        print("ERROR: bundled src/ does not match the working tree.")
        print("       The dist/ is stale or built from a different commit "
              "(this is the v2.13.0 failure mode). Rebuild without "
              "--skip-build.")
        for rel in missing:
            print(f"  missing in bundle: {rel}")
        for rel in extra:
            print(f"  extra in bundle:   {rel}")
        for rel in differ:
            print(f"  differs:           {rel}")
        sys.exit(1)
    print(f"Bundle integrity OK — {len(src_files)} source files match HEAD")


def write_build_stamp(sha: str, dirty: bool) -> None:
    """Stamp BUILD_INFO.json into DIST_DIR so it lands in the zip."""
    stamp = {
        "version": VERSION,
        "commit": sha,
        "dirty": dirty,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = os.path.join(DIST_DIR, "BUILD_INFO.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
    flag = " (DIRTY)" if dirty else ""
    print(f"  Stamped BUILD_INFO.json: v{VERSION} @ {sha[:10]}{flag}")


def build():
    print(f"=== Building tai_backtest v{VERSION} ===")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "tai_backtest.spec", "--noconfirm"],
        cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
    )
    if result.returncode != 0:
        print("BUILD FAILED")
        sys.exit(1)
    print("Build OK")


def package():
    print(f"\n=== Packaging {ZIP_NAME} ===")

    if not os.path.isdir(DIST_DIR):
        print(f"ERROR: {DIST_DIR} not found. Run build first.")
        sys.exit(1)

    # Copy extra files into dist before zipping
    project = os.path.dirname(os.path.abspath(__file__)) or "."
    extras = {
        os.path.join(project, "settings.example.yaml"): os.path.join(DIST_DIR, "settings.yaml"),
        os.path.join(project, "必看安裝說明.txt"): os.path.join(DIST_DIR, "必看安裝說明.txt"),
    }
    for src, dst in extras.items():
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            print(f"  Copied {os.path.basename(src)} -> {os.path.basename(dst)}")

    zip_path = os.path.join("dist", ZIP_NAME)
    included = 0
    excluded = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(DIST_DIR):
            # Remove excluded dirs in-place so os.walk skips them
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

            for fname in files:
                if fname in EXCLUDE_FILES:
                    excluded += 1
                    continue
                fpath = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, "dist")
                zf.write(fpath, arcname)
                included += 1

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"Packaged {included} files ({excluded} excluded)")
    print(f"Output: {zip_path} ({size_mb:.1f} MB)")
    return zip_path


def write_checksum(zip_path: str) -> str:
    """Compute the SHA-256 of the release zip and write a sidecar file.

    Writes ``<zip>.sha256`` in the ``sha256sum``-compatible format
    (``<hex>  <filename>``) next to the zip. Returns the sidecar path.
    """
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    sha_path = zip_path + ".sha256"
    with open(sha_path, "w", encoding="utf-8") as f:
        f.write(f"{digest}  {os.path.basename(zip_path)}\n")
    print(f"  SHA-256: {digest}")
    print(f"  Wrote {sha_path}")
    return sha_path


def _load_release_notes() -> str:
    """Read release_notes_v{VERSION}.md from the project root, or empty."""
    project = os.path.dirname(os.path.abspath(__file__)) or "."
    path = os.path.join(project, f"release_notes_v{VERSION}.md")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        body = f.read().strip()
    # Strip the leading "# v{VERSION}" title line — redundant with the header.
    lines = body.splitlines()
    if lines and lines[0].strip().lstrip("#").strip() == f"v{VERSION}":
        body = "\n".join(lines[1:]).strip()
    return body


def notify_release(sha: str) -> None:
    """Send a release announcement to the Discord poster channel."""
    settings_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)) or ".", "settings.yaml")
    if not os.path.isfile(settings_path):
        print("  No settings.yaml — skipping Discord notification")
        return

    with open(settings_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    notif = cfg.get("notifications", {})
    bot_token = str(notif.get("discord_bot_token", "") or "").strip()
    poster_channel = str(notif.get("discord_poster_channel_id", "") or "").strip()
    if not bot_token or not poster_channel:
        print("  Discord poster channel not configured — skipping notification")
        return

    role_id = str(notif.get("discord_poster_role_id", "") or "").strip()
    role_mention = f"<@&{role_id}>\n" if role_id else ""
    commit_str = f" (`{sha[:10]}`)" if sha and sha != "unknown" else ""
    header = f"🚀 **tai-robot v{VERSION} released**{commit_str}"

    repo_url = _git("remote", "get-url", "origin").removesuffix(".git")
    release_url = f"{repo_url}/releases/tag/v{VERSION}"
    release_link = f"🔗 {release_url}"

    notes = _load_release_notes()
    if notes:
        if len(notes) > 1500:
            notes = notes[:1500].rsplit("\n", 1)[0] + "\n..."
        content = f"{role_mention}{header}\n\n{notes}\n\n{release_link}"
    else:
        content = f"{role_mention}{header}\n\n{release_link}"
        print(f"  No release_notes_v{VERSION}.md found — sending without notes")

    import httpx
    url = f"https://discord.com/api/v10/channels/{poster_channel}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(url, json={"content": content}, headers=headers, timeout=10)
    if resp.status_code in (200, 201):
        print(f"  Sent release announcement to Discord poster channel")
    else:
        print(f"  WARNING: Discord API returned {resp.status_code}: {resp.text}")


def main():
    parser = argparse.ArgumentParser(description="Build and package tai_backtest release")
    parser.add_argument("--skip-build", action="store_true", help="Skip PyInstaller build, just zip")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Permit packaging with uncommitted source changes")
    parser.add_argument("--notify-only", action="store_true",
                        help="Skip build/package, just send Discord notification")
    args = parser.parse_args()

    sha, dirty = git_commit_info()

    if args.notify_only:
        notify_release(sha)
        return

    if dirty and not args.allow_dirty:
        print("ERROR: tracked source (src/, run_backtest.py, version.py) has "
              "uncommitted changes.")
        print("       Commit them so the release is traceable to a commit, "
              "or pass --allow-dirty to override.")
        sys.exit(1)

    if not args.skip_build:
        build()

    # Guard: the packaged binary must match the committed source. This is
    # the check that would have prevented the v2.13.0 stale-EXE ship.
    verify_bundle_matches_source()
    write_build_stamp(sha, dirty)

    zip_path = package()
    sha_path = write_checksum(zip_path)

    notify_release(sha)

    print(f"\n=== Done ===")
    print(f"To create a GitHub release (upload zip + checksum):")
    print(f"  gh release create v{VERSION} {zip_path} {sha_path} "
          f"--title \"v{VERSION}\" --notes \"First release\"")


if __name__ == "__main__":
    main()

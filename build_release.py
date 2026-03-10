"""Build and package tai_backtest EXE for release.

Usage:
    python build_release.py          # build + zip
    python build_release.py --skip-build  # zip only (reuse existing dist/)
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

from version import APP_VERSION as VERSION
DIST_DIR = os.path.join("dist", "tai_backtest")
ZIP_NAME = f"tai_backtest_v{VERSION}_win_x64.zip"

# Files and directories to EXCLUDE from the release zip
EXCLUDE_FILES = set()  # settings.yaml is now included with default values
EXCLUDE_DIRS = {"CapitalLog_Backtest", "_comtypes_cache", "__pycache__", "data", "live"}


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


def main():
    parser = argparse.ArgumentParser(description="Build and package tai_backtest release")
    parser.add_argument("--skip-build", action="store_true", help="Skip PyInstaller build, just zip")
    args = parser.parse_args()

    if not args.skip_build:
        build()

    zip_path = package()

    print(f"\n=== Done ===")
    print(f"To create a GitHub release:")
    print(f"  gh release create v{VERSION} {zip_path} --title \"v{VERSION}\" --notes \"First release\"")


if __name__ == "__main__":
    main()

"""Bug report builder — creates debug zip and GitHub issue URL.

Pure logic, no GUI dependencies. Returns results for the caller to display.
"""

from __future__ import annotations

import os
import platform
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class BugReport:
    """Result of building a bug report."""
    zip_path: str
    files_added: int
    issue_url: str
    title: str


def build_bug_report(
    *,
    bot_dir: str | None,
    strategy: str,
    symbol: str,
    mode: str,
    position: int,
    strategy_code: str | None = None,
    app_version: str = "unknown",
    repo: str = "ericwu13/tai-robot",
    now: datetime | None = None,
) -> BugReport | None:
    """Create a debug zip and GitHub issue URL.

    Returns None if no files were collected (caller should show a message).
    """
    if now is None:
        from src.live.live_runner import _taipei_now
        now = _taipei_now()

    ts = now.strftime("%Y%m%d_%H%M%S")

    # Determine zip output path
    if bot_dir and os.path.isdir(bot_dir):
        zip_path = os.path.join(bot_dir, f"bug_report_{ts}.zip")
    else:
        os.makedirs("data", exist_ok=True)
        zip_path = os.path.join("data", f"bug_report_{ts}.zip")

    # Collect files into zip
    files_added = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if bot_dir and os.path.isdir(bot_dir):
            for fname in os.listdir(bot_dir):
                fpath = os.path.join(bot_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                if fname.endswith((".log", ".csv", ".json")) and not fname.startswith("bug_report"):
                    zf.write(fpath, fname)
                    files_added += 1

        if strategy_code:
            zf.writestr("strategy_code.py", strategy_code)
            files_added += 1

    if files_added == 0:
        try:
            os.remove(zip_path)
        except Exception:
            pass
        return None

    # Build issue body (compact — logs are in the zip)
    body = f"""**Version**: v{app_version}
**OS**: {platform.platform()}
**Python**: {platform.python_version()}
**Strategy**: {strategy}
**Symbol**: {symbol}
**Mode**: {mode}
**Position**: {position:+d}

## Description
<!-- Describe what happened -->


## Attachments
Debug zip: `{os.path.basename(zip_path)}` ({files_added} files)
Please drag-drop the zip file into this issue.
"""

    title = f"[Bug] {strategy} on {symbol}"
    issue_url = (
        f"https://github.com/{repo}/issues/new?"
        f"title={urllib.parse.quote(title)}&"
        f"body={urllib.parse.quote(body)}"
    )

    return BugReport(
        zip_path=zip_path,
        files_added=files_added,
        issue_url=issue_url,
        title=title,
    )

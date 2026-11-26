"""Gemini issue triage glue: collect context, validate JSON, apply labels & comment.

    python .github/scripts/gemini_triage_post.py collect OUT_JSON
        Fetch the target issue, repo labels, and recent open issues via `gh api`
        and write a single JSON context file for Gemini CLI (read-only).

    python .github/scripts/gemini_triage_post.py apply RESULT_FILE
        Parse Gemini CLI's raw response from RESULT_FILE, validate the triage
        JSON strictly, add ONLY allowed triage labels that exist in the repo
        (never overwriting existing labels or an existing P1/P2/P3 severity,
        and never adding privileged labels like `autofix` or `needs-owner`),
        and create or update a single edit-in-place triage comment.

Env: GITHUB_REPOSITORY, ISSUE, GH_TOKEN (for collect and apply). Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

MARKER = "<!-- gemini-triage-state -->"
BOT_LOGINS = frozenset({"github-actions", "github-actions[bot]"})
SEVERITY_LABELS = frozenset({"P1", "P2", "P3"})
# Hardcoded safe subset of repo labels that automated triage may add.
# Privileged/workflow-triggering labels (autofix, ultrathink, claude-review,
# needs-owner, follow-up, gemini-triage) are intentionally excluded.
ALLOWED_TRIAGE_LABELS = frozenset({
    "bug",
    "enhancement",
    "documentation",
    "question",
    "duplicate",
    "P1",
    "P2",
    "P3",
})
MAX_ISSUE_BODY_CHARS = 12_000
MAX_COMMENT_CHARS = 12_000
MAX_OPEN_ISSUES = 40


def parse_issue_number(raw: str) -> int:
    text = str(raw or "").strip()
    if not re.fullmatch(r"[1-9]\d*", text):
        raise ValueError(f"invalid issue number: {raw!r}")
    return int(text)


def gh_api(path: str, *, method: str = "GET", payload: Any = None) -> Any:
    cmd = ["gh", "api", "--method", method, path]
    inp = None
    if payload is not None:
        cmd.extend(["--input", "-"])
        inp = json.dumps(payload)
    proc = subprocess.run(
        cmd,
        input=inp,
        text=True,
        capture_output=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"gh api {method} {path} failed: {err[:400]}")
    out = (proc.stdout or "").strip()
    return json.loads(out) if out else None


def collect_context(repo: str, issue_num: int, out_path: str | os.PathLike) -> dict[str, Any]:
    issue = gh_api(f"repos/{repo}/issues/{issue_num}")
    if not isinstance(issue, dict) or "pull_request" in issue:
        raise ValueError(f"#{issue_num} is not a plain issue")

    raw_labels = gh_api(f"repos/{repo}/labels?per_page=100") or []
    available_labels = sorted(
        str(lb["name"])
        for lb in raw_labels
        if isinstance(lb, dict) and lb.get("name") in ALLOWED_TRIAGE_LABELS
    )

    raw_open = gh_api(
        f"repos/{repo}/issues?state=open&per_page={MAX_OPEN_ISSUES}&sort=updated&direction=desc"
    ) or []
    open_issues = []
    for item in raw_open:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        num = item.get("number")
        if num == issue_num:
            continue
        open_issues.append({
            "number": num,
            "title": str(item.get("title") or "")[:200],
            "labels": [
                str(lb["name"])
                for lb in (item.get("labels") or [])
                if isinstance(lb, dict) and lb.get("name")
            ],
        })

    ctx = {
        "repository": repo,
        "issue": {
            "number": issue_num,
            "title": str(issue.get("title") or "")[:300],
            "author": str((issue.get("user") or {}).get("login") or ""),
            "labels": [
                str(lb["name"])
                for lb in (issue.get("labels") or [])
                if isinstance(lb, dict) and lb.get("name")
            ],
            "body": str(issue.get("body") or "")[:MAX_ISSUE_BODY_CHARS],
        },
        "allowed_labels": available_labels,
        "open_issues": open_issues,
    }
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    return ctx


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extract the triage JSON object from Gemini CLI output.

    Accepts bare JSON, fenced ```json blocks, or prose wrapping a JSON object.
    Raises ValueError if no valid JSON object is found.
    """
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("empty triage output")

    # 1. Direct parse
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    # 2. Fenced ```json ... ``` or ``` ... ``` blocks (prefer the last block)
    fenced = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.S | re.I)
    for block in reversed(fenced):
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue

    # 3. Scan from the last '{' backwards using JSONDecoder.raw_decode
    decoder = json.JSONDecoder()
    for idx in range(len(text) - 1, -1, -1):
        if text[idx] != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict) and (
                "summary_markdown" in obj or "labels" in obj or "severity" in obj
            ):
                return obj
        except ValueError:
            continue

    raise ValueError("no valid triage JSON object found in output")


def select_labels_to_add(
    proposed: list[Any] | None,
    *,
    severity: str = "",
    existing_labels: set[str],
    repo_labels: set[str],
) -> list[str]:
    """Return the validated subset of labels safe to add to the issue.

    Rules:
    - Only labels in ALLOWED_TRIAGE_LABELS and present in repo_labels may be added.
    - Existing labels are never removed or re-added.
    - At most one severity label (P1/P2/P3) is added, and ONLY if the issue
      does not already carry a severity label.
    """
    candidates: list[str] = []
    for item in proposed or []:
        if isinstance(item, str):
            name = item.strip()
            if name and name not in candidates:
                candidates.append(name)

    sev = str(severity or "").strip().upper()
    if sev in SEVERITY_LABELS and sev not in candidates:
        candidates.append(sev)

    already_has_severity = bool(existing_labels & SEVERITY_LABELS)
    chosen_sev: str | None = None
    if not already_has_severity:
        if sev in SEVERITY_LABELS and sev in repo_labels:
            chosen_sev = sev
        else:
            for c in candidates:
                if c in SEVERITY_LABELS and c in repo_labels:
                    chosen_sev = c
                    break

    to_add: list[str] = []
    for c in candidates:
        if c not in ALLOWED_TRIAGE_LABELS or c not in repo_labels or c in existing_labels:
            continue
        if c in SEVERITY_LABELS:
            if c == chosen_sev and c not in to_add:
                to_add.append(c)
            continue
        if c not in to_add:
            to_add.append(c)

    if chosen_sev and chosen_sev not in to_add and chosen_sev not in existing_labels:
        to_add.append(chosen_sev)

    return to_add


def format_triage_comment(
    data: dict[str, Any],
    *,
    added_labels: list[str],
    run_url: str = "",
) -> str:
    """Build the single edit-in-place triage comment markdown."""
    summary = str(data.get("summary_markdown") or "").replace(MARKER, "").strip()
    if not summary:
        raise ValueError("triage JSON missing non-empty summary_markdown")

    if not summary.lstrip().startswith("## "):
        summary = f"## Gemini triage\n\n{summary}"

    footer_parts = ["Automated read-only triage via Gemini CLI"]
    if added_labels:
        footer_parts.append("labels added: " + ", ".join(f"`{lb}`" for lb in added_labels))
    else:
        footer_parts.append("labels added: none")
    if run_url:
        footer_parts.append(f"[run]({run_url})")
    footer = f"\n\n<sub>{' | '.join(footer_parts)}</sub>\n{MARKER}"

    budget = MAX_COMMENT_CHARS - len(footer)
    if len(summary) > budget:
        summary = summary[: max(0, budget - 20)].rstrip() + "\n\n*(truncated)*"
    return summary + footer


def find_existing_triage_comment_id(comments: list[Any]) -> int | None:
    """Return the comment ID of the last bot triage comment carrying MARKER."""
    for item in reversed(comments or []):
        if not isinstance(item, dict):
            continue
        login = str((item.get("user") or {}).get("login") or "")
        body = str(item.get("body") or "")
        if login in BOT_LOGINS and MARKER in body and isinstance(item.get("id"), int):
            return item["id"]
    return None


def apply_triage(
    repo: str,
    issue_num: int,
    result_path: str | os.PathLike,
    *,
    run_url: str = "",
) -> dict[str, Any]:
    raw_text = Path(result_path).read_text(encoding="utf-8")
    data = extract_json_object(raw_text)

    issue = gh_api(f"repos/{repo}/issues/{issue_num}")
    if not isinstance(issue, dict) or "pull_request" in issue:
        raise ValueError(f"#{issue_num} is not a plain issue")

    existing_labels = {
        str(lb["name"])
        for lb in (issue.get("labels") or [])
        if isinstance(lb, dict) and lb.get("name")
    }
    raw_repo_labels = gh_api(f"repos/{repo}/labels?per_page=100") or []
    repo_labels = {
        str(lb["name"])
        for lb in raw_repo_labels
        if isinstance(lb, dict) and lb.get("name")
    }

    to_add = select_labels_to_add(
        data.get("labels") if isinstance(data.get("labels"), list) else [],
        severity=str(data.get("severity") or ""),
        existing_labels=existing_labels,
        repo_labels=repo_labels,
    )

    body = format_triage_comment(data, added_labels=to_add, run_url=run_url)

    if to_add:
        gh_api(
            f"repos/{repo}/issues/{issue_num}/labels",
            method="POST",
            payload={"labels": to_add},
        )

    comments = gh_api(f"repos/{repo}/issues/{issue_num}/comments?per_page=100") or []
    existing_id = find_existing_triage_comment_id(comments)
    if existing_id is not None:
        gh_api(
            f"repos/{repo}/issues/comments/{existing_id}",
            method="PATCH",
            payload={"body": body},
        )
        action = "updated"
    else:
        gh_api(
            f"repos/{repo}/issues/{issue_num}/comments",
            method="POST",
            payload={"body": body},
        )
        action = "created"

    print(f"Gemini triage {action} on #{issue_num}; labels added: {to_add}")
    return {"action": action, "added_labels": to_add}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in ("collect", "apply"):
        print("usage: gemini_triage_post.py <collect|apply> <path>", file=sys.stderr)
        return 2
    cmd, path = args
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo or "/" not in repo:
        print("GITHUB_REPOSITORY must be set to owner/repo", file=sys.stderr)
        return 2
    issue_num = parse_issue_number(os.environ.get("ISSUE", ""))
    if cmd == "collect":
        collect_context(repo, issue_num, path)
    else:
        apply_triage(repo, issue_num, path, run_url=os.environ.get("RUN_URL", "").strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

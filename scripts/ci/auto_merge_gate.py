"""Approval-gated auto-merge for ericwu13/tai-robot.

    python auto_merge_gate.py trigger-sage
        If the live PR is an open, non-draft, same-repo PR into master and
        its diff touches ``.claude/skills/`` or ``CLAUDE.md``, POST
        ``{"pr", "head_sha"}`` to the Sage webhook unless Sage already
        reviewed that exact head. The webhook URL and key are never printed.

    python auto_merge_gate.py gate
        Require a cursor[bot] APPROVED review at the exact head from Reed,
        and from Sage when the diff touches skills or CLAUDE.md. Resolve
        unresolved threads opened by an automation that is approved at that
        head, post the ``approval-gate`` commit status, and enable squash
        auto-merge when the PR is eligible. A release (``version.py``), a
        workflow or CI script edit, a draft, or the ``needs-owner`` label is not eligible.

Env: GH_TOKEN (read by gh), GITHUB_REPOSITORY, PR.
trigger-sage also: SAGE_REVIEW_WEBHOOK_URL, SAGE_REVIEW_WEBHOOK_KEY.
gate also: RUN_URL (optional link on the status).

Stdlib and the gh CLI only. The live pull request is re-read here; a
closed, forked, or non-master PR is ignored.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

REED_AUTOMATION_ID = "91d043c7-dbb9-11f1-8120-06b44be262b7"
SAGE_AUTOMATION_ID = "94d41356-e44b-11f1-9535-02900022bdcd"
STATUS_CONTEXT = "approval-gate"

_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WEBHOOK_TIMEOUT_S = 30

THREADS_QUERY = """
query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first:1) { nodes { author { login } body } }
        }
      }
    }
  }
}
"""

RESOLVE_MUTATION = """
mutation($id:ID!) {
  resolveReviewThread(input: {threadId: $id}) { thread { isResolved } }
}
"""


def run(args, *, input_text=None):
    """Run gh. ``args`` is everything after the ``gh`` binary."""
    return subprocess.run(
        ["gh", *args],
        input=input_text,
        text=True,
        capture_output=True,
        encoding="utf-8",
    )


def _fail(proc, fallback):
    detail = ((proc.stderr or proc.stdout) or "").strip()
    raise RuntimeError(detail[:500] or fallback)


def gh_get(path):
    proc = run(["api", path])
    if proc.returncode != 0:
        _fail(proc, f"gh api failed for {path}")
    return json.loads(proc.stdout or "null")


def gh_post(path, payload):
    proc = run(
        ["api", "--method", "POST", path, "--input", "-"],
        input_text=json.dumps(payload),
    )
    if proc.returncode != 0:
        _fail(proc, "gh api post failed")
    if not (proc.stdout or "").strip():
        return {}
    return json.loads(proc.stdout)


def paginate_list(path):
    """GET ``path`` page by page. Keep going while a page is full (100)."""
    page = 1
    items = []
    while True:
        batch = gh_get(f"{path}?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise RuntimeError(f"expected a list from {path}")
        items.extend(batch)
        if len(batch) != 100:
            break
        page += 1
    return items


def graphql(query, variables):
    data = gh_post("graphql", {"query": query, "variables": variables})
    if data.get("errors"):
        raise RuntimeError("graphql error")
    return data.get("data") or {}


def require_repo():
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not _REPO.fullmatch(repo):
        print("GITHUB_REPOSITORY is not owner/name", file=sys.stderr)
        return None
    return repo


def require_pr():
    raw = os.environ.get("PR", "").strip()
    if not raw.isdigit() or int(raw) < 1:
        print("PR is not a positive integer", file=sys.stderr)
        return None
    return int(raw)


def first_line(body):
    """Line 1 of a review or comment body, leading BOM removed."""
    text = body if isinstance(body, str) else ""
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    return text.split("\n", 1)[0]


def body_has_automation(body, automation_id):
    return first_line(body).startswith(f"<!-- CURSOR_AUTOMATION_ID: {automation_id}")


def head_sha_of(pr):
    return ((pr.get("head") or {}).get("sha")) or ""


def pr_is_guarded(pr, repo):
    """Open, same-repo, base master, and a 40-char hex head."""
    if not isinstance(pr, dict) or pr.get("state") != "open":
        return False
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    full = ((head.get("repo") or {}).get("full_name"))
    if full != repo or base.get("ref") != "master":
        return False
    return bool(_SHA.fullmatch(head.get("sha") or ""))


def fetch_guarded_pr(repo, number):
    pr = gh_get(f"repos/{repo}/pulls/{number}")
    if not pr_is_guarded(pr, repo):
        return None
    return pr


def changed_paths(files):
    """Filenames plus previous_filename, so a rename still classifies."""
    paths = []
    for item in files:
        name = item.get("filename")
        if name:
            paths.append(name)
        prev = item.get("previous_filename")
        if prev:
            paths.append(prev)
    return paths


def is_skill_or_claude(paths):
    return any(p == "CLAUDE.md" or p.startswith(".claude/skills/") for p in paths)


def is_release(paths):
    return any(p == "version.py" for p in paths)


def is_workflow(paths):
    """Workflow files and the CI script this job executes from master."""
    return any(p.startswith((".github/workflows/", "scripts/ci/")) for p in paths)


def label_names(pr):
    names = []
    for lab in pr.get("labels") or []:
        if isinstance(lab, str):
            names.append(lab)
        elif isinstance(lab, dict) and lab.get("name"):
            names.append(lab["name"])
    return names


def _matching_reviews(reviews, head_sha, automation_id):
    matched = []
    for review in reviews:
        if ((review.get("user") or {}).get("login")) != "cursor[bot]":
            continue
        if review.get("commit_id") != head_sha:
            continue
        if not body_has_automation(review.get("body"), automation_id):
            continue
        matched.append(review)
    return matched


def is_approved_at_head(reviews, head_sha, automation_id):
    """Latest cursor[bot] review from this automation at ``head_sha`` is APPROVED.

    A newer COMMENTED, CHANGES_REQUESTED, or DISMISSED review from the same
    automation on that SHA wins, so an earlier APPROVED no longer counts.
    """
    matched = _matching_reviews(reviews, head_sha, automation_id)
    if not matched:
        return False
    matched.sort(key=lambda r: (r.get("submitted_at") or "", int(r.get("id") or 0)))
    return matched[-1].get("state") == "APPROVED"


def approved_automation_ids(reviews, head_sha):
    found = []
    for aid in (REED_AUTOMATION_ID, SAGE_AUTOMATION_ID):
        if is_approved_at_head(reviews, head_sha, aid):
            found.append(aid)
    return found


def status_for(need_sage, reed_ok, sage_ok):
    if reed_ok and (sage_ok or not need_sage):
        desc = "Reed + Sage approved" if need_sage else "Reed approved"
        return "success", desc
    if not reed_ok and need_sage and not sage_ok:
        return "pending", "Waiting for Reed QA and Sage"
    if not reed_ok:
        return "pending", "Waiting for Reed QA"
    return "pending", "Waiting for Sage"


def iter_threads(repo, number):
    owner, name = repo.split("/", 1)
    cursor = None
    seen = set()
    while True:
        data = graphql(THREADS_QUERY, {
            "owner": owner,
            "name": name,
            "number": int(number),
            "cursor": cursor,
        })
        conn = (((data.get("repository") or {}).get("pullRequest") or {})
                .get("reviewThreads") or {})
        for node in conn.get("nodes") or []:
            yield node
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)


def _thread_author_and_body(thread):
    nodes = ((thread.get("comments") or {}).get("nodes")) or []
    if not nodes or not isinstance(nodes[0], dict):
        return "", ""
    first = nodes[0]
    login = ((first.get("author") or {}).get("login")) or ""
    return login, first.get("body")


def threads_to_resolve(threads, approved_ids):
    """Unresolved threads whose first comment is cursor and an approved automation.

    GraphQL reports the bot as ``cursor`` (no ``[bot]``). Human, Claude,
    and github-actions threads are left alone, as are other automation ids.
    """
    wanted = set(approved_ids)
    out = []
    seen = set()
    for thread in threads:
        tid = thread.get("id")
        if not tid or tid in seen or thread.get("isResolved"):
            continue
        seen.add(tid)
        login, body = _thread_author_and_body(thread)
        if login != "cursor":
            continue
        if any(body_has_automation(body, aid) for aid in wanted):
            out.append(tid)
    return out


def resolve_approved_threads(repo, number, approved_ids):
    if not approved_ids:
        return
    for tid in threads_to_resolve(iter_threads(repo, number), approved_ids):
        try:
            graphql(RESOLVE_MUTATION, {"id": tid})
        except RuntimeError as exc:
            print(f"could not resolve {tid}: {exc}", file=sys.stderr)
            continue
        print(f"resolved {tid}")


def post_status(repo, sha, state, description, target_url):
    payload = {
        "context": STATUS_CONTEXT,
        "state": state,
        "description": description[:140],
    }
    if target_url:
        payload["target_url"] = target_url
    gh_post(f"repos/{repo}/statuses/{sha}", payload)
    print(f"approval-gate: {state} {description}")


def _merge_text(proc):
    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


def _log_proc(proc):
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        print(out, file=sys.stderr)
    if err:
        print(err, file=sys.stderr)


def _pr_merge(repo, number, *flags):
    return run(["pr", "merge", str(number), "--repo", repo, *flags])


def enable_auto_merge(repo, number, head_sha):
    """Squash auto-merge pinned to ``head_sha``. One clean-status retry, no admin."""
    proc = _pr_merge(repo, number, "--auto", "--squash", "--match-head-commit", head_sha)
    text = _merge_text(proc)
    if proc.returncode == 0 or "already merged" in text.lower():
        print(f"auto-merge enabled for {repo}#{number} at {head_sha}")
        return 0
    if "clean status" in text.lower():
        proc = _pr_merge(repo, number, "--squash", "--match-head-commit", head_sha)
        text = _merge_text(proc)
        if proc.returncode == 0 or "already merged" in text.lower():
            print(f"merged {repo}#{number} at {head_sha}")
            return 0
    _log_proc(proc)
    return proc.returncode or 1


def disable_auto_merge(repo, number):
    proc = _pr_merge(repo, number, "--disable-auto")
    if proc.returncode != 0:
        _log_proc(proc)
        return proc.returncode or 1
    print(f"auto-merge disabled for {repo}#{number}")
    return 0


def _load_diff(repo, number):
    files = paginate_list(f"repos/{repo}/pulls/{number}/files")
    paths = changed_paths(files)
    reviews = paginate_list(f"repos/{repo}/pulls/{number}/reviews")
    return paths, reviews


def gate():
    try:
        return _gate()
    except RuntimeError as exc:
        print(f"approval-gate: {exc}", file=sys.stderr)
        return 1


def _gate():
    repo, number = require_repo(), require_pr()
    if repo is None or number is None:
        return 1
    pr = fetch_guarded_pr(repo, number)
    if pr is None:
        print("approval-gate: skip")
        return 0
    head_sha = head_sha_of(pr)
    paths, reviews = _load_diff(repo, number)
    need_sage = is_skill_or_claude(paths)
    approved = approved_automation_ids(reviews, head_sha)
    reed_ok = REED_AUTOMATION_ID in approved
    sage_ok = SAGE_AUTOMATION_ID in approved
    all_approved = reed_ok and (sage_ok or not need_sage)
    resolve_approved_threads(repo, number, approved)
    state, description = status_for(need_sage, reed_ok, sage_ok)
    eligible = (
        not pr.get("draft")
        and "needs-owner" not in label_names(pr)
        and not is_release(paths)
        and not is_workflow(paths)
    )
    # Drop an armed auto-merge before the status write. A failed status on an
    # unapproved or ineligible PR must not leave the merge queued.
    disarm_rc = None
    if not (all_approved and eligible) and pr.get("auto_merge"):
        disarm_rc = disable_auto_merge(repo, number)
    post_status(repo, head_sha, state, description, os.environ.get("RUN_URL", "").strip())
    if all_approved and eligible:
        return enable_auto_merge(repo, number, head_sha)
    if disarm_rc is not None:
        return disarm_rc
    return 0


def sage_already_reviewed(reviews, head_sha):
    return bool(_matching_reviews(reviews, head_sha, SAGE_AUTOMATION_ID))


def post_sage_webhook(url, key, pr_number, head_sha):
    """POST the trigger. Failures report a status code or exception class only."""
    if not url or not key:
        print("sage webhook is not configured", file=sys.stderr)
        return 1
    body = json.dumps({"pr": int(pr_number), "head_sha": head_sha}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_S) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            print(f"sage webhook: HTTP {code}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"sage webhook failed: HTTP {exc.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        kind = type(exc.reason).__name__ if exc.reason is not None else "URLError"
        print(f"sage webhook failed: {kind}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"sage webhook failed: {type(exc).__name__}", file=sys.stderr)
        return 1


def trigger_sage():
    try:
        return _trigger_sage()
    except RuntimeError as exc:
        print(f"trigger-sage: {exc}", file=sys.stderr)
        return 1


def _trigger_sage():
    repo, number = require_repo(), require_pr()
    if repo is None or number is None:
        return 1
    pr = fetch_guarded_pr(repo, number)
    if pr is None:
        print("trigger-sage: skip")
        return 0
    if pr.get("draft"):
        print("trigger-sage: skip (draft)")
        return 0
    head_sha = head_sha_of(pr)
    files = paginate_list(f"repos/{repo}/pulls/{number}/files")
    if not is_skill_or_claude(changed_paths(files)):
        print("trigger-sage: skip (no skill change)")
        return 0
    reviews = paginate_list(f"repos/{repo}/pulls/{number}/reviews")
    if sage_already_reviewed(reviews, head_sha):
        print("trigger-sage: skip (sage already reviewed)")
        return 0
    return post_sage_webhook(
        os.environ.get("SAGE_REVIEW_WEBHOOK_URL", "").strip(),
        os.environ.get("SAGE_REVIEW_WEBHOOK_KEY", "").strip(),
        number,
        head_sha,
    )


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "trigger-sage":
        return trigger_sage()
    if cmd == "gate":
        return gate()
    print("usage: auto_merge_gate.py trigger-sage|gate", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

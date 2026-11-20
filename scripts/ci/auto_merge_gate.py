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

    python auto_merge_gate.py retrigger-reed
        When the latest ``test`` and ``review`` checks (highest run id) are
        both completed and successful, POST ``{"pr", "head_sha"}`` once for
        each open, non-draft, same-repo master PR whose only Reed review at
        that SHA is COMMENTED with verdict WAIT (``**WAIT**`` or
        ``Verdict: WAIT``; a later CHANGES NEEDED line does not count, and
        APPROVED does not). ``workflow_run`` posts only for the check that
        finished last (``completed_at``, then id). ``pull_request_review``
        uses the live PR head and skips that ordering rule. Missing webhook
        secrets skip with exit 0.

    python auto_merge_gate.py notify-selina
        POST a Selina webhook for a cursor[bot] Reed or Sage review, a CI
        failure, a Claude Review success on a master draft, or a merged PR
        that still has open same-repo PRs stacked on its head ref. Missing
        webhook secrets skip with exit 0. URLs and keys are never printed.

Env: GH_TOKEN (read by gh), GITHUB_REPOSITORY, PR.
trigger-sage also: SAGE_REVIEW_WEBHOOK_URL, SAGE_REVIEW_WEBHOOK_KEY.
gate also: RUN_URL (optional link on the status).
retrigger-reed also: HEAD_SHA, EVENT_NAME, PR, WORKFLOW_NAME,
REED_QA_WEBHOOK_URL, REED_QA_WEBHOOK_KEY.
notify-selina also: EVENT_NAME, REVIEW_ID, HEAD_SHA, WORKFLOW_NAME,
WORKFLOW_CONCLUSION, SELINA_QA_WEBHOOK_URL, SELINA_QA_WEBHOOK_KEY.
The live pull request, review, and check runs are re-read here; a fork is
ignored. trigger-sage, retrigger-reed, and gate also ignore a closed or
non-master PR.

Stdlib and the gh CLI only.
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
    sep = "&" if "?" in path else "?"
    while True:
        batch = gh_get(f"{path}{sep}per_page=100&page={page}")
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


def require_positive_int(name):
    raw = os.environ.get(name, "").strip()
    if not raw.isdigit() or int(raw) < 1:
        print(f"{name} is not a positive integer", file=sys.stderr)
        return None
    return int(raw)


def require_pr():
    return require_positive_int("PR")


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
    """Squash auto-merge pinned to ``head_sha``. One direct-merge retry when already mergeable (clean/unstable), no admin."""
    proc = _pr_merge(repo, number, "--auto", "--squash", "--match-head-commit", head_sha)
    text = _merge_text(proc)
    if proc.returncode == 0 or "already merged" in text.lower():
        print(f"auto-merge enabled for {repo}#{number} at {head_sha}")
        return 0
    if any(s in text.lower() for s in ("clean status", "unstable status")):
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


def _post_webhook(label, url, key, payload, *, skip_if_unset=False):
    """POST ``payload`` as JSON. Failures report a status code or exception class only."""
    url = (url or "").strip()
    key = (key or "").strip()
    if not url or not key:
        if skip_if_unset:
            print(f"{label} webhook: skip (not configured)")
            return 0
        print(f"{label} webhook is not configured", file=sys.stderr)
        return 1
    auth = key if key.lower().startswith("bearer ") else f"Bearer {key}"
    body = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": auth,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_S) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            print(f"{label} webhook: HTTP {code}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"{label} webhook failed: HTTP {exc.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        kind = type(exc.reason).__name__ if exc.reason is not None else "URLError"
        print(f"{label} webhook failed: {kind}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"{label} webhook failed: {type(exc).__name__}", file=sys.stderr)
        return 1


def post_sage_webhook(url, key, pr_number, head_sha):
    """POST the trigger. Failures report a status code or exception class only."""
    return _post_webhook(
        "sage",
        url,
        key,
        {"pr": int(pr_number), "head_sha": head_sha},
        skip_if_unset=False,
    )


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


def _require_sha(name="HEAD_SHA"):
    sha = os.environ.get(name, "").strip()
    if not _SHA.fullmatch(sha):
        print(f"{name} is not a 40-char hex sha", file=sys.stderr)
        return None
    return sha


def _same_repo(pr, repo):
    if not isinstance(pr, dict):
        return False
    head = pr.get("head") or {}
    return ((head.get("repo") or {}).get("full_name")) == repo


def _latest_check(runs, name):
    """The run with the highest integer id."""
    matched = [r for r in runs if isinstance(r, dict) and r.get("name") == name]
    if not matched:
        return None
    matched.sort(key=lambda r: int(r.get("id") or 0))
    return matched[-1]


def _checks_green(runs):
    for name in ("test", "review"):
        latest = _latest_check(runs, name)
        if latest is None:
            return False
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            return False
    return True


def _fetch_check_runs(repo, head_sha):
    """GET check runs page by page. Keep going while a page is full (100)."""
    page = 1
    found = []
    while True:
        batch = gh_get(
            f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100&page={page}"
        )
        if not isinstance(batch, dict):
            raise RuntimeError("expected check-runs object")
        runs = batch.get("check_runs") or []
        if not isinstance(runs, list):
            raise RuntimeError("expected check_runs list")
        found.extend(runs)
        if len(runs) != 100:
            break
        page += 1
    return found


_VERDICT_LABEL = re.compile(r"(?i)^verdict:\s*")
_CHECK_FOR_WORKFLOW = {"Tests": "test", "Claude Review": "review"}


def reed_verdict(body):
    """WAIT or CHANGES NEEDED from the first non-empty line after the marker.

    ``**WAIT**`` and ``Verdict: WAIT...`` both return ``WAIT``. The same two
    shapes apply to CHANGES NEEDED. Later lines are ignored.
    """
    text = body if isinstance(body, str) else ""
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    found = ""
    for line in text.split("\n")[1:]:
        if line.strip():
            found = line
            break
    if not found:
        return None
    cleaned = _VERDICT_LABEL.sub("", found.replace("**", "")).strip()
    for kind in ("CHANGES NEEDED", "WAIT"):
        if cleaned == kind or (
            cleaned.startswith(kind) and not cleaned[len(kind)].isalnum()
        ):
            return kind
    return None


def _this_check_finished_last(runs, workflow_name):
    """True when this workflow's latest check finished after the other.

    Compared only between the two latest runs. A tied ``completed_at``
    goes to the higher id.
    """
    ours_name = _CHECK_FOR_WORKFLOW.get((workflow_name or "").strip())
    if ours_name is None:
        return False
    ours = _latest_check(runs, ours_name)
    other_name = "review" if ours_name == "test" else "test"
    other = _latest_check(runs, other_name)
    if ours is None or other is None:
        return False

    def _key(r):
        return (r.get("completed_at") or "", int(r.get("id") or 0))

    return _key(ours) > _key(other)


def retrigger_reed():
    try:
        return _retrigger_reed()
    except RuntimeError as exc:
        print(f"retrigger-reed: {exc}", file=sys.stderr)
        return 1


def _retrigger_reed():
    repo = require_repo()
    if repo is None:
        return 1
    url = os.environ.get("REED_QA_WEBHOOK_URL", "").strip()
    key = os.environ.get("REED_QA_WEBHOOK_KEY", "").strip()
    if not url or not key:
        print("retrigger-reed: skip (webhook not configured)")
        return 0
    event = os.environ.get("EVENT_NAME", "").strip()
    if event == "pull_request_review":
        number = require_pr()
        if number is None:
            return 1
        pr = gh_get(f"repos/{repo}/pulls/{number}")
        if not pr_is_guarded(pr, repo) or pr.get("draft"):
            print("retrigger-reed: skip (no matching PR)")
            return 0
        head_sha = head_sha_of(pr)
        matched = [pr]
        enforce_order = False
    else:
        head_sha = _require_sha()
        if head_sha is None:
            return 1
        matched = []
        for pr in paginate_list(f"repos/{repo}/pulls?state=open"):
            if not pr_is_guarded(pr, repo) or pr.get("draft"):
                continue
            if head_sha_of(pr) != head_sha:
                continue
            matched.append(pr)
        if not matched:
            print("retrigger-reed: skip (no matching PR)")
            return 0
        enforce_order = True
    runs = _fetch_check_runs(repo, head_sha)
    if not _checks_green(runs):
        print("retrigger-reed: skip (checks not green)")
        return 0
    if enforce_order and not _this_check_finished_last(
        runs, os.environ.get("WORKFLOW_NAME", "")
    ):
        print("retrigger-reed: skip (other check finished last)")
        return 0
    rc = 0
    for pr in matched:
        number = int(pr["number"])
        reviews = paginate_list(f"repos/{repo}/pulls/{number}/reviews")
        reed_reviews = _matching_reviews(reviews, head_sha, REED_AUTOMATION_ID)
        if any(rev.get("state") == "APPROVED" for rev in reed_reviews):
            print(f"retrigger-reed: skip #{number} (already approved)")
            continue
        if len(reed_reviews) != 1:
            print(f"retrigger-reed: skip #{number} (review count {len(reed_reviews)})")
            continue
        rev = reed_reviews[0]
        body = rev.get("body") if isinstance(rev.get("body"), str) else ""
        if rev.get("state") != "COMMENTED" or reed_verdict(body) != "WAIT":
            print(f"retrigger-reed: skip #{number} (not a WAIT review)")
            continue
        one = _post_webhook(
            "reed",
            url,
            key,
            {"pr": number, "head_sha": head_sha},
            skip_if_unset=True,
        )
        if one != 0:
            rc = one
    return rc


def notify_selina():
    try:
        return _notify_selina()
    except RuntimeError as exc:
        print(f"notify-selina: {exc}", file=sys.stderr)
        return 1


def _notify_selina():
    repo = require_repo()
    if repo is None:
        return 1
    url = os.environ.get("SELINA_QA_WEBHOOK_URL", "").strip()
    key = os.environ.get("SELINA_QA_WEBHOOK_KEY", "").strip()
    if not url or not key:
        print("notify-selina: skip (webhook not configured)")
        return 0
    event = os.environ.get("EVENT_NAME", "").strip()
    if event == "pull_request_review":
        return _notify_selina_review(repo, url, key)
    if event == "workflow_run":
        return _notify_selina_workflow(repo, url, key)
    if event == "pull_request_target":
        return _notify_selina_merged(repo, url, key)
    print("notify-selina: skip (event)")
    return 0


def _open_same_repo(pr, repo):
    return isinstance(pr, dict) and pr.get("state") == "open" and _same_repo(pr, repo)


def _has_reed_or_sage(body):
    return (
        body_has_automation(body, REED_AUTOMATION_ID)
        or body_has_automation(body, SAGE_AUTOMATION_ID)
    )


def _notify_selina_review(repo, url, key):
    number = require_pr()
    review_id = require_positive_int("REVIEW_ID")
    if number is None or review_id is None:
        return 1
    pr = gh_get(f"repos/{repo}/pulls/{number}")
    if not isinstance(pr, dict) or pr.get("state") != "open" or not _same_repo(pr, repo):
        print("notify-selina: skip (pr not open same-repo)")
        return 0
    head_sha = head_sha_of(pr)
    if not _SHA.fullmatch(head_sha):
        print("notify-selina: skip (bad head sha)")
        return 0
    review = gh_get(f"repos/{repo}/pulls/{number}/reviews/{review_id}")
    if not isinstance(review, dict):
        print("notify-selina: skip (review)")
        return 0
    login = ((review.get("user") or {}).get("login")) or ""
    if login != "cursor[bot]" or not _has_reed_or_sage(review.get("body")):
        print("notify-selina: skip (not a reed/sage review)")
        return 0
    review_state = review.get("state") or ""
    review_sha = review.get("commit_id") or ""
    if review_sha != head_sha:
        print("notify-selina: skip (stale review)")
        return 0
    base_ref = (pr.get("base") or {}).get("ref")
    if review_state == "APPROVED" and not pr.get("draft") and base_ref == "master":
        print("notify-selina: skip (ready master approval)")
        return 0
    return _post_webhook(
        "selina",
        url,
        key,
        {
            "event": "cursor_review",
            "pr": number,
            "head_sha": head_sha,
            "review_id": review_id,
            "review_state": review_state,
        },
        skip_if_unset=True,
    )


def _notify_selina_workflow(repo, url, key):
    head_sha = _require_sha()
    if head_sha is None:
        return 1
    workflow_name = os.environ.get("WORKFLOW_NAME", "").strip()
    conclusion = os.environ.get("WORKFLOW_CONCLUSION", "").strip()
    rc = 0
    for pr in paginate_list(f"repos/{repo}/pulls?state=open"):
        if not _open_same_repo(pr, repo) or head_sha_of(pr) != head_sha:
            continue
        number = int(pr["number"])
        if conclusion == "failure":
            unlabeled_draft = (
                workflow_name == "Claude Review"
                and pr.get("draft")
                and "claude-review" not in label_names(pr)
            )
            if unlabeled_draft:
                print(f"notify-selina: skip #{number} (unlabeled draft claude failure)")
                continue
            payload = {
                "event": "ci_failed",
                "workflow": workflow_name,
                "pr": number,
                "head_sha": head_sha,
            }
        elif (
            conclusion == "success"
            and workflow_name == "Claude Review"
            and pr.get("draft")
            and (pr.get("base") or {}).get("ref") == "master"
        ):
            payload = {
                "event": "claude_passed_draft",
                "pr": number,
                "head_sha": head_sha,
            }
        else:
            continue
        one = _post_webhook("selina", url, key, payload, skip_if_unset=True)
        if one != 0:
            rc = one
    return rc


def _notify_selina_merged(repo, url, key):
    number = require_pr()
    if number is None:
        return 1
    pr = gh_get(f"repos/{repo}/pulls/{number}")
    if not isinstance(pr, dict) or not _same_repo(pr, repo) or pr.get("merged") is not True:
        print("notify-selina: skip (not a merged same-repo PR)")
        return 0
    merged_head_ref = ((pr.get("head") or {}).get("ref") or "").strip()
    if not merged_head_ref:
        print("notify-selina: skip (empty head ref)")
        return 0
    stacked = sorted(
        int(item["number"])
        for item in paginate_list(f"repos/{repo}/pulls?state=open")
        if _open_same_repo(item, repo)
        and (item.get("base") or {}).get("ref") == merged_head_ref
    )
    if not stacked:
        print("notify-selina: skip (no stacked PRs)")
        return 0
    return _post_webhook(
        "selina",
        url,
        key,
        {
            "event": "base_pr_merged",
            "merged_pr": number,
            "merged_head_ref": merged_head_ref,
            "stacked_prs": stacked,
        },
        skip_if_unset=True,
    )


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "trigger-sage":
        return trigger_sage()
    if cmd == "gate":
        return gate()
    if cmd == "retrigger-reed":
        return retrigger_reed()
    if cmd == "notify-selina":
        return notify_selina()
    print(
        "usage: auto_merge_gate.py trigger-sage|gate|retrigger-reed|notify-selina",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

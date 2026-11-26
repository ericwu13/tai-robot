"""Gemini PR review glue: diff-hash reuse, context collection, native reviews.

    python .github/scripts/gemini_review_post.py reuse
        Compute diff_hash = sha256(merge-base(origin/$BASE, HEAD)..HEAD)[:16].
        If the previous Gemini review summary matches that diff hash, write
        reuse=true and verdict=PASS|BLOCK to $GITHUB_OUTPUT so Gemini CLI is
        skipped (no wasted tokens on no-op master merges).

    python .github/scripts/gemini_review_post.py collect OUT_JSON
        Write PR metadata, unified diff, changed files, open Gemini threads
        (with author replies), other reviewers' open threads (to avoid
        duplicate nits), and previous Gemini summary into OUT_JSON.

    python .github/scripts/gemini_review_post.py apply RESULT_FILE
        Validate Gemini CLI's structured JSON output (fail-closed consistency:
        any P1/P2, inline finding, or kept open thread forces BLOCK), submit a
        native GitHub App PR review (`APPROVE` on PASS, `REQUEST_CHANGES` on
        BLOCK when APP_SLUG is set; `COMMENT`-only fallback under github.token),
        upsert a single edit-in-place summary comment, and resolve Gemini's own
        threads that were fixed.

    python .github/scripts/gemini_review_post.py carry-over
        Carry over an unchanged-diff verdict onto the new HEAD_SHA: update the
        summary comment note and (when APP_SLUG is set) submit APPROVE /
        REQUEST_CHANGES at the new commit SHA.

Env: GITHUB_REPOSITORY, PR, HEAD_SHA, GH_TOKEN (+ BASE, DIFF_HASH, APP_SLUG,
GEMINI_MODEL, RUN_URL, REUSED_VERDICT). Stdlib and `gh`/`git` only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

STATE_MARKER_RE = re.compile(r"<!-- gemini-review-state: (\{.*?\}) -->")
CARRY_RE = re.compile(r"\n?<sub>Carried over to [^<]*</sub>")
THREAD_MARKER = "<!-- gemini-review-thread -->"
BOT_LOGINS = frozenset({"github-actions", "github-actions[bot]"})
MAX_DIFF_CHARS = 60_000
MAX_PR_BODY_CHARS = 8_000
MAX_SUMMARY_CHARS = 24_000

THREADS_QUERY = """
query($owner:String!, $name:String!, $pr:Int!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      reviewThreads(first:100, after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved path line
          comments(first:1) { nodes { author { login } body url } }
          replies: comments(last:10) { totalCount nodes { author { login } body } }
        }
      }
    }
  }
}
"""


def parse_pr_number(raw: str) -> int:
    text = str(raw or "").strip()
    if not re.fullmatch(r"[1-9]\d*", text):
        raise ValueError(f"invalid PR number: {raw!r}")
    return int(text)


def bot_logins(app_slug: str | None = None) -> frozenset[str]:
    slug = (os.environ.get("APP_SLUG", "") if app_slug is None else str(app_slug or "")).strip()
    logins = set(BOT_LOGINS)
    if slug:
        base = slug[:-5] if slug.endswith("[bot]") else slug
        logins.update({base, f"{base}[bot]"})
    return frozenset(logins)


def actor_logins(app_slug: str | None = None) -> frozenset[str]:
    slug = (os.environ.get("APP_SLUG", "") if app_slug is None else str(app_slug or "")).strip()
    if slug:
        base = slug[:-5] if slug.endswith("[bot]") else slug
        return frozenset({base, f"{base}[bot]"})
    return BOT_LOGINS


def gh_api(path: str, *, method: str = "GET", payload: Any = None) -> tuple[bool, Any]:
    cmd = ["gh", "api", "--method", method, path]
    inp = None
    if payload is not None:
        cmd.extend(["--input", "-"])
        inp = json.dumps(payload)
    proc = subprocess.run(
        cmd, input=inp, text=True, capture_output=True, encoding="utf-8",
    )
    ok = proc.returncode == 0
    raw = (proc.stdout if ok else (proc.stderr or proc.stdout) or "").strip()
    if not ok:
        return False, raw[:500]
    if not raw:
        return True, None
    try:
        return True, json.loads(raw)
    except ValueError:
        return True, raw


def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    ok, data = gh_api("graphql", method="POST", payload={"query": query, "variables": variables})
    if not ok or not isinstance(data, dict):
        raise RuntimeError(str(data))
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"])[:500])
    return data["data"]


def _is_gemini_thread(login: str, first_body: str, app_slug: str | None = None) -> bool:
    slug = (os.environ.get("APP_SLUG", "") if app_slug is None else str(app_slug or "")).strip()
    if slug:
        base = slug[:-5] if slug.endswith("[bot]") else slug
        if login in {base, f"{base}[bot]"}:
            return True
        return login in BOT_LOGINS and THREAD_MARKER in (first_body or "")
    # Read-only review job has no App token/slug: recognize any bot thread
    # stamped with THREAD_MARKER (excludes humans, Claude, and Reed).
    is_bot = login in BOT_LOGINS or login.endswith("[bot]")
    return is_bot and THREAD_MARKER in (first_body or "")


def list_review_threads(repo: str, pr: int, *, app_slug: str | None = None) -> tuple[list[dict], list[dict]]:
    """Return ``(gemini_open_threads, other_open_threads)``."""
    owner, name = repo.split("/", 1)
    known = bot_logins(app_slug)
    gemini_threads: list[dict] = []
    other_threads: list[dict] = []
    after = None
    while True:
        conn = graphql(
            THREADS_QUERY,
            {"owner": owner, "name": name, "pr": int(pr), "after": after},
        )["repository"]["pullRequest"]["reviewThreads"]
        for t in conn["nodes"]:
            if t.get("isResolved"):
                continue
            first = (t["comments"]["nodes"] or [{}])[0]
            login = str((first.get("author") or {}).get("login") or "")
            body = str(first.get("body") or "")
            if _is_gemini_thread(login, body, app_slug):
                replies_nodes = (t.get("replies") or {}).get("nodes") or []
                replies = [
                    {
                        "author": str((n.get("author") or {}).get("login") or ""),
                        "body": str(n.get("body") or "")[:1500],
                    }
                    for n in replies_nodes
                    if str((n.get("author") or {}).get("login") or "") not in (known | {login})
                ][-3:]
                gemini_threads.append({
                    "id": t["id"],
                    "path": t.get("path"),
                    "line": t.get("line"),
                    "body": body.replace(THREAD_MARKER, "").strip()[:1200],
                    "replies": replies,
                })
            else:
                other_threads.append({
                    "author": login,
                    "path": t.get("path"),
                    "line": t.get("line"),
                    "body": body[:400],
                })
        if not conn["pageInfo"]["hasNextPage"]:
            return gemini_threads, other_threads
        after = conn["pageInfo"]["endCursor"]


def latest_summary_with_author(repo: str, pr: int, *, app_slug: str | None = None) -> tuple[int | None, str, str]:
    """Return ``(comment_id, body, author_login)`` of the newest Gemini summary."""
    ok, comments = gh_api(f"repos/{repo}/issues/{pr}/comments?per_page=100")
    if not ok or not isinstance(comments, list):
        return None, "", ""
    slug = (os.environ.get("APP_SLUG", "") if app_slug is None else str(app_slug or "")).strip()
    known = bot_logins(slug)
    last = (None, "", "")
    for c in comments:
        if not isinstance(c, dict):
            continue
        login = str((c.get("user") or {}).get("login") or "")
        body = str(c.get("body") or "")
        allowed_author = (login in known) if slug else (login in BOT_LOGINS or login.endswith("[bot]"))
        if allowed_author and STATE_MARKER_RE.search(body):
            cid = c.get("id")
            if isinstance(cid, int):
                last = (cid, body, login)
    return last


def summary_state(body: str) -> dict[str, Any]:
    m = STATE_MARKER_RE.search(body or "")
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(1))
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def diff_hash(base: str) -> tuple[str, str]:
    """Return ``(merge_base_sha, diff_hash_16)`` for ``merge-base(origin/base, HEAD)..HEAD``."""
    mb = subprocess.run(
        ["git", "merge-base", f"origin/{base}", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not mb:
        return "", ""
    diff_bytes = subprocess.run(
        ["git", "diff", "--no-color", mb, "HEAD"],
        capture_output=True,
    ).stdout
    return mb, hashlib.sha256(diff_bytes).hexdigest()[:16]


def run_reuse(repo: str, pr: int, base: str) -> dict[str, str]:
    _mb, h = diff_hash(base)
    _cid, body, _author = latest_summary_with_author(repo, pr)
    prev = summary_state(body)
    hit = bool(h) and prev.get("diff") == h and prev.get("verdict") in ("PASS", "BLOCK")
    verdict = str(prev.get("verdict") or "") if hit else ""
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(f"diff_hash={h}\n")
            f.write(f"reuse={'true' if hit else 'false'}\n")
            f.write(f"verdict={verdict}\n")
    if hit:
        print(f"diff {h} unchanged since {prev.get('head')}: carrying over {verdict}")
    else:
        print(f"diff {h or '?'} differs from last Gemini review ({prev.get('diff') or 'none'}): reviewing")
    return {"diff_hash": h, "reuse": "true" if hit else "false", "verdict": verdict}


def collect_context(repo: str, pr: int, base: str, head_sha: str, out_path: str | os.PathLike) -> dict[str, Any]:
    ok, pr_data = gh_api(f"repos/{repo}/pulls/{pr}")
    if not ok or not isinstance(pr_data, dict):
        raise RuntimeError(f"could not fetch PR #{pr}: {pr_data}")

    mb, _h = diff_hash(base)
    diff_ref = f"{mb}..HEAD" if mb else f"origin/{base}...HEAD"
    changed_files = [
        line.strip()
        for line in subprocess.run(
            ["git", "diff", "--name-only", diff_ref],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.splitlines()
        if line.strip()
    ]
    full_diff = subprocess.run(
        ["git", "diff", "--no-color", diff_ref],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    diff_truncated = len(full_diff) > MAX_DIFF_CHARS
    diff_excerpt = full_diff[:MAX_DIFF_CHARS]

    try:
        gemini_threads, other_threads = list_review_threads(repo, pr)
    except RuntimeError as exc:
        print(f"could not list review threads: {exc}")
        gemini_threads, other_threads = [], []

    _cid, prev_body, _author = latest_summary_with_author(repo, pr)
    prev_summary = STATE_MARKER_RE.sub("", prev_body).strip()[:6000] if prev_body else ""

    ctx = {
        "repository": repo,
        "pr": {
            "number": pr,
            "title": str(pr_data.get("title") or "")[:300],
            "body": str(pr_data.get("body") or "")[:MAX_PR_BODY_CHARS],
            "base_ref": base,
            "head_sha": head_sha,
            "head_short": head_sha[:7],
        },
        "changed_files": changed_files,
        "diff_truncated": diff_truncated,
        "diff": diff_excerpt,
        "is_rereview": bool(prev_summary),
        "previous_summary": prev_summary,
        "open_gemini_threads": gemini_threads,
        "other_open_threads": other_threads[:30],
    }
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"collected PR #{pr}: {len(changed_files)} file(s), "
        f"{len(gemini_threads)} open Gemini thread(s), "
        f"{len(other_threads)} other open thread(s)"
    )
    return ctx


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("empty Gemini review output")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    fenced = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.S | re.I)
    for block in reversed(fenced):
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    for idx in range(len(text) - 1, -1, -1):
        if text[idx] != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict) and ("verdict" in obj or "summary" in obj):
                return obj
        except ValueError:
            continue
    raise ValueError("no valid Gemini review JSON object found")


def normalize_review_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and enforce fail-closed verdict consistency."""
    def _nonneg_int(val: Any) -> int:
        if isinstance(val, bool):
            return 0
        try:
            return max(0, int(val))
        except (TypeError, ValueError):
            return 0

    p1 = _nonneg_int(raw.get("p1", 0))
    p2 = _nonneg_int(raw.get("p2", 0))
    p3 = _nonneg_int(raw.get("p3", 0))

    inline_comments: list[dict[str, Any]] = []
    for c in raw.get("inline_comments") or []:
        if not isinstance(c, dict):
            continue
        path = str(c.get("path") or "").strip()
        body = str(c.get("body") or "").strip()
        try:
            line = int(c.get("line"))
        except (TypeError, ValueError):
            continue
        if not path or line < 1 or not body:
            continue
        item: dict[str, Any] = {"path": path, "line": line, "body": body}
        if c.get("start_line") is not None:
            try:
                sl = int(c["start_line"])
                if 1 <= sl < line:
                    item["start_line"] = sl
            except (TypeError, ValueError):
                pass
        inline_comments.append(item)

    still_open = [str(i) for i in (raw.get("still_open_threads") or []) if str(i).strip()]
    raw_verdict = str(raw.get("verdict") or "").strip().upper()
    if raw_verdict not in ("PASS", "BLOCK"):
        raise ValueError(f"invalid verdict: {raw_verdict!r}")

    # Fail-closed consistency: any P1/P2 count, inline finding, or kept open thread forces BLOCK.
    if p1 + p2 > 0 or inline_comments or still_open or raw_verdict == "BLOCK":
        verdict = "BLOCK"
    else:
        verdict = "PASS"

    summary = str(raw.get("summary") or "").strip()
    if not summary:
        raise ValueError("review JSON missing non-empty summary")

    return {
        "verdict": verdict,
        "p1": p1,
        "p2": p2,
        "p3": p3,
        "summary": summary,
        "inline_comments": inline_comments,
        "still_open_threads": still_open,
    }


def _to_review_comment(c: dict[str, Any]) -> dict[str, Any]:
    body = c["body"]
    if THREAD_MARKER not in body:
        body = f"{body}\n\n{THREAD_MARKER}"
    out: dict[str, Any] = {"path": c["path"], "line": int(c["line"]), "side": "RIGHT", "body": body}
    if c.get("start_line") and int(c["start_line"]) < int(c["line"]):
        out["start_line"] = int(c["start_line"])
        out["start_side"] = "RIGHT"
    return out


def post_inline_comments(repo: str, pr: int, head_sha: str, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not comments:
        return []
    path = f"repos/{repo}/pulls/{pr}/reviews"
    ok, err = gh_api(
        path,
        method="POST",
        payload={
            "commit_id": head_sha,
            "event": "COMMENT",
            "body": "Gemini review: inline findings (see summary comment).",
            "comments": [_to_review_comment(c) for c in comments],
        },
    )
    if ok:
        return []
    print(f"inline batch rejected ({err}); retrying individually")
    failed: list[dict[str, Any]] = []
    for c in comments:
        ok1, err1 = gh_api(
            path,
            method="POST",
            payload={
                "commit_id": head_sha,
                "event": "COMMENT",
                "body": "",
                "comments": [_to_review_comment(c)],
            },
        )
        if not ok1:
            print(f"could not post inline at {c['path']}:{c['line']}: {err1}")
            failed.append(c)
    return failed


def submit_review(
    repo: str,
    pr: int,
    head_sha: str,
    verdict: str,
    comments: list[dict[str, Any]],
    *,
    carried_over: bool = False,
    app_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Submit native APPROVE / REQUEST_CHANGES when APP_SLUG is set, else COMMENT-only."""
    slug = (os.environ.get("APP_SLUG", "") if app_slug is None else str(app_slug or "")).strip()
    if not slug:
        return post_inline_comments(repo, pr, head_sha, comments)

    event = "APPROVE" if verdict == "PASS" else "REQUEST_CHANGES"
    path = f"repos/{repo}/pulls/{pr}/reviews"
    if verdict == "PASS":
        suffix = " (carried over: diff unchanged)" if carried_over else ""
        review_body = f"Gemini review @ `{head_sha[:7]}`: **PASS**{suffix} — see summary comment."
    else:
        review_body = (
            f"Gemini review @ `{head_sha[:7]}`: **BLOCK** — "
            + ("inline P1/P2 findings attached (see summary comment)."
               if comments else "unresolved P1/P2 findings remain (see summary comment).")
        )

    payload: dict[str, Any] = {"commit_id": head_sha, "event": event, "body": review_body}
    if comments:
        payload["comments"] = [_to_review_comment(c) for c in comments]

    ok, err = gh_api(path, method="POST", payload=payload)
    if ok:
        print(f"submitted {event} review with {len(comments)} inline comment(s)")
        return []

    print(f"{event} review rejected ({err}); retrying inline comments and decision separately")
    failed = post_inline_comments(repo, pr, head_sha, comments) if comments else []
    ok2, err2 = gh_api(
        path,
        method="POST",
        payload={"commit_id": head_sha, "event": event, "body": review_body},
    )
    if not ok2:
        print(f"::warning::could not submit {event} review: {err2}")
    return failed


def resolve_gemini_threads(
    repo: str,
    pr: int,
    head_sha: str,
    shown_ids: list[str],
    still_open: list[str],
    *,
    app_slug: str | None = None,
) -> tuple[int, int]:
    if not shown_ids:
        return 0, 0
    try:
        gemini_threads, _ = list_review_threads(repo, pr, app_slug=app_slug)
    except RuntimeError as exc:
        print(f"could not re-check Gemini threads for resolution: {exc}")
        return 0, 0
    open_ids = {str(t["id"]) for t in gemini_threads}
    keep = set(still_open)
    todo = [tid for tid in shown_ids if tid in open_ids and tid not in keep]
    done = failed = 0
    for tid in todo:
        try:
            graphql(
                "mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}",
                {"t": tid},
            )
        except RuntimeError as exc:
            failed += 1
            print(f"::warning::could not resolve {tid}: {exc}")
            continue
        done += 1
        try:
            graphql(
                "mutation($t:ID!,$b:String!){addPullRequestReviewThreadReply("
                "input:{pullRequestReviewThreadId:$t, body:$b}){comment{id}}}",
                {
                    "t": tid,
                    "b": f"Not raised by Gemini review at `{head_sha[:7]}` "
                         "(fixed, superseded, or not a P1/P2). Resolved.",
                },
            )
        except RuntimeError as exc:
            print(f"resolved {tid} but could not reply: {exc}")
    return done, failed


def upsert_summary(
    repo: str,
    pr: int,
    body: str,
    comment_id: int | None,
    comment_author: str,
    *,
    app_slug: str | None = None,
) -> str:
    if comment_id is not None and comment_author in actor_logins(app_slug):
        ok, _ = gh_api(
            f"repos/{repo}/issues/comments/{comment_id}",
            method="PATCH",
            payload={"body": body},
        )
        if ok:
            return "updated"
    ok, err = gh_api(
        f"repos/{repo}/issues/{pr}/comments",
        method="POST",
        payload={"body": body},
    )
    if not ok:
        raise RuntimeError(f"failed to post Gemini summary comment: {err}")
    return "created"


def apply_review(
    repo: str,
    pr: int,
    head_sha: str,
    result_path: str | os.PathLike,
    context_path: str | os.PathLike,
    *,
    diff_h: str = "",
    model_name: str = "",
    run_url: str = "",
    app_slug: str | None = None,
) -> dict[str, Any]:
    raw_text = Path(result_path).read_text(encoding="utf-8")
    parsed = normalize_review_result(extract_json_object(raw_text))

    shown_ids: list[str] = []
    if context_path and Path(context_path).is_file():
        try:
            ctx = json.loads(Path(context_path).read_text(encoding="utf-8"))
            shown_ids = [
                str(t["id"])
                for t in (ctx.get("open_gemini_threads") or [])
                if isinstance(t, dict) and t.get("id")
            ]
        except (OSError, ValueError, TypeError):
            shown_ids = []

    resolved, _res_failed = resolve_gemini_threads(
        repo, pr, head_sha, shown_ids, parsed["still_open_threads"], app_slug=app_slug,
    )
    comments = parsed["inline_comments"]
    failed = submit_review(
        repo, pr, head_sha, parsed["verdict"], comments, app_slug=app_slug,
    )

    body = STATE_MARKER_RE.sub("", parsed["summary"]).strip()
    if not body.lstrip().startswith("## Gemini review"):
        body = f"## Gemini review @ {head_sha[:7]}\n\n{body}"
    if failed:
        body += "\n\n### Findings that could not be posted inline\n"
        for c in failed:
            body += f"\n**`{c['path']}:{c['line']}`**\n\n{c['body']}\n"

    state = {"diff": diff_h, "verdict": parsed["verdict"], "head": head_sha[:12]}
    footer_bits = [
        f"Verdict: **{parsed['verdict']}**",
        f"P1 {parsed['p1']} | P2 {parsed['p2']} | P3 {parsed['p3']}",
        f"head `{head_sha[:7]}`",
        f"inline {len(comments) - len(failed)}/{len(comments)}",
        f"threads resolved {resolved}, kept {len(parsed['still_open_threads'])}",
    ]
    if model_name:
        footer_bits.append(f"model `{model_name}`")
    if run_url:
        footer_bits.append(f"[run]({run_url})")
    footer = f"\n\n<sub>{' | '.join(footer_bits)}</sub>\n<!-- gemini-review-state: {json.dumps(state)} -->"

    budget = MAX_SUMMARY_CHARS - len(footer)
    if len(body) > budget:
        body = body[: max(0, budget - 20)].rstrip() + "\n\n*(truncated)*"
    full_body = body + footer

    cid, _prev_body, prev_author = latest_summary_with_author(repo, pr, app_slug=app_slug)
    action = upsert_summary(repo, pr, full_body, cid, prev_author, app_slug=app_slug)
    print(f"Gemini review {action} on PR #{pr}: verdict={parsed['verdict']} inline={len(comments) - len(failed)}/{len(comments)}")
    return {"verdict": parsed["verdict"], "action": action, "resolved": resolved, "failed_inline": len(failed)}


def carry_over(repo: str, pr: int, head_sha: str, verdict: str, *, app_slug: str | None = None) -> None:
    cid, body, author = latest_summary_with_author(repo, pr, app_slug=app_slug)
    if cid is not None and body and author in actor_logins(app_slug):
        note = (
            f"<sub>Carried over to `{head_sha[:7]}`: the PR's own diff is unchanged "
            "(e.g. a merge from master), so it was not re-reviewed.</sub>"
        )
        new_body = CARRY_RE.sub("", body).rstrip() + "\n" + note
        gh_api(f"repos/{repo}/issues/comments/{cid}", method="PATCH", payload={"body": new_body})
    submit_review(repo, pr, head_sha, verdict, [], carried_over=True, app_slug=app_slug)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: gemini_review_post.py <reuse|collect|apply|carry-over> ...", file=sys.stderr)
        return 2
    mode = args[0]
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo or "/" not in repo:
        print("GITHUB_REPOSITORY must be set to owner/repo", file=sys.stderr)
        return 2
    pr = parse_pr_number(os.environ.get("PR", ""))
    head_sha = os.environ.get("HEAD_SHA", "").strip()
    base = os.environ.get("BASE", "master").strip()

    if mode == "reuse":
        run_reuse(repo, pr, base)
    elif mode == "collect" and len(args) == 2:
        collect_context(repo, pr, base, head_sha, args[1])
    elif mode == "apply" and len(args) == 3:
        apply_review(
            repo,
            pr,
            head_sha,
            args[1],
            args[2],
            diff_h=os.environ.get("DIFF_HASH", "").strip(),
            model_name=os.environ.get("GEMINI_MODEL", "").strip(),
            run_url=os.environ.get("RUN_URL", "").strip(),
        )
    elif mode == "carry-over":
        verdict = os.environ.get("REUSED_VERDICT", "").strip().upper()
        if verdict in ("PASS", "BLOCK") and head_sha:
            carry_over(repo, pr, head_sha, verdict)
    else:
        print(f"unknown mode/args: {args!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

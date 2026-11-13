"""Claude review glue: collect open Claude threads, then post the result.

    python claude_review_post.py collect OUT_JSON
        Write the PR's unresolved review threads that THIS workflow opened
        (first comment by the job token's bot) to OUT_JSON, so Claude can
        decide which ones the new head fixes.

    python claude_review_post.py post
        Read the action's structured_output JSON from $RESULT and:
        1. resolve every `resolved_threads` entry that is still an open thread
           opened by this workflow (anything else is ignored: Claude can never
           resolve a human's or Cursor's thread), replying "Fixed in <sha>";
        2. post every `inline_comments` entry as a line comment, all in one PR
           review (one resolvable thread each). If GitHub rejects the batch
           (422: a line not in the diff), each comment is retried alone and
           the rejected ones are listed in the summary instead;
        3. post `summary` (plus a footer) as a PR comment.

Env: PR, HEAD_SHA, GITHUB_REPOSITORY, GH_TOKEN (+ RESULT for post). Uses gh.
"""
import json
import os
import subprocess
import sys

BOT_LOGINS = {"github-actions", "github-actions[bot]"}

THREADS_QUERY = """
query($owner:String!, $name:String!, $pr:Int!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      reviewThreads(first:100, after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved path line
          comments(first:1) { nodes { author { login } body url } }
        }
      }
    }
  }
}
"""


def gh_api(path, payload):
    proc = subprocess.run(
        ["gh", "api", "--method", "POST", path, "--input", "-"],
        input=json.dumps(payload), text=True, capture_output=True, encoding="utf-8")
    ok = proc.returncode == 0
    # On success the payload is stdout; gh may still print warnings to stderr.
    return ok, ((proc.stdout if ok else (proc.stderr or proc.stdout)) or "").strip()


def graphql(query, variables):
    ok, out = gh_api("graphql", {"query": query, "variables": variables})
    if not ok:
        raise RuntimeError(out)
    data = json.loads(out)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"])[:500])
    return data["data"]


def open_bot_threads(repo, pr):
    """Unresolved threads whose first comment was written by this workflow."""
    owner, name = repo.split("/", 1)
    threads, after = [], None
    while True:
        conn = graphql(THREADS_QUERY, {"owner": owner, "name": name, "pr": int(pr),
                                       "after": after})["repository"]["pullRequest"]["reviewThreads"]
        for t in conn["nodes"]:
            first = (t["comments"]["nodes"] or [{}])[0]
            login = ((first.get("author") or {}).get("login") or "")
            if not t["isResolved"] and login in BOT_LOGINS:
                threads.append({"id": t["id"], "path": t["path"], "line": t["line"],
                                "body": first.get("body", ""), "url": first.get("url", "")})
        if not conn["pageInfo"]["hasNextPage"]:
            return threads
        after = conn["pageInfo"]["endCursor"]


def resolve_threads(repo, pr, head_sha, requested):
    """Resolve requested threads, but only open ones this workflow opened."""
    if not requested:
        return 0
    allowed = {t["id"] for t in open_bot_threads(repo, pr)}
    done = 0
    for item in requested:
        tid = str(item.get("id") or "")
        if tid not in allowed:
            print(f"skip resolve {tid!r}: not an open thread opened by this workflow")
            continue
        reason = str(item.get("reason") or "").strip() or "the finding no longer applies"
        try:
            graphql("mutation($t:ID!,$b:String!){addPullRequestReviewThreadReply("
                    "input:{pullRequestReviewThreadId:$t, body:$b}){comment{id}}}",
                    {"t": tid, "b": f"Fixed in `{head_sha[:7]}`: {reason}"})
            graphql("mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}",
                    {"t": tid})
            done += 1
        except RuntimeError as exc:
            print(f"could not resolve {tid}: {exc}")
    print(f"resolved {done}/{len(requested)} thread(s)")
    return done


def to_review_comment(c):
    out = {"path": c["path"], "line": int(c["line"]), "side": "RIGHT", "body": c["body"]}
    start = c.get("start_line")
    if start and int(start) < int(c["line"]):
        out["start_line"] = int(start)
        out["start_side"] = "RIGHT"
    return out


def post_inline(repo, pr, head_sha, comments):
    """Return the comments that could not be posted inline."""
    if not comments:
        return []
    path = f"repos/{repo}/pulls/{pr}/reviews"
    review = {"commit_id": head_sha, "event": "COMMENT",
              "body": "Claude review: inline findings (see the summary comment).",
              "comments": [to_review_comment(c) for c in comments]}
    ok, err = gh_api(path, review)
    if ok:
        print(f"posted {len(comments)} inline comment(s) in one review")
        return []
    print(f"batch review rejected, retrying one by one: {err}")
    failed = []
    for c in comments:
        ok, err = gh_api(path, {"commit_id": head_sha, "event": "COMMENT", "body": "",
                                "comments": [to_review_comment(c)]})
        if not ok:
            print(f"could not post inline at {c.get('path')}:{c.get('line')}: {err}")
            failed.append(c)
    return failed


def collect(out_path):
    repo, pr = os.environ["GITHUB_REPOSITORY"], os.environ["PR"]
    try:
        threads = open_bot_threads(repo, pr)
    except RuntimeError as exc:
        print(f"could not list review threads: {exc}")
        threads = []
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(threads, f, ensure_ascii=False, indent=1)
    print(f"{len(threads)} open Claude thread(s) -> {out_path}")


def post():
    r = json.loads(os.environ["RESULT"])
    repo, pr, head = os.environ["GITHUB_REPOSITORY"], os.environ["PR"], os.environ["HEAD_SHA"]

    requested = [t for t in (r.get("resolved_threads") or []) if isinstance(t, dict)]
    try:
        resolved = resolve_threads(repo, pr, head, requested)
    except RuntimeError as exc:
        print(f"could not resolve threads: {exc}")
        resolved = 0

    comments = [c for c in (r.get("inline_comments") or [])
                if c.get("path") and c.get("line") and c.get("body")]
    failed = post_inline(repo, pr, head, comments)

    body = (r.get("summary") or "").strip() or "## Claude review\n\n(no summary returned)"
    if failed:
        body += "\n\n### Findings that could not be posted inline\n"
        for c in failed:
            body += f"\n**`{c['path']}:{c['line']}`**\n\n{c['body']}\n"
    body += ("\n\n<sub>Verdict: **%s** | P1 %s | P2 %s | P3 %s | inline %d/%d | "
             "resolved %d/%d | model claude-opus-5-5</sub>"
             % (r.get("verdict"), r.get("p1"), r.get("p2"), r.get("p3"),
                len(comments) - len(failed), len(comments), resolved, len(requested)))
    with open("summary.md", "w", encoding="utf-8") as f:
        f.write(body)
    sys.exit(subprocess.run(["gh", "pr", "comment", pr, "--body-file", "summary.md"]).returncode)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    if mode == "collect":
        collect(sys.argv[2])
    else:
        post()

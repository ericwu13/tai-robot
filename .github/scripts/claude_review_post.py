"""Claude review glue: reuse check, collect open Claude threads, post.

    python claude_review_post.py reuse
        Hash the PR's own diff (merge-base of origin/$BASE and HEAD .. HEAD).
        If the previous Claude summary was written for the same diff hash, the
        review is carried over: write reuse=true and the old verdict to
        $GITHUB_OUTPUT and note the new head in that summary. Always writes
        diff_hash. A merge from master that does not change the PR's own diff
        therefore costs no new review.

    python claude_review_post.py collect OUT_DIR
        Write OUT_DIR/open_threads.json: the PR's unresolved review threads
        that THIS workflow opened (first comment by the job token's bot), each
        with its last replies, so Claude can see "Fixed in" / "Won't fix" /
        "Needs owner" answers. Write OUT_DIR/previous_summary.md when the PR
        already has a Claude summary (the review is then a re-review).

    python claude_review_post.py post OPEN_JSON
        Read the action's structured_output JSON from $RESULT and:
        1. pick every thread listed in OPEN_JSON (what Claude was shown) that
           is still open and was opened by this workflow, EXCEPT the ids in
           `still_open_threads`, and write them as `resolve_ids` to
           $GITHUB_OUTPUT. One rule: the current review is the source of
           truth, so a thread it does not re-confirm as P1/P2 is closed
           (fixed, superseded, or not worth blocking on). Never a human's or
           Cursor's thread. If OPEN_JSON is missing, nothing is picked.
           The `resolve-threads` job resolves them (it holds the token that
           may resolve threads);
        2. post every `inline_comments` entry (P1/P2) as a line comment, all
           in one PR review. If GitHub rejects the batch (422: a line not in
           the diff), each comment is retried alone and the rejected ones are
           listed in the summary instead;
        3. write `summary` (plus a footer and a hidden state marker: diff
           hash, verdict, BLOCK count) into the PR's previous Claude summary
           comment, or create one if there is none, so the PR keeps a single
           current summary instead of one per push. On the third BLOCK the PR
           gets the `needs-owner` label: further pushes wait for the owner.

    python claude_review_post.py resolve
        Resolve the thread ids in $IDS (JSON list), re-checking that each is
        still an open thread opened by this workflow. Reply "Not raised by
        the review at <sha>" ONLY after the resolve succeeded, so a token
        without the right permission adds no comments.

Env: PR, HEAD_SHA, GITHUB_REPOSITORY, GH_TOKEN (+ BASE for reuse, RESULT
and DIFF_HASH for post, IDS for resolve). Uses gh and git.
"""
import hashlib
import json
import os
import re
import subprocess
import sys

NEEDS_OWNER_LABEL = "needs-owner"
BLOCKS_BEFORE_OWNER = 3
MARKER_RE = re.compile(r"<!-- claude-review-state: (\{.*?\}) -->")
CARRY_RE = re.compile(r"\n?<sub>Carried over to [^<]*</sub>")

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
          replies: comments(last:3) { totalCount nodes { author { login } body } }
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
                rep_nodes = (t.get("replies") or {}).get("nodes") or []
                if (t.get("replies") or {}).get("totalCount", 0) <= len(rep_nodes):
                    rep_nodes = rep_nodes[1:]  # the first node is the finding itself
                replies = [{"author": (n.get("author") or {}).get("login", ""),
                            "body": (n.get("body") or "")[:1500]} for n in rep_nodes]
                threads.append({"id": t["id"], "path": t["path"], "line": t["line"],
                                "body": first.get("body", ""), "url": first.get("url", ""),
                                "replies": replies})
        if not conn["pageInfo"]["hasNextPage"]:
            return threads
        after = conn["pageInfo"]["endCursor"]


def threads_to_resolve(shown_ids, open_ids, still_open):
    """Threads Claude was shown that are still open and bot-opened, minus
    the ones it re-confirmed. Pure function."""
    keep = {str(i) for i in still_open}
    return [t for t in shown_ids if t in open_ids and t not in keep]


def resolve_ids(repo, pr, head_sha, ids):
    """Resolve, then reply. Returns (resolved, failed)."""
    open_ids = {t["id"] for t in open_bot_threads(repo, pr)}
    done = failed = 0
    for tid in ids:
        if tid not in open_ids:
            print(f"skip {tid}: not an open thread opened by this workflow")
            continue
        try:
            graphql("mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}",
                    {"t": tid})
        except RuntimeError as exc:
            failed += 1
            print(f"::warning::could not resolve {tid}: {exc}")
            continue
        done += 1
        try:
            graphql("mutation($t:ID!,$b:String!){addPullRequestReviewThreadReply("
                    "input:{pullRequestReviewThreadId:$t, body:$b}){comment{id}}}",
                    {"t": tid, "b": f"Not raised by the review at `{head_sha[:7]}` "
                                    "(fixed, superseded, or not a P1/P2). Resolved."})
        except RuntimeError as exc:
            print(f"resolved {tid} but could not reply: {exc}")
    print(f"resolved {done}/{len(ids)} thread(s), {failed} failed")
    return done, failed


SUMMARY_JQ = ('.[] | select((.user.login == "github-actions[bot]") and '
              '(.body | startswith("## Claude review"))) | {id, body}')


def latest_summary(repo, pr):
    """(comment_id, body) of the newest Claude summary, or (None, "")."""
    proc = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/issues/{pr}/comments", "--jq", SUMMARY_JQ],
        capture_output=True, text=True, encoding="utf-8")
    last = (None, "")
    if proc.returncode != 0:
        return last
    for line in (proc.stdout or "").splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        last = (c.get("id"), c.get("body") or "")
    return last


def summary_state(body):
    """The hidden state marker of a summary body ({} if none)."""
    m = MARKER_RE.search(body or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def next_state(prev, verdict, diff_hash, head_sha):
    """State for the new summary. Pure function."""
    blocks = int(prev.get("blocks") or 0) + (1 if verdict == "BLOCK" else 0)
    return {"diff": diff_hash or "", "verdict": verdict, "blocks": blocks,
            "head": (head_sha or "")[:12]}


def patch_comment(repo, comment_id, body):
    with open("summary.md", "w", encoding="utf-8") as f:
        f.write(body)
    return subprocess.run(["gh", "api", "--method", "PATCH",
                           f"repos/{repo}/issues/comments/{comment_id}",
                           "-F", "body=@summary.md"]).returncode


def upsert_summary(repo, pr, body, comment_id=None):
    """Edit the previous Claude summary comment, or create one."""
    if comment_id is not None:
        if patch_comment(repo, comment_id, body) == 0:
            print(f"updated summary comment {comment_id}")
            return 0
        print("could not update the previous summary; posting a new one")
    with open("summary.md", "w", encoding="utf-8") as f:
        f.write(body)
    return subprocess.run(["gh", "pr", "comment", pr, "--body-file", "summary.md"]).returncode


def diff_hash(base):
    """Hash of the PR's own diff: merge-base(origin/base, HEAD)..HEAD."""
    mb = subprocess.run(["git", "merge-base", f"origin/{base}", "HEAD"],
                        capture_output=True, text=True).stdout.strip()
    if not mb:
        return ""
    diff = subprocess.run(["git", "diff", "--no-color", mb, "HEAD"],
                          capture_output=True).stdout
    return hashlib.sha256(diff).hexdigest()[:16]


def reuse():
    repo, pr = os.environ["GITHUB_REPOSITORY"], os.environ["PR"]
    head = os.environ.get("HEAD_SHA", "")
    h = diff_hash(os.environ.get("BASE", "master"))
    cid, body = latest_summary(repo, pr)
    prev = summary_state(body)
    hit = bool(h) and prev.get("diff") == h and prev.get("verdict") in ("PASS", "BLOCK")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"diff_hash={h}\n")
            f.write(f"reuse={'true' if hit else 'false'}\n")
            f.write(f"verdict={prev.get('verdict', '') if hit else ''}\n")
    if not hit:
        print(f"diff {h or '?'} differs from the last review ({prev.get('diff') or 'none'}): reviewing")
        return
    print(f"diff {h} unchanged since {prev.get('head')}: carrying over {prev['verdict']}")
    note = (f"<sub>Carried over to `{head[:7]}`: the PR's own diff is unchanged "
            f"(e.g. a merge from master), so it was not re-reviewed.</sub>")
    new_body = CARRY_RE.sub("", body).rstrip() + "\n" + note
    if cid is not None:
        patch_comment(repo, cid, new_body)


def ensure_needs_owner(repo, pr):
    subprocess.run(["gh", "label", "create", NEEDS_OWNER_LABEL, "--repo", repo,
                    "--color", "B60205", "--description",
                    "Claude BLOCKed 3 times: owner decision before more pushes"],
                   capture_output=True)
    rc = subprocess.run(["gh", "pr", "edit", pr, "--repo", repo,
                         "--add-label", NEEDS_OWNER_LABEL]).returncode
    print(f"needs-owner label {'added' if rc == 0 else 'NOT added'}")


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


def collect(out_dir):
    repo, pr = os.environ["GITHUB_REPOSITORY"], os.environ["PR"]
    try:
        threads = open_bot_threads(repo, pr)
    except RuntimeError as exc:
        print(f"could not list review threads: {exc}")
        threads = []
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "open_threads.json"), "w", encoding="utf-8") as f:
        json.dump(threads, f, ensure_ascii=False, indent=1)
    _, body = latest_summary(repo, pr)
    prev_path = os.path.join(out_dir, "previous_summary.md")
    if body:
        with open(prev_path, "w", encoding="utf-8") as f:
            f.write(MARKER_RE.sub("", body))
    elif os.path.exists(prev_path):
        os.remove(prev_path)
    print(f"{len(threads)} open Claude thread(s); "
          f"{'re-review' if body else 'first review'} -> {out_dir}")


def post(open_json):
    r = json.loads(os.environ["RESULT"])
    repo, pr, head = os.environ["GITHUB_REPOSITORY"], os.environ["PR"], os.environ["HEAD_SHA"]

    try:
        with open(open_json, encoding="utf-8") as f:
            shown_ids = [str(t["id"]) for t in json.load(f) if t.get("id")]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"no shown-threads file ({exc}); resolving nothing")
        shown_ids = []
    still_open = [str(i) for i in (r.get("still_open_threads") or [])]
    try:
        open_ids = {t["id"] for t in open_bot_threads(repo, pr)} if shown_ids else set()
    except RuntimeError as exc:
        print(f"could not list review threads: {exc}")
        open_ids = set()
    todo = threads_to_resolve(shown_ids, open_ids, still_open)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"resolve_ids={json.dumps(todo)}\n")
    print(f"{len(todo)} thread(s) to resolve, {len(shown_ids) - len(todo)} kept")

    comments = [c for c in (r.get("inline_comments") or [])
                if c.get("path") and c.get("line") and c.get("body")]
    failed = post_inline(repo, pr, head, comments)

    body = (r.get("summary") or "").strip() or "## Claude review\n\n(no summary returned)"
    if failed:
        body += "\n\n### Findings that could not be posted inline\n"
        for c in failed:
            body += f"\n**`{c['path']}:{c['line']}`**\n\n{c['body']}\n"
    cid, prev_body = latest_summary(repo, pr)
    state = next_state(summary_state(prev_body), r.get("verdict"),
                       os.environ.get("DIFF_HASH", ""), head)
    if r.get("verdict") == "BLOCK" and state["blocks"] >= BLOCKS_BEFORE_OWNER:
        body += (f"\n\n**BLOCK #{state['blocks']} on this PR: owner decision needed** "
                 f"(label `{NEEDS_OWNER_LABEL}`). Please stop pushing fixes until the "
                 "owner answers; reply to each open thread with `Fixed in <sha>`, "
                 "`Won't fix: <reason>` or `Needs owner`.")
        ensure_needs_owner(repo, pr)
    body += ("\n\n<sub>Verdict: **%s** | P1 %s | P2 %s | P3 %s | head `%s` | inline %d/%d | "
             "old threads closing %d, kept %d | BLOCKs on this PR %d | model claude-opus-5-5</sub>"
             % (r.get("verdict"), r.get("p1"), r.get("p2"), r.get("p3"), head[:7],
                len(comments) - len(failed), len(comments), len(todo),
                len(shown_ids) - len(todo), state["blocks"]))
    body += "\n<!-- claude-review-state: " + json.dumps(state) + " -->"
    sys.exit(upsert_summary(repo, pr, body, cid))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    if mode == "reuse":
        reuse()
    elif mode == "collect":
        collect(sys.argv[2])
    elif mode == "resolve":
        ids = [str(i) for i in json.loads(os.environ.get("IDS") or "[]")]
        resolve_ids(os.environ["GITHUB_REPOSITORY"], os.environ["PR"],
                    os.environ["HEAD_SHA"], ids)
    else:
        post(sys.argv[2] if len(sys.argv) > 2 else "")

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

    python claude_review_post.py ping
        Post "owner decision needed" to Discord for a PR or issue that just
        got the `needs-owner` label from a person or Cursor (the
        needs-owner-ping workflow; env KIND, NUMBER, TITLE, URL, ACTOR).
        A label the review job adds itself uses the job token, which starts
        no workflow, so `post` sends that ping directly.

Env: PR, HEAD_SHA, GITHUB_REPOSITORY, GH_TOKEN (+ BASE for reuse, RESULT
and DIFF_HASH for post, IDS for resolve). Uses gh and git.
Discord (optional, both repo secrets): DISCORD_REVIEW_WEBHOOK, and
DISCORD_OWNER_MENTION (e.g. "<@user id>") to ping the owner. Without the
webhook nothing is sent. A Discord failure never fails the job.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

NEEDS_OWNER_LABEL = "needs-owner"
BLOCKS_BEFORE_OWNER = 3
MARKER_RE = re.compile(r"<!-- claude-review-state: (\{.*?\}) -->")
CARRY_RE = re.compile(r"\n?<sub>Carried over to [^<]*</sub>")

BOT_LOGINS = {"github-actions", "github-actions[bot]"}
DISCORD_LIMIT = 2000


def bot_logins(app_slug=None):
    """All logins recognized as Claude reviews (legacy Actions bot + App bot)."""
    slug = (os.environ.get("APP_SLUG", "") if app_slug is None else str(app_slug or "")).strip()
    logins = set(BOT_LOGINS)
    if slug:
        base = slug[:-5] if slug.endswith("[bot]") else slug
        logins.update({base, f"{base}[bot]"})
    return logins


def actor_logins(app_slug=None):
    """Logins owned by the current token (only these comments can be PATCHed)."""
    slug = (os.environ.get("APP_SLUG", "") if app_slug is None else str(app_slug or "")).strip()
    if slug:
        base = slug[:-5] if slug.endswith("[bot]") else slug
        return {base, f"{base}[bot]"}
    return set(BOT_LOGINS)

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
    known = bot_logins()
    threads, after = [], None
    while True:
        conn = graphql(THREADS_QUERY, {"owner": owner, "name": name, "pr": int(pr),
                                       "after": after})["repository"]["pullRequest"]["reviewThreads"]
        for t in conn["nodes"]:
            first = (t["comments"]["nodes"] or [{}])[0]
            login = ((first.get("author") or {}).get("login") or "")
            if not t["isResolved"] and login in known:
                replies = author_replies((t.get("replies") or {}).get("nodes") or [])
                threads.append({"id": t["id"], "path": t["path"], "line": t["line"],
                                "body": first.get("body", ""), "url": first.get("url", ""),
                                "replies": replies})
        if not conn["pageInfo"]["hasNextPage"]:
            return threads
        after = conn["pageInfo"]["endCursor"]


def author_replies(nodes, keep=3):
    """The newest `keep` replies NOT written by this workflow (oldest first).

    Bot comments ("Fixed in" / "Not raised") can pile up after the author's
    answer, so take the last 10 comments and drop the bot's own; the finding
    itself is a bot comment, so it is dropped too. Pure function."""
    known = bot_logins()
    mine = [{"author": (n.get("author") or {}).get("login", ""),
             "body": (n.get("body") or "")[:1500]}
            for n in nodes if ((n.get("author") or {}).get("login") or "") not in known]
    return mine[-keep:]


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


SUMMARY_JQ = '.[] | select(.body | startswith("## Claude review")) | {id, body, login: .user.login}'


def latest_summary_with_author(repo, pr):
    """(comment_id, body, author_login) of the newest Claude summary, or (None, "", "")."""
    proc = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/issues/{pr}/comments", "--jq", SUMMARY_JQ],
        capture_output=True, text=True, encoding="utf-8")
    last = (None, "", "")
    if proc.returncode != 0:
        return last
    known = bot_logins()
    for line in (proc.stdout or "").splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        login = str(c.get("login") or "")
        if login in known:
            last = (c.get("id"), c.get("body") or "", login)
    return last


def latest_summary(repo, pr):
    """(comment_id, body) of the newest Claude summary, or (None, "")."""
    cid, body, _author = latest_summary_with_author(repo, pr)
    return cid, body


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


def upsert_summary(repo, pr, body, comment_id=None, comment_author=None):
    """Edit the previous Claude summary comment (if owned by this actor), or create one."""
    can_patch = (
        comment_id is not None
        and (comment_author is None or comment_author in actor_logins())
    )
    if can_patch:
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
    cid, body, author = latest_summary_with_author(repo, pr)
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
    if cid is not None and author in actor_logins():
        patch_comment(repo, cid, new_body)
    if os.environ.get("APP_SLUG", "").strip() and head:
        submit_review(repo, pr, head, prev["verdict"], [], carried_over=True)


MENTION_RE = re.compile(r"<@(&?)!?(\d+)>")


def discord_payload(text, mention=""):
    """Webhook body: optional owner mention first, cut to Discord's limit.
    Only the ids in `mention` may ping: a `<@id>` or @everyone in a PR title
    stays plain text. Pure."""
    content = f"{mention} {text}".strip() if mention else text
    if len(content) > DISCORD_LIMIT:
        content = content[:DISCORD_LIMIT - 1] + "…"
    allowed = {"parse": [], "users": [], "roles": []}
    for role, ident in MENTION_RE.findall(mention or ""):
        allowed["roles" if role else "users"].append(ident)
    return {"content": content, "allowed_mentions": allowed}


def discord_notify(text, mention=None):
    """Post to DISCORD_REVIEW_WEBHOOK. Never raises: returns True if sent."""
    url = os.environ.get("DISCORD_REVIEW_WEBHOOK", "").strip()
    if not url:
        print("DISCORD_REVIEW_WEBHOOK not set; nothing sent to Discord")
        return False
    if mention is None:
        mention = os.environ.get("DISCORD_OWNER_MENTION", "").strip()
    try:
        req = urllib.request.Request(
            url, data=json.dumps(discord_payload(text, mention)).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     # Discord's edge rejects the default Python-urllib agent.
                     "User-Agent": "DiscordBot (https://github.com, 1.0) tai-robot-ci"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Discord: HTTP {resp.status}")
        return True
    except Exception as exc:  # noqa: BLE001 - a ping must never fail the job
        print(f"Discord post failed: {exc}")
        return False


def owner_ping_text(kind, number, title, url, why):
    """One-line owner ping. Pure."""
    return f"🙋 Owner decision needed: {kind} #{number} “{title}” ({why}) {url}"


def ensure_needs_owner(repo, pr):
    """Add the label; ping Discord only the first time (later BLOCKs on a
    PR that already waits for the owner stay quiet)."""
    try:
        had = NEEDS_OWNER_LABEL in subprocess.run(
            ["gh", "pr", "view", pr, "--repo", repo, "--json", "labels",
             "--jq", ".labels[].name"], capture_output=True, text=True).stdout.split()
    except OSError:
        had = False
    subprocess.run(["gh", "label", "create", NEEDS_OWNER_LABEL, "--repo", repo,
                    "--color", "B60205", "--description",
                    "Claude BLOCKed 3 times: owner decision before more pushes"],
                   capture_output=True)
    rc = subprocess.run(["gh", "pr", "edit", pr, "--repo", repo,
                         "--add-label", NEEDS_OWNER_LABEL]).returncode
    print(f"needs-owner label {'added' if rc == 0 else 'NOT added'}")
    if rc == 0 and not had:
        info = subprocess.run(["gh", "pr", "view", pr, "--repo", repo, "--json",
                               "title,url"], capture_output=True, text=True).stdout
        try:
            info = json.loads(info)
        except ValueError:
            info = {}
        discord_notify(owner_ping_text(
            "PR", pr, info.get("title", ""), info.get("url", ""),
            f"Claude BLOCKed it {BLOCKS_BEFORE_OWNER} times"))


def ping():
    """needs-owner added by a person or Cursor (see the docstring)."""
    e = os.environ
    discord_notify(owner_ping_text(e.get("KIND", "PR"), e.get("NUMBER", "?"),
                                   e.get("TITLE", ""), e.get("URL", ""),
                                   f"labeled by {e.get('ACTOR', '?')}"))


def to_review_comment(c):
    out = {"path": c["path"], "line": int(c["line"]), "side": "RIGHT", "body": c["body"]}
    start = c.get("start_line")
    if start and int(start) < int(c["line"]):
        out["start_line"] = int(start)
        out["start_side"] = "RIGHT"
    return out


def submit_review(repo, pr, head_sha, verdict, comments, *, carried_over=False):
    """Submit a PR review as the App bot (APPROVE on PASS, REQUEST_CHANGES on BLOCK).

    Falls back to COMMENT-only inline comments when running under github.token
    (APP_SLUG unset) or if GitHub rejects APPROVE/REQUEST_CHANGES. Returns
    the list of inline comments that could not be posted inline.
    """
    use_app = bool(os.environ.get("APP_SLUG", "").strip())
    if not use_app:
        return post_inline(repo, pr, head_sha, comments)

    event = "APPROVE" if verdict == "PASS" else "REQUEST_CHANGES"
    path = f"repos/{repo}/pulls/{pr}/reviews"
    if verdict == "PASS":
        suffix = " (carried over: diff unchanged)" if carried_over else ""
        review_body = f"Claude review @ `{head_sha[:7]}`: **PASS**{suffix} — see summary comment."
    else:
        review_body = (
            f"Claude review @ `{head_sha[:7]}`: **BLOCK** — "
            + ("inline P1/P2 findings attached (see summary comment)."
               if comments else "unresolved P1/P2 findings remain (see summary comment).")
        )

    payload = {"commit_id": head_sha, "event": event, "body": review_body}
    if comments:
        payload["comments"] = [to_review_comment(c) for c in comments]

    ok, err = gh_api(path, payload)
    if ok:
        print(f"submitted {event} review with {len(comments)} inline comment(s)")
        return []

    print(f"{event} review rejected ({err}); retrying inline comments and decision separately")
    failed = post_inline(repo, pr, head_sha, comments) if comments else []
    ok2, err2 = gh_api(path, {"commit_id": head_sha, "event": event, "body": review_body})
    if ok2:
        print(f"submitted {event} review without inline batch")
    else:
        print(f"::warning::could not submit {event} review: {err2}")
    return failed


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
    failed = submit_review(repo, pr, head, r.get("verdict"), comments)

    body = (r.get("summary") or "").strip() or "## Claude review\n\n(no summary returned)"
    if failed:
        body += "\n\n### Findings that could not be posted inline\n"
        for c in failed:
            body += f"\n**`{c['path']}:{c['line']}`**\n\n{c['body']}\n"
    cid, prev_body, prev_author = latest_summary_with_author(repo, pr)
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
    sys.exit(upsert_summary(repo, pr, body, cid, comment_author=prev_author))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    if mode == "reuse":
        reuse()
    elif mode == "collect":
        collect(sys.argv[2])
    elif mode == "ping":
        ping()
    elif mode == "resolve":
        ids = [str(i) for i in json.loads(os.environ.get("IDS") or "[]")]
        resolve_ids(os.environ["GITHUB_REPOSITORY"], os.environ["PR"],
                    os.environ["HEAD_SHA"], ids)
    else:
        post(sys.argv[2] if len(sys.argv) > 2 else "")

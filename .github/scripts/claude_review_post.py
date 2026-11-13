"""Post the Claude review's structured result to the PR.

Reads the action's structured_output JSON from $RESULT and:
1. posts every `inline_comments` entry as a line comment, all in one PR
   review (each becomes a resolvable thread). If GitHub rejects the batch
   (422: a line not in the diff), each comment is retried as its own review
   and the rejected ones are listed in the summary instead;
2. posts `summary` (plus a verdict footer) as a PR comment.

Env: RESULT, PR, HEAD_SHA, GITHUB_REPOSITORY, GH_TOKEN. Uses the gh CLI.
"""
import json
import os
import subprocess
import sys


def gh_api(path, payload):
    proc = subprocess.run(
        ["gh", "api", "--method", "POST", path, "--input", "-"],
        input=json.dumps(payload), text=True, capture_output=True)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


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


def main():
    r = json.loads(os.environ["RESULT"])
    repo, pr, head = os.environ["GITHUB_REPOSITORY"], os.environ["PR"], os.environ["HEAD_SHA"]

    comments = [c for c in (r.get("inline_comments") or [])
                if c.get("path") and c.get("line") and c.get("body")]
    failed = post_inline(repo, pr, head, comments)

    body = (r.get("summary") or "").strip() or "## Claude review\n\n(no summary returned)"
    if failed:
        body += "\n\n### Findings that could not be posted inline\n"
        for c in failed:
            body += f"\n**`{c['path']}:{c['line']}`**\n\n{c['body']}\n"
    body += ("\n\n<sub>Verdict: **%s** | P1 %s | P2 %s | P3 %s | inline %d/%d | model claude-opus-5-5</sub>"
             % (r.get("verdict"), r.get("p1"), r.get("p2"), r.get("p3"),
                len(comments) - len(failed), len(comments)))
    with open("summary.md", "w", encoding="utf-8") as f:
        f.write(body)
    sys.exit(subprocess.run(["gh", "pr", "comment", pr, "--body-file", "summary.md"]).returncode)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors
"""End-to-end test for the Gitea publishing example.

The test drives a real Gitea instance: it creates a repository, opens a
pull request, runs ``post_review.py`` against it and then asserts on the
state Gitea actually stored (inline review comments, the summary
conversation comment and the commit status).

Usage:
    python e2e_test.py --server http://localhost:3111 --token <api-token>

The token needs ``write:repository`` scope.  The test creates (and
deletes) a repository named ``ocr-gitea-e2e``.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
POST_REVIEW = os.path.join(HERE, "post_review.py")

REPO_NAME = "ocr-gitea-e2e"
DEFAULT_BRANCH = "main"

# The fixture deliberately contains a bug on line 9 of app.py so that the
# review has at least one anchorable line to comment on.
APP_PY = '''"""Fixture module."""


def add(a, b):
    """Add two numbers."""
    return a + b


def divide(a, b):
    """Divide a by b."""
    return a / b


def multiply(a, b):
    """Multiply two numbers."""
    return a * b
'''

FIXED_APP_PY = '''"""Fixture module."""


def add(a, b):
    """Add two numbers."""
    return a + b


def divide(a, b):
    """Divide a by b, guarding against zero."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b


def multiply(a, b):
    """Multiply two numbers."""
    return a * b
'''

# The line of the guarded division inside FIXED_APP_PY. It is an added line,
# so it is part of the diff and therefore anchorable.
ANCHOR_LINE = 11


class GiteaClient(object):
    """Minimal Gitea API client for the test fixture."""

    def __init__(self, server, token):
        self.server = server.rstrip("/")
        self.token = token
        # The test talks to a local instance; never route it through a proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, data=None, raw=False):
        url = self.server + "/api/v1" + path
        body = None
        headers = {
            "Authorization": "token %s" % self.token,
            "Accept": "application/json",
            "User-Agent": "ocr-gitea-e2e",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                payload = resp.read().decode("utf-8", "replace")
                if raw:
                    return payload
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError("%s %s failed: HTTP %s %s"
                               % (method, path, exc.code, detail[:400]))

    def create_repo(self, owner):
        return self.request("POST", "/user/repos", {
            "name": REPO_NAME,
            "auto_init": True,
            "default_branch": DEFAULT_BRANCH,
            "description": "Temporary repository for the Gitea e2e test",
        })

    def delete_repo(self, owner):
        self.request("DELETE", "/repos/%s/%s" % (owner, REPO_NAME))

    def get_pr(self, owner, index):
        return self.request("GET", "/repos/%s/%s/pulls/%s" % (owner, REPO_NAME, index))

    def list_pr_reviews(self, owner, index):
        return self.request("GET", "/repos/%s/%s/pulls/%s/reviews?limit=50"
                            % (owner, REPO_NAME, index))

    def list_issue_comments(self, owner, index):
        return self.request("GET", "/repos/%s/%s/issues/%s/comments?limit=50"
                            % (owner, REPO_NAME, index))

    def list_statuses(self, owner, sha):
        return self.request("GET", "/repos/%s/%s/statuses/%s" % (owner, REPO_NAME, sha))

    def get_branch(self, owner, branch):
        return self.request("GET", "/repos/%s/%s/branches/%s" % (owner, REPO_NAME, branch))

    def create_pr(self, owner, head, base, title, body):
        return self.request("POST", "/repos/%s/%s/pulls" % (owner, REPO_NAME), {
            "head": head,
            "base": base,
            "title": title,
            "body": body,
        })

    def get_user(self):
        return self.request("GET", "/user")


def local_env():
    """Return an environment that keeps local traffic away from any proxy."""
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_git(args, cwd, env=None):
    """Run a git command and raise on failure."""
    proc = subprocess.run(["git"] + args, cwd=cwd, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError("git %s failed:\n%s"
                           % (" ".join(args), proc.stdout.decode("utf-8", "replace")))
    return proc.stdout.decode("utf-8", "replace")


def make_commit(client, owner, branch, filename, content, message,
                base_branch=DEFAULT_BRANCH):
    """Create a branch with one changed file and return its commit sha."""
    base = client.get_branch(owner, base_branch)["commit"]["id"]
    work = tempfile.mkdtemp(prefix="ocr-e2e-")
    try:
        env = local_env()
        env["GIT_AUTHOR_NAME"] = "ocr e2e"
        env["GIT_AUTHOR_EMAIL"] = "ocr-e2e@example.com"
        env["GIT_COMMITTER_NAME"] = "ocr e2e"
        env["GIT_COMMITTER_EMAIL"] = "ocr-e2e@example.com"
        run_git(["init", "-q", "-b", branch, work], work, env)
        run_git(["remote", "add", "origin", "%s/%s/%s.git"
                 % (client.server, owner, REPO_NAME)], work, env)
        run_git(["-c", "http.extraHeader=Authorization: token %s" % client.token,
                 "fetch", "-q", "origin", base], work, env)
        run_git(["checkout", "-q", "-B", branch, "FETCH_HEAD"], work, env)
        path = os.path.join(work, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        run_git(["add", filename], work, env)
        run_git(["commit", "-q", "-m", message], work, env)
        run_git(["-c", "http.extraHeader=Authorization: token %s" % client.token,
                 "push", "-q", "origin", branch], work, env)
        head = run_git(["rev-parse", "HEAD"], work, env).strip()
        return head
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_ocr_result():
    """Build an OCR-shaped result document for the fixture."""
    return {
        "status": "success",
        "message": "",
        "comments": [
            {
                "path": "app.py",
                "content": "Division by zero is not guarded.",
                "start_line": ANCHOR_LINE,
                "end_line": ANCHOR_LINE,
                "category": "bug",
                "severity": "high",
            },
            {
                "path": "app.py",
                "content": "This line is outside the diff and must be routed to the summary.",
                "start_line": 999,
                "end_line": 999,
                "category": "bug",
                "severity": "medium",
            },
            {
                "path": "missing_file.py",
                "content": "File is not part of the pull request.",
                "start_line": 1,
                "end_line": 1,
                "category": "maintainability",
                "severity": "low",
            },
        ],
        "warnings": [
            {"file": "app.py", "message": "context was truncated", "type": "warning"},
        ],
    }


def run_post_review(env, result_path, stats_path):
    """Run the publishing script and return its combined output."""
    proc = subprocess.run(
        [sys.executable, POST_REVIEW, result_path, "--stats-file", stats_path],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--owner", default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    client = GiteaClient(args.server, args.token)
    user = client.get_user()
    owner = args.owner or user["login"]

    print("[setup] owner=%s" % owner)
    try:
        client.delete_repo(owner)
    except RuntimeError:
        pass
    client.create_repo(owner)

    # Seed the base branch with the pre-change version of the file so the
    # pull request modifies an existing file. That is the common case and it
    # exercises the diff-position mapping of the publishing script.
    make_commit(client, owner, DEFAULT_BRANCH, "app.py", APP_PY,
                "seed the fixture module")
    print("[setup] base branch seeded")

    branch = "feature/e2e"
    head_sha = make_commit(client, owner, branch, "app.py", FIXED_APP_PY,
                           "guard division by zero")
    print("[setup] head commit=%s" % head_sha)

    pr = client.create_pr(owner, branch, DEFAULT_BRANCH, "E2E pull request",
                          "Automated end-to-end fixture.")
    pr_index = pr["number"]
    print("[setup] pull request #%s created" % pr_index)

    result = build_ocr_result()
    work = tempfile.mkdtemp(prefix="ocr-e2e-out-")
    result_path = os.path.join(work, "ocr-result.json")
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)

    stats_path = os.path.join(work, "stats.env")
    env = local_env()
    env["GITEA_SERVER_URL"] = args.server
    env["GITEA_REPOSITORY"] = "%s/%s" % (owner, REPO_NAME)
    env["GITEA_PULL_REQUEST_INDEX"] = str(pr_index)
    env["GITEA_TOKEN"] = args.token
    env["GITEA_HEAD_SHA"] = head_sha
    env["GITEA_BASE_SHA"] = pr["base"]["sha"]

    print("[run] post_review.py")
    returncode, output = run_post_review(env, result_path, stats_path)
    print(output)
    if returncode != 0:
        print("[FAIL] post_review.py exited with %s" % returncode)
        return 1

    failures = []

    def check(label, condition, detail=""):
        if condition:
            print("[ok] %s" % label)
        else:
            print("[FAIL] %s %s" % (label, detail))
            failures.append(label)

    reviews = client.list_pr_reviews(owner, pr_index)
    check("a review was created", len(reviews) >= 1, "reviews=%s" % len(reviews))

    inline_found = []
    for review in reviews:
        comments = client.request(
            "GET", "/repos/%s/%s/pulls/%s/reviews/%s/comments"
            % (owner, REPO_NAME, pr_index, review["id"]))
        for comment in comments or []:
            inline_found.append(comment)

    check("exactly one inline comment was published", len(inline_found) == 1,
          "count=%s" % len(inline_found))
    if inline_found:
        comment = inline_found[0]
        print("[debug] review comment payload: %s"
              % json.dumps(comment, ensure_ascii=False, indent=2))
        # Gitea resolves new_position (the new-file line number) into the
        # stored diff position and returns the surrounding hunk. The hunk must
        # therefore contain the very line that was reviewed.
        diff_hunk = comment.get("diff_hunk") or ""
        check("inline comment is anchored on the reviewed line",
              "if b == 0:" in diff_hunk,
              "diff_hunk=%s" % diff_hunk[:200])
        check("inline comment is anchored on a diff position",
              isinstance(comment.get("position"), int)
              and comment.get("position", 0) > 0,
              "position=%s original_position=%s"
              % (comment.get("position"), comment.get("original_position")))
        check("inline comment mentions the defect",
              "Division by zero" in (comment.get("body") or ""),
              "body=%s" % (comment.get("body") or "")[:120])
        check("inline comment carries a category badge",
              "bug" in (comment.get("body") or "").lower(),
              "body=%s" % (comment.get("body") or "")[:120])
        check("inline comment targets the changed file",
              comment.get("path") == "app.py",
              "path=%s" % comment.get("path"))

    issue_comments = client.list_issue_comments(owner, pr_index)
    summary_bodies = [c.get("body") or "" for c in issue_comments]
    has_summary = any("OpenCodeReview" in body for body in summary_bodies)
    check("a summary comment was published", has_summary,
          "comments=%s" % len(issue_comments))
    if has_summary:
        summary = [b for b in summary_bodies if "OpenCodeReview" in b][0]
        check("summary lists the out-of-diff finding",
              "outside the diff" in summary, "summary=%s" % summary[:200])
        check("summary lists the missing-file finding",
              "not part of the pull request" in summary, "summary=%s" % summary[:200])

    statuses = client.list_statuses(owner, head_sha)
    check("a commit status was reported", len(statuses) >= 1, "statuses=%s" % len(statuses))
    if statuses:
        status = statuses[0]
        check("commit status carries a context", bool(status.get("context")),
              "context=%s" % status.get("context"))

    # --- rerun: the sticky summary must be updated, not duplicated ---------
    print("[run] post_review.py again (idempotency)")
    returncode, output = run_post_review(env, result_path, stats_path)
    if returncode != 0:
        check("rerun succeeded", False, "exit=%s" % returncode)
    else:
        check("rerun succeeded", True)
    rerun_comments = client.list_issue_comments(owner, pr_index)
    rerun_summaries = [c.get("body") or "" for c in rerun_comments
                       if "OpenCodeReview" in (c.get("body") or "")]
    check("the sticky summary was updated in place, not duplicated",
          len(rerun_summaries) == 1,
          "summaries=%s" % len(rerun_summaries))

    # --- severity routing: low severity findings leave the inline path ---
    print("[run] post_review.py with severity routing")
    routed_env = dict(env)
    routed_env["OCR_ROUTE_SEVERITY_BELOW"] = "high"
    routed_stats_path = os.path.join(work, "routed-stats.env")
    returncode, output = run_post_review(routed_env, result_path, routed_stats_path)
    if returncode != 0:
        check("routing run succeeded", False, "exit=%s" % output[-300:])
    else:
        check("routing run succeeded", True)
        routed_stats = ""
        if os.path.exists(routed_stats_path):
            with open(routed_stats_path, "r", encoding="utf-8") as handle:
                routed_stats = handle.read()
        check("every finding at or below the threshold was routed",
              "OCR_COMMENTS_ROUTED=3" in routed_stats,
              "stats=%s" % routed_stats.replace("\n", " | "))
        check("no finding stayed on the inline path",
              "OCR_COMMENTS_INLINE=0" in routed_stats,
              "stats=%s" % routed_stats.replace("\n", " | "))

    if os.path.exists(stats_path):
        with open(stats_path, "r", encoding="utf-8") as handle:
            stats = handle.read()
        print("[stats]\n%s" % stats)

    shutil.rmtree(work, ignore_errors=True)
    if not args.keep:
        try:
            client.delete_repo(owner)
            print("[cleanup] repository removed")
        except RuntimeError as exc:
            print("[warn] cleanup failed: %s" % exc)

    if failures:
        print("\n[RESULT] %d check(s) failed: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("\n[RESULT] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

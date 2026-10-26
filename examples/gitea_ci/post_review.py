#!/usr/bin/env python3

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""Post an OpenCodeReview result onto a Gitea pull request.

This is the CI-layer "glue" for Gitea, mirroring examples/gitlab_ci and
examples/gerrit_ci: it keeps platform-specific publishing out of the ``ocr``
binary and lives entirely in the pipeline.  It reads the JSON emitted by
``ocr review --format json`` and posts it onto the pull request as a review
with inline comments plus a summary conversation comment.

Gitea specifics handled here:

  - ``POST /repos/{owner}/{repo}/pulls/{index}/reviews`` is the ONLY way to
    create inline comments: Gitea has no ``POST .../reviews/{id}/comments``
    endpoint, so all inline comments of one review must travel in a single
    request.  The script therefore batches them into one review rather than
    posting one review per comment;
  - ``new_position`` in ``CreatePullReviewComment`` is the **new-file line
    number** (not a diff position index like GitLab's ``position``).  The
    server resolves the line against the patch itself, but an out-of-diff
    line makes ``GetCodeCommentPatch`` fail and the whole request is
    rejected, so the script validates every line against the fetched diff
    first and moves unverifiable findings to the summary instead;
  - a review body cannot be edited after submission (only
    ``DELETE /reviews/{id}`` exists), so the sticky summary lives in a
    conversation comment (``POST``/``PATCH /issues/{index}/comments``) where
    it can be updated in place across runs;
  - ``Authorization: token <token>`` is the current Gitea authentication
    header; ``?token=`` query parameters and the ``access_token`` scheme are
    deprecated since Gitea 1.23 and are not used;
  - the commit status is reported through ``POST /repos/{owner}/{repo}/statuses/{sha}``
    so the PR shows a passing/failing check like on the other platforms.

The script separates a transport-agnostic :func:`publish` (driven by an
injectable ``Poster`` object) from the Gitea REST transport
:class:`GiteaPoster`, so the full posting flow can be unit-tested with no
network access and no wall-clock sleep cost.

Behaviors aligned with the GitLab example:

  - category/severity badge on every comment,
  - publication policy routing findings to the summary by severity/category,
  - deterministic ordering before posting,
  - detailed warning rendering,
  - sticky summary (update-in-place across runs),
  - incremental mode (skip comments overlapping prior bot reviews),
  - idempotent retry (per-comment id tag; reconcile before retrying),
  - dotenv stats output and optional severity-based job gating.

Standard library only (json, urllib) so it runs on any stock python3 image.
"""

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- #
# Constants (mirrors examples/gitlab_ci/post_review.py)
# --------------------------------------------------------------------------- #

# HTML marker that tags the cross-run summary comment so the sticky upsert can
# find it across runs. Embedded verbatim in the summary body.
SUMMARY_MARKER = "<!-- ocr-summary -->"

# Per-run HTML marker embedded alongside SUMMARY_MARKER so the non-sticky
# summary path can locate THIS run's comment (find-by-tag) instead of always
# creating a new one.
def summary_tag_for(run_tag):
    return "<!-- ocr-summary-run:%s -->" % run_tag


def wrap_summary_body(content, run_tag):
    """Prefix the summary body with both the cross-run marker and the per-run tag.

    Sticky mode finds the comment by SUMMARY_MARKER (cross-run reuse);
    non-sticky finds it by the per-run tag (run-internal reuse, run-external
    new). Both markers are always embedded so the same body serves either
    matching mode.
    """
    return "%s\n%s\n%s" % (SUMMARY_MARKER, summary_tag_for(run_tag), content)


# Reason attached to comments that have no valid line range and therefore can
# never be posted as inline comments.
NO_LINE_REASON = "No line information provided"

# Reason attached to comments whose line could not be verified against the
# pull request diff. Gitea rejects the whole review when one line does not
# resolve, so these are moved to the summary rather than risking the batch.
LINE_NOT_IN_DIFF_REASON = "line is not part of the pull request diff"

# Reason attached to comments whose path is absent from the diff inventory
# (for example a file that was renamed or added without a resolvable hunk).
PATH_NOT_IN_DIFF_REASON = "file is not part of the pull request diff"

# Default IoU threshold for the incremental multi-line overlap test.
DEFAULT_OVERLAP_THRESHOLD = 0.6

# Enumerations for category/severity routing, sourced from the LLM output
# schema. Used both to validate the routing policy and to normalize metadata.
CATEGORIES = [
    "bug", "security", "performance", "maintainability",
    "test", "style", "documentation", "other",
]
# Severity rank: higher = more severe. critical=4, high=3, medium=2, low=1.
SEVERITIES = ["critical", "high", "medium", "low"]
SEVERITY_RANK = {s: len(SEVERITIES) - i for i, s in enumerate(SEVERITIES)}

# Sentinel policy object: "do not route anything". Returned by build_policy on
# any parse problem so the partition loop falls open to today's behavior.
NO_ROUTING = {"route_by_severity": False, "severity_rank": -1,
              "route_by_category": False, "categories": set()}

# Substring that flags a comment as authored by this bot (used for
# incremental history detection).
_BOT_MARKER = "<!-- ocr-"

# Maximum number of inline comments per review request. Gitea applies the
# whole request atomically, so a single oversized batch that trips a server
# limit would lose every finding in it. Chunks stay well below any plausible
# limit while keeping the request count low.
MAX_COMMENTS_PER_REVIEW = 20

# Number of attempts for transport failures that are safe to retry.
MAX_ATTEMPTS = 3

# Injectable so tests can run without real delays; production uses time.sleep.
_sleep = time.sleep


def log(msg):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Comment formatting + publication policy (pure)
# --------------------------------------------------------------------------- #

def sanitize_metadata(value):
    """Strip C0/C1 control characters from a metadata value."""
    if value is None:
        return ""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(value))


def build_badge(comment):
    """Build the ``[category · severity]`` badge (middot U+00B7).

    Byte-matches the CLI's buildBadge degeneration so review output is
    consistent across surfaces. Returns "" when neither is present, so callers
    can prepend conditionally without leaving a blank line.
    """
    category = sanitize_metadata(comment.get("category") if comment else None)
    severity = sanitize_metadata(comment.get("severity") if comment else None)
    if category and severity:
        return "[%s · %s]" % (category, severity)
    if category:
        return "[%s]" % category
    if severity:
        return "[%s]" % severity
    return ""


def build_policy(severity_threshold, categories):
    """Build the publication routing policy from the raw env values.

    Unknown or malformed input disables the corresponding rule, so the
    partition loop falls open to posting everything inline.
    """
    route_by_severity = False
    severity_rank = -1
    if severity_threshold:
        norm = str(severity_threshold).strip().lower()
        if norm in SEVERITY_RANK:
            route_by_severity = True
            severity_rank = SEVERITY_RANK[norm]
    route_by_category = False
    category_set = set()
    if categories:
        for token in str(categories).split(","):
            t = sanitize_metadata(token).strip().lower()
            if t in CATEGORIES:
                category_set.add(t)
        route_by_category = bool(category_set)
    if not route_by_severity and not route_by_category:
        return NO_ROUTING
    return {"route_by_severity": route_by_severity, "severity_rank": severity_rank,
            "route_by_category": route_by_category, "categories": category_set}


def route_comment(comment, policy):
    """Decide whether a finding is routed to the summary instead of inline.

    A finding matches when its severity is at-or-below the threshold OR its
    category is in the category list. Unknown/malformed metadata never matches.
    """
    if not policy or (not policy.get("route_by_severity") and not policy.get("route_by_category")):
        return {"routed": False, "reason": ""}
    cat_raw = sanitize_metadata(comment.get("category") if comment else None).strip().lower()
    sev_raw = sanitize_metadata(comment.get("severity") if comment else None).strip().lower()
    cat_known = cat_raw in CATEGORIES
    sev_known = sev_raw in SEVERITY_RANK
    if policy["route_by_severity"] and sev_known and SEVERITY_RANK[sev_raw] <= policy["severity_rank"]:
        suffix = " · category %s" % cat_raw if cat_known else ""
        return {"routed": True, "reason": "Routed to summary (severity %s%s)" % (sev_raw, suffix)}
    if policy["route_by_category"] and cat_known and cat_raw in policy["categories"]:
        suffix = " · severity %s" % sev_raw if sev_known else ""
        return {"routed": True, "reason": "Routed to summary (category %s%s)" % (cat_raw, suffix)}
    return {"routed": False, "reason": ""}


def sort_to_send(items):
    """Deterministically order items (path -> start_line -> end_line -> index).

    Sorting on the original index as the final key keeps the order stable for
    two findings that share the same path and line range.
    """
    return sorted(items, key=lambda it: (
        str(it["comment"].get("path", "")),
        int(it["comment"].get("start_line") or 0),
        int(it["comment"].get("end_line") or 0),
        it["index"],
    ))


def safe_fence(content):
    """Pick a backtick fence long enough to enclose ``content`` (min 3)."""
    text = str(content or "")
    max_ticks = 0
    for run in re.findall(r"`+", text):
        max_ticks = max(max_ticks, len(run))
    return "`" * max(3, max_ticks + 1)


def fenced_block(content, language=""):
    """Render ``content`` as a fenced code block with a safe fence."""
    text = str(content or "")
    fence = safe_fence(text)
    block = fence + language + "\n" + text
    if not text.endswith("\n"):
        block += "\n"
    return block + fence


def format_comment(comment, comment_id=None):
    """Assemble the visible inline-review-comment body.

    The per-comment id tag (when provided) is prepended as an HTML comment so
    :func:`reconcile_posted_id` can match it back on retry. The category/severity
    badge is then prepended on its own line. Gitea renders Markdown in review
    comments, so a fenced ``suggestion`` block is used for the proposed change.
    """
    body = ""
    if comment_id:
        body += "<!-- %s -->\n" % comment_id
    badge = build_badge(comment)
    if badge:
        body += badge + "\n"
    body += comment.get("content", "") or ""
    suggestion = comment.get("suggestion_code", "")
    existing = comment.get("existing_code", "")
    if suggestion and existing:
        body += "\n\n**Suggestion:**\n"
        body += "```suggestion\n%s\n```" % suggestion
    return body


def format_comment_fallback(comment, reason=None):
    """Format a comment for fallback (non-inline) display in the summary.

    Uses ``<details><summary>`` HTML so the suggested change is collapsible.
    The Before/After blocks use :func:`fenced_block` so code containing
    backticks still renders.
    """
    path = comment.get("path", "unknown")
    start_line = comment.get("start_line", 0)
    end_line = comment.get("end_line", 0)
    content = comment.get("content", "")

    md = ""
    badge = build_badge(comment)
    if badge:
        md += badge + "\n"
    md += "### 📄 `%s`" % path
    if start_line and end_line:
        md += " (L%d-L%d)" % (start_line, end_line)
    md += "\n\n"
    if reason:
        md += "⚠️ Could not be posted inline: %s\n\n" % reason
    md += content

    existing = comment.get("existing_code", "")
    suggestion = comment.get("suggestion_code", "")
    if suggestion and existing:
        md += "\n\n<details><summary>💡 Suggested Change</summary>\n\n"
        md += "**Before:**\n" + fenced_block(existing) + "\n\n"
        md += "**After:**\n" + fenced_block(suggestion) + "\n\n"
        md += "</details>"
    return md


def format_warning_entry(w):
    """Format a single warning as a compact ``file (type): message`` bullet."""
    if w is None:
        return ""
    if isinstance(w, str):
        return w
    if isinstance(w, dict):
        prefix_parts = []
        if w.get("file") and str(w["file"]) != "":
            prefix_parts.append("`%s`" % w["file"])
        if w.get("type") and str(w["type"]) != "":
            prefix_parts.append("(`%s`)" % w["type"])
        prefix = " ".join(prefix_parts)
        msg = str(w.get("message", "")) if w.get("message") is not None else ""
        if prefix and msg:
            return "%s: %s" % (prefix, msg)
        if msg:
            return msg
        if prefix:
            return prefix
        try:
            return json.dumps(w)
        except (TypeError, ValueError):
            return str(w)
    return str(w)


def format_warnings(warnings):
    """Render warnings as a bulleted list under a heading ("" when empty)."""
    if not warnings:
        return ""
    body = "\n\n---\n\n⚠️ **Warnings:**"
    for w in warnings:
        body += "\n- %s" % format_warning_entry(w)
    return body


def build_summary_body(total, inline, summary, skipped, routed, failed, warnings):
    """Merged summary header. Counts are mutually exclusive and sum to total."""
    body = "🔍 **OpenCodeReview** found **%d** issue(s) in this pull request." % total
    if total > 0:
        body += "\n- ✅ Successfully posted inline: %d comment(s)" % inline
        if summary > 0:
            body += "\n- 📝 In summary (no line info): %d comment(s)" % summary
        if routed > 0:
            body += "\n- 📋 Routed to summary by policy: %d comment(s)" % routed
        if skipped > 0:
            body += "\n- ⏭️ Skipped (overlap with history): %d comment(s)" % skipped
        if failed > 0:
            body += "\n- ❌ Failed to post inline: %d comment(s)" % failed
    if warnings:
        body += "\n\n⚠️ %d warning(s) occurred during review." % len(warnings)
    return body


def build_pre_review_summary_body(total, no_line, routed, warnings):
    """Summary body shown in the sticky anchor while inline comments post."""
    body = "🔍 **OpenCodeReview** found **%d** issue(s) in this pull request." % total
    if total > 0:
        body += "\n- ⏳ _Posting review comments…_"
        if routed:
            body += "\n- 📋 Routed to summary by policy: %d comment(s)" % len(routed)
    if warnings:
        body += "\n\n⚠️ %d warning(s) occurred during review." % len(warnings)
    body += format_summary_comments(no_line)
    body += format_summary_comments(routed)
    body += format_warnings(warnings)
    return body


def format_summary_comments(items):
    """Render a list of ``{comment, reason}`` as a continuous block."""
    body = ""
    for item in items:
        body += "\n\n---\n\n"
        body += format_comment_fallback(item["comment"], item.get("reason"))
    return body


# --------------------------------------------------------------------------- #
# Incremental overlap helpers (pure)
# --------------------------------------------------------------------------- #

def resolve_threshold(threshold):
    try:
        n = float(threshold)
    except (TypeError, ValueError):
        return DEFAULT_OVERLAP_THRESHOLD
    return n if 0 < n <= 1 else DEFAULT_OVERLAP_THRESHOLD


def _num(v):
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def comment_span(comment):
    """Resolve a review comment into a line span tagged single/multi-line."""
    s = _num(comment.get("start_line"))
    e = _num(comment.get("end_line"))
    if s is None and e is None:
        return None
    if s is not None and e is not None and s != e:
        return {"start": min(s, e), "end": max(s, e), "multiline": True}
    single = e if e is not None else s
    return {"start": single, "end": single, "multiline": False}


def same_comment_span(cur, other, threshold):
    """Compare two spans; single-line comments match only on the exact line.

    A single-line and a multi-line comment never match, mirroring the GitLab
    example: extending a one-line finding to a range is a different finding.
    """
    if cur["multiline"] != other["multiline"]:
        return False
    if not cur["multiline"]:
        return cur["start"] == other["start"]
    inter = min(cur["end"], other["end"]) - max(cur["start"], other["start"]) + 1
    if inter <= 0:
        return False
    union = max(cur["end"], other["end"]) - min(cur["start"], other["start"]) + 1
    return (inter / union) > threshold


def overlaps_history(comment, cur_span, history, threshold):
    """True when the finding overlaps a prior bot finding on the same path."""
    path = comment.get("path")
    for h in history:
        if h.get("path") != path:
            continue
        if same_comment_span(cur_span, h["span"], threshold):
            return True
    return False


# --------------------------------------------------------------------------- #
# Diff inventory (pure)
# --------------------------------------------------------------------------- #

def parse_diff_hunk_inventory(patch):
    """Parse a unified diff into ``{path: {"new": {line: kind}, "old": {...}}}``.

    ``new`` maps new-file line numbers to ``"added"`` or ``"context"``;
    ``old`` maps old-file line numbers the same way. Deleted lines appear in
    neither map, matching what a review comment may anchor to.
    """
    inventory = {}
    current = None
    new_ln = 0
    old_ln = 0

    def flush():
        if current is not None:
            inventory.setdefault(current["path"], {"new": {}, "old": {}})
            for k, v in current["new"].items():
                inventory[current["path"]]["new"].setdefault(k, v)
            for k, v in current["old"].items():
                inventory[current["path"]]["old"].setdefault(k, v)

    for line in str(patch or "").splitlines():
        if line.startswith("diff --git "):
            flush()
            current = {"path": None, "new": {}, "old": {}}
            # "diff --git a/x b/y" - the new path is the b-side.
            parts = line.split(" ")
            if len(parts) >= 4:
                new_part = parts[3]
                if new_part.startswith("b/"):
                    new_part = new_part[2:]
                current["path"] = new_part
            new_ln = 0
            old_ln = 0
            continue
        if current is None:
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current["path"] = current["path"] or ""
            elif target.startswith("b/"):
                current["path"] = target[2:]
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("@@"):
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                old_ln = int(m.group(1))
                new_ln = int(m.group(3))
            continue
        if line.startswith("+"):
            current["new"][new_ln] = "added"
            new_ln += 1
            continue
        if line.startswith("-"):
            old_ln += 1
            continue
        if line.startswith(" ") or line == "":
            current["new"][new_ln] = "context"
            current["old"][old_ln] = "context"
            new_ln += 1
            old_ln += 1
            continue
    flush()
    return inventory


def line_is_anchorable(inventory, path, line):
    """True when ``line`` of ``path`` can carry a Gitea inline comment.

    Gitea resolves the comment against the diff patch, so only lines that
    appear in the patch (added or context) can be anchored. A line outside
    the patch makes the server reject the whole review request.
    """
    if not path or not line:
        return False
    entry = inventory.get(path)
    if not entry:
        return False
    return line in entry["new"]


def validate_comment_against_diff(comment, inventory):
    """Return a reason string when the comment cannot be anchored, else None."""
    path = comment.get("path", "")
    span = comment_span(comment)
    if span is None:
        return NO_LINE_REASON
    if path not in inventory:
        return PATH_NOT_IN_DIFF_REASON
    # Gitea anchors a comment to a single line. For a multi-line finding the
    # end line is the anchor (the last line of the offending block), matching
    # the Gerrit example.
    anchor = span["end"]
    if not line_is_anchorable(inventory, path, anchor):
        return LINE_NOT_IN_DIFF_REASON
    return None


# --------------------------------------------------------------------------- #
# Idempotency helpers (pure)
# --------------------------------------------------------------------------- #

def new_comment_id(run_tag):
    """Build a deterministic per-comment id tag for a run.

    The tag embeds the run identity so a retry of the same run produces the
    same id, which is what makes reconciliation possible.
    """
    digest = hashlib.sha1(str(run_tag).encode("utf-8")).hexdigest()[:12]
    return "ocr-%s" % digest


def reconcile_posted_id(body, comment_id):
    """True when ``body`` already carries the given comment id tag."""
    if not comment_id:
        return False
    return ("<!-- %s -->" % comment_id) in (body or "")


def run_tag_from_env(env):
    """Build a per-run identity from the CI environment."""
    for key in ("GITHUB_RUN_ID", "GITEA_RUN_NUMBER", "CI_PIPELINE_ID", "GITHUB_RUN_NUMBER"):
        value = env.get(key)
        if value:
            return "%s-%s" % (key, value)
    return "local"


def _parse_bool(value, default=False):
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #

def _truncate_error(text, limit=400):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "... (truncated)"


def _get_header(headers, name):
    if not headers:
        return None
    value = headers.get(name)
    if value is None:
        # Some servers vary the case of rate-limit headers.
        lowered = name.lower()
        for key, val in headers.items():
            if key.lower() == lowered:
                return val
    return value


def _api_request_once(api_base, token, endpoint, data=None, method="GET",
                      expect_text=False):
    """Perform one API call and return a normalized result dict.

    ``expect_text`` keeps the raw body instead of parsing it as JSON, which
    the ``.diff`` endpoint requires: Gitea serves the pull request patch as
    ``text/plain``.
    """
    url = api_base + endpoint
    body = None
    headers = {
        "Accept": "text/plain" if expect_text else "application/json",
        "Authorization": "token %s" % token,
        "User-Agent": "open-code-review",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            payload = None
            if expect_text:
                payload = raw
            elif raw:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = None
            return {"success": True, "status": resp.getcode(),
                    "data": payload, "url": resp.geturl(),
                    "headers": dict(resp.headers.items())}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        return {"success": False, "status": e.code, "error_body": raw,
                "headers": dict(e.headers.items()) if e.headers else {},
                "url": url}
    except urllib.error.URLError as e:
        return {"success": False, "status": 0, "error_body": str(e.reason),
                "headers": {}, "url": url}
    except (TimeoutError, OSError) as e:
        return {"success": False, "status": 0, "error_body": str(e),
                "headers": {}, "url": url}


def _compute_retry_delay(attempt, config, headers=None):
    """Compute the delay before the next attempt, honoring Retry-After."""
    retry_after = _get_header(headers, "Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), config["max_retry_delay"])
        except (TypeError, ValueError):
            pass
    base = config["retry_base_delay"] * (2 ** (attempt - 1))
    jitter = random.uniform(0, base * 0.25)
    return min(base + jitter, config["max_retry_delay"])


def _api_request_with_retry(api_base, token, endpoint, data=None, method="GET",
                            config=None, sleep=_sleep, expect_text=False):
    """Retry transient failures with exponential backoff.

    Only failures that are safe to retry are retried: network errors, 5xx and
    429. A 4xx (other than 429) is a client error and is returned as-is.
    """
    config = config or {}
    attempt = 0
    last = None
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        resp = _api_request_once(api_base, token, endpoint, data=data, method=method,
                                  expect_text=expect_text)
        last = resp
        status = resp.get("status")
        if resp.get("success"):
            return resp
        if status == 429 or status == 0 or (status is not None and status >= 500):
            if attempt >= MAX_ATTEMPTS:
                break
            delay = _compute_retry_delay(attempt, config, resp.get("headers"))
            log("[retry] %s %s failed (HTTP %s); retrying in %.1fs"
                % (method, endpoint, status, delay))
            sleep(delay)
            continue
        break
    return last


def fetch_pull_request(api_base, token, config):
    """Fetch the pull request to resolve the current head commit SHA."""
    resp = _api_request_with_retry(api_base, token, "", config=config)
    if not resp.get("success"):
        log("[warn] could not fetch pull request: HTTP %s %s"
            % (resp.get("status"), _truncate_error(resp.get("error_body"))))
        return None
    data = resp.get("data") or {}
    return {"head_sha": data.get("head", {}).get("sha"),
            "base_sha": data.get("base", {}).get("sha"),
            "merge_base": data.get("merge_base")}


def fetch_pr_diff(api_base, token, config):
    """Fetch the unified diff of the pull request as text.

    Gitea serves the raw patch at ``/pulls/{index}.diff`` with a
    ``text/plain`` content type, so the response must not be parsed as JSON.
    """
    resp = _api_request_with_retry(api_base, token, ".diff", config=config,
                                   expect_text=True)
    if not resp.get("success"):
        log("[warn] could not fetch pull request diff: HTTP %s %s"
            % (resp.get("status"), _truncate_error(resp.get("error_body"))))
        return None
    raw = resp.get("data")
    if isinstance(raw, str):
        return raw
    return ""


# --------------------------------------------------------------------------- #
# Gitea transport
# --------------------------------------------------------------------------- #

class GiteaPoster:
    """Transport-agnostic Gitea poster for pull request reviews.

    The class implements the operations the publish flow needs:
    :meth:`list_comments`, :meth:`create_review`, :meth:`list_reviews` and
    :meth:`create_comment`.  Every call is retried on transient failures.
    """

    def __init__(self, pull_base, issue_base, token, config):
        self.pull_base = pull_base
        self.issue_base = issue_base
        self.token = token
        self.config = config

    def _retry(self, base, endpoint, data=None, method="GET"):
        return _api_request_with_retry(base, self.token, endpoint,
                                       data=data, method=method,
                                       config=self.config)

    def list_comments(self):
        """List the conversation comments of the pull request."""
        resp = self._retry(self.issue_base, "/comments?limit=50")
        if not resp.get("success"):
            log("[summary] cannot list comments for upsert; skipping to avoid duplicate.")
            return None
        return resp.get("data") or []

    def create_comment(self, body):
        """Create a conversation comment (used for the sticky summary)."""
        resp = self._retry(self.issue_base, "/comments", data={"body": body}, method="POST")
        if not resp.get("success"):
            log("[summary] failed to create comment: HTTP %s %s"
                % (resp.get("status"), _truncate_error(resp.get("error_body"))))
            return {"success": False, "http_status": resp.get("status"),
                    "error_body": resp.get("error_body")}
        data = resp.get("data") or {}
        return {"success": True, "id": data.get("id"), "url": data.get("html_url")}

    def update_comment(self, comment_id, body):
        """Update an existing conversation comment in place."""
        resp = self._retry(self.issue_base, "/comments/%s" % comment_id,
                           data={"body": body}, method="PATCH")
        if not resp.get("success"):
            return {"success": False, "http_status": resp.get("status"),
                    "error_body": resp.get("error_body")}
        data = resp.get("data") or {}
        return {"success": True, "id": data.get("id"), "url": data.get("html_url")}

    def list_reviews(self):
        """List the reviews of the pull request."""
        resp = self._retry(self.pull_base, "/reviews?limit=50")
        if not resp.get("success"):
            return None
        return resp.get("data") or []

    def list_review_comments(self, review_id):
        """List the inline comments of one review."""
        resp = self._retry(self.pull_base, "/reviews/%s/comments" % review_id)
        if not resp.get("success"):
            return None
        return resp.get("data") or []

    def create_review(self, body, comments, commit_id, event="COMMENT"):
        """Create one review carrying all its inline comments.

        Gitea applies the request atomically: a single unresolvable line
        rejects the whole review. Callers must therefore validate every line
        against the diff before calling this.
        """
        payload = {"body": body, "event": event}
        if commit_id:
            payload["commit_id"] = commit_id
        if comments:
            payload["comments"] = comments
        resp = self._retry(self.pull_base, "/reviews", data=payload, method="POST")
        if not resp.get("success"):
            return {"success": False, "http_status": resp.get("status"),
                    "error_body": resp.get("error_body")}
        data = resp.get("data") or {}
        return {"success": True, "id": data.get("id"), "url": data.get("html_url"),
                "comments_count": data.get("comments_count")}


class DryRunPoster:
    """Poster used by ``--dry-run``: records calls instead of performing them."""

    def __init__(self):
        self.calls = []

    def list_comments(self):
        return []

    def create_comment(self, body):
        self.calls.append(("create_comment", {"body": body}))
        return {"success": True, "id": "dry-run", "url": "(dry-run)"}

    def update_comment(self, comment_id, body):
        self.calls.append(("update_comment", {"id": comment_id, "body": body}))
        return {"success": True, "id": comment_id, "url": "(dry-run)"}

    def list_reviews(self):
        return []

    def list_review_comments(self, review_id):
        return []

    def create_review(self, body, comments, commit_id, event="COMMENT"):
        self.calls.append(("create_review", {"body": body, "comments": comments,
                                             "commit_id": commit_id, "event": event}))
        return {"success": True, "id": "dry-run", "url": "(dry-run)",
                "comments_count": len(comments or [])}


def make_poster(pull_base, issue_base, token, config):
    return GiteaPoster(pull_base, issue_base, token, config)


# --------------------------------------------------------------------------- #
# Summary helpers
# --------------------------------------------------------------------------- #

def find_summary_comment(comments, tag):
    """Find the comment carrying ``tag`` in its body."""
    if not tag or not comments:
        return None
    for c in comments:
        if tag in (c.get("body") or ""):
            return c
    return None


def upsert_summary(poster, body, sticky, tag=None):
    """Find-or-create/update the summary conversation comment.

    Sticky matches the cross-run :data:`SUMMARY_MARKER`; non-sticky matches
    this run's ``tag`` (per-run), so retries within a run update the comment
    in place rather than creating duplicates.
    """
    comments = poster.list_comments()
    if comments is None:
        log("[summary] cannot list comments for upsert; skipping to avoid duplicate.")
        return None
    needle = SUMMARY_MARKER if sticky else tag
    existing = find_summary_comment(comments, needle) if needle else None
    if existing is not None:
        resp = poster.update_comment(existing.get("id"), body)
        if not (resp or {}).get("success"):
            log("[summary] failed to update comment %s (HTTP %s): %s; stale content may remain."
                % (existing.get("id"), (resp or {}).get("http_status"),
                   _truncate_error((resp or {}).get("error_body"))))
        return (resp or {}).get("url") or existing.get("html_url")
    resp = poster.create_comment(body)
    return (resp or {}).get("url")


def ensure_summary_anchor(poster, body, sticky, tag=None):
    """Create the summary comment used as the anchor for the run."""
    return upsert_summary(poster, body, sticky, tag=tag)


def load_incremental_history(poster):
    """Return a list of ``{path, span}`` for prior bot findings.

    None when the read API failed (caller then disables incremental filtering
    rather than risk re-posting everything).
    """
    reviews = poster.list_reviews()
    if reviews is None:
        log("[incremental] could not list reviews; disabling incremental filter for this run.")
        return None
    history = []
    for review in reviews:
        review_id = review.get("id")
        if review_id is None:
            continue
        comments = poster.list_review_comments(review_id)
        if comments is None:
            continue
        for c in comments:
            body = c.get("body") or ""
            if _BOT_MARKER not in body:
                continue
            line = _num(c.get("line") or c.get("new_position"))
            path = c.get("tree_path") or c.get("path")
            if line is None or not path:
                continue
            history.append({"path": path, "span": {"start": line, "end": line,
                                                    "multiline": False}})
    return history


# --------------------------------------------------------------------------- #
# Publication
# --------------------------------------------------------------------------- #

def chunk_comments(items, size=MAX_COMMENTS_PER_REVIEW):
    """Split inline items into chunks of at most ``size`` entries."""
    if size <= 0:
        size = MAX_COMMENTS_PER_REVIEW
    return [items[i:i + size] for i in range(0, len(items), size)]


def publish(result, diff_refs, poster, config, sleep=_sleep):
    """Post the review result via the injectable ``poster`` object.

    Returns a stats dict with mutually-exclusive counts::

        {total, inline, summary, routed, skipped, failed, summary_url}
    """
    comments = result.get("comments") or []
    warnings = result.get("warnings") or []
    sticky = config.get("sticky_summary", True)
    incremental = config.get("incremental", False)
    overlap_threshold = config.get("incremental_overlap_threshold", DEFAULT_OVERLAP_THRESHOLD)
    policy = build_policy(config.get("route_severity_below", ""),
                          config.get("route_categories", ""))
    success_delay = config["success_delay"]
    failure_delay = config["failure_delay"]
    stats = {"total": len(comments), "inline": 0, "summary": 0,
             "routed": 0, "skipped": 0, "failed": 0, "summary_url": None}

    # No comments: LGTM summary (sticky-aware).
    if not comments:
        message = result.get("message", "No comments generated. Looks good to me.")
        body = wrap_summary_body("✅ **OpenCodeReview**: %s" % message,
                                 config.get("run_tag", "0-0"))
        stats["summary_url"] = upsert_summary(poster, body, sticky,
                                              tag=summary_tag_for(config.get("run_tag", "0-0")))
        return stats

    inventory = diff_refs.get("inventory") if diff_refs else None
    head_sha = diff_refs.get("head_sha") if diff_refs else None

    # Partition: inline / no-line / routed / not-in-diff.
    inline_items = []
    no_line = []
    routed = []
    unverifiable = []
    for index, comment in enumerate(comments):
        path = comment.get("path", "")
        end_line = comment.get("end_line", 0)
        # Inline posting needs a valid end_line. A start_line-only comment
        # cannot be positioned and must land in the summary, not be
        # misclassified as a posting failure.
        has_line = bool(end_line and end_line >= 1)
        if not has_line or not path:
            no_line.append({"comment": comment, "reason": NO_LINE_REASON})
            continue
        route = route_comment(comment, policy)
        if route["routed"]:
            routed.append({"comment": comment, "reason": route["reason"]})
            continue
        # An empty inventory means the diff could not be parsed (or the run is
        # a dry run), not that every line is invalid. Treat it as "unknown"
        # and let the server resolve the lines, rather than silently demoting
        # every finding to the summary.
        if inventory:
            reason = validate_comment_against_diff(comment, inventory)
            if reason:
                unverifiable.append({"comment": comment, "reason": reason})
                continue
        cid = new_comment_id("%s:%s:%s:%s" % (
            config.get("run_tag", "0-0"), path, comment.get("start_line"), index))
        inline_items.append({"comment": comment, "body": format_comment(comment, cid),
                             "id": cid, "index": index})

    stats["summary"] = len(no_line) + len(unverifiable)
    stats["routed"] = len(routed)

    # Deterministic order so reruns reproduce the same post sequence.
    inline_items = sort_to_send(inline_items)

    # Incremental filtering: drop comments overlapping prior bot findings.
    if incremental and inline_items:
        history = load_incremental_history(poster)
        if history is not None:
            kept = []
            for it in inline_items:
                span = comment_span(it["comment"])
                if span and overlaps_history(it["comment"], span, history, overlap_threshold):
                    stats["skipped"] += 1
                    continue
                kept.append(it)
            if stats["skipped"]:
                log("[incremental] skipped %d overlapping comment(s); %d to post."
                    % (stats["skipped"], len(kept)))
            inline_items = kept

    # Summary anchor (pre-review body) - created before the review so it pins
    # above the findings on the first run.
    run_tag = config.get("run_tag", "0-0")
    anchor_body = wrap_summary_body(
        build_pre_review_summary_body(stats["total"], no_line + unverifiable, routed, warnings),
        run_tag,
    )
    ensure_summary_anchor(poster, anchor_body, sticky,
                          tag=summary_tag_for(run_tag))

    # Post inline comments. Gitea applies a review atomically, so the batch is
    # validated against the diff beforehand and only then submitted.
    failed_comments = []
    for chunk in chunk_comments(inline_items):
        payload = []
        for it in chunk:
            comment = it["comment"]
            span = comment_span(comment)
            payload.append({
                "body": it["body"],
                "path": comment.get("path", ""),
                # Gitea's new_position is the new-file line number; the
                # server resolves it against the patch.
                "new_position": span["end"] if span else comment.get("end_line", 0),
                "old_position": 0,
            })
        review_body = _build_review_body(len(payload))
        resp = poster.create_review(review_body, payload, head_sha, "COMMENT")
        if not (resp or {}).get("success"):
            log("[inline] review failed: HTTP %s %s"
                % ((resp or {}).get("http_status"), _truncate_error((resp or {}).get("error_body"))))
            for it in chunk:
                failed_comments.append({"comment": it["comment"],
                                        "reason": "review request failed"})
            stats["failed"] += len(chunk)
            sleep(failure_delay)
            continue
        stats["inline"] += len(payload)
        sleep(success_delay)

    # Finalize the summary with the real counts.
    final_body = wrap_summary_body(
        build_summary_body(stats["total"], stats["inline"], stats["summary"],
                           stats["skipped"], stats["routed"], stats["failed"], warnings)
        + format_summary_comments(no_line)
        + format_summary_comments(unverifiable)
        + format_summary_comments(routed)
        + format_summary_comments(failed_comments)
        + format_warnings(warnings),
        run_tag,
    )
    stats["summary_url"] = upsert_summary(poster, final_body, sticky,
                                           tag=summary_tag_for(run_tag))
    return stats


def _build_review_body(count):
    """Build the body of a review that carries inline comments only.

    The review body is immutable in Gitea, so it only holds a short pointer;
    the mutable summary lives in a conversation comment.
    """
    if count <= 0:
        return "🔍 **OpenCodeReview**"
    return "🔍 **OpenCodeReview**: %d inline comment(s). See the summary comment below." % count


# --------------------------------------------------------------------------- #
# Config + stats
# --------------------------------------------------------------------------- #

def build_config(env):
    """Build the config dict from environment variables (with defaults)."""
    return {
        # Pacing (used by publish).
        "success_delay": int(env.get("OCR_SUCCESS_DELAY", "2000")) / 1000,
        "failure_delay": int(env.get("OCR_FAILURE_DELAY", "1000")) / 1000,
        "rate_limit_threshold": int(env.get("OCR_RATE_LIMIT_THRESHOLD", "10")),
        # Read-API pacing.
        "read_success_delay": int(env.get("OCR_READ_SUCCESS_DELAY", "500")) / 1000,
        "read_low_remaining_spacing": int(env.get("OCR_READ_LOW_REMAINING_SPACING", "5000")) / 1000,
        # Retry (used by make_poster / fetch_pull_request).
        "retry_base_delay": int(env.get("OCR_RETRY_BASE_DELAY", "2000")) / 1000,
        "max_retries": int(env.get("OCR_MAX_RETRIES", "3")),
        "max_retry_delay": int(env.get("OCR_MAX_RETRY_DELAY", "60000")) / 1000,
        # Publication policy + summary + incremental.
        "sticky_summary": _parse_bool(env.get("OCR_STICKY_SUMMARY", "true"), default=True),
        "incremental": _parse_bool(env.get("OCR_INCREMENTAL", "false"), default=False),
        "incremental_overlap_threshold": resolve_threshold(env.get("OCR_INCREMENTAL_OVERLAP_THRESHOLD", "0.6")),
        "route_severity_below": env.get("OCR_ROUTE_SEVERITY_BELOW", ""),
        "route_categories": env.get("OCR_ROUTE_CATEGORIES", ""),
        # Job gating.
        "fail_on_severity": env.get("OCR_FAIL_ON_SEVERITY", ""),
        # Per-run identity for idempotency tags.
        "run_tag": run_tag_from_env(env),
    }


ZERO_STATS = {
    "total": 0, "inline": 0, "summary": 0, "routed": 0,
    "skipped": 0, "failed": 0, "summary_url": None,
}


def write_stats_file(path, stats):
    """Write the dotenv stats file consumed by later CI stages."""
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for key in ("OCR_COMMENTS_TOTAL", "OCR_COMMENTS_INLINE",
                        "OCR_COMMENTS_SUMMARY", "OCR_COMMENTS_ROUTED",
                        "OCR_COMMENTS_SKIPPED", "OCR_COMMENTS_FAILED"):
                field = {
                    "OCR_COMMENTS_TOTAL": "total",
                    "OCR_COMMENTS_INLINE": "inline",
                    "OCR_COMMENTS_SUMMARY": "summary",
                    "OCR_COMMENTS_ROUTED": "routed",
                    "OCR_COMMENTS_SKIPPED": "skipped",
                    "OCR_COMMENTS_FAILED": "failed",
                }[key]
                f.write("%s=%s\n" % (key, stats.get(field, 0)))
            f.write("OCR_SUMMARY_URL=%s\n" % (stats.get("summary_url") or ""))
    except OSError as e:
        log("[warn] could not write stats file %s: %s" % (path, e))


def check_fail_on_severity(comments, threshold):
    """True when any finding's severity is at-or-above ``threshold``."""
    if not threshold:
        return False
    rank = SEVERITY_RANK.get(str(threshold).strip().lower())
    if rank is None:
        return False
    for c in comments:
        sev = sanitize_metadata(c.get("severity") if c else None).strip().lower()
        if sev in SEVERITY_RANK and SEVERITY_RANK[sev] >= rank:
            return True
    return False


# --------------------------------------------------------------------------- #
# Commit status
# --------------------------------------------------------------------------- #

def post_commit_status(repo_base, token, sha, state, description="", context="",
                       target_url="", config=None):
    """Report a commit status on the head commit of the pull request."""
    if not sha:
        log("[status] no head SHA available; skipping commit status.")
        return False
    payload = {"state": state}
    if description:
        payload["description"] = description[:255]
    if context:
        payload["context"] = context
    if target_url:
        payload["target_url"] = target_url
    resp = _api_request_with_retry(repo_base, token, "/statuses/%s" % sha,
                                   data=payload, method="POST",
                                   config=config or {})
    if not resp.get("success"):
        log("[status] failed to post commit status: HTTP %s %s"
            % (resp.get("status"), _truncate_error(resp.get("error_body"))))
        return False
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def load_review_result(path):
    """Read the JSON produced by ``ocr review --format json``."""
    with open(path, encoding="utf-8") as f:
        return json.loads(f.read())


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Post `ocr review --format json` output onto a Gitea pull request."
    )
    p.add_argument("input", nargs="?", default=".ocr/ocr-result.json",
                   help="review result JSON path (default: .ocr/ocr-result.json)")
    p.add_argument("--stderr-log", default=".ocr/ocr-stderr.log",
                   help="OCR stderr log path, read on parse failure (default: .ocr/ocr-stderr.log)")
    p.add_argument("--stats-file", default=".ocr/ocr-stats.env",
                   help="dotenv output path for posting stats (default: .ocr/ocr-stats.env)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the planned requests instead of posting them")
    return p.parse_args(argv)


def _resolve_ci_context(env):
    """Resolve the Gitea instance URL, owner/repo and pull request index."""
    server_url = (
        env.get("GITEA_SERVER_URL")
        or env.get("GITHUB_SERVER_URL")
        or env.get("CI_SERVER_URL")
        or ""
    ).rstrip("/")
    repository = (
        env.get("GITEA_REPOSITORY")
        or env.get("GITHUB_REPOSITORY")
        or env.get("CI_PROJECT_PATH")
        or ""
    )
    owner = env.get("GITEA_REPOSITORY_OWNER") or env.get("GITHUB_REPOSITORY_OWNER") or ""
    repo = env.get("GITEA_REPOSITORY_NAME") or env.get("GITHUB_REPOSITORY_NAME") or ""
    pr_index = (
        env.get("GITEA_PULL_REQUEST_INDEX")
        or env.get("GITEA_PR_INDEX")
        or env.get("GITHUB_PULL_REQUEST_NUMBER")
        or env.get("CI_MERGE_REQUEST_IID")
        or ""
    )
    if (not owner or not repo) and "/" in repository:
        owner, repo = repository.split("/", 1)
    return server_url, owner, repo, pr_index


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ

    server_url, owner, repo, pr_index = _resolve_ci_context(env)
    api_token = env.get("GITEA_TOKEN") or env.get("GITEA_API_TOKEN") or env.get("GITHUB_TOKEN") or ""

    if not args.dry_run:
        missing = [name for name, value in (
            ("GITEA_SERVER_URL", server_url),
            ("repository owner", owner),
            ("repository name", repo),
            ("pull request index", pr_index),
        ) if not value]
        if missing:
            log("error: missing required %s (set via CI environment)" % ", ".join(missing))
            write_stats_file(args.stats_file, ZERO_STATS)
            return 1
        if not api_token:
            log("ERROR: No API token available (GITEA_TOKEN). Cannot post comments.")
            write_stats_file(args.stats_file, ZERO_STATS)
            return 1

    pull_base = "%s/api/v1/repos/%s/%s/pulls/%s" % (server_url, owner, repo, pr_index)
    issue_base = "%s/api/v1/repos/%s/%s/issues/%s" % (server_url, owner, repo, pr_index)
    repo_base = "%s/api/v1/repos/%s/%s" % (server_url, owner, repo)

    config = build_config(env)

    # Read OCR result.
    try:
        result = load_review_result(args.input)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log("Failed to parse OCR output: %s" % e)
        write_stats_file(args.stats_file, ZERO_STATS)
        if args.dry_run:
            log("(dry-run: skipping error comment post)")
            return 0
        stderr_content = ""
        try:
            with open(args.stderr_log, "r", encoding="utf-8") as f:
                stderr_content = f.read().strip()
        except FileNotFoundError:
            pass
        if stderr_content:
            poster = make_poster(pull_base, issue_base, api_token, config)
            run_tag = config.get("run_tag", "0-0")
            body = wrap_summary_body(
                "⚠️ **OpenCodeReview** encountered an error:\n%s" % fenced_block(stderr_content),
                run_tag,
            )
            upsert_summary(poster, body, config.get("sticky_summary", True),
                           tag=summary_tag_for(run_tag))
        return 0

    comments = result.get("comments", [])

    if args.dry_run:
        poster = DryRunPoster()
        diff_refs = {"head_sha": "(dry)", "base_sha": "(dry)", "inventory": {}}
    else:
        poster = make_poster(pull_base, issue_base, api_token, config)
        pr = fetch_pull_request(pull_base, api_token, config) or {}
        patch = fetch_pr_diff(pull_base, api_token, config)
        inventory = parse_diff_hunk_inventory(patch) if patch else None
        if inventory is None:
            log("Warning: Could not fetch the pull request diff. "
                "Inline comments cannot be validated and will be skipped.")
        diff_refs = {"head_sha": pr.get("head_sha"), "base_sha": pr.get("base_sha"),
                     "inventory": inventory}

    stats = publish(result, diff_refs, poster, config, sleep=_sleep)
    write_stats_file(args.stats_file, stats)

    if args.dry_run:
        for name, payload in getattr(poster, "calls", []):
            log("[dry-run] %s %s" % (name, json.dumps(payload, ensure_ascii=False)[:400]))
        return 0

    # Report the outcome as a commit status so the PR shows a check.
    state = "failure" if stats["failed"] else "success"
    description = "%d issue(s): %d inline, %d in summary" % (
        stats["total"], stats["inline"], stats["summary"])
    post_commit_status(repo_base, api_token, diff_refs.get("head_sha"), state,
                        description=description,
                        context=env.get("OCR_STATUS_CONTEXT", "open-code-review"),
                        target_url=env.get("OCR_STATUS_TARGET_URL", ""),
                        config=config)

    if check_fail_on_severity(comments, config.get("fail_on_severity", "")):
        log("Failing job: review contains severity at or above '%s'." % config["fail_on_severity"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

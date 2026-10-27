#!/usr/bin/env python3

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""Unit tests for the Gitea review publishing glue.

The tests use only the standard-library ``unittest`` and cover: comment
formatting (badge, suggestion, fallback), the publication policy
(severity/category routing), deterministic sort, warning rendering,
safe-fence code blocks, the diff inventory and line validation, the full
``publish()`` flow (partition -> sticky summary -> review creation), the
chunking of review payloads, the incremental overlap test, the CI context
resolution and the severity gating.

No network access and no wall-clock sleep cost: the transport is exercised
through the injectable poster object.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import post_review as pr  # noqa: E402


# The hunk header must agree with the number of lines the hunk carries:
# 3 context lines plus 3 added lines make 6 new-file lines.
PATCH = """diff --git a/src/main.go b/src/main.go
index abc1234..def5678 100644
--- a/src/main.go
+++ b/src/main.go
@@ -10,4 +10,6 @@ func main() {
 \tctx := context.Background()
+\tif err := run(ctx); err != nil {
+\t\treturn err
+\t}
 \tfmt.Println("done")
 }
diff --git a/src/other.go b/src/other.go
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/src/other.go
@@ -0,0 +1,2 @@
+package main
+
"""


class RecordingPoster:
    """Poster that records every call instead of performing it."""

    def __init__(self, comments=None, reviews=None, review_comments=None):
        self.existing_comments = comments or []
        self.existing_reviews = reviews or []
        self.review_comments = review_comments or {}
        self.created_comments = []
        self.updated_comments = []
        self.created_reviews = []

    def list_comments(self):
        return list(self.existing_comments)

    def create_comment(self, body):
        self.created_comments.append(body)
        return {"success": True, "id": len(self.created_comments), "url": "http://c/%d" % len(self.created_comments)}

    def update_comment(self, comment_id, body):
        self.updated_comments.append((comment_id, body))
        return {"success": True, "id": comment_id, "url": "http://c/%s" % comment_id}

    def list_reviews(self):
        return list(self.existing_reviews)

    def list_review_comments(self, review_id):
        return list(self.review_comments.get(review_id, []))

    def create_review(self, body, comments, commit_id, event="COMMENT"):
        self.created_reviews.append({"body": body, "comments": comments,
                                     "commit_id": commit_id, "event": event})
        return {"success": True, "id": len(self.created_reviews),
                "url": "http://r/%d" % len(self.created_reviews),
                "comments_count": len(comments or [])}


def make_config(**overrides):
    env = {
        "OCR_SUCCESS_DELAY": "0",
        "OCR_FAILURE_DELAY": "0",
        "OCR_RETRY_BASE_DELAY": "0",
        "OCR_MAX_RETRY_DELAY": "0",
    }
    env.update(overrides)
    config = pr.build_config(env)
    config["success_delay"] = 0
    config["failure_delay"] = 0
    return config


def no_sleep(_seconds):
    return None


class FormattingTests(unittest.TestCase):
    def test_badge_with_category_and_severity(self):
        self.assertEqual(pr.build_badge({"category": "bug", "severity": "high"}),
                         "[bug · high]")

    def test_badge_with_only_one_field(self):
        self.assertEqual(pr.build_badge({"category": "bug"}), "[bug]")
        self.assertEqual(pr.build_badge({"severity": "low"}), "[low]")

    def test_badge_absent_when_no_metadata(self):
        self.assertEqual(pr.build_badge({"content": "x"}), "")

    def test_badge_strips_control_characters(self):
        badge = pr.build_badge({"category": "bug\x07", "severity": "high\n"})
        self.assertEqual(badge, "[bug · high]")

    def test_format_comment_includes_id_tag_and_badge(self):
        body = pr.format_comment({"content": "boom", "category": "bug",
                                   "severity": "high"}, "ocr-abc")
        self.assertIn("<!-- ocr-abc -->", body)
        self.assertIn("[bug · high]", body)
        self.assertIn("boom", body)

    def test_format_comment_renders_suggestion_block(self):
        body = pr.format_comment({"content": "boom",
                                   "suggestion_code": "fixed()",
                                   "existing_code": "old()"})
        self.assertIn("```suggestion", body)
        self.assertIn("fixed()", body)

    def test_suggestion_with_backticks_gets_a_longer_fence(self):
        body = pr.format_comment({"content": "c", "existing_code": "x",
                                   "suggestion_code": "s = '```'"})
        self.assertIn("````suggestion\ns = '```'\n````", body)

    def test_format_comment_fallback_includes_path_and_reason(self):
        md = pr.format_comment_fallback({"path": "a.go", "content": "c",
                                          "start_line": 3, "end_line": 5},
                                         "because")
        self.assertIn("`a.go`", md)
        self.assertIn("(L3-L5)", md)
        self.assertIn("because", md)

    def test_safe_fence_wider_than_backticks_in_content(self):
        self.assertEqual(pr.safe_fence("a ``` b"), "````")

    def test_fenced_block_round_trip(self):
        block = pr.fenced_block("x")
        self.assertTrue(block.startswith("```"))
        self.assertTrue(block.rstrip().endswith("```"))

    def test_warning_rendering(self):
        body = pr.format_warnings([{"file": "a.go", "type": "warn", "message": "m"}])
        self.assertIn("`a.go`", body)
        self.assertIn("m", body)

    def test_warning_rendering_with_string(self):
        body = pr.format_warnings(["plain"])
        self.assertIn("plain", body)

    def test_warning_rendering_empty(self):
        self.assertEqual(pr.format_warnings([]), "")


class PolicyTests(unittest.TestCase):
    def test_no_routing_by_default(self):
        policy = pr.build_policy("", "")
        self.assertFalse(policy["route_by_severity"])
        self.assertFalse(policy["route_by_category"])

    def test_route_by_severity_below(self):
        policy = pr.build_policy("medium", "")
        self.assertTrue(policy["route_by_severity"])
        route = pr.route_comment({"severity": "low"}, policy)
        self.assertTrue(route["routed"])
        self.assertIn("low", route["reason"])

    def test_severity_above_threshold_not_routed(self):
        policy = pr.build_policy("medium", "")
        self.assertFalse(pr.route_comment({"severity": "critical"}, policy)["routed"])

    def test_route_by_category(self):
        policy = pr.build_policy("", "style,documentation")
        self.assertTrue(pr.route_comment({"category": "style"}, policy)["routed"])
        self.assertFalse(pr.route_comment({"category": "bug"}, policy)["routed"])

    def test_unknown_metadata_falls_open(self):
        policy = pr.build_policy("medium", "")
        self.assertFalse(pr.route_comment({"severity": "weird"}, policy)["routed"])
        self.assertFalse(pr.route_comment({}, policy)["routed"])

    def test_unknown_threshold_disables_routing(self):
        policy = pr.build_policy("nope", "")
        self.assertFalse(policy["route_by_severity"])


class SortTests(unittest.TestCase):
    def test_sort_is_deterministic(self):
        items = [
            {"comment": {"path": "b.go", "start_line": 2, "end_line": 2}, "index": 0},
            {"comment": {"path": "a.go", "start_line": 9, "end_line": 9}, "index": 1},
            {"comment": {"path": "a.go", "start_line": 1, "end_line": 1}, "index": 2},
        ]
        ordered = pr.sort_to_send(items)
        self.assertEqual([it["comment"]["path"] for it in ordered],
                         ["a.go", "a.go", "b.go"])
        self.assertEqual(ordered[0]["comment"]["start_line"], 1)

    def test_sort_keeps_original_index_for_ties(self):
        items = [
            {"comment": {"path": "a.go", "start_line": 1, "end_line": 1}, "index": 7},
            {"comment": {"path": "a.go", "start_line": 1, "end_line": 1}, "index": 3},
        ]
        ordered = pr.sort_to_send(items)
        self.assertEqual([it["index"] for it in ordered], [3, 7])


class SpanTests(unittest.TestCase):
    def test_single_line_span(self):
        span = pr.comment_span({"start_line": 4, "end_line": 4})
        self.assertFalse(span["multiline"])
        self.assertEqual(span["start"], 4)

    def test_multiline_span(self):
        span = pr.comment_span({"start_line": 4, "end_line": 8})
        self.assertTrue(span["multiline"])
        self.assertEqual((span["start"], span["end"]), (4, 8))

    def test_span_from_start_line_only(self):
        span = pr.comment_span({"start_line": 4})
        self.assertFalse(span["multiline"])
        self.assertEqual(span["start"], 4)

    def test_span_none_when_no_lines(self):
        self.assertIsNone(pr.comment_span({"content": "x"}))

    def test_same_span_single_line(self):
        a = {"start": 4, "end": 4, "multiline": False}
        b = {"start": 4, "end": 4, "multiline": False}
        self.assertTrue(pr.same_comment_span(a, b, 0.6))

    def test_single_line_never_matches_multiline(self):
        a = {"start": 4, "end": 4, "multiline": False}
        b = {"start": 4, "end": 8, "multiline": True}
        self.assertFalse(pr.same_comment_span(a, b, 0.6))

    def test_multiline_overlap_threshold(self):
        a = {"start": 10, "end": 20, "multiline": True}
        b = {"start": 12, "end": 22, "multiline": True}
        # IoU = 9 / 13 = 0.69 > 0.6
        self.assertTrue(pr.same_comment_span(a, b, 0.6))
        # IoU = 9 / 13 = 0.69 < 0.9
        self.assertFalse(pr.same_comment_span(a, b, 0.9))

    def test_multiline_disjoint(self):
        a = {"start": 10, "end": 12, "multiline": True}
        b = {"start": 30, "end": 40, "multiline": True}
        self.assertFalse(pr.same_comment_span(a, b, 0.6))


class DiffInventoryTests(unittest.TestCase):
    def setUp(self):
        self.inventory = pr.parse_diff_hunk_inventory(PATCH)

    def test_paths_detected(self):
        self.assertIn("src/main.go", self.inventory)
        self.assertIn("src/other.go", self.inventory)

    def test_added_and_context_lines_present(self):
        new = self.inventory["src/main.go"]["new"]
        # Context line 10, added lines 11-13, context lines 14-15.
        for line in (10, 11, 12, 13, 14, 15):
            self.assertIn(line, new)

    def test_added_lines_marked(self):
        new = self.inventory["src/main.go"]["new"]
        self.assertEqual(new[11], "added")
        self.assertEqual(new[10], "context")

    def test_new_file_lines_present(self):
        self.assertIn(1, self.inventory["src/other.go"]["new"])
        self.assertIn(2, self.inventory["src/other.go"]["new"])

    def test_line_is_anchorable(self):
        self.assertTrue(pr.line_is_anchorable(self.inventory, "src/main.go", 13))

    def test_line_out_of_range_not_anchorable(self):
        self.assertFalse(pr.line_is_anchorable(self.inventory, "src/main.go", 99))

    def test_unknown_path_not_anchorable(self):
        self.assertFalse(pr.line_is_anchorable(self.inventory, "src/nope.go", 1))

    def test_empty_patch_yields_empty_inventory(self):
        self.assertEqual(pr.parse_diff_hunk_inventory(""), {})

    def test_validate_comment_accepts_diff_line(self):
        comment = {"path": "src/main.go", "start_line": 13, "end_line": 13}
        self.assertIsNone(pr.validate_comment_against_diff(comment, self.inventory))

    def test_validate_comment_rejects_out_of_diff_line(self):
        comment = {"path": "src/main.go", "start_line": 99, "end_line": 99}
        self.assertEqual(pr.validate_comment_against_diff(comment, self.inventory),
                         pr.LINE_NOT_IN_DIFF_REASON)

    def test_validate_comment_rejects_unknown_path(self):
        comment = {"path": "src/nope.go", "start_line": 1, "end_line": 1}
        self.assertEqual(pr.validate_comment_against_diff(comment, self.inventory),
                         pr.PATH_NOT_IN_DIFF_REASON)

    def test_validate_multiline_uses_end_line_as_anchor(self):
        comment = {"path": "src/main.go", "start_line": 11, "end_line": 13}
        self.assertIsNone(pr.validate_comment_against_diff(comment, self.inventory))


TRUNCATED_PATCH = """diff --git a/src/main.go b/src/main.go
index abc1234..def5678 100644
--- a/src/main.go
+++ b/src/main.go
@@ -10,4 +10,6 @@ func main() {
 \tctx := context.Background()
+\tif err := run(ctx); err != nil {
+\t\treturn err
"""


class TruncatedDiffTests(unittest.TestCase):
    """A patch that was cut short cannot prove a finding out of diff.

    Gitea applies a review atomically: one unresolvable line rejects the
    whole request. A truncated patch would therefore make the script drop
    legitimate findings into the summary, so the inventory has to report the
    truncation and the validation has to stop rejecting on partial data.
    """

    def setUp(self):
        self.inventory = pr.parse_diff_hunk_inventory(TRUNCATED_PATCH)

    def test_truncated_patch_is_not_complete(self):
        self.assertFalse(pr.inventory_is_complete(self.inventory))

    def test_complete_patch_is_complete(self):
        self.assertTrue(pr.inventory_is_complete(
            pr.parse_diff_hunk_inventory(PATCH)))

    def test_empty_inventory_is_not_complete(self):
        self.assertFalse(pr.inventory_is_complete({}))

    def test_out_of_diff_line_is_not_rejected_on_truncated_data(self):
        comment = {"path": "src/main.go", "start_line": 40, "end_line": 40}
        self.assertIsNone(pr.validate_comment_against_diff(comment, self.inventory))

    def test_unknown_path_is_not_rejected_on_truncated_data(self):
        comment = {"path": "src/never_seen.go", "start_line": 1, "end_line": 1}
        self.assertIsNone(pr.validate_comment_against_diff(comment, self.inventory))

    def test_missing_line_information_is_still_rejected(self):
        comment = {"path": "src/main.go", "content": "x"}
        self.assertEqual(pr.validate_comment_against_diff(comment, self.inventory),
                         pr.NO_LINE_REASON)

    def test_out_of_diff_line_is_rejected_on_complete_data(self):
        complete = pr.parse_diff_hunk_inventory(PATCH)
        comment = {"path": "src/main.go", "start_line": 999, "end_line": 999}
        self.assertEqual(pr.validate_comment_against_diff(comment, complete),
                         pr.LINE_NOT_IN_DIFF_REASON)

    def test_unknown_path_is_rejected_on_complete_data(self):
        complete = pr.parse_diff_hunk_inventory(PATCH)
        comment = {"path": "src/never_seen.go", "start_line": 1, "end_line": 1}
        self.assertEqual(pr.validate_comment_against_diff(comment, complete),
                         pr.PATH_NOT_IN_DIFF_REASON)


class ChunkTests(unittest.TestCase):
    def test_chunk_splits_evenly(self):
        items = list(range(45))
        chunks = pr.chunk_comments(items, 20)
        self.assertEqual([len(c) for c in chunks], [20, 20, 5])

    def test_chunk_single(self):
        chunks = pr.chunk_comments([1, 2], 20)
        self.assertEqual(len(chunks), 1)

    def test_chunk_empty(self):
        self.assertEqual(pr.chunk_comments([], 20), [])


class PublishTests(unittest.TestCase):
    def _result(self, comments, warnings=None):
        return {"comments": comments, "warnings": warnings or [],
                "message": "msg"}

    def _diff_refs(self, inventory=None):
        return {"head_sha": "deadbeef", "base_sha": "cafe",
                "inventory": inventory}

    def test_no_comments_posts_lgtm_summary(self):
        poster = RecordingPoster()
        stats = pr.publish({"comments": [], "message": "Looks good"},
                           self._diff_refs(), poster, make_config(), sleep=no_sleep)
        self.assertEqual(stats["total"], 0)
        self.assertEqual(len(poster.created_comments), 1)
        self.assertIn("Looks good", poster.created_comments[0])
        self.assertEqual(poster.created_reviews, [])

    def test_inline_comment_creates_review_with_new_position(self):
        poster = RecordingPoster()
        comments = [{"path": "src/main.go", "content": "boom",
                     "start_line": 13, "end_line": 13,
                     "category": "bug", "severity": "high"}]
        stats = pr.publish(self._result(comments), self._diff_refs(),
                           poster, make_config(), sleep=no_sleep)
        self.assertEqual(stats["inline"], 1)
        self.assertEqual(len(poster.created_reviews), 1)
        review = poster.created_reviews[0]
        self.assertEqual(review["commit_id"], "deadbeef")
        self.assertEqual(review["event"], "COMMENT")
        self.assertEqual(review["comments"][0]["new_position"], 13)
        self.assertEqual(review["comments"][0]["old_position"], 0)
        self.assertEqual(review["comments"][0]["path"], "src/main.go")
        self.assertIn("boom", review["comments"][0]["body"])

    def test_comment_without_line_goes_to_summary(self):
        poster = RecordingPoster()
        comments = [{"path": "src/main.go", "content": "boom"}]
        stats = pr.publish(self._result(comments), self._diff_refs(),
                           poster, make_config(), sleep=no_sleep)
        self.assertEqual(stats["inline"], 0)
        self.assertEqual(stats["summary"], 1)
        self.assertEqual(poster.created_reviews, [])

    def test_comment_outside_diff_goes_to_summary(self):
        poster = RecordingPoster()
        inventory = pr.parse_diff_hunk_inventory(PATCH)
        comments = [{"path": "src/main.go", "content": "boom",
                     "start_line": 99, "end_line": 99}]
        stats = pr.publish(self._result(comments),
                           self._diff_refs(inventory), poster, make_config(),
                           sleep=no_sleep)
        self.assertEqual(stats["inline"], 0)
        self.assertEqual(stats["summary"], 1)
        self.assertEqual(poster.created_reviews, [])

    def test_empty_inventory_keeps_comments_inline(self):
        # An empty inventory means "diff unknown", not "all lines invalid".
        poster = RecordingPoster()
        comments = [{"path": "src/main.go", "content": "boom",
                     "start_line": 13, "end_line": 13}]
        stats = pr.publish(self._result(comments), self._diff_refs({}),
                           poster, make_config(), sleep=no_sleep)
        self.assertEqual(stats["inline"], 1)

    def test_routed_comment_is_not_posted_inline(self):
        poster = RecordingPoster()
        comments = [{"path": "src/main.go", "content": "style",
                     "start_line": 13, "end_line": 13,
                     "category": "style", "severity": "low"}]
        config = make_config(OCR_ROUTE_CATEGORIES="style")
        stats = pr.publish(self._result(comments), self._diff_refs(),
                           poster, config, sleep=no_sleep)
        self.assertEqual(stats["routed"], 1)
        self.assertEqual(stats["inline"], 0)
        self.assertEqual(poster.created_reviews, [])

    def test_sticky_summary_updates_existing_comment(self):
        existing = [{"id": 42, "body": "<!-- ocr-summary -->\nold"}]
        poster = RecordingPoster(comments=existing)
        stats = pr.publish({"comments": [], "message": "x"}, self._diff_refs(),
                           poster, make_config(), sleep=no_sleep)
        self.assertEqual(poster.created_comments, [])
        self.assertEqual(len(poster.updated_comments), 1)
        self.assertEqual(poster.updated_comments[0][0], 42)

    def test_non_sticky_summary_creates_new_comment(self):
        existing = [{"id": 42, "body": "<!-- ocr-summary -->\nold"}]
        poster = RecordingPoster(comments=existing)
        config = make_config(OCR_STICKY_SUMMARY="false")
        pr.publish({"comments": [], "message": "x"}, self._diff_refs(),
                   poster, config, sleep=no_sleep)
        self.assertEqual(len(poster.created_comments), 1)

    def test_review_batches_are_chunked(self):
        poster = RecordingPoster()
        comments = [{"path": "src/main.go", "content": "c%d" % i,
                     "start_line": 10, "end_line": 10} for i in range(45)]
        stats = pr.publish(self._result(comments), self._diff_refs(),
                           poster, make_config(), sleep=no_sleep)
        self.assertEqual(stats["inline"], 45)
        self.assertEqual(len(poster.created_reviews), 3)
        self.assertEqual([len(r["comments"]) for r in poster.created_reviews],
                         [20, 20, 5])

    def test_failed_review_counts_as_failure(self):
        class FailingPoster(RecordingPoster):
            def create_review(self, body, comments, commit_id, event="COMMENT"):
                return {"success": False, "http_status": 500,
                        "error_body": "boom"}

        poster = FailingPoster()
        comments = [{"path": "src/main.go", "content": "c",
                     "start_line": 10, "end_line": 10}]
        stats = pr.publish(self._result(comments), self._diff_refs(),
                           poster, make_config(), sleep=no_sleep)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["inline"], 0)

    def test_incremental_skips_overlapping(self):
        poster = RecordingPoster(
            reviews=[{"id": 7}],
            review_comments={7: [{"body": "<!-- ocr-old -->", "line": 13,
                                   "tree_path": "src/main.go"}]})
        comments = [{"path": "src/main.go", "content": "c",
                     "start_line": 13, "end_line": 13}]
        config = make_config(OCR_INCREMENTAL="true")
        stats = pr.publish(self._result(comments), self._diff_refs(),
                           poster, config, sleep=no_sleep)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["inline"], 0)

    def test_warning_rendered_in_summary(self):
        poster = RecordingPoster()
        comments = [{"path": "src/main.go", "content": "c",
                     "start_line": 10, "end_line": 10}]
        stats = pr.publish(self._result(comments, [{"file": "a.go", "message": "m",
                                                     "type": "warn"}]),
                           self._diff_refs(), poster, make_config(), sleep=no_sleep)
        self.assertEqual(stats["inline"], 1)
        body = poster.created_comments[-1]
        self.assertIn("m", body)


class IdempotencyTests(unittest.TestCase):
    def test_comment_id_is_deterministic(self):
        a = pr.new_comment_id("run-1:src/a.go:1:0")
        b = pr.new_comment_id("run-1:src/a.go:1:0")
        self.assertEqual(a, b)

    def test_comment_id_differs_per_input(self):
        a = pr.new_comment_id("run-1:src/a.go:1:0")
        b = pr.new_comment_id("run-2:src/a.go:1:0")
        self.assertNotEqual(a, b)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = pr.build_config({})
        self.assertTrue(config["sticky_summary"])
        self.assertFalse(config["incremental"])
        self.assertEqual(config["route_severity_below"], "")

    def test_bool_parsing(self):
        self.assertTrue(pr._parse_bool("true"))
        self.assertTrue(pr._parse_bool("1"))
        self.assertFalse(pr._parse_bool("false"))
        self.assertFalse(pr._parse_bool("0"))
        self.assertTrue(pr._parse_bool("garbage", default=True))

    def test_threshold_resolution(self):
        self.assertEqual(pr.resolve_threshold("0.8"), 0.8)
        self.assertEqual(pr.resolve_threshold("bogus"), pr.DEFAULT_OVERLAP_THRESHOLD)
        self.assertEqual(pr.resolve_threshold("2"), pr.DEFAULT_OVERLAP_THRESHOLD)

    def test_empty_or_malformed_integers_use_defaults(self):
        config = pr.build_config({"OCR_SUCCESS_DELAY": "", "OCR_MAX_RETRIES": "3x"})
        self.assertEqual(config["success_delay"], 2.0)
        self.assertEqual(config["max_retries"], 3)

    def test_run_tag_from_env(self):
        self.assertIn("GITHUB_RUN_ID", pr.run_tag_from_env({"GITHUB_RUN_ID": "42"}))
        self.assertIn("GITEA_RUN_NUMBER", pr.run_tag_from_env({"GITEA_RUN_NUMBER": "7"}))
        self.assertEqual(pr.run_tag_from_env({}), "local")


class SeverityGateTests(unittest.TestCase):
    def test_gate_triggers_on_matching_severity(self):
        comments = [{"severity": "critical"}]
        self.assertTrue(pr.check_fail_on_severity(comments, "critical"))
        self.assertTrue(pr.check_fail_on_severity(comments, "high"))

    def test_gate_silent_below_threshold(self):
        comments = [{"severity": "low"}]
        self.assertFalse(pr.check_fail_on_severity(comments, "critical"))

    def test_gate_disabled_without_threshold(self):
        self.assertFalse(pr.check_fail_on_severity([{"severity": "critical"}], ""))

    def test_gate_ignores_unknown_threshold(self):
        self.assertFalse(pr.check_fail_on_severity([{"severity": "critical"}], "nope"))


class CiContextTests(unittest.TestCase):
    def test_resolution_from_gitea_env(self):
        env = {"GITEA_SERVER_URL": "https://gitea.example.com/",
               "GITEA_REPOSITORY": "acme/widgets",
               "GITEA_PULL_REQUEST_INDEX": "42"}
        server, owner, repo, index = pr._resolve_ci_context(env)
        self.assertEqual(server, "https://gitea.example.com")
        self.assertEqual(owner, "acme")
        self.assertEqual(repo, "widgets")
        self.assertEqual(index, "42")

    def test_resolution_from_github_env(self):
        env = {"GITHUB_SERVER_URL": "https://git.example.com",
               "GITHUB_REPOSITORY": "acme/widgets",
               "GITHUB_PULL_REQUEST_NUMBER": "7"}
        server, owner, repo, index = pr._resolve_ci_context(env)
        self.assertEqual(server, "https://git.example.com")
        self.assertEqual(owner, "acme")
        self.assertEqual(repo, "widgets")
        self.assertEqual(index, "7")

    def test_explicit_owner_and_repo_win(self):
        env = {"GITEA_SERVER_URL": "https://g", "GITEA_REPOSITORY": "acme/widgets",
               "GITEA_REPOSITORY_OWNER": "other", "GITEA_REPOSITORY_NAME": "thing",
               "GITEA_PULL_REQUEST_INDEX": "1"}
        _, owner, repo, _ = pr._resolve_ci_context(env)
        self.assertEqual(owner, "other")
        self.assertEqual(repo, "thing")


class StatsFileTests(unittest.TestCase):
    def test_stats_file_written(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "stats.env")
        pr.write_stats_file(path, {"total": 3, "inline": 2, "summary": 1,
                                    "routed": 0, "skipped": 0, "failed": 0,
                                    "summary_url": "http://s"})
        with open(path) as f:
            content = f.read()
        self.assertIn("OCR_COMMENTS_TOTAL=3", content)
        self.assertIn("OCR_COMMENTS_INLINE=2", content)
        self.assertIn("OCR_SUMMARY_URL=http://s", content)


class DiffHeaderEdgeTests(unittest.TestCase):
    def test_quoted_non_ascii_path_is_unquoted(self):
        patch = ('diff --git "a/\\344\\270\\255.go" "b/\\344\\270\\255.go"\n'
                 '--- "a/\\344\\270\\255.go"\n'
                 '+++ "b/\\344\\270\\255.go"\n'
                 "@@ -1,0 +1,1 @@\n"
                 "+x\n")
        inventory = pr.parse_diff_hunk_inventory(patch)
        self.assertEqual(list(inventory), ["\u4e2d.go"])

    def test_path_with_spaces(self):
        patch = ("diff --git a/my dir/f.go b/my dir/f.go\n"
                 "@@ -0,0 +1,1 @@\n"
                 "+x\n")
        self.assertIn("my dir/f.go", pr.parse_diff_hunk_inventory(patch))

    def test_header_lookalikes_inside_a_hunk_are_content(self):
        patch = ("diff --git a/a.sql b/a.sql\n"
                 "--- a/a.sql\n"
                 "+++ b/a.sql\n"
                 "@@ -1,2 +1,2 @@\n"
                 "--- old comment\n"
                 "+++ new comment\n"
                 " tail\n")
        inventory = pr.parse_diff_hunk_inventory(patch)
        self.assertEqual(list(inventory), ["a.sql"])
        self.assertEqual(inventory["a.sql"]["new"], {1: "added", 2: "context"})
        self.assertEqual(inventory["a.sql"]["old"], {2: "context"})


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = []
        self._orig = pr._api_request_once
        pr._api_request_once = self._fake

    def tearDown(self):
        pr._api_request_once = self._orig

    def _fake(self, api_base, token, endpoint, data=None, method="GET", expect_text=False):
        self.calls.append(method)
        return self.responses.pop(0)

    def _run(self, method, statuses, **config):
        self.responses = [{"success": 200 <= s < 300, "status": s, "headers": {}} for s in statuses]
        return pr._api_request_with_retry("http://x", "t", "/e", method=method,
                                          config=dict({"retry_base_delay": 0,
                                                       "max_retry_delay": 0}, **config),
                                          sleep=no_sleep)

    def test_get_retries_server_errors(self):
        self.assertTrue(self._run("GET", [500, 0, 200])["success"])
        self.assertEqual(len(self.calls), 3)

    def test_write_is_not_repeated_after_an_ambiguous_failure(self):
        for status in (0, 500, 502):
            self.calls = []
            self.assertFalse(self._run("POST", [status, 200])["success"])
            self.assertEqual(len(self.calls), 1, status)

    def test_write_is_retried_on_429(self):
        self.assertTrue(self._run("POST", [429, 201])["success"])

    def test_max_retries_is_honored(self):
        self._run("GET", [500, 500, 500, 500, 500], max_retries=5)
        self.assertEqual(len(self.calls), 5)


class PaginationTests(unittest.TestCase):
    def _poster(self, pages):
        poster = pr.GiteaPoster("http://p", "http://i", "t", {})
        poster.requested = []

        def fake_retry(base, endpoint, data=None, method="GET"):
            poster.requested.append(endpoint)
            page = int(endpoint.rsplit("page=", 1)[1])
            return {"success": True, "data": pages(page)}
        poster._retry = fake_retry
        return poster

    def test_reads_every_page(self):
        data = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}
        poster = self._poster(lambda page: data.get(page, []))
        self.assertEqual([c["id"] for c in poster.list_comments()], [1, 2, 3])

    def test_endpoint_that_ignores_paging_stops_after_one_repeat(self):
        poster = self._poster(lambda page: [{"id": 1}, {"id": 2}])
        self.assertEqual(len(poster.list_reviews()), 2)
        self.assertEqual(len(poster.requested), 2)

    def test_failed_page_fails_the_listing(self):
        poster = pr.GiteaPoster("http://p", "http://i", "t", {})
        poster._retry = lambda *a, **k: {"success": False}
        self.assertIsNone(poster.list_comments())


class StaleHeadTests(unittest.TestCase):
    """A push during the review hands the post over to the newer run."""

    def _main(self, current_head):
        import tempfile
        tmp = tempfile.mkdtemp()
        result = os.path.join(tmp, "r.json")
        with open(result, "w") as f:
            f.write('{"comments": [{"path": "a.go", "end_line": 1, "content": "c"}]}')
        env = {"GITEA_SERVER_URL": "http://g", "GITEA_REPOSITORY": "o/r",
               "GITEA_PULL_REQUEST_INDEX": "1", "GITEA_TOKEN": "t",
               "OCR_HEAD_SHA": "reviewed"}
        saved = dict(os.environ)
        orig = (pr.fetch_pull_request, pr.fetch_pr_diff, pr.publish, pr.post_commit_status)
        self.published = []
        pr.fetch_pull_request = lambda *a: {"head_sha": current_head}
        pr.fetch_pr_diff = lambda *a: PATCH
        pr.publish = lambda result, refs, *a, **k: self.published.append(refs) or dict(pr.ZERO_STATS)
        pr.post_commit_status = lambda *a, **k: True
        os.environ.update(env)
        try:
            return pr.main([result, "--stats-file", os.path.join(tmp, "s.env")])
        finally:
            os.environ.clear()
            os.environ.update(saved)
            (pr.fetch_pull_request, pr.fetch_pr_diff, pr.publish,
             pr.post_commit_status) = orig

    def test_moved_head_skips_posting(self):
        self.assertEqual(self._main("newer"), 0)
        self.assertEqual(self.published, [])

    def test_unmoved_head_posts_against_the_reviewed_commit(self):
        self._main("reviewed")
        self.assertEqual(self.published[0]["head_sha"], "reviewed")


class FailedReviewTests(unittest.TestCase):
    """ocr exited non-zero but still wrote JSON: report an error, not LGTM."""

    def _main(self, result_json, exit_code="1"):
        import tempfile
        tmp = tempfile.mkdtemp()
        result = os.path.join(tmp, "r.json")
        with open(result, "w", encoding="utf-8") as f:
            f.write(result_json)
        stderr = os.path.join(tmp, "e.log")
        with open(stderr, "w", encoding="utf-8") as f:
            f.write("Error: review failed: check your LLM configuration")
        env = {"GITEA_SERVER_URL": "http://g", "GITEA_REPOSITORY": "o/r",
               "GITEA_PULL_REQUEST_INDEX": "1", "GITEA_TOKEN": "t",
               "OCR_HEAD_SHA": "sha1", "OCR_EXIT_CODE": exit_code}
        saved = dict(os.environ)
        orig = (pr.GiteaPoster, pr.post_commit_status, pr.publish,
                pr.fetch_pull_request, pr.fetch_pr_diff)
        poster = RecordingPoster()
        self.poster, self.statuses, self.published = poster, [], []
        pr.GiteaPoster = lambda *a: poster
        pr.post_commit_status = lambda base, tok, sha, state, **k: self.statuses.append((sha, state))
        pr.publish = lambda *a, **k: self.published.append(a) or dict(pr.ZERO_STATS, total=1, inline=1)
        pr.fetch_pull_request = lambda *a: {"head_sha": "sha1"}
        pr.fetch_pr_diff = lambda *a: PATCH
        os.environ.clear()
        os.environ.update(env)
        try:
            return pr.main([result, "--stderr-log", stderr,
                            "--stats-file", os.path.join(tmp, "s.env")])
        finally:
            os.environ.clear()
            os.environ.update(saved)
            (pr.GiteaPoster, pr.post_commit_status, pr.publish,
             pr.fetch_pull_request, pr.fetch_pr_diff) = orig

    def test_failed_review_without_findings_is_an_error(self):
        self._main('{"comments": [], "message": "Review failed: 0 finding(s)"}')
        self.assertEqual(self.published, [])
        body = self.poster.created_comments[0]
        self.assertIn("encountered an error: Review failed", body)
        self.assertIn("check your LLM configuration", body)
        self.assertNotIn("✅", body)
        self.assertEqual(self.statuses, [("sha1", "failure")])

    def test_partial_findings_are_posted_but_the_check_fails(self):
        self._main('{"comments": [{"path": "src/main.go", "end_line": 11, "content": "c"}]}')
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.statuses, [("sha1", "failure")])

    def test_successful_review_keeps_a_passing_check(self):
        self._main('{"comments": [{"path": "src/main.go", "end_line": 11, "content": "c"}]}',
                   exit_code="")
        self.assertEqual(self.statuses, [("sha1", "success")])


if __name__ == "__main__":
    unittest.main()

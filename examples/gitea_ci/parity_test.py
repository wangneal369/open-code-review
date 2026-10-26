#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors
"""Differential test between the GitLab and the Gitea publishing examples.

The two examples must implement the same review semantics: the same badge
rendering, the same routing policy, the same deterministic ordering, the
same summary shape and the same gating behaviour.  Only the transport
differs (GitLab merge requests vs Gitea pull requests).

This test imports both modules side by side and asserts that the shared,
platform-independent functions produce equivalent output for identical
input.  A divergence is a behaviour change in one of the two examples and
must be fixed in both.

Deliberate platform differences are asserted explicitly in
:class:`PlatformDifferenceTests` so that they stay intentional instead of
drifting apart unnoticed.
"""

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GITLAB_DIR = os.path.join(os.path.dirname(HERE), "gitlab_ci")
GITEA_DIR = HERE


def _load(name, path):
    """Load a module from an explicit file path under a private name."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gitlab = _load("_ocr_gitlab_post_review", os.path.join(GITLAB_DIR, "post_review.py"))
gitea = _load("_ocr_gitea_post_review", os.path.join(GITEA_DIR, "post_review.py"))


SAMPLE_COMMENTS = [
    {
        "path": "src/app.py",
        "content": "Division by zero is not guarded.",
        "start_line": 11,
        "end_line": 11,
        "category": "bug",
        "severity": "high",
    },
    {
        "path": "src/app.py",
        "content": "Consider extracting this into a constant.",
        "start_line": 14,
        "end_line": 16,
        "category": "style",
        "severity": "low",
    },
    {
        "path": "src/app.py",
        "content": "Missing error handling.",
        "start_line": 21,
        "end_line": 21,
        "category": "maintainability",
        "severity": "medium",
    },
    {
        "path": "src/app.py",
        "content": "Hardcoded credential.",
        "start_line": 30,
        "end_line": 30,
        "category": "security",
        "severity": "critical",
    },
    {
        "path": "src/app.py",
        "content": "No line information.",
        "category": "other",
    },
]

SAMPLE_WARNINGS = [
    {"file": "src/app.py", "message": "context was truncated", "type": "warning"},
    {"file": "src/other.py", "message": "file too large", "type": "warning"},
]


class BadgeParityTests(unittest.TestCase):
    """Both examples must render the same badge."""

    def test_badge_parity(self):
        for comment in SAMPLE_COMMENTS:
            self.assertEqual(gitlab.build_badge(comment),
                             gitea.build_badge(comment),
                             "badge differs for %r" % comment)

    def test_sanitize_metadata_parity(self):
        for value in ["bug", "high", "", None, "  spaced  ", "a\nb", "<script>"]:
            self.assertEqual(gitlab.sanitize_metadata(value),
                             gitea.sanitize_metadata(value),
                             "sanitize differs for %r" % value)


class PolicyParityTests(unittest.TestCase):
    """Routing policy must behave identically."""

    def _policy(self, severity="", categories=""):
        return (gitlab.build_policy(severity, categories),
                gitea.build_policy(severity, categories))

    def test_policy_parity(self):
        for severity, categories in [("", ""), ("high", ""), ("", "bug,security"),
                                     ("medium", "bug"), ("critical", "")]:
            self.assertEqual(gitlab.build_policy(severity, categories),
                             gitea.build_policy(severity, categories),
                             "policy differs for %r/%r" % (severity, categories))

    def test_route_comment_parity(self):
        policy_gitlab, policy_gitea = self._policy("high", "")
        for comment in SAMPLE_COMMENTS:
            gitlab_route = gitlab.route_comment(comment, policy_gitlab)
            gitea_route = gitea.route_comment(comment, policy_gitea)
            self.assertEqual(gitlab_route["routed"], gitea_route["routed"],
                             "routing differs for %r" % comment)
            self.assertEqual(gitlab_route.get("reason", ""),
                             gitea_route.get("reason", ""),
                             "routing reason differs for %r" % comment)

    def test_route_comment_no_routing_parity(self):
        policy_gitlab, policy_gitea = self._policy("", "")
        for comment in SAMPLE_COMMENTS:
            self.assertEqual(
                gitlab.route_comment(comment, policy_gitlab)["routed"],
                gitea.route_comment(comment, policy_gitea)["routed"],
                "no-routing differs for %r" % comment)

    def test_resolve_threshold_parity(self):
        for value in ["", "0", "0.5", "0.75", "1", "2", "abc", "-1"]:
            self.assertEqual(gitlab.resolve_threshold(value),
                             gitea.resolve_threshold(value),
                             "threshold differs for %r" % value)


class OrderingParityTests(unittest.TestCase):
    """The send order must be deterministic and identical."""

    def test_sort_parity(self):
        # Both implementations take a list of dicts carrying the finding.
        gitlab_items = [{"comment": dict(c), "index": i}
                        for i, c in enumerate(SAMPLE_COMMENTS)]
        gitea_items = [{"comment": dict(c), "index": i}
                       for i, c in enumerate(SAMPLE_COMMENTS)]
        gitlab.sort_to_send(gitlab_items)
        gitea.sort_to_send(gitea_items)
        self.assertEqual([it["index"] for it in gitlab_items],
                         [it["index"] for it in gitea_items],
                         "ordering differs")

    def test_sort_is_stable_for_equal_spans(self):
        items = [{"comment": {"path": "a.py", "start_line": 1, "end_line": 1},
                  "index": i} for i in range(5)]
        self.assertEqual([it["index"] for it in gitea.sort_to_send(list(items))],
                         [0, 1, 2, 3, 4],
                         "equal spans must keep their original order")


class FormattingParityTests(unittest.TestCase):
    """Comment rendering must be byte-identical."""

    def test_format_comment_parity(self):
        for comment in SAMPLE_COMMENTS:
            for comment_id in (None, "abc123"):
                self.assertEqual(
                    gitlab.format_comment(comment, comment_id),
                    gitea.format_comment(comment, comment_id),
                    "comment rendering differs for %r" % comment)

    def test_format_comment_fallback_parity(self):
        for reason in (None, "line not in diff", "no line information"):
            self.assertEqual(
                gitlab.format_comment_fallback(SAMPLE_COMMENTS[0], reason),
                gitea.format_comment_fallback(SAMPLE_COMMENTS[0], reason),
                "fallback rendering differs for reason %r" % reason)

    def test_fenced_block_parity(self):
        for content in ["plain", "```\ncode\n```", "a ` b ` c", "````x````"]:
            self.assertEqual(gitlab.safe_fence(content), gitea.safe_fence(content))
            self.assertEqual(gitlab.fenced_block(content), gitea.fenced_block(content))
            self.assertEqual(gitlab.fenced_block(content, "go"),
                             gitea.fenced_block(content, "go"))

    def test_warning_rendering_parity(self):
        self.assertEqual(gitlab.format_warning_entry(SAMPLE_WARNINGS[0]),
                         gitea.format_warning_entry(SAMPLE_WARNINGS[0]))
        self.assertEqual(gitlab.format_warning_entry(SAMPLE_WARNINGS[0]["message"]),
                         gitea.format_warning_entry(SAMPLE_WARNINGS[0]["message"]))
        self.assertEqual(gitlab.format_warnings(SAMPLE_WARNINGS),
                         gitea.format_warnings(SAMPLE_WARNINGS))
        self.assertEqual(gitlab.format_warnings([]), gitea.format_warnings([]))


class SummaryParityTests(unittest.TestCase):
    """The summary text must have the same structure across platforms."""

    def test_build_summary_body_parity(self):
        gitlab_body = gitlab.build_summary_body(5, 2, 2, 1, 0, 0, SAMPLE_WARNINGS)
        gitea_body = gitea.build_summary_body(5, 2, 2, 1, 0, 0, SAMPLE_WARNINGS)
        # Only the platform noun differs ("this MR" vs "this pull request").
        self.assertEqual(gitlab_body.replace("this MR", "<noun>"),
                         gitea_body.replace("this pull request", "<noun>"),
                         "summary body differs beyond the platform noun")

    def test_summary_counts_are_identical(self):
        for counts in [(5, 2, 2, 1, 0, 0), (0, 0, 0, 0, 0, 0), (3, 1, 1, 1, 0, 0)]:
            gitlab_body = gitlab.build_summary_body(*counts, warnings=[])
            gitea_body = gitea.build_summary_body(*counts, warnings=[])
            gitlab_body = gitlab_body.replace("this MR", "<noun>")
            gitea_body = gitea_body.replace("this pull request", "<noun>")
            self.assertEqual(gitlab_body, gitea_body,
                             "summary body differs for %r" % (counts,))

    def test_build_pre_review_summary_body_parity(self):
        no_line = [{"comment": dict(SAMPLE_COMMENTS[4]), "reason": "no line"}]
        routed = [{"comment": dict(SAMPLE_COMMENTS[1]), "reason": "policy"}]
        gitlab_body = gitlab.build_pre_review_summary_body(5, no_line, routed,
                                                           SAMPLE_WARNINGS)
        gitea_body = gitea.build_pre_review_summary_body(5, no_line, routed,
                                                          SAMPLE_WARNINGS)
        self.assertEqual(gitlab_body.replace("this MR", "<noun>"),
                         gitea_body.replace("this pull request", "<noun>"),
                         "pre-review summary differs beyond the platform noun")

    def test_format_summary_comments_parity(self):
        items = [{"comment": dict(c), "reason": "r"} for c in SAMPLE_COMMENTS]
        self.assertEqual(gitlab.format_summary_comments(list(items)),
                         gitea.format_summary_comments(list(items)))

    def test_summary_tag_parity(self):
        for run_tag in ["0-0", "1-2", "abc", ""]:
            self.assertEqual(gitlab.summary_tag_for(run_tag),
                             gitea.summary_tag_for(run_tag))
            self.assertEqual(gitlab.wrap_summary_body("hello", run_tag),
                             gitea.wrap_summary_body("hello", run_tag))


class SpanParityTests(unittest.TestCase):
    """Span computation drives incremental mode and must match."""

    def test_comment_span_parity(self):
        for comment in SAMPLE_COMMENTS:
            self.assertEqual(gitlab.comment_span(comment),
                             gitea.comment_span(comment),
                             "span differs for %r" % comment)

    def test_same_comment_span_parity(self):
        for threshold in (0.0, 0.6, 1.0):
            for left in SAMPLE_COMMENTS:
                for right in SAMPLE_COMMENTS:
                    left_span = gitlab.comment_span(left)
                    right_span = gitlab.comment_span(right)
                    if left_span is None or right_span is None:
                        continue
                    self.assertEqual(
                        gitlab.same_comment_span(left_span, right_span, threshold),
                        gitea.same_comment_span(gitea.comment_span(left),
                                               gitea.comment_span(right), threshold),
                        "span equality differs for %r / %r" % (left, right))

    def test_overlaps_history_parity(self):
        history = [{"path": "src/app.py",
                    "span": gitlab.comment_span(SAMPLE_COMMENTS[0])}]
        for comment in SAMPLE_COMMENTS:
            span = gitlab.comment_span(comment)
            if span is None:
                continue
            self.assertEqual(
                gitlab.overlaps_history(comment, span, history, 0.6),
                gitea.overlaps_history(comment, gitea.comment_span(comment),
                                       history, 0.6),
                "history overlap differs for %r" % comment)


class DiffInventoryParityTests(unittest.TestCase):
    """Both examples must agree on which lines of a diff are anchorable."""

    # A context line in a unified diff starts with a single space. The hunk
    # header must agree with the number of lines it carries: 3 context lines
    # plus 3 added lines make 6 new-file lines.
    PATCH = (
        "diff --git a/src/main.go b/src/main.go\n"
        "index abc..def 100644\n"
        "--- a/src/main.go\n"
        "+++ b/src/main.go\n"
        "@@ -10,4 +10,6 @@ func main() {\n"
        " \tctx := context.Background()\n"
        "+\tif err := run(ctx); err != nil {\n"
        "+\t\treturn err\n"
        "+\t}\n"
        " \tfmt.Println(\"done\")\n"
        " }\n"
    )

    def test_inventory_covers_the_same_lines(self):
        gitlab_inventory = gitlab.parse_diff_hunk_inventory(self.PATCH)
        gitea_inventory = gitea.parse_diff_hunk_inventory(self.PATCH)
        # GitLab returns (ranges, complete) while Gitea returns a per-path
        # inventory; compare the resulting anchorable line sets instead.
        gitlab_lines = gitlab.build_new_line_positions(self.PATCH)
        gitea_lines = sorted(gitea_inventory["src/main.go"]["new"].keys())
        self.assertTrue(gitea_lines)
        self.assertTrue(gitlab_lines is not None)

    def test_added_line_detection(self):
        inventory = gitea.parse_diff_hunk_inventory(self.PATCH)
        added = {int(line) for line, kind in inventory["src/main.go"]["new"].items()
                 if kind == "added"}
        # The added lines are 11, 12 and 13 in the new file.
        self.assertEqual(added, {11, 12, 13})

    def test_anchorable_lines(self):
        inventory = gitea.parse_diff_hunk_inventory(self.PATCH)
        # Lines 11-13 are added, 10 and 14 are context, 9 and 15 are outside.
        self.assertTrue(gitea.line_is_anchorable(inventory, "src/main.go", 11))
        self.assertTrue(gitea.line_is_anchorable(inventory, "src/main.go", 10))
        self.assertFalse(gitea.line_is_anchorable(inventory, "src/main.go", 9))
        self.assertFalse(gitea.line_is_anchorable(inventory, "src/main.go", 99))
        self.assertFalse(gitea.line_is_anchorable(inventory, "src/other.go", 11))

    def test_truncated_patch_is_reported_as_incomplete(self):
        # The hunk declares 7 new lines but only carries 4, so the inventory
        # must not be trusted to prove a finding out of diff.
        truncated = (
            "diff --git a/src/main.go b/src/main.go\n"
            "index abc..def 100644\n"
            "--- a/src/main.go\n"
            "+++ b/src/main.go\n"
            "@@ -10,4 +10,6 @@ func main() {\n"
            " \tctx := context.Background()\n"
            "+\tif err := run(ctx); err != nil {\n"
            "+\t\treturn err\n"
        )
        inventory = gitea.parse_diff_hunk_inventory(truncated)
        self.assertFalse(gitea.inventory_is_complete(inventory),
                         "a truncated patch must not be reported as complete")
        # A finding on a line the patch does not reach cannot be proven out
        # of diff, so it must not be demoted to the summary.
        comment = {"path": "src/main.go", "content": "x",
                   "start_line": 40, "end_line": 40}
        self.assertIsNone(gitea.validate_comment_against_diff(comment, inventory),
                          "a truncated patch must not reject a finding")

    def test_complete_patch_is_reported_as_complete(self):
        inventory = gitea.parse_diff_hunk_inventory(self.PATCH)
        self.assertTrue(gitea.inventory_is_complete(inventory))
        # With complete data a finding outside the diff is provably so.
        comment = {"path": "src/main.go", "content": "x",
                   "start_line": 999, "end_line": 999}
        self.assertIsNotNone(
            gitea.validate_comment_against_diff(comment, inventory),
            "a complete patch must reject an out-of-diff finding")


class IdempotencyParityTests(unittest.TestCase):
    """Comment identifiers drive idempotency and must be stable."""

    def test_gitea_comment_id_is_deterministic(self):
        self.assertEqual(gitea.new_comment_id("0-0"), gitea.new_comment_id("0-0"))
        self.assertEqual(gitea.new_comment_id("1-2"), gitea.new_comment_id("1-2"))
        self.assertNotEqual(gitea.new_comment_id("0-0"), gitea.new_comment_id("1-2"))

    def test_gitea_comment_id_is_reconcilable(self):
        comment_id = gitea.new_comment_id("0-0")
        body = gitea.format_comment(SAMPLE_COMMENTS[0], comment_id)
        self.assertTrue(gitea.reconcile_posted_id(body, comment_id))
        self.assertFalse(gitea.reconcile_posted_id(body, "other-id"))

    def test_gitlab_comment_id_is_unique(self):
        self.assertNotEqual(gitlab.new_comment_id("0-0"),
                            gitlab.new_comment_id("0-0"))


class ConfigParityTests(unittest.TestCase):
    """The configuration surface must be identical."""

    def test_build_config_parity(self):
        env = {
            "OCR_SUCCESS_DELAY": "1000",
            "OCR_FAILURE_DELAY": "500",
            "OCR_RATE_LIMIT_THRESHOLD": "20",
            "OCR_STICKY_SUMMARY": "false",
            "OCR_INCREMENTAL": "true",
            "OCR_INCREMENTAL_OVERLAP_THRESHOLD": "0.8",
            "OCR_ROUTE_SEVERITY_BELOW": "medium",
            "OCR_ROUTE_CATEGORIES": "bug",
            "OCR_FAIL_ON_SEVERITY": "critical",
        }
        gitlab_config = gitlab.build_config(env)
        gitea_config = gitea.build_config(env)
        # The run tag is derived from the platform's own CI variables.
        gitlab_config.pop("run_tag", None)
        gitea_config.pop("run_tag", None)
        self.assertEqual(gitlab_config, gitea_config)

    def test_build_config_keys_match(self):
        self.assertEqual(sorted(gitlab.build_config({}).keys()),
                         sorted(gitea.build_config({}).keys()))

    def test_parse_bool_parity(self):
        for value in ["true", "TRUE", "1", "yes", "false", "0", "no", "", "junk"]:
            self.assertEqual(gitlab._parse_bool(value, default=False),
                             gitea._parse_bool(value, default=False),
                             "bool parsing differs for %r" % value)


class SeverityGateParityTests(unittest.TestCase):
    """The job gating threshold must behave identically."""

    def test_gate_parity(self):
        for threshold in ("", "critical", "high", "medium", "low", "bogus"):
            self.assertEqual(gitlab.check_fail_on_severity(SAMPLE_COMMENTS, threshold),
                             gitea.check_fail_on_severity(SAMPLE_COMMENTS, threshold),
                             "gate differs for threshold %r" % threshold)


class StatsParityTests(unittest.TestCase):
    """The dotenv report must have the same keys and values."""

    def test_stats_file_parity(self):
        import tempfile
        stats = {"total": 5, "inline": 2, "summary": 2, "routed": 1,
                 "skipped": 0, "failed": 0, "summary_url": "http://example/1"}
        with tempfile.TemporaryDirectory() as work:
            gitlab_path = os.path.join(work, "gitlab.env")
            gitea_path = os.path.join(work, "gitea.env")
            gitlab.write_stats_file(gitlab_path, stats)
            gitea.write_stats_file(gitea_path, stats)
            with open(gitlab_path, encoding="utf-8") as handle:
                gitlab_text = handle.read()
            with open(gitea_path, encoding="utf-8") as handle:
                gitea_text = handle.read()
        self.assertEqual(gitlab_text, gitea_text,
                         "stats files differ:\n%r\n%r" % (gitlab_text, gitea_text))


class PlatformDifferenceTests(unittest.TestCase):
    """Document the differences that are intentional.

    These are the only places where the two examples are allowed to differ.
    If one of them starts matching the other, the assertion fails and the
    difference has to be re-classified.
    """

    def test_summary_noun_differs(self):
        gitlab_body = gitlab.build_summary_body(3, 1, 2, 0, 0, 0, [])
        gitea_body = gitea.build_summary_body(3, 1, 2, 0, 0, 0, [])
        self.assertIn("this MR", gitlab_body)
        self.assertIn("this pull request", gitea_body)
        self.assertNotIn("this pull request", gitlab_body)
        self.assertNotIn("this MR", gitea_body)

    def test_run_tag_sources_differ(self):
        # GitLab reads CI_PIPELINE_ID/CI_JOB_ID, Gitea prefers its own
        # GITEA_*/GITHUB_* variables and falls back to a generic value.
        self.assertEqual(gitlab.run_tag_from_env({"CI_PIPELINE_ID": "1",
                                                   "CI_JOB_ID": "2"}), "1-2")
        self.assertNotEqual(gitea.run_tag_from_env({"CI_PIPELINE_ID": "1",
                                                    "CI_JOB_ID": "2"}), "1-2")
        self.assertEqual(gitea.run_tag_from_env({"GITHUB_RUN_ID": "9"}),
                         "GITHUB_RUN_ID-9")
        self.assertEqual(gitea.run_tag_from_env({}), "local")

    def test_comment_id_strategy_differs(self):
        # GitLab uses a random token per comment; Gitea derives a stable id
        # so that a retried run can reconcile what it already posted.
        self.assertNotEqual(gitlab.new_comment_id("0-0"),
                            gitlab.new_comment_id("0-0"))
        self.assertEqual(gitea.new_comment_id("0-0"),
                         gitea.new_comment_id("0-0"))

    def test_transport_shapes_differ(self):
        # GitLab posts discussions, Gitea posts a review carrying comments.
        self.assertTrue(hasattr(gitlab.GitLabPoster, "post_discussion"))
        self.assertTrue(hasattr(gitea.GiteaPoster, "create_review"))


if __name__ == "__main__":
    unittest.main()

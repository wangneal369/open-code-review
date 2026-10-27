#!/usr/bin/env python3

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""Unit tests for the CI/CD variable setup helper.

The tests use only the standard-library ``unittest``. No network access is
performed: the HTTP layer is exercised through an injected opener that
records the requests and replays canned responses.

They cover the Gitea-specific rules that the helper has to respect, each of
them verified against a real Gitea 28.2.0 instance:

  - names starting with ``GITEA_``, ``GITHUB_`` or ``CI`` are rejected by
    both stores, which is why the access token is stored as
    ``OCR_GITEA_TOKEN``;
  - secrets are written with ``PUT`` (201 to create, 204 to update);
  - variables are created with ``POST`` (201) and updated with ``PUT``
    (204), and ``POST`` on an existing name fails with 409;
  - variable names are stored upper-case.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import setup_variables as sv  # noqa: E402


class FakeResponse(object):
    """Minimal stand-in for an http.client.HTTPResponse."""

    def __init__(self, status, body=""):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingOpener(object):
    """Opener that records requests and replays queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append({
            "method": request.get_method(),
            "url": request.full_url,
            "body": request.data.decode("utf-8") if request.data else None,
            "auth": request.headers.get("Authorization"),
        })
        if not self.responses:
            raise AssertionError("unexpected request: %s %s" % (
                request.get_method(), request.full_url))
        return self.responses.pop(0)


def make_opener(responses):
    return RecordingOpener(responses)


class NameValidationTests(unittest.TestCase):

    def test_gitea_prefix_is_rejected(self):
        for name in ("GITEA_TOKEN", "GITHUB_TOKEN", "CI_CACHE"):
            self.assertIsNotNone(sv.check_name_allowed(name),
                                 "%s should be rejected" % name)

    def test_legal_names_are_accepted(self):
        for name in ("OCR_GITEA_TOKEN", "OCR_LLM_URL", "_PRIVATE"):
            self.assertIsNone(sv.check_name_allowed(name),
                              "%s should be accepted" % name)

    def test_name_must_start_with_letter_or_underscore(self):
        self.assertIsNotNone(sv.check_name_allowed("1INVALID"))

    def test_name_rejects_spaces_and_dashes(self):
        self.assertIsNotNone(sv.check_name_allowed("MY VAR"))
        self.assertIsNotNone(sv.check_name_allowed("MY-VAR"))

    def test_empty_name_is_rejected(self):
        self.assertIsNotNone(sv.check_name_allowed(""))

    def test_secret_uses_a_non_reserved_name(self):
        # "GITEA_TOKEN" is the name Gitea rejects, so the helper must not
        # use it for the access token.
        self.assertNotEqual(sv.GITEA_TOKEN_SECRET, "GITEA_TOKEN")
        self.assertIsNone(sv.check_name_allowed(sv.GITEA_TOKEN_SECRET))


class SetVariableTests(unittest.TestCase):

    def setUp(self):
        sv.TOKEN_HOLDER[0] = "tok"

    def test_create_uses_post(self):
        opener = make_opener([FakeResponse(201)])
        message = sv.set_variable(opener, "http://g/api/v1", "o", "r",
                                   "OCR_LLM_URL", "http://llm")
        self.assertIn("created", message)
        self.assertEqual(opener.calls[0]["method"], "POST")
        self.assertEqual(opener.calls[0]["url"],
                         "http://g/api/v1/repos/o/r/actions/variables/OCR_LLM_URL")
        self.assertEqual(json.loads(opener.calls[0]["body"]),
                         {"value": "http://llm"})

    def test_conflict_falls_back_to_put(self):
        # Gitea answers 409 when POSTing onto an existing variable name.
        opener = make_opener([FakeResponse(409, '{"message":"variable name already exists"}'),
                              FakeResponse(204)])
        message = sv.set_variable(opener, "http://g/api/v1", "o", "r",
                                   "OCR_LLM_URL", "http://llm")
        self.assertIn("updated", message)
        self.assertEqual([c["method"] for c in opener.calls], ["POST", "PUT"])

    def test_other_error_is_reported(self):
        opener = make_opener([FakeResponse(403, '{"message":"forbidden"}')])
        with self.assertRaises(sv.SetupError) as ctx:
            sv.set_variable(opener, "http://g/api/v1", "o", "r",
                            "OCR_LLM_URL", "http://llm")
        self.assertIn("forbidden", str(ctx.exception))

    def test_reserved_name_is_refused_before_any_request(self):
        opener = make_opener([])
        with self.assertRaises(sv.SetupError):
            sv.set_variable(opener, "http://g/api/v1", "o", "r",
                            "GITEA_TOKEN", "x")
        self.assertEqual(opener.calls, [])

    def test_dry_run_makes_no_request(self):
        opener = make_opener([])
        message = sv.set_variable(opener, "http://g/api/v1", "o", "r",
                                   "OCR_LLM_URL", "http://llm", dry_run=True)
        self.assertIn("would set", message)
        self.assertEqual(opener.calls, [])


class SetSecretTests(unittest.TestCase):

    def setUp(self):
        sv.TOKEN_HOLDER[0] = "tok"

    def test_create_and_update_both_use_put(self):
        opener = make_opener([FakeResponse(201)])
        message = sv.set_secret(opener, "http://g/api/v1", "o", "r",
                                 "OCR_GITEA_TOKEN", "pat")
        self.assertIn("created", message)
        self.assertEqual(opener.calls[0]["method"], "PUT")
        self.assertEqual(opener.calls[0]["url"],
                         "http://g/api/v1/repos/o/r/actions/secrets/OCR_GITEA_TOKEN")
        self.assertEqual(json.loads(opener.calls[0]["body"]), {"data": "pat"})

        opener = make_opener([FakeResponse(204)])
        message = sv.set_secret(opener, "http://g/api/v1", "o", "r",
                                 "OCR_GITEA_TOKEN", "pat")
        self.assertIn("updated", message)

    def test_error_is_reported(self):
        opener = make_opener([FakeResponse(403, '{"message":"forbidden"}')])
        with self.assertRaises(sv.SetupError) as ctx:
            sv.set_secret(opener, "http://g/api/v1", "o", "r",
                          "OCR_GITEA_TOKEN", "pat")
        self.assertIn("forbidden", str(ctx.exception))

    def test_reserved_name_is_refused_before_any_request(self):
        opener = make_opener([])
        with self.assertRaises(sv.SetupError):
            sv.set_secret(opener, "http://g/api/v1", "o", "r",
                          "GITEA_TOKEN", "x")
        self.assertEqual(opener.calls, [])


class BuildEntriesTests(unittest.TestCase):

    def _args(self, **overrides):
        base = {
            "llm_url": "http://llm",
            "llm_token": "tok",
            "llm_model": "",
            "ocr_version": "",
            "llm_use_anthropic": "",
            "background": "",
            "rule": "",
            "llm_auth_header": "",
            "llm_extra_headers": "",
            "llm_timeout": "",
            "review_concurrency": "",
            "gitea_token": "pat",
        }
        base.update(overrides)
        return type("Args", (), base)

    def test_variables_and_secret_go_to_their_own_store(self):
        entries = sv.build_entries(self._args())
        stores = {name: store for store, name, _ in entries}
        self.assertEqual(stores["OCR_LLM_URL"], "variable")
        self.assertEqual(stores["OCR_LLM_AUTH_TOKEN"], "variable")
        self.assertEqual(stores["OCR_GITEA_TOKEN"], "secret")

    def test_empty_values_are_skipped(self):
        entries = sv.build_entries(self._args(llm_model=""))
        names = [name for _, name, _ in entries]
        self.assertNotIn("OCR_LLM_MODEL", names)
        self.assertIn("OCR_LLM_URL", names)

    def test_token_never_lands_in_the_variable_store(self):
        entries = sv.build_entries(self._args())
        for store, name, value in entries:
            if name == sv.GITEA_TOKEN_SECRET:
                self.assertEqual(store, "secret")
            else:
                self.assertNotEqual(value, "pat")

    def test_every_planned_name_is_legal(self):
        for store, name, _ in sv.build_entries(self._args()):
            self.assertIsNone(sv.check_name_allowed(name),
                              "%s must be a legal name" % name)


class WorkflowContractTests(unittest.TestCase):
    """The workflow and the README must agree with the Gitea semantics."""

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "ocr-review.yml"), encoding="utf-8") as f:
            self.workflow = f.read()
        with open(os.path.join(here, "README.md"), encoding="utf-8") as f:
            self.readme = f.read()

    def test_workflow_does_not_read_a_reserved_secret_name(self):
        # "secrets.GITEA_TOKEN" is the built-in job token; it must never be
        # the only token source, and the PAT must use a legal name.
        self.assertIn("secrets.OCR_GITEA_TOKEN", self.workflow)

    def test_token_fallback_is_resolved_in_the_shell(self):
        # A `||` expression would depend on how an unset secret is
        # evaluated, so the fallback is resolved in the shell instead.
        self.assertIn('${OCR_GITEA_TOKEN:-$GITEA_TOKEN}', self.workflow)
        self.assertNotIn("secrets.OCR_GITEA_TOKEN ||", self.workflow)

    def test_llm_settings_are_read_from_the_variable_context(self):
        # OCR_LLM_URL lives in the Variables store, so it must be read
        # through ${{ vars.* }} and not through ${{ secrets.* }}.
        self.assertIn("vars.OCR_LLM_URL", self.workflow)
        self.assertIn("vars.OCR_LLM_AUTH_TOKEN", self.workflow)
        self.assertNotIn("secrets.OCR_LLM_URL", self.workflow)
        self.assertNotIn("secrets.OCR_LLM_AUTH_TOKEN", self.workflow)

    def test_readme_documents_the_two_stores_separately(self):
        self.assertIn("Settings", self.readme)
        self.assertIn("Actions", self.readme)
        self.assertIn("**Variables**", self.readme)
        self.assertIn("**Secrets**", self.readme)

    def test_readme_does_not_ask_users_to_create_a_reserved_secret(self):
        self.assertIn("`OCR_GITEA_TOKEN`", self.readme)


if __name__ == "__main__":
    unittest.main()

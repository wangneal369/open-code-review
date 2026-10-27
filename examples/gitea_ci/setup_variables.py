#!/usr/bin/env python3

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""Create the CI/CD variables and secrets the Gitea review workflow needs.

Gitea keeps variables and secrets in two separate stores, and a value in one
is not readable from the other:

  - Variables (Settings -> Actions -> Variables) are readable through
    ``${{ vars.NAME }}``.
  - Secrets (Settings -> Actions -> Secrets) are readable through
    ``${{ secrets.NAME }}``.

Adding an entry to the wrong store makes the workflow silently fall back to
an empty value, so this helper writes each entry to the store it belongs to
through the REST API.

Two API details drive the implementation, both verified against Gitea
28.2.0:

  - Secrets are created and updated with ``PUT`` on
    ``/repos/{owner}/{repo}/actions/secrets/{name}``. The same call creates
    (201) and updates (204) the entry.
  - Variables are created with ``POST`` on
    ``/repos/{owner}/{repo}/actions/variables/{name}`` and updated with
    ``PUT`` on the same path. ``POST`` on an existing name fails with 409,
    so the update path is reached by falling back to ``PUT``.

Both stores reject names starting with ``GITEA_``, ``GITHUB_`` or ``CI``.
``GITEA_TOKEN`` is in that reserved range: Gitea answers
``400 name cannot start with 'GITEA_' or 'GITHUB_'`` and injects its own job
token under that name instead. The personal access token is therefore stored
as ``OCR_GITEA_TOKEN``.

Usage::

    # Tokens come from the environment, not the command line.
    export GITEA_API_TOKEN=... OCR_LLM_AUTH_TOKEN=... OCR_GITEA_TOKEN=...
    python3 setup_variables.py \\
        --server http://gitea.example.com \\
        --owner myorg --repo myrepo \\
        --llm-url https://api.openai.com/v1/chat/completions

The script is idempotent: existing entries are updated in place instead of
being duplicated. Pass ``--dry-run`` to print the planned changes without
touching the instance.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# The access token is stored under this name. Gitea reserves the
# "GITEA_" prefix for its built-in job token and refuses to store a secret
# whose name starts with it.
GITEA_TOKEN_SECRET = "OCR_GITEA_TOKEN"

# Entries written to the Variables store (readable via ${{ vars.NAME }}).
VARIABLE_NAMES = (
    "OCR_LLM_URL",
    "OCR_LLM_MODEL",
    "OCR_VERSION",
    "OCR_LLM_USE_ANTHROPIC",
    "OCR_BACKGROUND",
    "OCR_RULE",
    "OCR_LLM_AUTH_HEADER",
    "OCR_LLM_EXTRA_HEADERS",
    "OCR_LLM_TIMEOUT",
    "OCR_REVIEW_CONCURRENCY",
)

# Entries written to the Secrets store (readable via ${{ secrets.NAME }}).
# Credentials must never be variables: those are plain text and are handed
# to fork pull requests, which run their own copy of the workflow.
SECRET_NAMES = (
    "OCR_LLM_AUTH_TOKEN",
    GITEA_TOKEN_SECRET,
)

# Name prefixes Gitea rejects in both stores.
RESERVED_PREFIXES = ("GITEA_", "GITHUB_", "CI")


class SetupError(Exception):
    """Raised when the instance rejects a request."""


def _build_opener():
    """Return an opener that ignores the ambient proxy settings.

    Gitea instances are commonly reachable only on the local network, so an
    inherited http_proxy would break the call.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(opener, method, url, token, payload=None):
    """Perform an API call and return the (status, body) pair."""
    data = None
    headers = {
        "Authorization": "token %s" % token,
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        return error.code, body


def _error_message(body):
    """Extract the message field from a Gitea error body."""
    try:
        return json.loads(body).get("message", body)
    except (ValueError, AttributeError):
        return body


def check_name_allowed(name):
    """Return an error message when Gitea would reject ``name``."""
    # Gitea upper-cases names before checking them, so "gitea_x" is reserved too.
    for prefix in RESERVED_PREFIXES:
        if name.upper().startswith(prefix):
            return ("name cannot start with %r -- pick another name" % prefix)
    if not name or not (name[0].isalpha() or name[0] == "_"):
        return "name must start with a letter or an underscore"
    for char in name:
        if not (char.isalnum() or char == "_"):
            return "name may only contain letters, digits and underscores"
    return None


def set_variable(opener, base_url, owner, repo, name, value, dry_run=False, token=""):
    """Create or update one variable. Returns the action that was taken."""
    problem = check_name_allowed(name)
    if problem:
        raise SetupError("invalid variable name %r: %s" % (name, problem))
    url = "%s/repos/%s/%s/actions/variables/%s" % (base_url, owner, repo, name)
    if dry_run:
        return "would set variable %s" % name
    status, body = _request(opener, "POST", url, token, {"value": value})
    if status == 201:
        return "created variable %s" % name
    if status == 409:
        status, body = _request(opener, "PUT", url, token, {"value": value})
        if status == 204:
            return "updated variable %s" % name
        raise SetupError(
            "could not update variable %s: %s" % (name, _error_message(body)))
    raise SetupError(
        "could not create variable %s: %s" % (name, _error_message(body)))


def set_secret(opener, base_url, owner, repo, name, value, dry_run=False, token=""):
    """Create or update one secret. Returns the action that was taken."""
    problem = check_name_allowed(name)
    if problem:
        raise SetupError("invalid secret name %r: %s" % (name, problem))
    url = "%s/repos/%s/%s/actions/secrets/%s" % (base_url, owner, repo, name)
    if dry_run:
        return "would set secret %s" % name
    status, body = _request(opener, "PUT", url, token, {"data": value})
    if status in (201, 204):
        return "%s secret %s" % ("created" if status == 201 else "updated", name)
    raise SetupError("could not set secret %s: %s" % (name, _error_message(body)))


def build_entries(args):
    """Build the ordered list of (store, name, value) entries to write."""
    entries = []
    values = {
        "OCR_LLM_URL": args.llm_url,
        "OCR_LLM_AUTH_TOKEN": args.llm_token,
        "OCR_LLM_MODEL": args.llm_model,
        "OCR_VERSION": args.ocr_version,
        "OCR_LLM_USE_ANTHROPIC": args.llm_use_anthropic,
        "OCR_BACKGROUND": args.background,
        "OCR_RULE": args.rule,
        "OCR_LLM_AUTH_HEADER": args.llm_auth_header,
        "OCR_LLM_EXTRA_HEADERS": args.llm_extra_headers,
        "OCR_LLM_TIMEOUT": args.llm_timeout,
        "OCR_REVIEW_CONCURRENCY": args.review_concurrency,
        GITEA_TOKEN_SECRET: args.gitea_token,
    }
    for store, names in (("variable", VARIABLE_NAMES), ("secret", SECRET_NAMES)):
        for name in names:
            if values.get(name):
                entries.append((store, name, values[name]))
    return entries


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Create the CI/CD variables and secrets for the Gitea "
                    "OpenCodeReview workflow.")
    parser.add_argument("--server", required=True,
                        help="Gitea instance URL, e.g. http://gitea.example.com")
    parser.add_argument("--owner", required=True, help="Repository owner")
    parser.add_argument("--repo", required=True, help="Repository name")
    # Tokens default to environment variables so they need not appear on the
    # command line, where the process list and shell history keep them.
    parser.add_argument("--token", default=os.environ.get("GITEA_API_TOKEN", ""),
                        help="API token used to authenticate the calls "
                             "(default: $GITEA_API_TOKEN)")
    parser.add_argument("--llm-url", default="",
                        help="LLM API endpoint (variable OCR_LLM_URL)")
    parser.add_argument("--llm-token", default=os.environ.get("OCR_LLM_AUTH_TOKEN", ""),
                        help="LLM API token (secret OCR_LLM_AUTH_TOKEN; "
                             "default: $OCR_LLM_AUTH_TOKEN)")
    parser.add_argument("--llm-model", default="",
                        help="Model name (variable OCR_LLM_MODEL)")
    parser.add_argument("--llm-use-anthropic", default="",
                        help="'true' for the Anthropic API format")
    parser.add_argument("--llm-auth-header", default="",
                        help="Custom auth header name")
    parser.add_argument("--llm-extra-headers", default="",
                        help="Extra headers, K=V,K=V")
    parser.add_argument("--llm-timeout", default="",
                        help="LLM request timeout in seconds")
    parser.add_argument("--ocr-version", default="",
                        help="npm version spec for @alibaba-group/open-code-review")
    parser.add_argument("--background", default="",
                        help="Business context passed to `ocr review --background`")
    parser.add_argument("--rule", default="",
                        help="Path to a custom rules JSON file")
    parser.add_argument("--review-concurrency", default="",
                        help="Max concurrent file reviews")
    parser.add_argument("--gitea-token", default=os.environ.get(GITEA_TOKEN_SECRET, ""),
                        help="Gitea access token (secret %s; default: $%s)"
                             % (GITEA_TOKEN_SECRET, GITEA_TOKEN_SECRET))
    parser.add_argument("--dry-run", action="store_true",
                        help="print the planned changes without applying them")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.token and not args.dry_run:
        print("ERROR: pass --token or set GITEA_API_TOKEN", file=sys.stderr)
        return 2
    base_url = "%s/api/v1" % args.server.rstrip("/")
    opener = _build_opener()

    entries = build_entries(args)
    if not entries:
        print("nothing to do: pass at least --llm-url or --gitea-token")
        return 0

    failures = 0
    for store, name, value in entries:
        try:
            if store == "variable":
                message = set_variable(opener, base_url, args.owner, args.repo,
                                       name, value, args.dry_run, args.token)
            else:
                message = set_secret(opener, base_url, args.owner, args.repo,
                                     name, value, args.dry_run, args.token)
        except SetupError as error:
            print("ERROR: %s" % error, file=sys.stderr)
            failures += 1
            continue
        print(message)

    if failures:
        print("%d of %d entries failed" % (failures, len(entries)), file=sys.stderr)
        return 1
    print("done: %d entries applied" % len(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())

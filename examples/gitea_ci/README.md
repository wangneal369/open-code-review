# OpenCodeReview - Gitea Actions Demo

This demo shows how to integrate OpenCodeReview into your Gitea Actions
pipeline to automatically review pull requests and post review comments as
inline review comments.

## How It Works

```
PR Opened/Updated -> Gitea Actions triggered -> OCR reviews the diff -> Review posted on the PR
```

1. When a pull request is opened or updated, the Gitea Actions workflow is triggered
2. It installs OCR via npm in a `node:20` Docker image
3. Runs `ocr review --from origin/<target> --to <head_sha> --format json --audience agent` to analyze the diff (the head SHA is used so pull requests from forks are reviewed correctly)
4. Parses the JSON output and posts the findings on the PR using Gitea's Pull Request Review API

## Setup

### 1. Copy the workflow and script files

Copy **both** `ocr-review.yml` and `post_review.py` to your repository's
`.gitea/workflows/` directory (adjust the `python3 post_review.py` path in
the YAML if you place the script elsewhere):

```bash
mkdir -p .gitea/workflows
cp ocr-review.yml .gitea/workflows/ocr-review.yml
cp post_review.py .gitea/workflows/post_review.py
```

### 2. Configure CI/CD Variables

Go to your repository's **Settings → Actions → Variables** and add:

| Variable | Required | Masked | Description |
|----------|----------|--------|-------------|
| `OCR_LLM_URL` | Yes | No | LLM API endpoint URL (e.g., `https://api.openai.com/v1/chat/completions`) |
| `OCR_LLM_AUTH_TOKEN` | Yes | Yes | API authentication token |
| `GITEA_TOKEN` | Yes | Yes | Gitea access token used to post the review |
| `OCR_LLM_MODEL` | No | No | Model name (e.g., `gpt-4o`) — OCR has no built-in default model and fails when this is unset |
| `OCR_VERSION` | No | No | npm version spec for `@alibaba-group/open-code-review` (default: `latest`). Pin it (e.g. `1.9.0`) for reproducible reviews. |
| `OCR_LLM_USE_ANTHROPIC` | No | No | `true` for the Anthropic API format, `false` (default) for OpenAI-compatible APIs |
| `OCR_BACKGROUND` | No | No | Business/requirement context, passed via `ocr review --background` |
| `OCR_RULE` | No | No | Path to a custom rules JSON file, passed via `ocr review --rule` |
| `OCR_LLM_AUTH_HEADER` | No | No | Custom auth header name, written via `ocr config set llm.auth_header` |
| `OCR_LLM_EXTRA_HEADERS` | No | No | Extra headers `K=V,K=V`, written via `ocr config set llm.extra_headers` |
| `OCR_LLM_TIMEOUT` | No | No | LLM request timeout in seconds (read natively by OCR from the env) |
| `OCR_REVIEW_CONCURRENCY` | No | No | Max concurrent file reviews, passed via `ocr review --concurrency` (default: 8) |

> **Note:** The workflow sets `llm.extra_body` to disable thinking mode for
> compatibility with various LLM providers.

### 3. Create a Gitea Access Token

You need a token with write permission to post review comments on pull
requests. Options:

- **Repository Access Token** (recommended): Settings → Actions → Runners → ... → or use an organization token
- **Personal Access Token**: User Settings → Applications → Generate Token with `write:repository` scope

Set it as the `GITEA_TOKEN` repository variable (masked).

> **Tip:** The token owner is the author of the review comments. Use a
> dedicated bot account if you want the comments to appear under a distinct
> identity (e.g. "OpenCodeReview Bot").

## Example Output

When a pull request is reviewed, comments appear as:

- **Inline review comments**: Directly on the changed lines in the PR "Files changed" view, each prefixed with a `[category · severity]` badge when the LLM provided that metadata
- **Summary comment**: A single comment (updated in place across runs by default) summarizing the total number of issues found, with mutually-exclusive counts (inline / summary / routed / skipped / failed) and a detailed warnings list

## Gitea API specifics

The posting script is written against the Gitea REST API and follows a few
rules that differ from the GitHub and GitLab examples:

- **Inline comments are posted as one review.** Gitea has no
  `POST /pulls/{index}/reviews/{id}/comments` endpoint, so all inline
  comments of a run travel in a single
  `POST /repos/{owner}/{repo}/pulls/{index}/reviews` request. The script
  therefore splits large runs into batches of at most 20 comments.
- **`new_position` is a line number**, not a diff position index. The server
  resolves the line against the patch itself.
- **Every line is validated against the diff before posting.** Gitea applies
  a review atomically: one line that cannot be resolved rejects the whole
  request. The script therefore fetches the pull request patch
  (`GET /pulls/{index}.diff`, served as `text/plain`) and moves findings
  whose line is not part of the diff into the summary note instead of
  losing the whole batch.
- **The summary is a conversation comment**, not a review body. A submitted
  Gitea review cannot be edited (only deleted), so the sticky summary lives
  on `POST /repos/{owner}/{repo}/issues/{index}/comments` where it can be
  updated in place across runs.
- **Authentication uses the `Authorization: token <token>` header.** The
  `?token=` query parameter and the `access_token` scheme are deprecated
  since Gitea 1.23 and are not used.
- **A commit status is reported** on the head commit through
  `POST /repos/{owner}/{repo}/statuses/{sha}`, so the PR shows a passing or
  failing check.

## Customization

### Publication behavior

The posting script exposes the same behaviors as the GitLab example. All are
optional and set through CI/CD Variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OCR_STICKY_SUMMARY` | `true` | Update the summary comment in place across runs. Set `false` to post a fresh comment each run. |
| `OCR_INCREMENTAL` | `false` | Skip comments whose line range overlaps a prior bot review comment on the same path, so re-runs only append new findings. |
| `OCR_INCREMENTAL_OVERLAP_THRESHOLD` | `0.6` | IoU (0–1) above which two multi-line comments are considered the same. Single-line comments match only on the exact same line; a single-line and a multi-line comment never match. |
| `OCR_ROUTE_SEVERITY_BELOW` | _(empty)_ | Route findings at-or-below this level (`critical`, `high`, `medium`, `low`) to the summary note instead of posting them inline. Unknown severities never match. |
| `OCR_ROUTE_CATEGORIES` | _(empty)_ | Comma-separated categories (`bug`, `security`, `performance`, `maintainability`, `test`, `style`, `documentation`, `other`) routed from inline to the summary note. |
| `OCR_FAIL_ON_SEVERITY` | _(empty)_ | Fail the CI job (non-zero exit) when any finding's severity is at-or-above this level. The summary comment is still posted before the job fails. |

Additional behaviors (always on):

- **Category/severity badge**: each inline comment and summary entry is prefixed with `[category · severity]` (using a middot, U+00B7) when the LLM provided that metadata.
- **Deterministic ordering**: comments are sorted by `path → start_line → end_line → original index` before posting, so identical reruns produce identical post sequences.
- **Detailed warnings**: warnings are rendered as a bulleted list (`file (type): message`) rather than a bare count.
- **Backtick-safe fences**: summary code blocks use a fence long enough to enclose any backticks in the code.
- **Idempotency tags**: each inline comment carries an invisible HTML-comment id tag (`<!-- ocr-<hash> -->`) so a retry can be reconciled against what was actually posted.

### Retry and pacing settings

| Variable | Default | Description |
|----------|---------|-------------|
| `OCR_RETRY_BASE_DELAY` | `2000` | Base delay (ms) for exponential backoff when a retryable error is hit |
| `OCR_MAX_RETRIES` | `3` | Maximum retry attempts per request |
| `OCR_MAX_RETRY_DELAY` | `60000` | Maximum delay (ms) for a single retry |
| `OCR_SUCCESS_DELAY` | `2000` | Delay (ms) after a successful request to pace subsequent requests |
| `OCR_FAILURE_DELAY` | `1000` | Delay (ms) after a non-retryable failure |
| `OCR_RATE_LIMIT_THRESHOLD` | `10` | Proactively slow down when the server reports a low remaining quota |

### Use a specific OCR version

Set the `OCR_VERSION` CI/CD variable (e.g. `1.9.0` or `~1.9`). Unset defaults to `latest`.

### Add custom review rules

Set the `OCR_RULE` CI/CD variable to the path of a custom rules JSON file (passed to `ocr review --rule`). For rules that live inside the repo, commit the file and point `OCR_RULE` at its path.

### Provide background context

Set the `OCR_BACKGROUND` CI/CD variable (passed to `ocr review --background`). The pull request title is a common choice.

### Change the trigger events

By default, the workflow triggers on `pull_request` events (`opened`, `synchronize`, `reopened`). Adjust the `on:` block to change this.

## CI outputs and artifacts

The script writes a dotenv report (`.ocr/ocr-stats.env`) with
`OCR_COMMENTS_TOTAL`, `OCR_COMMENTS_INLINE`, `OCR_COMMENTS_SUMMARY`,
`OCR_COMMENTS_ROUTED`, `OCR_COMMENTS_SKIPPED`, `OCR_COMMENTS_FAILED` and
`OCR_SUMMARY_URL`, so later stages can consume them.

## Troubleshooting

Common issues:

1. **"error: missing required GITEA_SERVER_URL"** — the script could not resolve the PR context. It reads `GITEA_SERVER_URL`, `GITEA_REPOSITORY` and `GITEA_PULL_REQUEST_INDEX`; the workflow sets these from the GitHub-compatible context variables.
2. **"Cannot find merge-base"** — the checkout was shallow. The workflow uses `fetch-depth: 0`; if you changed it, restore the full history.
3. **A finding shows up in the summary instead of inline** — its line is not part of the pull request diff, so it cannot be anchored. The summary entry says so explicitly.
4. **"Failed to parse OCR output"** — `OCR_LLM_URL` and `OCR_LLM_AUTH_TOKEN` are unset, or the review crashed. The OCR stderr log is included in the summary comment in that case.

## Testing

The posting logic is unit-tested with no network access and no wall-clock sleep cost. The tests use only the standard-library `unittest`:

```bash
# From the example directory
cd examples/gitea_ci
python3 post_review_test.py

# From the repo root
python3 -m unittest discover -s examples/gitea_ci -p '*_test.py'
```

# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""OCR 评审服务单测：不访问网络，不启动真实 runner。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import settings  # noqa: E402


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "data", "settings.json")

    def test_secrets_are_encrypted_at_rest_and_round_trip(self):
        settings.save({"gitea_url": "http://g", "llm_key": "secret-\u4e2d", "bot_token": "pat"},
                      self.path)
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertNotIn("secret", raw["llm_key"])
        self.assertNotEqual(raw["bot_token"], "pat")
        values = settings.load(self.path)
        self.assertEqual((values["llm_key"], values["bot_token"]), ("secret-\u4e2d", "pat"))
        self.assertEqual(values["capacity"], 1)

    def test_undecryptable_secret_becomes_empty(self):
        # 换机器或换账号后 DPAPI 解不开，应置空而不是崩溃
        settings.save({"llm_key": "k"}, self.path)
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        raw["llm_key"] = "AAAA"
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        self.assertEqual(settings.load(self.path)["llm_key"], "")

    def test_missing_file_gives_defaults(self):
        self.assertEqual(settings.load(self.path), settings.DEFAULTS)

    def test_missing_fields(self):
        self.assertEqual(len(settings.missing_fields(settings.DEFAULTS)), 5)
        full = dict(settings.DEFAULTS, gitea_url="g", llm_url="u", llm_model="m",
                    llm_key="k", bot_token="t")
        self.assertEqual(settings.missing_fields(full), [])

    def test_bundled_tools_lead_the_path(self):
        env = settings.tool_path_env({"PATH": r"C:\Windows"})
        parts = env["PATH"].split(os.pathsep)
        self.assertEqual(parts[0], settings.BASE_DIR)
        self.assertIn(settings.GIT_CMD_DIR, parts)
        self.assertEqual(parts[-1], r"C:\Windows")


import subprocess  # noqa: E402

import review  # noqa: E402


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, env=settings.tool_path_env(), check=True,
                          capture_output=True, text=True).stdout.strip()


class ReviewEventTests(unittest.TestCase):
    def _write(self, payload):
        path = os.path.join(tempfile.mkdtemp(), "event.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def test_reads_pull_request_fields(self):
        path = self._write({"pull_request": {"number": 7, "base": {"ref": "main"},
                                             "head": {"sha": "abc"}},
                            "repository": {"full_name": "o/r"}})
        self.assertEqual(review.read_event(path),
                         {"number": 7, "base_ref": "main", "head_sha": "abc", "repo": "o/r"})

    def test_non_pr_event_is_rejected(self):
        with self.assertRaises(review.ReviewError):
            review.read_event(self._write({"ref": "refs/heads/main"}))

    def test_missing_event_file_is_rejected(self):
        with self.assertRaises(review.ReviewError):
            review.read_event("")

    def test_token_never_on_the_command_line(self):
        env = review.git_env("secret-token")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: token secret-token")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_ocr_env_maps_settings(self):
        env = review.ocr_env({"llm_url": "u", "llm_key": "k", "llm_model": "m",
                              "llm_protocol": "anthropic"})
        self.assertEqual((env["OCR_LLM_URL"], env["OCR_LLM_TOKEN"], env["OCR_LLM_MODEL"],
                          env["OCR_LLM_PROTOCOL"]), ("u", "k", "m", "anthropic"))


class CheckoutTests(unittest.TestCase):
    """用内置 git 与本地裸仓库模拟 Gitea 的 refs/pull/N/head。"""

    def setUp(self):
        root = tempfile.mkdtemp()
        self.remote = os.path.join(root, "remote.git")
        seed = os.path.join(root, "seed")
        os.makedirs(seed)
        _git(["init", "-q", "--bare", self.remote], root)
        _git(["init", "-q", "-b", "main"], seed)
        _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
              "--allow-empty", "-m", "base"], seed)
        _git(["push", "-q", self.remote, "main"], seed)
        _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
              "--allow-empty", "-m", "pr"], seed)
        self.head = _git(["rev-parse", "HEAD"], seed)
        _git(["push", "-q", self.remote, "HEAD:refs/pull/3/head"], seed)
        self.work = os.path.join(root, "work")
        self.event = {"number": 3, "base_ref": "main", "head_sha": self.head, "repo": "o/r"}

    def test_checkout_detaches_at_head_and_has_base(self):
        review.checkout(self.work, self.remote, self.event, "tok")
        self.assertEqual(_git(["rev-parse", "HEAD"], self.work), self.head)
        _git(["rev-parse", "--verify", "origin/main"], self.work)

    def test_checkout_is_reentrant_on_a_reused_workspace(self):
        review.checkout(self.work, self.remote, self.event, "tok")
        review.checkout(self.work, self.remote, self.event, "tok")
        self.assertEqual(_git(["rev-parse", "HEAD"], self.work), self.head)

    def test_unknown_commit_is_a_review_error(self):
        with self.assertRaises(review.ReviewError):
            review.checkout(self.work, self.remote, dict(self.event, head_sha="0" * 40), "tok")


class PostEnvTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.orig = review.post_review.main
        self.seen = {}
        review.post_review.main = lambda argv: self.seen.update(
            argv=argv, env=dict(os.environ)) or 0

    def tearDown(self):
        review.post_review.main = self.orig
        os.environ.clear()
        os.environ.update(self.saved)

    def test_uses_registered_address_not_root_url(self):
        values = {"gitea_url": "http://10.0.0.5:3000/", "bot_token": "pat"}
        event = {"number": 9, "head_sha": "abc", "repo": "o/r"}
        review.post("C:\\w", event, values, 1)
        env = self.seen["env"]
        self.assertEqual(env["GITEA_SERVER_URL"], "http://10.0.0.5:3000")
        self.assertEqual((env["GITEA_PULL_REQUEST_INDEX"], env["GITEA_TOKEN"],
                          env["OCR_HEAD_SHA"], env["OCR_EXIT_CODE"]), ("9", "pat", "abc", "1"))

    def test_successful_review_clears_exit_code(self):
        os.environ["OCR_EXIT_CODE"] = "1"
        review.post("C:\\w", {"number": 1, "head_sha": "a", "repo": "o/r"},
                    {"gitea_url": "http://g", "bot_token": "t"}, 0)
        self.assertNotIn("OCR_EXIT_CODE", self.seen["env"])


import time  # noqa: E402

import runner  # noqa: E402


class RunnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.patches = {
            (runner, "RUNNER_FILE"): os.path.join(tmp, ".runner"),
            (runner, "CONFIG_FILE"): os.path.join(tmp, "config.yaml"),
            (runner, "LOG_FILE"): os.path.join(tmp, "logs", "runner.log"),
            (runner, "BACKOFF"): (0,),
            (settings, "DATA_DIR"): tmp,
            (settings, "LOG_DIR"): os.path.join(tmp, "logs"),
            (settings, "ACT_RUNNER"): sys.executable,  # 会因参数无效立刻退出，模拟崩溃
        }
        self.saved = {k: getattr(*k) for k in self.patches}
        for (mod, name), value in self.patches.items():
            setattr(mod, name, value)

    def tearDown(self):
        for (mod, name), value in self.saved.items():
            setattr(mod, name, value)

    def test_config_uses_host_label_and_absolute_paths(self):
        text = runner.config_text(2)
        self.assertIn("- 'ocr-windows:host'", text)
        self.assertIn("capacity: 2", text)
        self.assertIn(runner.RUNNER_FILE.replace("'", "''"), text)
        self.assertIn("enabled: false", text)

    def test_unregistered_runner_does_not_start(self):
        m = runner.RunnerManager()
        m.start()
        self.assertEqual(m.state, "error")
        self.assertIn("注册", m.detail)

    def test_crashing_runner_is_restarted_until_stopped(self):
        with open(runner.RUNNER_FILE, "w") as f:
            f.write("{}")
        states = []
        m = runner.RunnerManager(on_change=states.append)
        m.start()
        deadline = time.monotonic() + 15
        while states.count("running") < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        m.stop()
        self.assertGreaterEqual(states.count("running"), 2, states)
        self.assertIn("error", states)
        self.assertEqual(m.state, "stopped")
        self.assertTrue(any("runner 异常退出" in line for line in runner.tail_log(50)))

    def test_registered_instance(self):
        with open(runner.RUNNER_FILE, "w") as f:
            json.dump({"address": "http://g:3000"}, f)
        self.assertEqual(runner.registered_instance(), "http://g:3000")


import base64  # noqa: E402
import urllib.error  # noqa: E402

import gitea_api  # noqa: E402


class _Resp:
    def __init__(self, status, body=None):
        self.status, self._body = status, json.dumps(body).encode() if body is not None else b""

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def open(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body))
        for (method, frag), resp in self.routes.items():
            if req.get_method() == method and frag in req.full_url:
                if resp.status >= 400:
                    raise urllib.error.HTTPError(req.full_url, resp.status, "", {}, resp)
                return resp
        raise AssertionError("unexpected %s %s" % (req.get_method(), req.full_url))


class DeployTests(unittest.TestCase):
    REPO = {"default_branch": "main", "permissions": {"push": True}}

    def test_creates_workflow_on_default_branch(self):
        op = _Opener({("GET", "/repos/o/r/contents/"): _Resp(404, {"message": "nf"}),
                      ("GET", "/repos/o/r"): _Resp(200, self.REPO),
                      ("POST", "/contents/"): _Resp(201, {})})
        text = gitea_api.deploy_workflow("http://g/", "t", "o", "r", opener=op)
        method, url, body = op.calls[-1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/contents/.gitea/workflows/ocr-review.yml"))
        self.assertEqual(body["branch"], "main")
        self.assertIn(b"runs-on: ocr-windows", base64.b64decode(body["content"]))
        self.assertIn("新建", text)

    def test_same_content_is_skipped_and_changed_content_updated(self):
        with open(gitea_api.WORKFLOW_SOURCE, "rb") as f:
            same = base64.b64encode(f.read()).decode()
        op = _Opener({("GET", "/repos/o/r/contents/"): _Resp(200, {"sha": "s", "content": same}),
                      ("GET", "/repos/o/r"): _Resp(200, self.REPO)})
        self.assertIn("无需改动", gitea_api.deploy_workflow("http://g", "t", "o", "r", opener=op))
        op = _Opener({("GET", "/repos/o/r/contents/"): _Resp(200, {"sha": "s", "content": ""}),
                      ("GET", "/repos/o/r"): _Resp(200, self.REPO),
                      ("PUT", "/contents/"): _Resp(200, {})})
        gitea_api.deploy_workflow("http://g", "t", "o", "r", opener=op)
        self.assertEqual(op.calls[-1][2]["sha"], "s")

    def test_without_push_permission_nothing_is_written(self):
        op = _Opener({("GET", "/repos/o/r"): _Resp(200, {"permissions": {"push": False}})})
        with self.assertRaisesRegex(gitea_api.ApiError, "写权限"):
            gitea_api.deploy_workflow("http://g", "t", "o", "r", opener=op)
        self.assertEqual([c[0] for c in op.calls], ["GET"])

    def test_remove_deletes_existing_and_skips_missing(self):
        op = _Opener({("GET", "/repos/o/r/contents/"): _Resp(200, {"sha": "s", "content": ""}),
                      ("GET", "/repos/o/r"): _Resp(200, self.REPO),
                      ("DELETE", "/contents/"): _Resp(200, {})})
        self.assertIn("删除", gitea_api.remove_workflow("http://g", "t", "o", "r", opener=op))
        method, url, body = op.calls[-1]
        self.assertEqual((method, body["sha"], body["branch"]), ("DELETE", "s", "main"))
        op = _Opener({("GET", "/repos/o/r/contents/"): _Resp(404, {}),
                      ("GET", "/repos/o/r"): _Resp(200, self.REPO)})
        self.assertIn("无需停用", gitea_api.remove_workflow("http://g", "t", "o", "r", opener=op))
        self.assertNotIn("DELETE", [c[0] for c in op.calls])

    def test_workflow_status(self):
        with open(gitea_api.WORKFLOW_SOURCE, "rb") as f:
            same = base64.b64encode(f.read()).decode()
        repo = {"name": "r", "default_branch": "dev"}
        for current, expected in ((_Resp(200, {"content": same}), "enabled"),
                                  (_Resp(200, {"content": ""}), "outdated"),
                                  (_Resp(404, {}), "disabled")):
            op = _Opener({("GET", "/repos/o/r/contents/"): current})
            self.assertEqual(gitea_api.workflow_status("http://g", "t", "o", repo, opener=op),
                             expected)
            self.assertIn("ref=dev", op.calls[0][1])

    def test_list_repos_pages_and_falls_back_to_user(self):
        page1 = [{"name": "r%d" % i} for i in range(50)]
        op = _Opener({("GET", "/orgs/o/repos?page=1"): _Resp(200, page1),
                      ("GET", "/orgs/o/repos?page=2"): _Resp(200, [{"name": "last"}])})
        self.assertEqual(len(gitea_api.list_repos("http://g", "t", "o", opener=op)), 51)
        op = _Opener({("GET", "/orgs/u/repos"): _Resp(404, {}),
                      ("GET", "/users/u/repos"): _Resp(200, [{"name": "x"}])})
        self.assertEqual(gitea_api.list_repos("http://g", "t", "u", opener=op), [{"name": "x"}])
        op = _Opener({("GET", "/orgs/n/repos"): _Resp(404, {}),
                      ("GET", "/users/n/repos"): _Resp(404, {})})
        with self.assertRaisesRegex(gitea_api.ApiError, "找不到"):
            gitea_api.list_repos("http://g", "t", "n", opener=op)

    def test_skip_reason(self):
        ok = {"permissions": {"push": True}}
        self.assertEqual(gitea_api.skip_reason(ok), "")
        self.assertEqual(gitea_api.skip_reason(dict(ok, archived=True)), "已归档")
        self.assertEqual(gitea_api.skip_reason(dict(ok, empty=True)), "空仓库")
        self.assertEqual(gitea_api.skip_reason({"permissions": {"push": False}}), "Bot 令牌无写权限")

    def test_check_user(self):
        op = _Opener({("GET", "/user"): _Resp(200, {"login": "bot"})})
        self.assertEqual(gitea_api.check_user("http://g", "t", opener=op), "bot")
        op = _Opener({("GET", "/user"): _Resp(401, {"message": "bad"})})
        with self.assertRaisesRegex(gitea_api.ApiError, "无效"):
            gitea_api.check_user("http://g", "t", opener=op)


if __name__ == "__main__":
    unittest.main()

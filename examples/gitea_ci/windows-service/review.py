# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""评审命令：由 runner 在 PR 事件中调用（ocr-review.exe）。

流程：读事件 → 用内置 git 拉取目标分支与 PR → 运行 ocr → 用 post_review 回帖。
全部配置来自本机设置，workflow 不需要传任何参数，仓库也不需要配置 Secrets。
"""

import json
import os
import subprocess
import sys

import settings

if not settings.FROZEN:
    sys.path.insert(0, os.path.normpath(os.path.join(settings.HERE, "..")))  # post_review.py
import post_review  # noqa: E402


class ReviewError(Exception):
    """无法继续评审的错误（配置缺失、拉代码失败等）。"""


def log(msg):
    print(msg, flush=True)


def read_event(path):
    """从 runner 提供的事件文件中取出评审所需的 PR 信息。"""
    if not path or not os.path.exists(path):
        raise ReviewError("找不到事件文件（GITHUB_EVENT_PATH），本命令只能由 runner 在 PR 事件中调用")
    with open(path, encoding="utf-8") as f:
        event = json.load(f)
    pr = event.get("pull_request")
    if not pr:
        raise ReviewError("当前事件不是 pull_request，跳过评审")
    return {
        "number": pr["number"],
        "base_ref": pr["base"]["ref"],
        "head_sha": pr["head"]["sha"],
        "repo": event["repository"]["full_name"],
    }


def git_env(token):
    """令牌经 GIT_CONFIG_* 传入，不出现在命令行（进程列表）里。"""
    env = settings.tool_path_env()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: token %s" % token,
    })
    return env


def git(args, cwd, env):
    proc = subprocess.run(["git"] + args, cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise ReviewError("git %s 失败：%s" % (args[0], (proc.stderr or proc.stdout).strip()))
    return proc.stdout


def checkout(workdir, clone_url, event, token):
    """拉取目标分支和 PR 头，检出 head 提交。工作区可能复用，所以每步都可重入。"""
    env = git_env(token)
    os.makedirs(workdir, exist_ok=True)
    if not os.path.isdir(os.path.join(workdir, ".git")):
        git(["init", "-q"], workdir, env)
    remotes = git(["remote"], workdir, env).split()
    git(["remote", "set-url" if "origin" in remotes else "add", "origin", clone_url], workdir, env)
    base = event["base_ref"]
    # Gitea 为每个 PR 维护 refs/pull/N/head，fork 来的 PR 也能从主仓库拉到
    git(["fetch", "--no-tags", "--prune", "origin",
         "+refs/heads/%s:refs/remotes/origin/%s" % (base, base),
         "+refs/pull/%d/head:refs/remotes/origin/pr/%d" % (event["number"], event["number"])],
        workdir, env)
    git(["checkout", "-q", "--force", "--detach", event["head_sha"]], workdir, env)


def ocr_env(values):
    env = settings.tool_path_env()
    env.update({
        "OCR_LLM_URL": values["llm_url"],
        "OCR_LLM_TOKEN": values["llm_key"],
        "OCR_LLM_MODEL": values["llm_model"],
        "OCR_LLM_PROTOCOL": values.get("llm_protocol") or "openai",
    })
    return env


def run_ocr(workdir, event, values):
    """运行 ocr，结果写入 .ocr/；返回 ocr 的退出码。stderr 同时回显到 runner 日志。"""
    out_dir = os.path.join(workdir, ".ocr")
    os.makedirs(out_dir, exist_ok=True)
    cmd = [settings.OCR, "review", "--from", "origin/%s" % event["base_ref"],
           "--to", event["head_sha"], "--format", "json", "--audience", "agent"]
    with open(os.path.join(out_dir, "ocr-result.json"), "wb") as out, \
            open(os.path.join(out_dir, "ocr-stderr.log"), "wb") as err:
        code = subprocess.call(cmd, cwd=workdir, env=ocr_env(values), stdout=out, stderr=err)
    with open(os.path.join(out_dir, "ocr-stderr.log"), encoding="utf-8", errors="replace") as f:
        log(f.read().rstrip())
    return code


def post(workdir, event, values, ocr_code):
    """用 post_review 回帖；它从环境变量读取 PR 上下文。"""
    out_dir = os.path.join(workdir, ".ocr")
    os.environ.update({
        # 用注册时填写的地址，而不是 Gitea 的 ROOT_URL：经隧道或内网 IP 访问时两者不同
        "GITEA_SERVER_URL": values["gitea_url"].rstrip("/"),
        "GITEA_REPOSITORY": event["repo"],
        "GITEA_PULL_REQUEST_INDEX": str(event["number"]),
        "GITEA_TOKEN": values["bot_token"],
        "OCR_HEAD_SHA": event["head_sha"],
    })
    if ocr_code:
        os.environ["OCR_EXIT_CODE"] = str(ocr_code)
    else:
        os.environ.pop("OCR_EXIT_CODE", None)
    return post_review.main([os.path.join(out_dir, "ocr-result.json"),
                             "--stderr-log", os.path.join(out_dir, "ocr-stderr.log"),
                             "--stats-file", os.path.join(out_dir, "ocr-stats.env")])


def main():
    for stream in (sys.stdout, sys.stderr):
        # 中文 Windows 控制台默认 GBK，遇到 emoji 会直接抛异常
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        values = settings.load()
        missing = settings.missing_fields(values)
        if missing:
            raise ReviewError("评审服务未配置完整，缺少：%s。请在托盘「设置」中填写"
                              % "、".join(missing))
        event = read_event(os.environ.get("GITHUB_EVENT_PATH"))
        workdir = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
        clone_url = "%s/%s.git" % (values["gitea_url"].rstrip("/"), event["repo"])
        log("» 拉取 %s PR #%d（%s）" % (event["repo"], event["number"], event["head_sha"][:10]))
        checkout(workdir, clone_url, event, values["bot_token"])
        log("» 运行 ocr 评审")
        ocr_code = run_ocr(workdir, event, values)
        log("» 发布评审结果（ocr 退出码 %d）" % ocr_code)
        post_code = post(workdir, event, values, ocr_code)
    except ReviewError as error:
        log("✗ %s" % error)
        return 2
    return 1 if ocr_code or post_code else 0


if __name__ == "__main__":
    sys.exit(main())

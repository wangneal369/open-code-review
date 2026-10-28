# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""接入仓库所需的 Gitea API：检查连接、部署 workflow。"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import settings

WORKFLOW_TARGET = ".gitea/workflows/ocr-review.yml"
if settings.FROZEN:
    WORKFLOW_SOURCE = os.path.join(sys._MEIPASS, "payload", "ocr-review-windows.yml")
else:
    WORKFLOW_SOURCE = os.path.normpath(os.path.join(settings.HERE, "..", "ocr-review-windows.yml"))


class ApiError(Exception):
    pass


def _opener():
    # 公司 Gitea 通常只在内网可达，继承的系统代理反而会把请求带偏
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(gitea_url, token, method, path, payload=None, opener=None):
    """返回 (status, JSON 或 None)。"""
    url = "%s/api/v1%s" % (gitea_url.rstrip("/"), path)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "token %s" % token, "Accept": "application/json",
        "Content-Type": "application/json"})
    try:
        with (opener or _opener()).open(req, timeout=30) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body) if body else None
        except ValueError:
            return error.code, {"message": body.decode("utf-8", "replace")}
    except (urllib.error.URLError, OSError) as error:
        raise ApiError("无法连接 %s：%s" % (gitea_url, getattr(error, "reason", error)))


def _msg(data):
    return (data or {}).get("message", "") if isinstance(data, dict) else str(data)


def check_user(gitea_url, token, opener=None):
    """校验地址与 bot 令牌，返回令牌所属用户名。"""
    status, data = call(gitea_url, token, "GET", "/user", opener=opener)
    if status == 401:
        raise ApiError("Bot 令牌无效或已过期")
    if status != 200:
        raise ApiError("连接失败（HTTP %s）：%s" % (status, _msg(data)))
    return data.get("login", "")


def _repo_base(owner, repo):
    return "/repos/%s/%s" % (urllib.parse.quote(owner.strip()), urllib.parse.quote(repo.strip()))


def _workflow_bytes():
    with open(WORKFLOW_SOURCE, "rb") as f:
        return f.read()


def _get_repo(gitea_url, token, owner, repo, opener):
    status, info = call(gitea_url, token, "GET", _repo_base(owner, repo), opener=opener)
    if status == 404:
        raise ApiError("找不到仓库 %s/%s，或 Bot 令牌无权访问" % (owner, repo))
    if status != 200:
        raise ApiError("读取仓库失败（HTTP %s）：%s" % (status, _msg(info)))
    return info


def _get_workflow(gitea_url, token, owner, repo, branch, opener):
    """返回 (内容 API 路径, 当前文件信息或 None)。"""
    path = "%s/contents/%s" % (_repo_base(owner, repo), urllib.parse.quote(WORKFLOW_TARGET))
    status, current = call(gitea_url, token, "GET", "%s?ref=%s" % (path, urllib.parse.quote(branch)),
                           opener=opener)
    if status == 404:
        return path, None
    if status != 200:
        raise ApiError("读取 workflow 失败（HTTP %s）：%s" % (status, _msg(current)))
    return path, current


def skip_reason(repo):
    """无法接入的仓库返回原因，否则返回空串。repo 为仓库列表 API 返回的对象。"""
    if repo.get("archived"):
        return "已归档"
    if repo.get("mirror"):
        return "镜像仓库"
    if repo.get("empty"):
        return "空仓库"
    if not (repo.get("permissions") or {}).get("push"):
        return "Bot 令牌无写权限"
    return ""


def workflow_status(gitea_url, token, owner, repo, opener=None):
    """仓库的接入状态："enabled" | "outdated"（已接入但不是当前版本）| "disabled"。"""
    _, current = _get_workflow(gitea_url, token, owner, repo["name"],
                               repo.get("default_branch") or "main", opener)
    if current is None:
        return "disabled"
    same = base64.b64decode(current.get("content") or "") == _workflow_bytes()
    return "enabled" if same else "outdated"


def deploy_workflow(gitea_url, token, owner, repo, opener=None):
    """启用：把 Windows 版 workflow 提交到仓库默认分支；内容相同则跳过。返回结果文本。"""
    info = _get_repo(gitea_url, token, owner, repo, opener)
    if not (info.get("permissions") or {}).get("push"):
        raise ApiError("Bot 令牌对 %s/%s 没有写权限，无法提交 workflow" % (owner, repo))
    branch = info.get("default_branch") or "main"
    content = _workflow_bytes()
    path, current = _get_workflow(gitea_url, token, owner, repo, branch, opener)
    payload = {"content": base64.b64encode(content).decode("ascii"), "branch": branch,
               "message": "ci: add OpenCodeReview workflow (Windows client runner)"}
    if current is not None:
        if base64.b64decode(current.get("content") or "") == content:
            return "%s/%s 的 workflow 已是最新，无需改动" % (owner, repo)
        payload["sha"] = current["sha"]
        status, data = call(gitea_url, token, "PUT", path, payload, opener=opener)
        ok, verb = status == 200, "更新"
    else:
        status, data = call(gitea_url, token, "POST", path, payload, opener=opener)
        ok, verb = status == 201, "新建"
    if not ok:
        raise ApiError("%s workflow 失败（HTTP %s）：%s" % (verb, status, _msg(data)))
    return "已在 %s/%s 的 %s 分支%s %s" % (owner, repo, branch, verb, WORKFLOW_TARGET)


def remove_workflow(gitea_url, token, owner, repo, opener=None):
    """停用：从默认分支删除 workflow 文件；本来就没有则跳过。返回结果文本。"""
    info = _get_repo(gitea_url, token, owner, repo, opener)
    branch = info.get("default_branch") or "main"
    path, current = _get_workflow(gitea_url, token, owner, repo, branch, opener)
    if current is None:
        return "%s/%s 未接入，无需停用" % (owner, repo)
    status, data = call(gitea_url, token, "DELETE", path, {
        "sha": current["sha"], "branch": branch,
        "message": "ci: remove OpenCodeReview workflow"}, opener=opener)
    if status != 200:
        raise ApiError("删除 workflow 失败（HTTP %s）：%s" % (status, _msg(data)))
    return "已从 %s/%s 的 %s 分支删除 %s" % (owner, repo, branch, WORKFLOW_TARGET)


def list_repos(gitea_url, token, owner, opener=None):
    """列出组织（或用户）下令牌可见的全部仓库。"""
    name = urllib.parse.quote(owner.strip())
    for kind in ("orgs", "users"):  # 先按组织查，不是组织再按用户查
        repos, page = [], 1
        while True:
            status, data = call(gitea_url, token, "GET", "/%s/%s/repos?page=%d&limit=50"
                                % (kind, name, page), opener=opener)
            if status == 404 and page == 1:
                break
            if status != 200:
                raise ApiError("读取 %s 的仓库列表失败（HTTP %s）：%s" % (owner, status, _msg(data)))
            repos += data
            if len(data) < 50:
                return repos
            page += 1
    raise ApiError("找不到组织或用户 %s，或 Bot 令牌无权访问" % owner)

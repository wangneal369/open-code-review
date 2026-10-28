# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""端到端测试：代替界面的①②③步，对解压出的绿色包目录完成注册、保存设置、接入仓库。

调用的正是界面按钮背后的函数，只是把路径指向绿色包，而不是源码目录。
用法：python seed.py <绿色包目录> <gitea 地址> <注册令牌文件> <bot 令牌> <owner> <repo[,repo...]> <llm 地址> [同时评审数]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settings  # noqa: E402

pkg, gitea_url, reg_file, bot_token, owner, repos, llm_url = sys.argv[1:8]
capacity = int(sys.argv[8]) if len(sys.argv) > 8 else 1
# 按冻结（绿色包）布局重设路径，须在导入 runner 之前完成
settings.BASE_DIR = pkg
settings.DATA_DIR = os.path.join(pkg, "data")
settings.SETTINGS_FILE = os.path.join(settings.DATA_DIR, "settings.json")
settings.LOG_DIR = os.path.join(settings.DATA_DIR, "logs")
settings.ACT_RUNNER = os.path.join(pkg, "bin", "act_runner.exe")
settings.OCR = os.path.join(pkg, "bin", "ocr.exe")
settings.GIT_ROOT = os.path.join(pkg, "bin", "git")
settings.GIT_CMD_DIR = os.path.join(settings.GIT_ROOT, "cmd")
import gitea_api  # noqa: E402
import runner  # noqa: E402

values = dict(settings.DEFAULTS, gitea_url=gitea_url, llm_url=llm_url, llm_model="mock",
              llm_protocol="anthropic", llm_key="mock-key", bot_token=bot_token,
              capacity=capacity)
print("① bot 用户：", gitea_api.check_user(gitea_url, bot_token))
with open(reg_file, encoding="utf-8") as f:
    ok, output = runner.register(gitea_url, f.read().strip(), capacity)
assert ok, output
print("② 已注册：", runner.registered_instance())
settings.save(values, settings.SETTINGS_FILE)
assert settings.load(settings.SETTINGS_FILE)["bot_token"] == bot_token
for repo in repos.split(","):
    print("③", gitea_api.deploy_workflow(gitea_url, bot_token, owner, repo))

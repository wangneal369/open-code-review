# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""路径解析与设置读写。

设置存于程序目录下 data/settings.json；LLM 密钥与 bot 令牌用 Windows DPAPI 按当前用户加密，
拷到别的机器或别的账号上无法解密，需要重新填写。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = getattr(sys, "frozen", False)
# 绿色版：一切相对于 exe 所在目录，整个文件夹可随意移动
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else HERE
DATA_DIR = os.path.join(BASE_DIR, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

if FROZEN:
    _BIN = os.path.join(BASE_DIR, "bin")
    ACT_RUNNER = os.path.join(_BIN, "act_runner.exe")
    OCR = os.path.join(_BIN, "ocr.exe")
    GIT_ROOT = os.path.join(_BIN, "git")
else:
    _VENDOR = os.path.join(HERE, "vendor")
    ACT_RUNNER = os.path.join(_VENDOR, "act_runner", "act_runner.exe")
    OCR = os.path.join(_VENDOR, "ocr", "pkg", "package", "bin", "opencodereview.exe")
    GIT_ROOT = os.path.join(_VENDOR, "mingit", "MinGit")
GIT_CMD_DIR = os.path.join(GIT_ROOT, "cmd")

RUNNER_LABEL = "ocr-windows"
SECRET_FIELDS = ("llm_key", "bot_token")
DEFAULTS = {
    "gitea_url": "",
    "llm_url": "",
    "llm_model": "",
    "llm_protocol": "openai",  # openai | anthropic
    "llm_key": "",
    "bot_token": "",
    "capacity": 1,
}


def tool_path_env(base_env=None):
    """runner 及其评审子进程使用的环境：内置的 git 与 exe 目录排在 PATH 最前。"""
    env = dict(os.environ if base_env is None else base_env)
    env["PATH"] = os.pathsep.join([BASE_DIR, os.path.dirname(OCR), GIT_CMD_DIR,
                                   env.get("PATH", "")])
    return env


# ---------- DPAPI ----------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(func, data):
    blob_in, _keep = _blob(data)
    blob_out = _Blob()
    # CRYPTPROTECT_UI_FORBIDDEN：服务在后台运行，不允许弹任何提示
    if not func(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise OSError(ctypes.GetLastError(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(text):
    if not text:
        return ""
    data = _crypt(ctypes.windll.crypt32.CryptProtectData, text.encode("utf-8"))
    return base64.b64encode(data).decode("ascii")


def unprotect(token):
    if not token:
        return ""
    return _crypt(ctypes.windll.crypt32.CryptUnprotectData,
                  base64.b64decode(token)).decode("utf-8")


# ---------- 读写 ----------

def load(path=SETTINGS_FILE):
    """读取设置；密文字段解密失败（换了机器/账号）时置空，由界面提示重新填写。"""
    values = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
    except FileNotFoundError:
        return values
    for key in DEFAULTS:
        if key not in stored:
            continue
        if key in SECRET_FIELDS:
            try:
                values[key] = unprotect(stored[key])
            except (OSError, ValueError):
                values[key] = ""
        else:
            values[key] = stored[key]
    return values


def save(values, path=SETTINGS_FILE):
    stored = {}
    for key in DEFAULTS:
        value = values.get(key, DEFAULTS[key])
        stored[key] = protect(value) if key in SECRET_FIELDS else value
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stored, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 原子替换，写到一半断电也不会留下损坏的设置


def missing_fields(values):
    """评审必需但未填写的设置项（中文名）。"""
    names = {"gitea_url": "Gitea 地址", "llm_url": "LLM 接口地址", "llm_model": "模型名",
             "llm_key": "LLM 密钥", "bot_token": "Bot 令牌"}
    return [label for key, label in names.items() if not str(values.get(key, "")).strip()]

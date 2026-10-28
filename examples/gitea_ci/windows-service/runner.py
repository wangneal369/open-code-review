# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""act_runner 的注册、启动、停止与看护。

runner 以 host 模式运行（标签 ocr-windows:host），评审直接在本机执行，不需要 Docker。
runner 及其派生的评审进程都放进一个 Windows Job Object：托盘进程无论正常退出、崩溃还是被结束，
系统都会连带清理它们，不会留下无人看管的后台 runner。
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import subprocess
import threading
import time

import settings

CONFIG_FILE = os.path.join(settings.DATA_DIR, "config.yaml")
RUNNER_FILE = os.path.join(settings.DATA_DIR, ".runner")
WORK_DIR = os.path.join(settings.DATA_DIR, "work")
LOG_FILE = os.path.join(settings.LOG_DIR, "runner.log")
LOG_MAX_BYTES = 5 * 1024 * 1024
CREATE_NO_WINDOW = 0x08000000
# 异常退出后的重启间隔（秒）；稳定运行超过 STABLE_SECONDS 后从头计
BACKOFF = (5, 10, 20, 40, 60)
STABLE_SECONDS = 300


# ---------- Job Object ----------

class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD),
                ("SchedulingClass", wt.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps",
                                               "ReadBytes", "WriteBytes", "OtherBytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("Basic", _BasicLimits), ("Io", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kill_on_close_job():
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = wt.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = _ExtendedLimits()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    # 9 = JobObjectExtendedLimitInformation
    if not k32.SetInformationJobObject(wt.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.GetLastError(), "SetInformationJobObject 失败")
    return job


# ---------- 配置 ----------

def config_text(capacity):
    """act_runner 配置。路径一律写绝对路径，与工作目录无关。"""
    def q(path):
        return "'%s'" % path.replace("'", "''")
    return "\n".join([
        "log:",
        "  level: info",
        "runner:",
        "  file: %s" % q(RUNNER_FILE),
        "  capacity: %d" % max(1, int(capacity)),
        "  timeout: 1h",
        "  fetch_interval: 2s",
        "  labels:",
        "    - '%s:host'" % settings.RUNNER_LABEL,
        "cache:",
        "  enabled: false",
        "host:",
        "  workdir_parent: %s" % q(WORK_DIR),
        "",
    ])


def write_config(capacity):
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(config_text(capacity))


def is_registered():
    return os.path.exists(RUNNER_FILE)


def registered_instance():
    """.runner 里记录的 Gitea 地址；地址变更后需要重新注册。"""
    try:
        with open(RUNNER_FILE, encoding="utf-8") as f:
            return json.load(f).get("address", "")
    except (OSError, ValueError):
        return ""


def register(gitea_url, token, capacity=1):
    """向 Gitea 注册本机 runner。返回 (成功与否, 输出文本)。"""
    write_config(capacity)
    if os.path.exists(RUNNER_FILE):
        os.replace(RUNNER_FILE, RUNNER_FILE + ".bak")  # 保留旧注册，失败时可恢复
    name = "%s-ocr" % socket.gethostname()
    proc = subprocess.run(
        [settings.ACT_RUNNER, "--config", CONFIG_FILE, "register", "--no-interactive",
         "--instance", gitea_url.rstrip("/"), "--token", token.strip(), "--name", name,
         "--labels", "%s:host" % settings.RUNNER_LABEL],
        cwd=settings.DATA_DIR, env=settings.tool_path_env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW, timeout=60)
    output = (proc.stdout + proc.stderr).strip()
    ok = proc.returncode == 0 and os.path.exists(RUNNER_FILE)
    if ok:
        if os.path.exists(RUNNER_FILE + ".bak"):
            os.remove(RUNNER_FILE + ".bak")
    elif os.path.exists(RUNNER_FILE + ".bak"):
        os.replace(RUNNER_FILE + ".bak", RUNNER_FILE)
    return ok, output


def _rotate_log():
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
        os.replace(LOG_FILE, LOG_FILE + ".1")


def tail_log(lines=40):
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            return f.read()[-20000:].splitlines()[-lines:]
    except OSError:
        return []


# ---------- 看护 ----------

class RunnerManager:
    """启动 / 停止 act_runner daemon，并在异常退出时按退避间隔自动重启。"""

    def __init__(self, on_change=None):
        self.on_change = on_change or (lambda state: None)
        self.state = "stopped"  # stopped | running | error
        self.detail = ""
        self._proc = None
        self._job = None
        self._want_running = False
        self._lock = threading.Lock()

    def _set(self, state, detail=""):
        self.state, self.detail = state, detail
        self.on_change(state)

    def start(self, capacity=1):
        with self._lock:
            if self._want_running:
                return
            if not is_registered():
                self._set("error", "尚未注册 runner")
                return
            self._want_running = True
        write_config(capacity)
        threading.Thread(target=self._supervise, daemon=True).start()

    def stop(self):
        with self._lock:
            self._want_running = False
            proc, job = self._proc, self._job
        if job:
            # 连同正在执行的评审进程一并结束
            ctypes.windll.kernel32.TerminateJobObject(wt.HANDLE(job), 1)
        if proc:
            proc.wait(timeout=10)
        self._set("stopped")

    def _launch(self):
        _rotate_log()
        log = open(LOG_FILE, "ab")
        proc = subprocess.Popen(
            [settings.ACT_RUNNER, "daemon", "--config", CONFIG_FILE],
            cwd=settings.DATA_DIR, env=settings.tool_path_env(), stdout=log,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        log.close()
        job = _kill_on_close_job()
        ctypes.windll.kernel32.AssignProcessToJobObject(wt.HANDLE(job), wt.HANDLE(int(proc._handle)))
        return proc, job

    def _supervise(self):
        failures = 0
        while True:
            with self._lock:
                if not self._want_running:
                    return
                try:
                    self._proc, self._job = self._launch()
                except OSError as error:
                    self._proc = self._job = None
                    self._set("error", "无法启动 runner：%s" % error)
                    return
            self._set("running")
            started = time.monotonic()
            code = self._proc.wait()
            ctypes.windll.kernel32.CloseHandle(wt.HANDLE(self._job))
            with self._lock:
                self._proc = self._job = None
                if not self._want_running:
                    return
            if time.monotonic() - started > STABLE_SECONDS:
                failures = 0
            delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
            failures += 1
            last = (tail_log(1) or [""])[-1]
            # 界面状态会被下次启动覆盖，写进日志才能事后排查
            with open(LOG_FILE, "a", encoding="utf-8") as log:
                log.write("[ocr-service] %s runner 异常退出（代码 %s），%d 秒后重启\n"
                          % (time.strftime("%Y-%m-%d %H:%M:%S"), code, delay))
            self._set("error", "runner 异常退出（代码 %s），%d 秒后重启。%s" % (code, delay, last))
            for _ in range(delay * 10):
                if not self._want_running:
                    return
                time.sleep(0.1)

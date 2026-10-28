# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""OCR 评审服务：托盘常驻 + 设置窗口（ocr-service.exe）。

启动参数 --background：开机自启时使用，配置完整则只在托盘运行，不弹窗。
"""

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
import winreg
from tkinter import messagebox, ttk

import pystray
from PIL import Image, ImageDraw

import gitea_api
import runner
import settings

APP_NAME = "OCR 评审服务"
MUTEX_NAME = "Local\\OCRReviewServiceSingleton"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "OCRReviewService"
STATE_TEXT = {"running": "运行中", "stopped": "已停止", "error": "异常"}
STATE_COLOR = {"running": "#2e9e4f", "stopped": "#8a8a8a", "error": "#d0392b"}
REPO_STATUS_TEXT = {"enabled": "✓ 已启用", "outdated": "已启用（workflow 需更新）",
                    "disabled": "未启用"}


# ---------- 开机自启 ----------

def autostart_command():
    return '"%s" --background' % sys.executable


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_VALUE)[0] == autostart_command()
    except OSError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def tray_image(state):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=STATE_COLOR[state])
    draw.text((22, 18), "R", fill="white")
    return img


class App:
    def __init__(self, background):
        self.events = queue.Queue()
        self.values = settings.load()
        self.manager = runner.RunnerManager(on_change=lambda s: self.events.put(self.refresh_state))
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.minsize(640, 760)
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)  # 关窗只隐藏到托盘
        self.vars = {}
        self._build()
        self.icon = pystray.Icon("ocr-review-service", tray_image("stopped"), APP_NAME, self._menu())
        self.icon.run_detached()
        self.root.after(100, self._drain)
        self.root.after(1000, self._refresh_log)

        ready = not settings.missing_fields(self.values) and runner.is_registered()
        if ready:
            self.manager.start(self.values["capacity"])
        if not ready or not background:
            self.show()
        else:
            self.root.withdraw()
        self.refresh_state()

    # ---------- 布局 ----------

    def _row(self, parent, row, key, label, secret=False, value=""):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        var = tk.StringVar(value=str(value))
        ttk.Entry(parent, textvariable=var, show="*" if secret else "").grid(
            row=row, column=1, sticky="ew", pady=2)
        parent.columnconfigure(1, weight=1)
        self.vars[key] = var

    def _build(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        self.status = ttk.Label(outer, text="", font=("", 10, "bold"))
        self.status.pack(fill="x")

        box = ttk.LabelFrame(outer, text="① 连接 Gitea 并注册本机 runner", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self._row(box, 0, "gitea_url", "Gitea 地址", value=self.values["gitea_url"] or "http://")
        self._row(box, 1, "reg_token", "Runner 注册令牌", secret=True)
        ttk.Label(box, text="注册令牌在 Gitea 的 仓库/组织设置 → Actions → Runners → 创建 Runner 中获取",
                  foreground="#666").grid(row=2, column=1, sticky="w")
        self.reg_label = ttk.Label(box, text="")
        self.reg_label.grid(row=3, column=1, sticky="w")
        ttk.Button(box, text="注册", command=self.on_register).grid(row=3, column=1, sticky="e")

        box = ttk.LabelFrame(outer, text="② 评审设置（保存在本机，密钥加密存储）", padding=8)
        box.pack(fill="x", pady=(8, 0))
        v = self.values
        self._row(box, 0, "llm_url", "LLM 接口地址", value=v["llm_url"])
        self._row(box, 1, "llm_model", "模型名", value=v["llm_model"])
        self._row(box, 2, "llm_key", "LLM 密钥", secret=True, value=v["llm_key"])
        ttk.Label(box, text="接口格式").grid(row=3, column=0, sticky="w", pady=2)
        self.vars["llm_protocol"] = tk.StringVar(value=v["llm_protocol"])
        ttk.Combobox(box, textvariable=self.vars["llm_protocol"], state="readonly",
                     values=("openai", "anthropic"), width=12).grid(row=3, column=1, sticky="w")
        self._row(box, 4, "bot_token", "Bot 令牌", secret=True, value=v["bot_token"])
        ttk.Label(box, text="发评论的账号的访问令牌，需要 write:repository、write:issue、"
                       "read:organization、read:user（加载仓库列表用）",
                  foreground="#666").grid(row=5, column=1, sticky="w")
        ttk.Label(box, text="同时评审数").grid(row=6, column=0, sticky="w", pady=2)
        self.vars["capacity"] = tk.StringVar(value=str(v["capacity"]))
        ttk.Spinbox(box, from_=1, to=4, textvariable=self.vars["capacity"], width=5,
                    state="readonly").grid(row=6, column=1, sticky="w")
        ttk.Button(box, text="保存", command=self.on_save).grid(row=6, column=1, sticky="e")

        box = ttk.LabelFrame(outer, text="③ 仓库接入（启用后，该仓库的 PR 会自动评审）", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self._row(box, 0, "owner", "组织/用户")
        ttk.Button(box, text="加载仓库", command=self.on_load_repos).grid(row=0, column=2, padx=(6, 0))
        self.tree = ttk.Treeview(box, columns=("status",), selectmode="extended", height=6)
        self.tree.heading("#0", text="仓库")
        self.tree.heading("status", text="状态")
        self.tree.column("status", width=220, stretch=False)
        self.tree.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.tree.bind("<Double-1>", lambda _: self.on_toggle())
        bar = ttk.Frame(box)
        bar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(bar, text="双击切换启用状态，可按住 Ctrl/Shift 多选",
                  foreground="#666").pack(side="left")
        ttk.Button(bar, text="停用所选", command=lambda: self.on_apply(False)).pack(side="right")
        ttk.Button(bar, text="启用所选", command=lambda: self.on_apply(True)).pack(side="right", padx=4)
        ttk.Button(bar, text="全部启用", command=self.on_enable_all).pack(side="right")
        self.repo_owner = ""
        self.repos = {}  # 仓库名 → 列表 API 返回的仓库对象
        self.repo_status = {}  # 仓库名 → "enabled" | "outdated" | "disabled" | 不可用原因

        box = ttk.LabelFrame(outer, text="runner 日志", padding=4)
        box.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(box, height=10, state="disabled", wrap="none")
        self.log.pack(fill="both", expand=True)

    def _menu(self):
        item = pystray.MenuItem
        later = self.events.put
        return pystray.Menu(
            item(lambda _: "%s - %s" % (APP_NAME, STATE_TEXT[self.manager.state]), None,
                 enabled=False),
            item("打开主界面", lambda: later(self.show), default=True),
            item("启动", lambda: later(self.on_start)),
            item("停止", lambda: later(self.manager.stop)),
            item("重启", lambda: later(self.on_restart)),
            item("打开日志目录", lambda: os.startfile(settings.LOG_DIR)
                 if os.path.isdir(settings.LOG_DIR) else None),
            item("开机自启", lambda: later(self.toggle_autostart),
                 checked=lambda _: autostart_enabled(), enabled=settings.FROZEN),
            item("退出", lambda: later(self.quit)),
        )

    # ---------- 事件处理 ----------

    def _drain(self):
        try:
            while True:
                self.events.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def background(self, func, done):
        """网络与子进程调用放后台线程，结果回到主线程处理。"""
        def work():
            try:
                result, error = func(), None
            except Exception as exc:  # 任何失败都要显示给用户
                result, error = None, exc
            self.events.put(lambda: done(result, error))
        threading.Thread(target=work, daemon=True).start()

    def refresh_state(self):
        state = self.manager.state
        text = "状态：%s" % STATE_TEXT[state]
        if self.manager.detail:
            text += "  —  %s" % self.manager.detail
        missing = settings.missing_fields(self.values)
        if missing:
            text += "  —  待填写：%s" % "、".join(missing)
        self.status.config(text=text, foreground=STATE_COLOR[state])
        if runner.is_registered():
            self.reg_label.config(text="✓ 已注册到 %s" % runner.registered_instance(),
                                  foreground=STATE_COLOR["running"])
        else:
            self.reg_label.config(text="尚未注册", foreground=STATE_COLOR["error"])
        self.icon.icon = tray_image(state)
        self.icon.title = "%s - %s" % (APP_NAME, STATE_TEXT[state])
        self.icon.update_menu()

    def _refresh_log(self):
        lines = runner.tail_log(200)
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(lines))
        self.log.see("end")
        self.log.config(state="disabled")
        self.root.after(2000, self._refresh_log)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def form_values(self):
        values = dict(self.values)
        for key in ("gitea_url", "llm_url", "llm_model", "llm_key", "llm_protocol", "bot_token"):
            values[key] = self.vars[key].get().strip()
        values["capacity"] = int(self.vars["capacity"].get() or 1)
        return values

    def on_register(self):
        url = self.vars["gitea_url"].get().strip().rstrip("/")
        token = self.vars["reg_token"].get().strip()
        if not url or url == "http:" or not token:
            messagebox.showerror(APP_NAME, "请填写 Gitea 地址和 Runner 注册令牌")
            return
        if runner.is_registered() and not messagebox.askyesno(
                APP_NAME, "本机已注册到 %s，重新注册会替换原有注册。继续？" % runner.registered_instance()):
            return
        self.manager.stop()

        def done(result, error):
            ok, output = result if result else (False, str(error))
            if not ok:
                messagebox.showerror(APP_NAME, "注册失败：\n%s" % output[-800:])
                return
            self.vars["reg_token"].set("")  # 注册令牌一次性使用，不保存
            self.values["gitea_url"] = url
            settings.save(self.values)
            messagebox.showinfo(APP_NAME, "注册成功")
            self.on_start()
        self.background(lambda: runner.register(url, token, self.values["capacity"]), done)

    def on_save(self):
        values = self.form_values()

        def done(login, error):
            if error:
                messagebox.showerror(APP_NAME, "未保存：%s" % error)
                return
            restart = values["capacity"] != self.values["capacity"]
            self.values = values
            settings.save(values)
            messagebox.showinfo(APP_NAME, "已保存。评审将以 %s 的身份发表评论。" % login)
            if restart and self.manager.state == "running":
                self.on_restart()
            elif self.manager.state != "running":
                self.on_start()
            self.refresh_state()
        # 保存前先验证 bot 令牌，避免评审跑完才发现无法回帖
        self.background(lambda: gitea_api.check_user(values["gitea_url"], values["bot_token"]), done)

    # ---------- 仓库接入 ----------

    def on_load_repos(self):
        owner = self.vars["owner"].get().strip()
        if not owner:
            messagebox.showerror(APP_NAME, "请填写组织或用户名")
            return
        if settings.missing_fields(self.values):
            messagebox.showerror(APP_NAME, "请先完成并保存第②步的评审设置")
            return
        url, token = self.values["gitea_url"], self.values["bot_token"]

        def load():
            repos = gitea_api.list_repos(url, token, owner)
            status = {}
            for repo in repos:
                try:
                    status[repo["name"]] = (gitea_api.skip_reason(repo)
                                            or gitea_api.workflow_status(url, token, owner, repo))
                except gitea_api.ApiError as error:  # 单个仓库读取失败不影响列表
                    status[repo["name"]] = "读取失败：%s" % error
            return owner, repos, status

        def done(result, error):
            if error:
                messagebox.showerror(APP_NAME, str(error))
                return
            self.repo_owner, repos, self.repo_status = result
            self.repos = {r["name"]: r for r in repos}
            self.tree.delete(*self.tree.get_children())
            for name in sorted(self.repos, key=str.lower):
                self.tree.insert("", "end", iid=name, text=name,
                                 values=(REPO_STATUS_TEXT.get(self.repo_status[name],
                                                              "不可用：%s" % self.repo_status[name]),))
        self.background(load, done)

    def _usable(self, name):
        return self.repo_status.get(name) in REPO_STATUS_TEXT

    def on_toggle(self):
        selected = self.tree.selection()
        if len(selected) == 1:
            self.on_apply(self.repo_status.get(selected[0]) == "disabled")

    def on_enable_all(self):
        names = [n for n in self.repos if self.repo_status[n] in ("disabled", "outdated")]
        if not names:
            messagebox.showinfo(APP_NAME, "没有需要启用的仓库" if self.repos else "请先加载仓库")
            return
        self._apply(True, names)

    def on_apply(self, enable):
        names = [n for n in self.tree.selection() if self._usable(n)]
        if not names:
            messagebox.showinfo(APP_NAME, "请先在列表中选择可用的仓库")
            return
        self._apply(enable, names)

    def _apply(self, enable, names):
        verb = "启用" if enable else "停用"
        action = "提交" if enable else "删除"
        if not messagebox.askyesno(APP_NAME, "将在以下 %d 个仓库%s自动评审（在默认分支%s %s）：\n\n%s\n\n继续？"
                                   % (len(names), verb, action, gitea_api.WORKFLOW_TARGET,
                                      "\n".join(sorted(names)[:15])
                                      + ("\n……" if len(names) > 15 else ""))):
            return
        url, token, owner = self.values["gitea_url"], self.values["bot_token"], self.repo_owner
        func = gitea_api.deploy_workflow if enable else gitea_api.remove_workflow

        def work():
            failed = []
            for name in names:
                try:
                    func(url, token, owner, name)
                except gitea_api.ApiError as error:  # 继续处理其余仓库
                    failed.append("%s：%s" % (name, error))
            return failed

        def done(failed, error):
            failed = failed if error is None else [str(error)]
            text = "已%s %d 个仓库" % (verb, len(names) - len(failed))
            if failed:
                messagebox.showwarning(APP_NAME, "%s，%d 个失败：\n\n%s"
                                       % (text, len(failed), "\n".join(failed[:10])))
            else:
                messagebox.showinfo(APP_NAME, text)
            self.on_load_repos()  # 以仓库实际状态为准刷新
        self.background(work, done)

    def on_start(self):
        missing = settings.missing_fields(self.values)
        if missing:
            self.show()
            messagebox.showerror(APP_NAME, "请先填写：%s" % "、".join(missing))
            return
        self.manager.start(self.values["capacity"])

    def on_restart(self):
        self.manager.stop()
        self.on_start()

    def toggle_autostart(self):
        set_autostart(not autostart_enabled())
        self.icon.update_menu()

    def quit(self):
        self.manager.stop()
        self.icon.stop()
        self.root.destroy()


def main():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS：已有实例在运行
        hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        return
    App(background="--background" in sys.argv)
    tk.mainloop()


if __name__ == "__main__":
    main()

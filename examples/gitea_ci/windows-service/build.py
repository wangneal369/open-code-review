# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 alibaba/open-code-review Contributors

"""打包绿色版：python build.py → dist/ocr-review-service/ 与 dist/ocr-review-service.zip

产物结构：
  ocr-service.exe   托盘程序（无控制台）
  ocr-review.exe    评审命令（控制台，由 runner 调用）
  _internal/        两个 exe 共用的 Python 运行时
  bin/              act_runner.exe、ocr.exe、git/（MinGit）

两个 exe 必须分开：托盘程序若是控制台程序会弹黑窗；评审命令若不是控制台程序，runner 拿不到输出和退出码。
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE_DIR = os.path.normpath(os.path.join(HERE, ".."))  # ocr-review-windows.yml、post_review.py
VENDOR = os.path.join(HERE, "vendor")
BUILD = os.path.join(HERE, "build")
DIST = os.path.join(HERE, "dist")
NAME = "ocr-review-service"

SPEC = r'''
import os, sys
here, example = {here!r}, {example!r}
# Anaconda 把扩展模块依赖的 DLL 放在 Library\bin，PyInstaller 收集不到；开发机上它在 PATH 里，
# 只有换到别的环境才暴露（如 _ctypes 缺 ffi.dll，程序直接启动失败）
conda_bin = os.path.join(sys.base_prefix, "Library", "bin")
tk_dlls = [(os.path.join(conda_bin, d), ".") for d in (
    "tcl86t.dll", "tk86t.dll", "zlib.dll", "ffi.dll", "libssl-3-x64.dll", "libcrypto-3-x64.dll",
    "libbz2.dll", "liblzma.dll", "libexpat.dll") if os.path.exists(os.path.join(conda_bin, d))]

# Pillow 会顺带拉进 numpy（约 27 MB），本程序用不到
EXCLUDES = ["numpy"]
app = Analysis([os.path.join(here, "app.py")], pathex=[here, example], binaries=tk_dlls,
               datas=[(os.path.join(example, "ocr-review-windows.yml"), "payload")],
               hiddenimports=["pystray._win32"], excludes=EXCLUDES)
rev = Analysis([os.path.join(here, "review.py")], pathex=[here, example],
               hiddenimports=["post_review"], excludes=EXCLUDES)
app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="ocr-service",
              console=False)
rev_exe = EXE(PYZ(rev.pure), rev.scripts, [], exclude_binaries=True, name="ocr-review",
              console=True)
COLLECT(app_exe, app.binaries, app.datas, rev_exe, rev.binaries, rev.datas, name={name!r})
'''


def main():
    os.makedirs(BUILD, exist_ok=True)
    spec = os.path.join(BUILD, NAME + ".spec")
    with open(spec, "w", encoding="utf-8") as f:
        f.write(SPEC.format(here=HERE, example=EXAMPLE_DIR, name=NAME))
    code = subprocess.call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                            "--distpath", DIST, "--workpath", BUILD, spec])
    if code:
        return code

    out = os.path.join(DIST, NAME)
    bin_dir = os.path.join(out, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    shutil.copy2(os.path.join(VENDOR, "act_runner", "act_runner.exe"), bin_dir)
    shutil.copy2(os.path.join(VENDOR, "ocr", "pkg", "package", "bin", "opencodereview.exe"),
                 os.path.join(bin_dir, "ocr.exe"))
    git_dir = os.path.join(bin_dir, "git")
    if os.path.isdir(git_dir):
        shutil.rmtree(git_dir)  # 只清理本脚本自己生成的产物目录
    shutil.copytree(os.path.join(VENDOR, "mingit", "MinGit"), git_dir)
    shutil.copy2(os.path.join(HERE, "使用说明.txt"), out)

    # 自检：PATH 只留系统目录，模拟没装 Python 的机器；未配置时评审命令应以 2 正常退出
    clean_env = dict(os.environ, PATH=os.path.join(os.environ["SystemRoot"], "System32"))
    proc = subprocess.run([os.path.join(out, "ocr-review.exe")], env=clean_env, cwd=out,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 2:
        print("自检失败（退出码 %d）：\n%s" % (proc.returncode, proc.stdout + proc.stderr))
        return 1

    archive = shutil.make_archive(os.path.join(DIST, NAME), "zip", DIST, NAME)
    print("打包完成：%s\n压缩包：%s（%.1f MB）" % (out, archive, os.path.getsize(archive) / 2**20))
    return 0


if __name__ == "__main__":
    sys.exit(main())

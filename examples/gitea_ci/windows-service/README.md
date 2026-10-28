# OCR review service for Windows clients

A tray app that turns a Windows machine into a Gitea Actions runner for
[`ocr-review-windows.yml`](../ocr-review-windows.yml). It bundles act_runner,
MinGit and ocr into a portable folder, so the machine needs neither Docker
nor Python nor Git. End-user instructions (Chinese) are in
[使用说明.txt](使用说明.txt), which ships inside the package.

| File | Role |
|---|---|
| `app.py` | Tray app and settings window (`ocr-service.exe`): register the runner, store LLM settings and the bot token, enable/disable repositories |
| `runner.py` | Registers, starts and supervises `act_runner daemon` (host mode, label `ocr-windows`) inside a kill-on-close Job Object |
| `review.py` | The command the workflow runs (`ocr-review.exe`): fetch the PR, run ocr, post via [`post_review.py`](../post_review.py) |
| `gitea_api.py` | Gitea API calls: check the bot token, list repositories, enable (commit) / disable (delete) the workflow |
| `settings.py` | Paths and settings; the LLM key and bot token are encrypted with Windows DPAPI for the current user |
| `build.py` | Builds `dist/ocr-review-service/` and `dist/ocr-review-service.zip` with PyInstaller |
| `e2e/` | Manual end-to-end helpers: a fake Anthropic-protocol LLM and a script that seeds a package without the GUI |

## Build

Requirements: Windows, Python 3.10 with `pyinstaller`, `pystray` and `Pillow`.
The build was developed against Anaconda; `build.py` bundles the extension-module
DLLs Anaconda keeps in `Library\bin`, then runs `ocr-review.exe` with only
`System32` on `PATH` and fails if the package cannot start on a clean machine.

Put the third-party binaries under `vendor/` (git-ignored):

| Path | Source | SHA-256 |
|---|---|---|
| `vendor/act_runner/act_runner.exe` | `https://dl.gitea.com/act_runner/0.6.1/act_runner-0.6.1-windows-amd64.exe` | `f9eee934cfd03d14caabde65603881b9e59f732d89c934aa3e4333ba644a8270` |
| `vendor/mingit/MinGit/` | extract `https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/MinGit-2.56.0.4-64-bit.zip` | `e3bf78351fe777fba47ddee5950082f8a98bb0f02604fdc7ba3ee2cb68ffa877` (zip) |
| `vendor/ocr/pkg/package/bin/opencodereview.exe` | extract `https://registry.npmjs.org/@alibaba-group/ocr-win32-x64/-/ocr-win32-x64-1.13.3.tgz` into `vendor/ocr/pkg` | `7723b735428bc31a07504ea8faeb45da7387a59938ce9a54f8c8d254e5949825` (tgz) |

Then:

```bat
python -m pytest test_service.py
python build.py
```

## Security

Jobs run directly on the host without isolation. Anyone who can open a pull
request in an enabled repository can change the workflow and run commands on
the machine, so register the runner only for trusted repositories or
organizations.

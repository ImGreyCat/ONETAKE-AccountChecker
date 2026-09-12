#!/usr/bin/env python3
"""
Сборка ONETAKE Account Checker в один .exe (Windows) или бинарник (Linux).

Использование:
    python build.py

PyInstaller не умеет кросс-компиляцию: под Windows получится .exe,
под Linux — ELF-бинарник. Для сборки под обе ОС сразу используй
GitHub Actions (см. .github/workflows/build.yml).
"""

import os
import sys
import shutil
import subprocess

APP_NAME = "ONETAKE-AccountChecker"
ENTRY    = "checker.py"


def ensure_pyinstaller(): # проверка на наличие pyinstaller
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[*] PyInstaller не найден. Устанавливаю...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--upgrade", "pyinstaller"])


def build():
    ensure_pyinstaller()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--clean",
        "--noconfirm",
        "--name", APP_NAME,
        ENTRY,
    ]

    # На Windows хотим .exe с иконкой, на Linux — обычный ELF.
    if sys.platform == "win32":
        icon = os.path.join("assets", "icon.ico")
        if os.path.exists(icon):
            cmd += ["--icon", icon]
    else:
        icon = os.path.join("assets", "icon.png")
        if os.path.exists(icon):
            cmd += ["--icon", icon]

    print("[*] Собираю:", " ".join(cmd))
    subprocess.check_call(cmd)

    # Итоговый файл
    if sys.platform == "win32":
        out = os.path.join("dist", APP_NAME + ".exe")
    else:
        out = os.path.join("dist", APP_NAME)

    if os.path.exists(out):
        size_mb = os.path.getsize(out) / (1024 * 1024)
        print(f"\n[✓] Готово: {out}  ({size_mb:.1f} MB)")
    else:
        print("\n[!] Что-то пошло не так — файл не найден в dist/")
        sys.exit(1)


if __name__ == "__main__":
    # Чистим прошлые сборки, чтобы не путаться
    for d in ("build", "dist"):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    build()
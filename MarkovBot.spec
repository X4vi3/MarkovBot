# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для сборки MarkovBot в один .exe.

Сборка:
    pyinstaller MarkovBot.spec --noconfirm

Результат: dist/MarkovBot.exe
"""
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Словари pymorphy3 — критично для морфологии
pymorphy_data = collect_data_files("pymorphy3_dicts_ru")
pymorphy_hidden = collect_submodules("pymorphy3_dicts_ru")

# httpx[socks] требует socksio
socks_hidden = ["socksio"]

datas = [
    # Стартовый корпус
    ("phrases_telegram.csv", "."),
    # Голосовое сообщение для команды (опционально)
    # ("jaba.ogg", "."),
] + pymorphy_data

hidden = [
    "pymorphy3",
    "pymorphy3.units",
    "pymorphy3.units.by_lookup",
    "pymorphy3.units.by_analogy",
    "pymorphy3.units.by_hyphen",
    "pymorphy3.units.by_shape",
    "pymorphy3.units.unkn",
    "telegram.ext",
    "httpx",
    "proxy_detect",   # авто-детект прокси при первом запуске .exe
] + pymorphy_hidden + socks_hidden


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",       # бот не использует
        "test",
        "unittest",
        "pydoc",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MarkovBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

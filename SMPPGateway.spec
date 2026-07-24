# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['main.py'],
    pathex=['src', '.'],
    binaries=[],
    datas=[('src/gui/app_icon.ico', 'gui')],
    hiddenimports=['serial.tools.list_ports', 'openpyxl', 'audio', 'miniaudio',
                   'paramiko', 'tunnel',
                   'modem_manager', 'call_handler', 'sms_handler',
                   'smpp_server', 'database'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SMPPGateway',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='src/gui/app_icon.ico',
)

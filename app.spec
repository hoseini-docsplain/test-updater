# build.spec

block_cipher = None
from PyInstaller.utils.hooks import collect_data_files

customtkinter_datas = collect_data_files('customtkinter')

# ---- app.py ----
a_app = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=customtkinter_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz_app = PYZ(a_app.pure)

exe_app = EXE(
    pyz_app,
    a_app.scripts,
    [],
    exclude_binaries=True,
    name='app',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---- updater.py ----
a_updater = Analysis(
    ['updater.py'],
    pathex=[],
    binaries=[],
    datas=customtkinter_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz_updater = PYZ(a_updater.pure)

exe_updater = EXE(
    pyz_updater,
    a_updater.scripts,
    [],
    exclude_binaries=True,
    name='updater',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll_bundle = COLLECT(
    exe_app,
    exe_updater,
    a_app.binaries,
    a_app.datas,
    a_updater.binaries,
    a_updater.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='bundle',
)

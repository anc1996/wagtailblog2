# PyInstaller spec for the Windows Markdown import wizard.
from pathlib import Path

ROOT = Path(SPECPATH)
hiddenimports = [
    "markdown_import.client",
    "blog.services.markdown_import_parser",
    "blog.services.markdown_import_paths",
    "blog.services.markdown_import_remote",
]

a = Analysis(
    [str(ROOT / "markdown_import_gui.py")],
    pathex=[str(ROOT), str(ROOT.parent / "wagtailblog3" / "apps")],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["django", "wagtail", "pymongo"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="markdown-importer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)

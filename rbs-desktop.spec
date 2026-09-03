# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the self-contained RBS Desktop application.

Build this spec independently on each target operating system. The output is an
``RBS Desktop.app`` on macOS and an ``RBS Desktop`` onedir application
elsewhere. Onedir avoids the extraction delay and signing complications of a
onefile native app.
"""

import sys
import runpy
from pathlib import Path

project_root = Path(SPECPATH)
app_version = runpy.run_path(
    str(project_root / "src" / "rbs" / "__init__.py")
)["__version__"]

third_party_license_bundle = (
    project_root / "build" / "licenses" / "THIRD_PARTY_LICENSES.txt"
)
if not third_party_license_bundle.is_file():
    raise FileNotFoundError(
        "third-party notices have not been generated; run tools/build_desktop.sh "
        "or python tools/build_third_party_licenses.py first"
    )

rbs_datas = [
    (str(project_root / "src" / "rbs" / "ui" / "static"), "rbs/ui/static"),
    (str(project_root / "data" / "catalog.json"), "rbs/data"),
    (str(project_root / "CHANGELOG.md"), "rbs"),
    (str(project_root / "LICENSE"), "licenses"),
    (str(third_party_license_bundle), "rbs/legal"),
]

desktop_analysis = Analysis(
    [str(project_root / "packaging" / "rbs_desktop.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=rbs_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "rbs.cloud",
        "rbs.solver.core",
        "rbs.solver.process",
        "rbs.solver.service",
        "_pytest",
        "cryptography",
        "jwt",
        "numpy",
        "ortools",
        "pandas",
        "pypdf",
        "pytest",
        "ruff",
    ],
    noarchive=False,
    optimize=1,
)

solver_analysis = Analysis(
    [str(project_root / "packaging" / "rbs_solver.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "rbs.cloud",
        "rbs.desktop",
        "rbs.store",
        "rbs.ui",
        "_pytest",
        "cryptography",
        "jwt",
        "nicegui",
        "pypdf",
        "pytest",
        "reportlab",
        "ruff",
        "webview",
    ],
    noarchive=False,
    optimize=1,
)

desktop_pyz = PYZ(desktop_analysis.pure)
solver_pyz = PYZ(solver_analysis.pure)

desktop_exe = EXE(
    desktop_pyz,
    desktop_analysis.scripts,
    [],
    exclude_binaries=True,
    name="RBS Desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    # NiceGUI native mode starts its webview with multiprocessing spawn.
    # PyInstaller's Apple-event argv emulation runs in the bootloader before
    # freeze_support() can divert those children and crashes macOS Dock
    # registration. Documents remain available through the native Open dialog
    # and through an explicit command-line path.
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

solver_exe = EXE(
    solver_pyz,
    solver_analysis.scripts,
    [],
    exclude_binaries=True,
    name="rbs-solver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# Both executables share this one-folder collection. MERGE is deliberately not
# used: its cross-executable dependency references have one-file semantics and
# cannot resolve shared macOS libraries stored in Contents/Frameworks.
application = COLLECT(
    desktop_exe,
    solver_exe,
    desktop_analysis.binaries,
    desktop_analysis.datas,
    solver_analysis.binaries,
    solver_analysis.datas,
    strip=False,
    upx=False,
    name="RBS Desktop",
)

if sys.platform == "darwin":
    app = BUNDLE(
        application,
        name="RBS Desktop.app",
        icon=str(project_root / "packaging" / "assets" / "rbs.icns"),
        bundle_identifier="com.rbs.desktop",
        info_plist={
            "CFBundleName": "RBS Desktop",
            "CFBundleDisplayName": "RBS Desktop",
            # COLLECT also contains the console-based solver helper, so
            # PyInstaller otherwise infers that the whole app is background
            # only from the last executable it sees.
            "LSBackgroundOnly": False,
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": app_version,
            "CFBundleVersion": app_version,
            "UTExportedTypeDeclarations": [
                {
                    "UTTypeIdentifier": "com.rbs.rbsc",
                    "UTTypeDescription": "RBS Workspace",
                    "UTTypeConformsTo": ["public.json", "public.data"],
                    "UTTypeTagSpecification": {
                        "public.filename-extension": ["rbsc"],
                        "public.mime-type": "application/vnd.rbs.workspace+json",
                    },
                }
            ],
        },
    )

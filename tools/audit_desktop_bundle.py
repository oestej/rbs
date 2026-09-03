#!/usr/bin/env python3
"""Verify that PyInstaller did not collect undeclared desktop dependencies."""

from rbs.desktop.bundle_audit import main

if __name__ == "__main__":
    raise SystemExit(main())

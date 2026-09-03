#!/usr/bin/env python3
"""Generate the legal-notices artifact consumed by the desktop build."""

from rbs.desktop.license_bundle import main

if __name__ == "__main__":
    raise SystemExit(main())

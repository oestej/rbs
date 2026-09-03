"""macOS-only desktop integration.

Nothing outside this package may import AppKit, Foundation, ``objc`` or
PyObjCTools. Even this package keeps those imports inside callback functions so
the platform-neutral desktop composition root remains safe to import anywhere.
"""


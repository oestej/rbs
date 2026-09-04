"""Finder layout for the shipped macOS disk image.

``tools/package_dmg.sh`` invokes dmgbuild with this file. The result is the
usual drag-to-Applications installer: the application on the left, an
Applications symlink on the right, and dmgbuild's built-in arrow between them.

dmgbuild execs this file with ``-D`` values in ``defines``.
"""

from __future__ import annotations

import os.path

# dmgbuild injects ``defines`` via exec; it is not a real module global.
_defines = globals()["defines"]
application = _defines["app"]
app_name = os.path.basename(application)

format = "UDZO"
filesystem = "HFS+"

files = [application]
symlinks = {"Applications": "/Applications"}
hide_extensions = [app_name]
# __file__ is not set under exec. The packager passes the volume icon as -D icon=.
icon = _defines.get("icon") or None

# builtin-arrow is a 640x240 retina TIFF. The window is 40pt taller so Finder's
# title bar does not clip the background; icon coordinates match dmgbuild's
# example layout for that image.
background = "builtin-arrow"
window_rect = ((200, 120), (640, 280))
icon_size = 128
text_size = 16
icon_locations = {
    app_name: (140, 120),
    "Applications": (500, 120),
}

default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
arrange_by = None
show_icon_preview = False
include_icon_view_settings = True

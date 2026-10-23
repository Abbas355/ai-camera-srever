"""Compatibility entry for the HTTP API.

Implementation: v380.api.http.server
Package layout: v380.api (auth, cameras, live, recordings, tables, controllers, routes)
"""

from __future__ import annotations

from v380.api.http.server import (  # noqa: F401
    HUB,
    MODS,
    PORT,
    STORE,
    Handler,
    Session,
    Hub,
    auth_user,
    cam_json,
    camera_from_body,
    check_user,
    clip_index,
    ensure_admin,
    local_ips,
    main,
    new_token,
    ping_camera,
)

if __name__ == "__main__":
    main()

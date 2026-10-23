"""Cameras controller — same functions the HTTP handler already uses."""

from v380.api.cameras.service import (  # noqa: F401
    cam_json,
    camera_from_body,
    ping_camera,
)

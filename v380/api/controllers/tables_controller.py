"""Tables controller — same store/aim helpers the HTTP handler already uses."""

from v380.api.tables.service import (  # noqa: F401
    TableStore,
    goto_table_aim,
    save_table_panorama_point,
)

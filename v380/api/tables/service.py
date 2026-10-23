"""Restaurant tables — store and PTZ aim helpers.

HTTP routes stay on the main handler; domain logic lives in v380.tables.
"""

from v380.tables.ptz_presets import goto_table_aim, save_table_panorama_point  # noqa: F401
from v380.tables.store import TableStore  # noqa: F401
from v380.tables.types import Table, TableAim  # noqa: F401

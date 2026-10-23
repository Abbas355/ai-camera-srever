"""Restaurant floor tables → PTZ camera presets.

Package layout
--------------
v380/tables/
  types.py       Table / Aim / PtzStep dataclasses
  store.py       JSON persist under data/tables.json
  ptz_presets.py save / goto aim (ONVIF + relative recipe)

Public: TableStore, Table, PtzStep, save_table_aim, goto_table_aim
"""

from v380.tables.ptz_presets import goto_table_aim, save_table_aim, save_table_panorama_point
from v380.tables.store import TableStore
from v380.tables.types import PtzStep, Table, TableAim

__all__ = [
    "PtzStep",
    "Table",
    "TableAim",
    "TableStore",
    "goto_table_aim",
    "save_table_aim",
    "save_table_panorama_point",
]

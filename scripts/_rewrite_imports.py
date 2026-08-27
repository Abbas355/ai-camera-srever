from pathlib import Path

root = Path(__file__).resolve().parent.parent
repls = [
    ("from v380_client import", "from v380.client.v380_client import"),
    ("from camera_store import", "from v380.store.camera_store import"),
    ("from db import", "from v380.store.db import"),
    ("from secretbox import", "from v380.store.secretbox import"),
    ("from extras import", "from v380.client.extras import"),
    ("from api_client import", "from v380.client.api_client import"),
    ("from preview import", "from v380.client.preview import"),
    ("from auto_record import", "from v380.record.auto_record import"),
    ("from theme import", "from v380.ui.theme import"),
    ("from home import", "from v380.ui.home import"),
    ("from live_view import", "from v380.ui.live_view import"),
    ("from clips import", "from v380.ui.clips import"),
    ("from login import", "from v380.ui.login import"),
    ("from studio_server import", "from v380.server.studio_server import"),
]

folders = [root / "v380", root / "tools", root / "tests"]
for folder in folders:
    for path in folder.rglob("*.py"):
        if path.name.startswith("_rewrite"):
            continue
        text = path.read_text(encoding="utf-8")
        orig = text
        for a, b in repls:
            text = text.replace(a, b)
        if text != orig:
            path.write_text(text, encoding="utf-8")
            print("updated", path.relative_to(root))

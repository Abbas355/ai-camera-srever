import os
import time

import paramiko

pw = os.environ["V380_SSH_PASS"]
remote = "/home/amf/v380-studio"
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.1.68", username="amf", password=pw, timeout=20)
sftp = c.open_sftp()
sftp.put("auto_record.py", f"{remote}/auto_record.py")
sftp.close()


def run(cmd: str, timeout: int = 60) -> str:
    print(">>>", cmd[:160])
    _i, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    print(out)
    if err.strip():
        print("ERR", err)
    return out


run("systemctl --user stop v380-record")
time.sleep(1)
run(
    r"""
python3 - <<'PY'
from datetime import datetime, timedelta
from pathlib import Path
import re

root = Path("/home/amf/v380-studio/recordings")
mark = root / ".tz_pkt_plus5"
if mark.is_file():
    print("already shifted")
    raise SystemExit(0)
if not root.is_dir():
    print("no recordings")
    raise SystemExit(0)
shift = timedelta(hours=5)
exts = {".h264", ".h265", ".mp4"}
files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
moved = 0
for p in files:
    rel = p.relative_to(root)
    parts = rel.parts
    if len(parts) < 3:
        continue
    camera, day = parts[0], parts[1]
    m = re.match(r"^(\d{2})-(\d{2})-(\d{2})$", p.stem)
    if not m:
        continue
    try:
        dt = datetime.strptime(f"{day} {m.group(1)}:{m.group(2)}:{m.group(3)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        continue
    nt = dt + shift
    new_day = nt.strftime("%Y-%m-%d")
    new_hour = nt.strftime("%H")
    new_name = nt.strftime("%H-%M-%S") + p.suffix
    if len(parts) == 4:
        dest = root / camera / new_day / new_hour / new_name
    else:
        dest = root / camera / new_day / new_name
    if dest.resolve() == p.resolve():
        continue
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print("skip exists", dest)
        continue
    p.rename(dest)
    moved += 1
    print(p, "->", dest)
mark.write_text("plus5\n", encoding="utf-8")
print("moved", moved)
PY
""",
    timeout=120,
)
run(
    r"""
UNIT="$HOME/.config/systemd/user/v380-record.service"
if [ -f "$UNIT" ]; then
  if ! grep -q 'TZ=Asia/Karachi' "$UNIT"; then
    sed -i '/^\[Service\]/a Environment=TZ=Asia/Karachi' "$UNIT"
  fi
  echo "unit patched"
  grep -n Environment "$UNIT" || true
else
  echo "no user unit"
fi
"""
)
run("python3 -m py_compile ~/v380-studio/auto_record.py")
run("systemctl --user daemon-reload")
run("systemctl --user start v380-record")
time.sleep(2)
run("systemctl --user is-active v380-record")
run("date; TZ=Asia/Karachi date")
c.close()
print("DONE")

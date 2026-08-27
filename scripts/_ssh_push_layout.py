import os
import time
from pathlib import Path

import paramiko

pw = os.environ["V380_SSH_PASS"]
root = Path(__file__).resolve().parent.parent
remote = "/home/amf/v380-studio"


def upload_dir(sftp, local: Path, dest: str) -> None:
    try:
        sftp.mkdir(dest)
    except OSError:
        pass
    for path in local.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(local)
        remote_path = f"{dest}/{rel.as_posix()}"
        parent = "/".join(remote_path.split("/")[:-1])
        parts = parent.split("/")
        cur = ""
        for part in parts:
            if not part:
                continue
            cur += "/" + part
            try:
                sftp.mkdir(cur)
            except OSError:
                pass
        sftp.put(str(path), remote_path)
        print("put", path.relative_to(root))


c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.1.68", username="amf", password=pw, timeout=20)


def run(cmd: str) -> None:
    print(">>>", cmd)
    _i, o, e = c.exec_command(cmd, timeout=60)
    print(o.read().decode("utf-8", "replace"))
    err = e.read().decode("utf-8", "replace")
    if err.strip():
        print("ERR", err)


run("systemctl --user stop v380-clips v380-record")
sftp = c.open_sftp()
upload_dir(sftp, root / "v380", f"{remote}/v380")
for name in ("clip_server.py", "record_worker.py", "app.py"):
    sftp.put(str(root / name), f"{remote}/{name}")
    print("put", name)
sftp.close()
run("python3 -m py_compile ~/v380-studio/clip_server.py ~/v380-studio/record_worker.py ~/v380-studio/v380/server/studio_server.py ~/v380-studio/v380/record/auto_record.py")
run("systemctl --user start v380-record v380-clips")
time.sleep(2)
run("systemctl --user is-active v380-record v380-clips")
run("curl -s http://127.0.0.1:8080/api/ping")
c.close()
print("DONE")

import os
import time

import paramiko

pw = os.environ["V380_SSH_PASS"]
remote_dir = "/home/amf/v380-studio"
files = [
    "studio_server.py",
    "clip_server.py",
    "db.py",
    "extras.py",
    "v380_client.py",
    "camera_store.py",
    "auto_record.py",
]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.1.68", username="amf", password=pw, timeout=20)
sftp = c.open_sftp()
for name in files:
    local = os.path.abspath(name)
    dest = f"{remote_dir}/{name}"
    print("put", name, "->", dest)
    sftp.put(local, dest)
sftp.close()


def run(cmd: str) -> None:
    print(">>>", cmd)
    _i, o, e = c.exec_command(cmd, timeout=60)
    print(o.read().decode("utf-8", "replace"))
    err = e.read().decode("utf-8", "replace")
    if err.strip():
        print("ERR", err)


run("python3 -m py_compile ~/v380-studio/studio_server.py ~/v380-studio/clip_server.py")
run("systemctl --user restart v380-clips")
time.sleep(2)
run("systemctl --user is-active v380-clips v380-record")
run("curl -s http://127.0.0.1:8080/api/ping")
run("ss -tlnp | grep -E ':22 |:8000 |:8080 |:8081 ' || true")
c.close()
print("DONE")

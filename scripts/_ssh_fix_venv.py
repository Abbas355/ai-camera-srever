import os
import time

import paramiko

pw = os.environ["V380_SSH_PASS"]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.1.68", username="amf", password=pw, timeout=20)


def run(cmd: str, sudo: bool = False) -> None:
    if sudo:
        cmd = f"echo {pw} | sudo -S -p '' bash -lc {cmd!r}"
    print(">>>", cmd[:140])
    _i, o, e = c.exec_command(cmd, timeout=300)
    print(o.read().decode("utf-8", "replace"))
    err = e.read().decode("utf-8", "replace")
    if err.strip() and "password" not in err.lower():
        print("ERR", err)


run("apt-get install -y python3.14-venv python3-pip", sudo=True)
run("rm -rf ~/v380-studio/.venv && python3 -m venv ~/v380-studio/.venv")
run("~/v380-studio/.venv/bin/python -m pip install -U pip")
run("~/v380-studio/.venv/bin/pip install pycryptodome imageio-ffmpeg")
run("systemctl --user restart v380-record v380-clips")
time.sleep(2)
run("systemctl --user --no-pager -l status v380-record")
run("systemctl --user --no-pager -l status v380-clips")
run("journalctl --user -u v380-record -n 40 --no-pager")
run("curl -s http://127.0.0.1:8080/api/ping")
c.close()
print("DONE")

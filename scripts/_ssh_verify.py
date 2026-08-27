import os
import time

import paramiko

pw = os.environ["V380_SSH_PASS"]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.1.68", username="amf", password=pw, timeout=20)


def run(cmd: str) -> str:
    print(">>>", cmd)
    _i, o, e = c.exec_command(cmd, timeout=60)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    print(out)
    if err.strip():
        print("ERR", err)
    return out


time.sleep(4)
run("systemctl --user is-active v380-record v380-clips")
run("systemctl --user --no-pager -l status v380-record")
run("journalctl --user -u v380-record -n 20 --no-pager --since '1 min ago'")
run("journalctl --user -u v380-clips -n 20 --no-pager --since '1 min ago'")
run("curl -s http://127.0.0.1:8080/api/ping")
run("curl -s http://127.0.0.1:8080/api/index")
run("ls -la ~/v380-studio ~/v380-studio/data 2>/dev/null; ls ~/v380-studio/.venv/bin/python")
run("ss -tlnp | grep -E ':22 |:8000 |:8080 |:8081 ' || true")
run("systemctl is-active nginx mysql 2>/dev/null; systemctl is-active servex 2>/dev/null; true")
c.close()
print("DONE")

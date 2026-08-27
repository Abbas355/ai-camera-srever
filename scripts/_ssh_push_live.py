import os
import time

import paramiko

pw = os.environ["V380_SSH_PASS"]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.1.68", username="amf", password=pw, timeout=20)
sftp = c.open_sftp()
sftp.put("studio_server.py", "/home/amf/v380-studio/studio_server.py")
sftp.put("v380_client.py", "/home/amf/v380-studio/v380_client.py")
sftp.close()
_i, o, e = c.exec_command(
    "python3 -m py_compile ~/v380-studio/studio_server.py ~/v380-studio/v380_client.py && systemctl --user restart v380-clips",
    timeout=30,
)
print(o.read().decode("utf-8", "replace"))
print(e.read().decode("utf-8", "replace"))
time.sleep(2)
_i, o, e = c.exec_command("systemctl --user is-active v380-clips; curl -s http://127.0.0.1:8080/api/ping", timeout=20)
print(o.read().decode("utf-8", "replace"))
c.close()
print("DONE")

"""One-shot installer for Ubuntu home dir. Password from env V380_SSH_PASS."""

from __future__ import annotations

import os
from pathlib import Path

import paramiko

HOST = "192.168.1.68"
USER = "amf"
APP = Path(__file__).resolve().parent
REMOTE = f"/home/{USER}/v380-studio"


def run(client: paramiko.SSHClient, cmd: str, sudo: bool = False) -> tuple[str, str]:
    pw = os.environ["V380_SSH_PASS"]
    if sudo:
        cmd = f"echo {pw} | sudo -S -p '' bash -lc {cmd!r}"
    print(">>>", cmd if not sudo else "(sudo) " + cmd[:120])
    _stdin, stdout, stderr = client.exec_command(cmd, timeout=300)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    print(out)
    if err.strip() and "password" not in err.lower():
        print("ERR:", err)
    return out, err


def main() -> None:
    pw = os.environ.get("V380_SSH_PASS")
    if not pw:
        raise SystemExit("Set V380_SSH_PASS")
    tar_path = next(APP.glob("v380-linux-*.tar.gz"))
    data_dir = APP / "data"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=pw, timeout=20)

    sftp = client.open_sftp()
    print("upload", tar_path.name)
    sftp.put(str(tar_path), f"/home/{USER}/{tar_path.name}")
    sftp.close()

    run(client, f"rm -rf /tmp/v380-unpack && mkdir -p /tmp/v380-unpack && tar -xzf ~/{tar_path.name} -C /tmp/v380-unpack")
    run(client, "mkdir -p ~/v380-studio && cp -a /tmp/v380-unpack/v380-studio/. ~/v380-studio/")
    run(client, "mkdir -p ~/v380-studio/data ~/v380-studio/recordings")

    sftp = client.open_sftp()
    try:
        sftp.mkdir(f"{REMOTE}/data")
    except OSError:
        pass
    if data_dir.is_dir():
        for p in data_dir.iterdir():
            if p.is_file() and p.suffix not in {".log", ".pid"} and not p.name.startswith("."):
                print("put data", p.name)
                sftp.put(str(p), f"{REMOTE}/data/{p.name}")
    sftp.close()

    run(client, "python3 -m venv ~/v380-studio/.venv")
    run(client, "~/v380-studio/.venv/bin/python -m pip install -U pip")
    run(client, "~/v380-studio/.venv/bin/pip install pycryptodome imageio-ffmpeg")

    run(client, "apt-get install -y ffmpeg", sudo=True)
    run(client, "ufw allow 8080/tcp", sudo=True)
    run(client, "loginctl enable-linger amf", sudo=True)

    record_unit = """[Unit]
Description=V380 auto-record (does not replace other services)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/amf/v380-studio
ExecStart=/home/amf/v380-studio/.venv/bin/python /home/amf/v380-studio/record_worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
    clips_unit = """[Unit]
Description=V380 recordings API on port 8080
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/amf/v380-studio
ExecStart=/home/amf/v380-studio/.venv/bin/python /home/amf/v380-studio/clip_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
    run(client, "mkdir -p ~/.config/systemd/user")
    sftp = client.open_sftp()
    with sftp.file(f"/home/{USER}/.config/systemd/user/v380-record.service", "w") as fh:
        fh.write(record_unit)
    with sftp.file(f"/home/{USER}/.config/systemd/user/v380-clips.service", "w") as fh:
        fh.write(clips_unit)
    sftp.close()

    run(client, "systemctl --user daemon-reload")
    run(client, "systemctl --user enable --now v380-record.service v380-clips.service")
    run(client, "systemctl --user --no-pager -l status v380-record.service")
    run(client, "systemctl --user --no-pager -l status v380-clips.service")
    run(client, "hostname -I")
    run(client, "ss -tlnp | grep 8080 || true")
    client.close()
    print("DONE")


if __name__ == "__main__":
    main()

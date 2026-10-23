# Optiview backend

Python runs the **API + recording + AI** on this PC (or Linux/WSL).  
The **Flutter app** is the UI (cameras, live, clips, tables, floor, staff, guests).

```
app.py / optiview_backend.py   start API (port 8080) + record worker
v380/                          application code
  api/                         HTTP server + controllers
  occupancy/                   floor table Free/Occupied (YOLO/face)
  attendance/                  staff check-in
  customers/                   guest visits
  record/                      continuous recording / HLS
  client/                      camera protocols (EZVIZ, V380, …)
  ai/                          YOLO weights helpers (used by Floor)
linux/                         Ubuntu systemd + WSL scripts
data/                          db, keys, worker state
recordings/
```

## Windows

**See logs in the terminal** (recommended while developing):

```bat
run.bat
```

or:

```bat
python app.py
```

Logs are printed in the console **and** appended to `data/server.log`.

If you see `API already running on port 8080`, a background `pythonw optiview_backend.py` is already up — stop that process first, or just use Flutter against it.

**Background / after reboot:** `optiview_backend.py` via scheduled task (`pythonw`, no window). Check `data/server.log` for errors.

Default login (Flutter): `admin` / `1290`

## Flutter client

Point the app at `http://THIS_PC_IP:8080`.

## WSL (test backend on this PC)

```bash
bash linux/wsl-setup.sh
bash ~/optiview/linux/wsl-start.sh
```

Stop:

```bash
bash ~/optiview/linux/wsl-stop.sh
```

## Ubuntu server

```bash
python scripts/build_linux_tar.py
# copy archive to the server, extract, then:
bash linux/install.sh
```

Use the printed IP in the Flutter app.

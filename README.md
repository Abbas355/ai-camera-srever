# V380 Studio

Windows is the UI. Linux holds cameras, live, talk, record, and clips.

```
v380-snapshot-app/
  app.py                 Windows start
  clip_server.py         Linux API (port 8080)
  record_worker.py        Linux auto-record
  run.bat
  v380/
    ui/                  screens (login, home, live, clips)
    client/              camera protocol + HTTP client
    store/               database + secrets
    record/              auto-record
    server/              Linux API
    paths.py             data/ and recordings/ at repo root
  linux/                 systemd units + install.sh
  tools/                 diagnose helpers
  tests/
  scripts/
  data/                  db, keys, worker state
  recordings/
  audio/
```

Windows: `run.bat`  
Linux: `python3 clip_server.py` and `python3 record_worker.py` from this folder.

"""V380 Studio — Windows UI. All camera work runs on the Linux server."""

from __future__ import annotations

import tkinter as tk

from v380.client.api_client import RemoteCameraStore, RemotePreview, RemoteRecordSupervisor
from v380.store.camera_store import Camera
from v380.ui.clips import ClipsFrame
from v380.ui.home import EditDialog, HomeFrame
from v380.ui.live_view import LiveView
from v380.ui.login import LoginFrame
from v380.ui.theme import BG, apply_root_style


class StudioApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("V380 Studio")
        self.geometry("1280x780")
        self.minsize(1020, 640)
        self.configure(bg=BG)
        apply_root_style(self)

        self.api = None
        self.store = None
        self.recorders = None
        self._home: HomeFrame | None = None
        self._live: LiveView | None = None
        self._clips_ui: ClipsFrame | None = None
        self._login = LoginFrame(self, self._start)
        self._login.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _start(self, api) -> None:
        self.api = api
        self.store = RemoteCameraStore(api)
        self.recorders = RemoteRecordSupervisor(api)
        if self._login is not None:
            self._login.destroy()
            self._login = None
        self._home = HomeFrame(
            self,
            self.store,
            self.recorders,
            self._add,
            self._view,
            self._edit,
            self._show_clips,
            previews=RemotePreview(api, self._preview_state),
            api=api,
        )
        self._home.pack(fill="both", expand=True)
        self.recorders.sync(self.store.list())
        self.title(f"V380 Studio  ·  {api.user}@{api.base}")

    def _preview_state(self, camera_id: int, text: str) -> None:
        if self._home is not None:
            self._home._push_state(camera_id, text)

    def _show_home(self) -> None:
        if self._live is not None:
            self._live.shutdown()
            self._live.destroy()
            self._live = None
        if self._clips_ui is not None:
            self._clips_ui.destroy()
            self._clips_ui = None
        if self._home is not None:
            self._home.pack(fill="both", expand=True)
            self._home.resume()

    def _show_clips(self) -> None:
        if self._home is None:
            return
        self._home.pause()
        self._home.pack_forget()
        self._clips_ui = ClipsFrame(self, on_back=self._show_home, api=self.api)
        self._clips_ui.pack(fill="both", expand=True)

    def _show_live(self, camera: Camera | None, auto_connect: bool) -> None:
        if self._home is None:
            return
        self._home.pause()
        self._home.pack_forget()
        self._live = LiveView(
            self,
            on_back=self._show_home,
            on_connected=self._save_connected,
            camera=camera,
            auto_connect=auto_connect,
            recorders=self.recorders,
            api=self.api,
        )
        self._live.pack(fill="both", expand=True)

    def _add(self) -> None:
        self._show_live(None, auto_connect=False)

    def _view(self, cam: Camera) -> None:
        self._show_live(cam, auto_connect=True)

    def _edit(self, cam: Camera) -> None:
        EditDialog(self, cam, self._save_edit, api=self.api)

    def _save_edit(self, cam: Camera) -> None:
        self.store.update(cam)
        self.recorders.sync(self.store.list())
        if self._home is not None:
            self._home.reload()

    def _save_connected(self, fields: dict):
        existing = self.store.get_by_device_id(str(fields["device_id"]))
        cam = Camera(
            id=existing.id if existing else 0,
            name=fields["name"],
            device_id=fields["device_id"],
            mac=fields.get("mac") or "",
            ip=fields["ip"],
            port=int(fields["port"]),
            username=fields["username"],
            password=fields["password"],
            source=fields["source"],
            quality=int(fields["quality"]),
            auto_record=existing.auto_record if existing else False,
            record_chunk=existing.record_chunk if existing else "hour",
            created_at="",
            updated_at="",
        )
        saved = self.store.upsert(cam)
        self.recorders.sync(self.store.list())
        if self._live is None and self._home is not None:
            self._home.reload()
        return saved

    def _on_close(self) -> None:
        if self._live is not None:
            self._live.shutdown()
        if self._clips_ui is not None:
            self._clips_ui.destroy()
        if self._home is not None:
            self._home.shutdown()
        if self.recorders is not None:
            self.recorders.release_to_worker()
        if self.store is not None:
            self.store.close()
        self.destroy()


def main() -> None:
    StudioApp().mainloop()


if __name__ == "__main__":
    main()

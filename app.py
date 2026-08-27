"""V380 Studio — home grid plus the existing live-view controls."""

from __future__ import annotations

import tkinter as tk

from auto_record import RecordSupervisor
from camera_store import Camera, CameraStore
from clips import ClipsFrame
from home import EditDialog, HomeFrame
from live_view import LiveView
from theme import BG


class StudioApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("V380 Studio")
        self.geometry("1280x780")
        self.minsize(1020, 640)
        self.configure(bg=BG)

        self.store = CameraStore()
        self.recorders = RecordSupervisor()
        self._home = HomeFrame(self, self.store, self.recorders, self._add, self._view, self._edit, self._show_clips)
        self._live: LiveView | None = None
        self._clips_ui: ClipsFrame | None = None
        self._home.pack(fill="both", expand=True)
        self.recorders.sync(self.store.list())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _show_home(self) -> None:
        if self._live is not None:
            self._live.shutdown()
            self._live.destroy()
            self._live = None
        if self._clips_ui is not None:
            self._clips_ui.destroy()
            self._clips_ui = None
        self._home.pack(fill="both", expand=True)
        self._home.resume()

    def _show_clips(self) -> None:
        self._home.pause()
        self._home.pack_forget()
        self._clips_ui = ClipsFrame(self, on_back=self._show_home)
        self._clips_ui.pack(fill="both", expand=True)

    def _show_live(self, camera: Camera | None, auto_connect: bool) -> None:
        self._home.pause()
        self._home.pack_forget()
        self._live = LiveView(
            self,
            on_back=self._show_home,
            on_connected=self._save_connected,
            camera=camera,
            auto_connect=auto_connect,
            recorders=self.recorders,
        )
        self._live.pack(fill="both", expand=True)

    def _add(self) -> None:
        self._show_live(None, auto_connect=False)

    def _view(self, cam: Camera) -> None:
        self._show_live(cam, auto_connect=True)

    def _edit(self, cam: Camera) -> None:
        EditDialog(self, cam, self._save_edit)

    def _save_edit(self, cam: Camera) -> None:
        self.store.update(cam)
        self.recorders.sync(self.store.list())
        self._home.reload()

    def _save_connected(self, fields: dict) -> None:
        existing = self.store.get_by_device_id(str(fields["device_id"]))
        cam = Camera(
            id=0,
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
        self.store.upsert(cam)
        self.recorders.sync(self.store.list())
        if self._live is None:
            self._home.reload()

    def _on_close(self) -> None:
        if self._live is not None:
            self._live.shutdown()
        if self._clips_ui is not None:
            self._clips_ui.destroy()
        self._home.shutdown()
        self.recorders.release_to_worker()
        self.store.close()
        self.destroy()


if __name__ == "__main__":
    StudioApp().mainloop()

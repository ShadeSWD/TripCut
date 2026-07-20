"""Виджет видеоплеера на libmpv (точная перемотка, покадровый шаг)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import QWidget

# libmpv (mpv-2.dll) лежит в bin/ рядом с программой
_here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
_bin = _here / "bin"
if _bin.is_dir():
    os.environ["PATH"] = str(_bin) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(_bin))

import mpv  # noqa: E402


class _Bridge(QObject):
    time_pos = Signal(float)
    eof = Signal()


class PlayerWidget(QWidget):
    """Окно mpv. Все колбэки mpv переправляются в Qt-поток сигналами."""

    time_changed = Signal(float)   # позиция в текущем файле, сек
    reached_eof = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setStyleSheet("background:#101010;")
        self._bridge = _Bridge()
        self._bridge.time_pos.connect(self.time_changed)
        self._bridge.eof.connect(self.reached_eof)
        self._mpv = None
        self.current_path: str | None = None

    def init_mpv(self):
        if self._mpv is not None:
            return
        import locale
        locale.setlocale(locale.LC_NUMERIC, "C")  # обязательное требование libmpv
        self._mpv = mpv.MPV(
            wid=str(int(self.winId())),
            vo="gpu", hwdec="auto-safe",
            keep_open="yes", pause=True,
            osc=False, input_default_bindings=False,
            mute=False,
        )

        @self._mpv.property_observer("time-pos")
        def _tp(_name, value):  # вызывается из потока mpv
            if value is not None:
                self._bridge.time_pos.emit(float(value))

        @self._mpv.property_observer("eof-reached")
        def _eof(_name, value):
            if value:
                self._bridge.eof.emit()

    # ---------------- управление
    def load(self, path: str, start: float = 0.0):
        self.init_mpv()
        self.current_path = path
        self._mpv.pause = True
        self._mpv.loadfile(path, start=f"{start:.3f}")

    def seek(self, t: float):
        if self._mpv is None or self.current_path is None:
            return
        try:
            self._mpv.command("seek", f"{t:.3f}", "absolute+exact")
        except mpv.ShutdownError:
            pass
        except SystemError:
            pass  # seek до загрузки файла

    def play_pause(self):
        if self._mpv is not None:
            self._mpv.pause = not self._mpv.pause

    @property
    def paused(self) -> bool:
        return True if self._mpv is None else bool(self._mpv.pause)

    def set_paused(self, p: bool):
        if self._mpv is not None:
            self._mpv.pause = p

    def frame_step(self, back=False):
        if self._mpv is None:
            return
        self._mpv.command("frame-back-step" if back else "frame-step")

    @property
    def time_pos(self) -> float | None:
        if self._mpv is None:
            return None
        try:
            v = self._mpv.time_pos
            return float(v) if v is not None else None
        except mpv.ShutdownError:
            return None

    def shutdown(self):
        if self._mpv is not None:
            try:
                self._mpv.terminate()
            except Exception:
                pass
            self._mpv = None

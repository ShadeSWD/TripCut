"""Главное окно TripCut: плеер + таймлайн + горячие клавиши + фото-режим."""
from __future__ import annotations

import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QSettings, Signal, QObject, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCheckBox, QDateTimeEdit, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressDialog,
    QPushButton, QSplitter, QVBoxLayout, QWidget, QStatusBar,
)

from . import ffmpeg_tools as ft
from . import model as M
from . import exifw
from .geo import load_gpx, gopro_track, Track
from .player import PlayerWidget
from .timeline import TimelineWidget, fmt_t

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".mts", ".m2ts", ".lrv", ".360")


class _Notifier(QObject):
    done = Signal(str, object)      # (тег, результат/ошибка)


def _bg(notifier: _Notifier, tag: str, fn):
    def run():
        try:
            notifier.done.emit(tag, fn())
        except Exception as e:                      # noqa: BLE001
            traceback.print_exc()
            notifier.done.emit(tag, e)
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- настройки

class Settings:
    def __init__(self):
        self.q = QSettings("TripCut", "TripCut")

    @property
    def photo_dir(self) -> str:
        return self.q.value("photo_dir", "", str)

    @photo_dir.setter
    def photo_dir(self, v):
        self.q.setValue("photo_dir", v)

    @property
    def photo_fmt(self) -> str:
        return self.q.value("photo_fmt", "jpeg", str)

    @photo_fmt.setter
    def photo_fmt(self, v):
        self.q.setValue("photo_fmt", v)

    @property
    def smart_cut(self) -> bool:
        return self.q.value("smart_cut", False, bool)

    @smart_cut.setter
    def smart_cut(self, v):
        self.q.setValue("smart_cut", v)

    def geo(self) -> M.GeoConfig:
        def fnum(key):
            raw = self.q.value(key, "", str)
            try:
                return float(raw) if raw else None
            except ValueError:
                return None
        return M.GeoConfig(
            mode=self.q.value("geo_mode", "auto", str),
            utc_offset_h=self.q.value("utc_offset", 3.0, float),
            manual_lat=fnum("manual_lat"),
            manual_lon=fnum("manual_lon"),
        )

    def set_geo(self, cfg: M.GeoConfig):
        self.q.setValue("geo_mode", cfg.mode)
        self.q.setValue("utc_offset", cfg.utc_offset_h)
        self.q.setValue("manual_lat", "" if cfg.manual_lat is None else cfg.manual_lat)
        self.q.setValue("manual_lon", "" if cfg.manual_lon is None else cfg.manual_lon)


class SettingsDialog(QDialog):
    def __init__(self, st: Settings, parent=None):
        super().__init__(parent)
        self.st = st
        self.setWindowTitle("Настройки")
        geo = st.geo()
        form = QFormLayout(self)

        self.dir_edit = QLineEdit(st.photo_dir)
        b = QPushButton("…")
        b.setFixedWidth(28)
        b.clicked.connect(self._pick_dir)
        row = QHBoxLayout(); row.addWidget(self.dir_edit); row.addWidget(b)
        form.addRow("Папка для фото (пусто = рядом с видео):", row)

        self.png_cb = QCheckBox("PNG вместо JPEG")
        self.png_cb.setChecked(st.photo_fmt == "png")
        form.addRow("Формат кадров:", self.png_cb)

        self.smart_cb = QCheckBox("Точный рез (smart-cut, перекодирует стыки)")
        self.smart_cb.setChecked(st.smart_cut)
        form.addRow("Компиляция:", self.smart_cb)

        self.geo_mode = QComboBox()
        for key, label in [("auto", "Авто (GPS камеры → трек → ручная точка)"),
                           ("camera", "Только GPS камеры (GoPro)"),
                           ("track", "Только загруженный трек"),
                           ("manual", "Одна ручная точка"),
                           ("none", "Без координат")]:
            self.geo_mode.addItem(label, key)
        self.geo_mode.setCurrentIndex(max(0, self.geo_mode.findData(geo.mode)))
        form.addRow("Источник координат:", self.geo_mode)

        self.utc_spin = QDoubleSpinBox()
        self.utc_spin.setRange(-12, 14); self.utc_spin.setDecimals(1)
        self.utc_spin.setSingleStep(0.5); self.utc_spin.setValue(geo.utc_offset_h)
        form.addRow("Часовой пояс камеры (UTC+):", self.utc_spin)

        self.lat_edit = QLineEdit("" if geo.manual_lat is None else str(geo.manual_lat))
        self.lon_edit = QLineEdit("" if geo.manual_lon is None else str(geo.manual_lon))
        self.lat_edit.setPlaceholderText("55.7515 или «55.7515, 37.6115»")
        form.addRow("Ручная точка — широта:", self.lat_edit)
        form.addRow("Ручная точка — долгота:", self.lon_edit)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Папка для фото", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def accept(self):
        self.st.photo_dir = self.dir_edit.text().strip()
        self.st.photo_fmt = "png" if self.png_cb.isChecked() else "jpeg"
        self.st.smart_cut = self.smart_cb.isChecked()
        lat = lon = None
        raw = self.lat_edit.text().replace(";", ",").strip()
        if "," in raw and not self.lon_edit.text().strip():
            a, b = raw.split(",", 1)
            self.lat_edit.setText(a.strip()); self.lon_edit.setText(b.strip())
        try:
            if self.lat_edit.text().strip():
                lat = float(self.lat_edit.text().replace(",", "."))
            if self.lon_edit.text().strip():
                lon = float(self.lon_edit.text().replace(",", "."))
        except ValueError:
            QMessageBox.warning(self, "TripCut", "Координаты не распознаны")
            return
        self.st.set_geo(M.GeoConfig(
            mode=self.geo_mode.currentData(), utc_offset_h=self.utc_spin.value(),
            manual_lat=lat, manual_lon=lon))
        super().accept()


class ClipTimeDialog(QDialog):
    """Правка даты-времени старта записи клипа."""

    def __init__(self, clip: M.Clip, parent=None):
        super().__init__(parent)
        self.clip = clip
        self.setWindowTitle(f"Время начала: {clip.name}")
        form = QFormLayout(self)
        self.dt = QDateTimeEdit()
        self.dt.setDisplayFormat("dd.MM.yyyy HH:mm:ss")
        self.dt.setCalendarPopup(True)
        if clip.start_dt:
            self.dt.setDateTime(clip.start_dt)
        form.addRow("Локальное время старта записи:", self.dt)
        note = QLabel("Из метаданных камеры: " +
                      (clip.info.creation_time.strftime("%d.%m.%Y %H:%M:%S")
                       if clip.info.creation_time else "нет"))
        note.setStyleSheet("color:#888;")
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        form.addRow(bb)

    def accept(self):
        self.clip.start_dt = self.dt.dateTime().toPython().replace(microsecond=0)
        super().accept()


# ---------------------------------------------------------------- главное окно

class MainWindow(QMainWindow):
    HELP = ("Space — играть/пауза   ←/→ — ±5с   ,/. — кадр   PgUp/PgDn — пред./след. видео   "
            "Q — убрать слева   W — убрать справа   E — рассечь   Del — удалить сегмент   "
            "/ — кадр в фото   Ctrl+Z — отмена")
    HELP_QUICK = ("⚡ Быстрый режим: только фото.   / — кадр в фото   Space — играть/пауза   "
                  "←/→ — ±5с   ,/. — кадр   PgUp/PgDn — пред./след. видео")

    def __init__(self):
        super().__init__()
        self.setWindowTitle("TripCut — нарезка без перекодирования")
        self.resize(1280, 800)
        self.st = Settings()
        self.project = M.Project()
        self.track: Track | None = None
        self.cur_global = 0.0
        self.active_idx = 0
        self.notifier = _Notifier()
        self.notifier.done.connect(self._bg_done)
        self._kf_pending: set[str] = set()
        self._gps_pending: set[str] = set()
        self.quick_mode = False

        # --- виджеты
        central = QWidget(); self.setCentralWidget(central)
        v = QVBoxLayout(central); v.setContentsMargins(6, 6, 6, 6); v.setSpacing(6)

        top = QHBoxLayout()
        for text, slot in [("➕ Видео", self.add_videos),
                           ("⚡ Папка → фото", self.open_quick_folder),
                           ("🗺 Трек (GPX)", self.load_track),
                           ("🎬 Скомпилировать", self.compile_video),
                           ("⚙ Настройки", self.open_settings)]:
            b = QPushButton(text); b.clicked.connect(slot)
            b.setFocusPolicy(Qt.NoFocus)
            top.addWidget(b)
        self.full_btn = QPushButton("✏ В полный редактор")
        self.full_btn.setFocusPolicy(Qt.NoFocus)
        self.full_btn.clicked.connect(self.to_full_editor)
        self.full_btn.setVisible(False)
        top.addWidget(self.full_btn)
        self.mode_lbl = QLabel("")
        self.mode_lbl.setStyleSheet("color:#6ac06a; font-weight:bold;")
        top.addWidget(self.mode_lbl)
        self.track_lbl = QLabel("трек не загружен")
        self.track_lbl.setStyleSheet("color:#888;")
        top.addWidget(self.track_lbl)
        top.addStretch()
        v.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.player = PlayerWidget()
        self.player.time_changed.connect(self._on_time)
        self.player.reached_eof.connect(self._on_eof)
        split.addWidget(self.player)

        right = QWidget(); rv = QVBoxLayout(right); rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("Клипы (двойной клик — время старта):"))
        self.clip_list = QListWidget()
        self.clip_list.setFocusPolicy(Qt.NoFocus)
        self.clip_list.itemDoubleClicked.connect(self._edit_clip_time)
        rv.addWidget(self.clip_list)
        right.setMaximumWidth(320)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        v.addWidget(split, 1)

        self.geo_lbl = QLabel(" ")
        self.geo_lbl.setStyleSheet("font-size:13px;")
        v.addWidget(self.geo_lbl)

        self.timeline = TimelineWidget(self.project)
        self.timeline.seek_requested.connect(self.seek_global)
        v.addWidget(self.timeline)

        self.hint = QLabel(self.HELP)
        self.hint.setStyleSheet("color:#777; font-size:11px;")
        v.addWidget(self.hint)

        self.setStatusBar(QStatusBar())
        self.setAcceptDrops(True)

        # --- клавиши
        def key(seq, fn):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.ApplicationShortcut)
            s.activated.connect(fn)
        key(Qt.Key_Space, self._play_pause)
        key(Qt.Key_Q, lambda: self._edit_op("q"))
        key(Qt.Key_W, lambda: self._edit_op("w"))
        key(Qt.Key_E, lambda: self._edit_op("e"))
        key(Qt.Key_Delete, lambda: self._edit_op("del"))
        key(Qt.Key_Slash, self.snap_photo)
        key(Qt.Key_Left, lambda: self.seek_global(self.cur_global - 5))
        key(Qt.Key_Right, lambda: self.seek_global(self.cur_global + 5))
        key(Qt.Key_Comma, lambda: self._frame(back=True))
        key(Qt.Key_Period, lambda: self._frame(back=False))
        key(Qt.Key_PageDown, lambda: self.jump_clip(1))
        key(Qt.Key_PageUp, lambda: self.jump_clip(-1))
        key(QKeySequence.Undo, self._undo)

        self._suppress_time = False

    # ---------------- drag&drop
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        vids, gpx = [], []
        for u in ev.mimeData().urls():
            p = u.toLocalFile()
            (gpx if p.lower().endswith(".gpx") else
             vids if p.lower().endswith(VIDEO_EXT) else []).append(p)
        if vids:
            self._add_clip_paths(vids)
        for g in gpx:
            self._load_track_path(g)

    # ---------------- клипы
    def add_videos(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Видео", "", "Видео (*.mp4 *.mov *.mkv *.avi *.mts *.m2ts);;Все файлы (*)")
        if paths:
            self._add_clip_paths(paths)

    def _add_clip_paths(self, paths: list[str]):
        utc_offset_h = self.st.geo().utc_offset_h
        prog = None
        if len(paths) > 3:
            prog = QProgressDialog("Читаю видео…", None, 0, len(paths), self)
            prog.setWindowTitle("TripCut"); prog.setWindowModality(Qt.WindowModal)
            prog.setMinimumDuration(0); prog.setCancelButton(None)
        errors = []
        for k, path in enumerate(paths):
            if prog:
                prog.setValue(k); prog.setLabelText(os.path.basename(path))
                QApplication.processEvents()
            try:
                info = ft.probe(path, utc_offset_h)
            except Exception as e:                  # noqa: BLE001
                errors.append(f"{os.path.basename(path)}: {e}")
                continue
            clip = M.Clip(info=info, start_dt=info.creation_time)
            self.project.add_clip(clip)
            item = QListWidgetItem(self._clip_label(clip))
            item.setData(Qt.UserRole, clip)
            self.clip_list.addItem(item)
            if not self.quick_mode:
                # редактор: сразу индексируем ключевые кадры + GPS камеры
                self._start_kf(clip)
                self._start_gps(clip)
        if prog:
            prog.setValue(len(paths))
        if errors:
            QMessageBox.warning(self, "TripCut", "Не открылись:\n" + "\n".join(errors[:10]))
        self.timeline.update()
        if self.player.current_path is None and self.project.segments:
            self.seek_global(0.0)
        self._update_status()

    def _start_kf(self, clip: M.Clip):
        if clip.keyframes or clip.path in self._kf_pending:
            return
        self._kf_pending.add(clip.path)
        _bg(self.notifier, f"kf|{clip.path}", lambda p=clip.path: ft.keyframes(p))

    def _start_gps(self, clip: M.Clip):
        """GPS камеры; в быстром режиме зовётся лениво — при заходе курсора в клип."""
        if clip.info.gpmd_stream is None or clip.own_track is not None \
                or clip.path in self._gps_pending:
            return
        self._gps_pending.add(clip.path)
        _bg(self.notifier, f"gps|{clip.path}",
            lambda p=clip.path, s=clip.info.gpmd_stream: gopro_track(p, s))

    # ---------------- быстрый режим (папка → фото)
    def open_quick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Папка с видео")
        if not d:
            return
        files = sorted(
            os.path.join(d, f) for f in os.listdir(d)
            if f.lower().endswith(VIDEO_EXT) and not f.lower().endswith(".lrv"))
        if not files:
            QMessageBox.information(self, "TripCut", "В папке нет видеофайлов")
            return
        if self.project.segments and QMessageBox.question(
                self, "TripCut", "Закрыть текущий проект и открыть папку?") != QMessageBox.Yes:
            return
        self._reset_project()
        self.quick_mode = True
        self._update_mode_ui()
        self._add_clip_paths(files)
        self.statusBar().showMessage(
            f"⚡ Быстрый режим: {len(files)} видео из {d}. «/» — фото, PgDn — следующее видео",
            10000)

    def to_full_editor(self):
        self.quick_mode = False
        self._update_mode_ui()
        for clip in self.project.clips:      # доиндексировать для нарезки
            self._start_kf(clip)
            self._start_gps(clip)
        self._update_status()

    def _update_mode_ui(self):
        self.full_btn.setVisible(self.quick_mode)
        self.mode_lbl.setText("⚡ БЫСТРЫЙ РЕЖИМ" if self.quick_mode else "")
        self.hint.setText(self.HELP_QUICK if self.quick_mode else self.HELP)

    def _reset_project(self):
        self.player.set_paused(True)
        self.project.clips.clear()
        self.project.segments.clear()
        self.project._undo.clear()
        self.clip_list.clear()
        self.cur_global = 0.0
        self.active_idx = 0
        self.timeline.update()

    def jump_clip(self, delta: int):
        """PgUp/PgDn: к началу предыдущего/следующего видео на таймлайне."""
        if not self.project.segments:
            return
        starts, acc, cur = [], 0.0, None
        for s in self.project.segments:
            if s.clip is not cur:
                starts.append(acc)
                cur = s.clip
            acc += s.duration
        i = max((j for j, st in enumerate(starts) if st <= self.cur_global + 1e-3),
                default=0)
        tgt = i + delta
        if 0 <= tgt < len(starts):
            self.seek_global(starts[tgt] + 0.001)
            loc = self.project.locate(self.cur_global)
            if loc:
                self.statusBar().showMessage(
                    f"видео {tgt + 1}/{len(starts)}: {loc[0].clip.name}", 4000)

    def _clip_label(self, clip: M.Clip) -> str:
        dt = clip.start_dt.strftime("%d.%m %H:%M:%S") if clip.start_dt else "⚠ нет времени"
        gps = " ·GPS" if clip.own_track and len(clip.own_track) else ""
        return f"{clip.name}  [{dt}]{gps}"

    def _refresh_clip_list(self):
        for i in range(self.clip_list.count()):
            it = self.clip_list.item(i)
            it.setText(self._clip_label(it.data(Qt.UserRole)))

    def _edit_clip_time(self, item: QListWidgetItem):
        clip = item.data(Qt.UserRole)
        if ClipTimeDialog(clip, self).exec():
            self._refresh_clip_list()
            self._update_geo_label()

    def _bg_done(self, tag: str, result):
        kind, path = tag.split("|", 1)
        if isinstance(result, Exception):
            self.statusBar().showMessage(f"Ошибка ({kind}): {result}", 8000)
            self._kf_pending.discard(path)
            self._gps_pending.discard(path)
            return
        for clip in self.project.clips:
            if clip.path != path:
                continue
            if kind == "kf":
                clip.keyframes = result
                self._kf_pending.discard(path)
                self.statusBar().showMessage(
                    f"{clip.name}: {len(result)} ключевых кадров", 4000)
            elif kind == "gps":
                clip.own_track = result
                self._gps_pending.discard(path)
                self.statusBar().showMessage(
                    f"{clip.name}: GPS-точек в файле: {len(result)}", 6000)
                self._update_geo_label()
            elif kind == "photo":
                self.statusBar().showMessage(f"📸 {result}", 6000)
            elif kind == "compile":
                pass
        self._refresh_clip_list()

    # ---------------- трек
    def load_track(self):
        p, _ = QFileDialog.getOpenFileName(self, "GPX-трек", "", "GPX (*.gpx)")
        if p:
            self._load_track_path(p)

    def _load_track_path(self, p: str):
        try:
            self.track = load_gpx(p)
        except Exception as e:                      # noqa: BLE001
            QMessageBox.warning(self, "TripCut", f"GPX не прочитался:\n{e}")
            return
        if len(self.track) == 0:
            QMessageBox.warning(self, "TripCut", "В GPX нет точек со временем")
            return
        s, e = self.track.start_utc, self.track.end_utc
        self.track_lbl.setText(
            f"трек: {os.path.basename(p)} · {len(self.track)} тчк · "
            f"{s:%d.%m %H:%M}–{e:%H:%M} UTC")
        self._update_geo_label()

    # ---------------- курсор/навигация
    def seek_global(self, gt: float):
        loc = self.project.locate(max(0.0, min(gt, self.project.total)))
        if not loc:
            return
        seg, src_t, i = loc
        self.cur_global = self.project.global_of(i, src_t)
        self.active_idx = i
        if self.quick_mode:
            self._start_gps(seg.clip)   # ленивый парс GPS при заходе в клип
        self._suppress_time = True
        if self.player.current_path != seg.clip.path:
            self.player.load(seg.clip.path, start=src_t)
        else:
            self.player.seek(src_t)
        QTimer.singleShot(150, lambda: setattr(self, "_suppress_time", False))
        self.timeline.set_cursor(self.cur_global)
        self._update_geo_label()

    def _on_time(self, src_t: float):
        if self._suppress_time or not self.project.segments:
            return
        i = min(self.active_idx, len(self.project.segments) - 1)
        seg = self.project.segments[i]
        if self.player.current_path != seg.clip.path:
            return
        # выскочили за конец сегмента при воспроизведении -> прыжок дальше
        if src_t > seg.end + 0.05 and not self.player.paused:
            if self._advance_to(i + 1):
                return
        self.cur_global = self.project.global_of(i, src_t)
        self.timeline.set_cursor(self.cur_global)
        self._update_geo_label()

    def _on_eof(self):
        """mpv (keep_open=yes) замирает в конце файла: time-pos дальше не растёт и
        _on_time не сработает — сегмент до самого конца клипа доигрывает сюда."""
        if not self.project.segments or self.player.current_path is None:
            return
        i = min(self.active_idx, len(self.project.segments) - 1)
        self._advance_to(i + 1)

    def _advance_to(self, i: int) -> bool:
        """Перейти на сегмент i, продолжая воспроизведение. False — сегменты кончились."""
        if i >= len(self.project.segments):
            self.player.set_paused(True)
            return False
        nxt = self.project.segments[i]
        self.active_idx = i
        self._suppress_time = True
        if self.player.current_path != nxt.clip.path:
            self.player.load(nxt.clip.path, start=nxt.start)
            self.player.set_paused(False)
        else:
            self.player.seek(nxt.start)
        if self.quick_mode:
            self._start_gps(nxt.clip)   # ленивый парс GPS при заходе в клип
        QTimer.singleShot(150, lambda: setattr(self, "_suppress_time", False))
        self.cur_global = self.project.global_of(i, nxt.start)
        self.timeline.set_cursor(self.cur_global)
        self._update_geo_label()
        return True

    def _play_pause(self):
        if self.player.current_path:
            self.player.play_pause()

    def _frame(self, back: bool):
        self.player.set_paused(True)
        self.player.frame_step(back=back)

    # ---------------- операции резки
    def _edit_op(self, op: str):
        if not self.project.segments:
            return
        if self.quick_mode:
            self.statusBar().showMessage(
                "⚡ Быстрый режим — только фото. Кнопка «В полный редактор» включит нарезку", 5000)
            return
        was = self.cur_global
        ok = {"q": self.project.trim_left, "w": self.project.trim_right,
              "e": self.project.split, "del": self.project.delete_segment}[op](was)
        if not ok:
            self.statusBar().showMessage("Нечего резать в этой точке", 3000)
            return
        self.timeline.update()
        names = {"q": "убрано слева", "w": "убрано справа", "e": "рассечено",
                 "del": "сегмент удалён"}
        self.statusBar().showMessage(
            f"{names[op]} · осталось {fmt_t(self.project.total)}", 4000)
        if op == "q":
            self.seek_global(min(was, self.project.total))
        elif op == "w":
            self.seek_global(min(was, self.project.total - 0.001))
        else:
            self.seek_global(was)

    def _undo(self):
        if self.project.undo():
            self.timeline.update()
            self.seek_global(min(self.cur_global, self.project.total))
            self.statusBar().showMessage("Отменено", 2000)

    # ---------------- гео/дата подпись
    def _cursor_ctx(self):
        loc = self.project.locate(self.cur_global)
        if not loc:
            return None
        seg, src_t, _ = loc
        # точное время из плеера, если он в этом же файле
        tp = self.player.time_pos
        if tp is not None and self.player.current_path == seg.clip.path \
                and seg.start - 0.5 <= tp <= seg.end + 0.5:
            src_t = tp
        return seg.clip, src_t

    def _update_geo_label(self):
        ctx = self._cursor_ctx()
        if not ctx:
            self.geo_lbl.setText(" ")
            return
        clip, src_t = ctx
        dt = M.capture_datetime(clip, src_t)
        cfg = self.st.geo()
        p = M.resolve_location(clip, src_t, self.track, cfg)
        parts = [f"🎞 {clip.name}",
                 f"📅 {dt:%d.%m.%Y %H:%M:%S}" if dt else "⚠ нет времени старта клипа"]
        if p:
            parts.append(f"📍 {p.lat:.6f}, {p.lon:.6f}")
        elif cfg.mode != "none":
            parts.append("📍 нет координат на этот момент")
        self.geo_lbl.setText("   ".join(parts))

    # ---------------- фото
    def snap_photo(self):
        ctx = self._cursor_ctx()
        if not ctx:
            return
        clip, src_t = ctx
        fmt = self.st.photo_fmt
        out_dir = self.st.photo_dir or os.path.join(os.path.dirname(clip.path), "TripCut_photos")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, M.photo_filename(clip, src_t, fmt))
        dt = M.capture_datetime(clip, src_t)
        cfg = self.st.geo()
        p = M.resolve_location(clip, src_t, self.track, cfg)
        self.statusBar().showMessage("📸 извлекаю кадр…")

        def work():
            ft.extract_frame(clip.path, src_t, out, fmt)
            if dt:
                dt_utc = datetime.fromtimestamp(p.ts, tz=timezone.utc) if p else None
                exifw.stamp_file(out, dt, p.lat if p else None, p.lon if p else None,
                                 p.ele if p else None, dt_utc=dt_utc)
            geo_s = f" · {p.lat:.5f},{p.lon:.5f}" if p else ""
            dt_s = f" · {dt:%H:%M:%S}" if dt else ""
            return f"{os.path.basename(out)}{dt_s}{geo_s}"

        _bg(self.notifier, f"photo|{clip.path}", work)

    # ---------------- компиляция
    def compile_video(self):
        if not self.project.segments:
            QMessageBox.information(self, "TripCut", "Сначала добавьте видео")
            return
        if self.quick_mode:
            QMessageBox.information(
                self, "TripCut",
                "Быстрый режим — только фото.\nНажми «В полный редактор», чтобы нарезать видео.")
            return
        if self._kf_pending and not self.st.smart_cut:
            QMessageBox.information(self, "TripCut",
                                    "Ещё идёт индексация ключевых кадров — секунду…")
            return
        first = self.project.segments[0].clip.path
        default = str(Path(first).with_name(Path(first).stem + "_cut.mp4"))
        out, _ = QFileDialog.getSaveFileName(self, "Сохранить видео", default, "MP4 (*.mp4)")
        if not out:
            return
        smart = self.st.smart_cut
        dlg = QProgressDialog("Компиляция…", None, 0, 100, self)
        dlg.setWindowTitle("TripCut")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setCancelButton(None)
        dlg.setValue(1)

        note = _Notifier()

        def prog(i, n, msg):
            note.done.emit("p", (int(i / max(n, 1) * 100), msg))

        def on_note(_tag, val):
            if isinstance(val, tuple):
                dlg.setValue(max(val[0], 1)); dlg.setLabelText(val[1])
            elif isinstance(val, Exception):
                dlg.close()
                QMessageBox.critical(self, "TripCut", f"Ошибка компиляции:\n{val}")
            else:
                dlg.setValue(100); dlg.close()
                self.statusBar().showMessage(f"✅ Готово: {out}", 10000)
                QMessageBox.information(
                    self, "TripCut",
                    f"Готово!\n{out}\n\nРежим: {'точный (smart)' if smart else 'по ключевым кадрам'}")

        note.done.connect(on_note)

        def work():
            try:
                self.project.compile(out, smart=smart, progress=prog)
                note.done.emit("done", None)
            except Exception as e:                  # noqa: BLE001
                traceback.print_exc()
                note.done.emit("err", e)

        threading.Thread(target=work, daemon=True).start()
        self._compile_refs = (dlg, note)   # держим ссылки

    # ---------------- прочее
    def open_settings(self):
        if SettingsDialog(self.st, self).exec():
            self._update_geo_label()
            self._update_status()

    def _update_status(self):
        cfg = self.st.geo()
        self.statusBar().showMessage(
            f"Фото: {self.st.photo_fmt.upper()} → "
            f"{self.st.photo_dir or 'рядом с видео (TripCut_photos)'} · "
            f"гео: {cfg.mode} · TZ камеры UTC+{cfg.utc_offset_h:g} · "
            f"рез: {'smart' if self.st.smart_cut else 'по ключевым кадрам'}")

    def closeEvent(self, ev):
        self.player.shutdown()
        super().closeEvent(ev)


def main():
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName("TripCut")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

"""Модель редактора: клипы, сегменты (оставленные интервалы), операции резки,
маппинг глобального таймлайна на исходники, компиляция и фото-штампы."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ffmpeg_tools as ft
from .geo import Track, TrackPoint

MIN_SEG = 0.15  # сегменты короче не создаём


@dataclass
class Clip:
    info: ft.ClipInfo
    keyframes: list[float] = field(default_factory=list)
    start_dt: datetime | None = None    # локальное время старта записи (правится в UI)
    own_track: Track | None = None      # GPS из самого файла (GoPro)

    @property
    def path(self) -> str:
        return self.info.path

    @property
    def name(self) -> str:
        return os.path.basename(self.info.path)


@dataclass
class Segment:
    clip: Clip
    start: float   # сек внутри исходника
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class Project:
    """Плейлист сегментов + undo. Глобальное время = сумма длительностей сегментов."""

    def __init__(self):
        self.clips: list[Clip] = []
        self.segments: list[Segment] = []
        self._undo: list[tuple[list[Clip], list[Segment]]] = []

    # ---------------- clips
    def add_clip(self, clip: Clip):
        self._push_undo()
        self.clips.append(clip)
        self.segments.append(Segment(clip, 0.0, clip.info.duration))

    def remove_clips(self, clips: list[Clip]) -> list[Clip]:
        """Убрать клипы целиком — и их самих, и все их сегменты. Одна отмена на всю пачку."""
        drop = [c for c in clips if c in self.clips]
        if not drop:
            return []
        self._push_undo()
        for c in drop:
            self.clips.remove(c)
        self.segments = [s for s in self.segments if s.clip not in drop]
        return drop

    @property
    def total(self) -> float:
        return sum(s.duration for s in self.segments)

    # ---------------- время: глобальное <-> исходник
    def locate(self, gt: float) -> tuple[Segment, float, int] | None:
        """Глобальное время -> (сегмент, время в исходнике, индекс сегмента)."""
        acc = 0.0
        for i, s in enumerate(self.segments):
            if gt <= acc + s.duration or i == len(self.segments) - 1:
                local = min(max(gt - acc, 0.0), s.duration)
                return s, s.start + local, i
            acc += s.duration
        return None

    def global_of(self, seg_index: int, src_t: float) -> float:
        acc = sum(s.duration for s in self.segments[:seg_index])
        s = self.segments[seg_index]
        return acc + min(max(src_t - s.start, 0.0), s.duration)

    # ---------------- операции
    def _push_undo(self):
        # клипы тоже, иначе отмена удаления клипа вернула бы его сегменты без него самого
        self._undo.append((list(self.clips),
                           [Segment(s.clip, s.start, s.end) for s in self.segments]))
        if len(self._undo) > 100:
            self._undo.pop(0)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.clips, self.segments = self._undo.pop()
        return True

    def trim_left(self, gt: float) -> bool:
        """q: в сегменте под курсором выбросить всё левее курсора."""
        loc = self.locate(gt)
        if not loc:
            return False
        seg, src_t, _ = loc
        if src_t - seg.start < MIN_SEG:
            return False
        self._push_undo()
        seg.start = src_t
        return True

    def trim_right(self, gt: float) -> bool:
        """w: в сегменте под курсором выбросить всё правее курсора."""
        loc = self.locate(gt)
        if not loc:
            return False
        seg, src_t, _ = loc
        if seg.end - src_t < MIN_SEG:
            return False
        self._push_undo()
        seg.end = src_t
        return True

    def split(self, gt: float) -> bool:
        """e: рассечь сегмент на два."""
        loc = self.locate(gt)
        if not loc:
            return False
        seg, src_t, i = loc
        if src_t - seg.start < MIN_SEG or seg.end - src_t < MIN_SEG:
            return False
        self._push_undo()
        right = Segment(seg.clip, src_t, seg.end)
        seg.end = src_t
        self.segments.insert(i + 1, right)
        return True

    def delete_segment(self, gt: float) -> bool:
        loc = self.locate(gt)
        if not loc or len(self.segments) <= 1:
            return False
        self._push_undo()
        self.segments.pop(loc[2])
        return True

    # ---------------- компиляция
    def compile(self, out_path: str, smart: bool, progress=None) -> None:
        """Собрать все сегменты в один файл. smart=False — рез по ключевым кадрам."""
        if not self.segments:
            raise RuntimeError("Нет сегментов")
        tmpdir = tempfile.mkdtemp(prefix="tripcut_")
        parts: list[str] = []
        n = len(self.segments)
        try:
            for i, seg in enumerate(self.segments):
                if progress:
                    progress(i, n, f"Сегмент {i + 1}/{n}")
                kf = seg.clip.keyframes
                if not smart:
                    a = ft.snap_to_keyframe(kf, seg.start, "nearest") if kf else seg.start
                    b = seg.end
                    part = os.path.join(tmpdir, f"p{i:03d}.mp4")
                    ft.cut_copy(seg.clip.path, a, b, part)
                    parts.append(part)
                else:
                    parts.extend(self._smart_parts(seg, tmpdir, i))
            if progress:
                progress(n, n, "Склейка…")
            if len(parts) == 1:
                import shutil
                shutil.move(parts[0], out_path)
            else:
                ft.concat(parts, out_path)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _smart_parts(self, seg: Segment, tmpdir: str, i: int) -> list[str]:
        """Smart-cut: голова/хвост перекодируются, середина копируется."""
        kf = seg.clip.keyframes
        info = seg.clip.info
        a, b = seg.start, seg.end
        kf_in = ft.snap_to_keyframe(kf, a, "ceil") if kf else a
        kf_out = ft.snap_to_keyframe(kf, b, "floor") if kf else b
        parts = []
        eps = 0.05
        if kf_in >= kf_out - eps:
            # сегмент внутри одного GOP — целиком перекодировать
            p = os.path.join(tmpdir, f"p{i:03d}_enc.mp4")
            ft.cut_encode(seg.clip.path, a, b, p, info)
            return [p]
        if kf_in - a > eps:
            p = os.path.join(tmpdir, f"p{i:03d}_head.mp4")
            ft.cut_encode(seg.clip.path, a, kf_in, p, info)
            parts.append(p)
        p = os.path.join(tmpdir, f"p{i:03d}_mid.mp4")
        ft.cut_copy(seg.clip.path, kf_in, kf_out, p)
        parts.append(p)
        if b - kf_out > eps:
            p = os.path.join(tmpdir, f"p{i:03d}_tail.mp4")
            ft.cut_encode(seg.clip.path, kf_out, b, p, info)
            parts.append(p)
        return parts


# ---------------------------------------------------------------- фото-штамп

@dataclass
class GeoConfig:
    mode: str = "auto"            # auto | track | camera | manual | none
    utc_offset_h: float = 3.0     # локальное время камеры = UTC + offset
    manual_lat: float | None = None
    manual_lon: float | None = None


def capture_datetime(clip: Clip, src_t: float) -> datetime | None:
    """Локальная дата-время кадра src_t внутри клипа."""
    if clip.start_dt is None:
        return None
    return clip.start_dt + timedelta(seconds=src_t)


def resolve_location(clip: Clip, src_t: float, track: Track | None,
                     cfg: GeoConfig) -> TrackPoint | None:
    """Координаты кадра по настройкам: трек / GPS камеры / ручная точка."""
    dt = capture_datetime(clip, src_t)
    ts_utc = None
    if dt is not None:
        ts_utc = dt.replace(tzinfo=timezone.utc).timestamp() - cfg.utc_offset_h * 3600

    def from_track(tr: Track | None):
        return tr.locate(ts_utc) if (tr and ts_utc is not None) else None

    if cfg.mode == "none":
        return None
    if cfg.mode == "manual":
        if cfg.manual_lat is None or cfg.manual_lon is None:
            return None
        return TrackPoint(ts_utc or 0.0, cfg.manual_lat, cfg.manual_lon)
    if cfg.mode == "track":
        return from_track(track)
    if cfg.mode == "camera":
        return from_track(clip.own_track)
    # auto: GPS камеры -> загруженный трек -> ручная точка
    p = from_track(clip.own_track) or from_track(track)
    if p is None and cfg.manual_lat is not None and cfg.manual_lon is not None:
        p = TrackPoint(ts_utc or 0.0, cfg.manual_lat, cfg.manual_lon)
    return p


def photo_filename(clip: Clip, src_t: float, fmt: str) -> str:
    base = Path(clip.name).stem
    dt = capture_datetime(clip, src_t)
    ext = "jpg" if fmt == "jpeg" else "png"
    if dt:
        return f"{base}_{dt.strftime('%Y%m%d_%H%M%S')}_{int((src_t % 1) * 1000):03d}.{ext}"
    return f"{base}_t{src_t:08.3f}.{ext}"

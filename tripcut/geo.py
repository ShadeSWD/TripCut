"""Геолокация: GPX-треки и GPMF-телеметрия GoPro (GPS5 / GPS9)."""
from __future__ import annotations

import struct
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class TrackPoint:
    ts: float          # unix UTC
    lat: float
    lon: float
    ele: float | None = None


class Track:
    """Трек, отсортированный по времени, с линейной интерполяцией по timestamp."""

    def __init__(self, points: list[TrackPoint]):
        self.points = sorted(points, key=lambda p: p.ts)
        self._ts = [p.ts for p in self.points]

    def __len__(self):
        return len(self.points)

    @property
    def start_utc(self) -> datetime | None:
        return datetime.fromtimestamp(self._ts[0], tz=timezone.utc) if self.points else None

    @property
    def end_utc(self) -> datetime | None:
        return datetime.fromtimestamp(self._ts[-1], tz=timezone.utc) if self.points else None

    def locate(self, ts_utc: float, max_gap: float = 300.0) -> TrackPoint | None:
        """Точка на момент времени. За пределами трека (или в дыре > max_gap) — None,
        но у краёв допускаем снос до max_gap."""
        if not self.points:
            return None
        i = bisect_left(self._ts, ts_utc)
        if i == 0:
            p = self.points[0]
            return p if (p.ts - ts_utc) <= max_gap else None
        if i >= len(self.points):
            p = self.points[-1]
            return p if (ts_utc - p.ts) <= max_gap else None
        a, b = self.points[i - 1], self.points[i]
        if (b.ts - a.ts) > max_gap:
            return a if (ts_utc - a.ts) <= max_gap else (b if (b.ts - ts_utc) <= max_gap else None)
        k = (ts_utc - a.ts) / (b.ts - a.ts) if b.ts > a.ts else 0.0
        ele = None
        if a.ele is not None and b.ele is not None:
            ele = a.ele + (b.ele - a.ele) * k
        return TrackPoint(ts_utc, a.lat + (b.lat - a.lat) * k,
                          a.lon + (b.lon - a.lon) * k, ele)


def load_gpx(path: str) -> Track:
    import gpxpy
    with open(path, encoding="utf-8") as f:
        gpx = gpxpy.parse(f)
    pts: list[TrackPoint] = []
    for trk in gpx.tracks:
        for seg in trk.segments:
            for p in seg.points:
                if p.time is None:
                    continue
                t = p.time if p.time.tzinfo else p.time.replace(tzinfo=timezone.utc)
                pts.append(TrackPoint(t.timestamp(), p.latitude, p.longitude, p.elevation))
    for wp in gpx.waypoints:
        if wp.time is not None:
            t = wp.time if wp.time.tzinfo else wp.time.replace(tzinfo=timezone.utc)
            pts.append(TrackPoint(t.timestamp(), wp.latitude, wp.longitude, wp.elevation))
    return Track(pts)


# ---------------------------------------------------------------- GPMF (GoPro)

_SZ = {"b": 1, "B": 1, "s": 2, "S": 2, "l": 4, "L": 4, "f": 4, "d": 8,
       "j": 8, "J": 8, "q": 4, "Q": 8, "c": 1, "U": 16}
_FMT = {"b": "b", "B": "B", "s": "h", "S": "H", "l": "i", "L": "I",
        "f": "f", "d": "d", "j": "q", "J": "Q"}


def _parse_klv(buf: memoryview, out: list, depth=0):
    """Плоский обход KLV-дерева GPMF: (fourcc, type, values)."""
    off = 0
    n = len(buf)
    while off + 8 <= n:
        fourcc = bytes(buf[off:off + 4]).decode("latin1")
        t = buf[off + 4]
        ssize = buf[off + 5]
        repeat = struct.unpack(">H", buf[off + 6:off + 8])[0]
        total = ssize * repeat
        payload = buf[off + 8: off + 8 + total]
        if t == 0:  # вложенный контейнер
            _parse_klv(payload, out, depth + 1)
        else:
            out.append((fourcc, chr(t), ssize, repeat, bytes(payload)))
        off += 8 + ((total + 3) // 4) * 4
    return out


def _decode_simple(tchar: str, ssize: int, repeat: int, payload: bytes):
    if tchar == "U":
        return [payload[i * 16:(i + 1) * 16].decode("latin1") for i in range(repeat)]
    if tchar == "c":
        return payload.decode("latin1", "replace")
    f = _FMT.get(tchar)
    if not f:
        return payload
    per = ssize // struct.calcsize(">" + f)
    vals = []
    for i in range(repeat):
        chunk = payload[i * ssize:(i + 1) * ssize]
        vals.append(struct.unpack(">" + f * per, chunk))
    return vals


def _gpsu_to_ts(s: str) -> float | None:
    # 'yymmddhhmmss.sss'
    try:
        dt = datetime.strptime(s[:12], "%y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        frac = float("0" + s[12:]) if len(s) > 12 else 0.0
        return dt.timestamp() + frac
    except ValueError:
        return None


def parse_gpmf(raw: bytes) -> Track:
    """Достаёт GPS-точки из сырого gpmd-потока. Поддержка GPS5 (+GPSU/SCAL/GPSF)
    и GPS9 (HERO11+; per-sample время, поле fix)."""
    items: list = []
    _parse_klv(memoryview(raw), items)

    pts: list[TrackPoint] = []
    scal: list[float] = []
    gpsu: float | None = None
    gpsf = 3  # считаем lock по умолчанию, если GPSF нет
    ctype = ""

    for fourcc, t, ssize, repeat, payload in items:
        if fourcc == "SCAL":
            vals = _decode_simple(t, ssize, repeat, payload)
            scal = [float(v[0]) for v in vals] if vals and isinstance(vals[0], tuple) else []
        elif fourcc == "GPSU" and t == "U":
            gpsu = _gpsu_to_ts(_decode_simple(t, ssize, repeat, payload)[0])
        elif fourcc == "GPSF":
            v = _decode_simple(t, ssize, repeat, payload)
            gpsf = int(v[0][0]) if v else 0
        elif fourcc == "TYPE":
            ctype = _decode_simple("c", ssize, repeat, payload).strip("\x00")
        elif fourcc == "GPS5":
            if gpsf == 0 or gpsu is None or len(scal) < 3:
                continue
            vals = _decode_simple(t, ssize, repeat, payload)
            for i, v in enumerate(vals):
                lat = v[0] / scal[0]
                lon = v[1] / scal[1]
                ele = v[2] / scal[2]
                # сэмплы внутри пакета ~18 Гц; растягиваем на секунду пакета
                ts = gpsu + (i / max(len(vals), 1))
                pts.append(TrackPoint(ts, lat, lon, ele))
        elif fourcc == "GPS9":
            # complex type: раскладка задаётся TYPE, обычно l,l,l,l,l,l,l,S,S
            fmt = ">" + "".join(_FMT.get(c, "x" * _SZ.get(c, 1)) for c in ctype) if ctype \
                  else ">lllllllHH"
            try:
                one = struct.calcsize(fmt)
            except struct.error:
                continue
            if one != ssize:
                fmt = ">lllllllHH"
                if struct.calcsize(fmt) != ssize:
                    continue
            sc = scal + [1.0] * (9 - len(scal))
            for i in range(repeat):
                v = struct.unpack(fmt, payload[i * ssize:(i + 1) * ssize])
                fix = v[8] if len(v) > 8 else 1
                if fix == 0:
                    continue
                lat = v[0] / sc[0]
                lon = v[1] / sc[1]
                ele = v[2] / sc[2]
                days = v[5] / sc[5]
                secs = v[6] / sc[6]
                base = datetime(2000, 1, 1, tzinfo=timezone.utc)
                ts = (base + timedelta(days=days, seconds=secs)).timestamp()
                pts.append(TrackPoint(ts, lat, lon, ele))

    # отсечь мусорные нули (нет лока)
    pts = [p for p in pts if abs(p.lat) > 1e-6 or abs(p.lon) > 1e-6]
    return Track(pts)


def gopro_track(video_path: str, gpmd_stream: int) -> Track:
    """Трек из GoPro-файла (выгрузка gpmd + парсинг)."""
    import tempfile, os
    from . import ffmpeg_tools as ft
    fd, tmp = tempfile.mkstemp(suffix=".gpmd")
    os.close(fd)
    try:
        ft.extract_data_stream(video_path, gpmd_stream, tmp)
        with open(tmp, "rb") as f:
            return parse_gpmf(f.read())
    finally:
        os.unlink(tmp)

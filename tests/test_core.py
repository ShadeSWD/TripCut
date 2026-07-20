import os, subprocess, sys, struct
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tripcut import ffmpeg_tools as ft
from tripcut import model as M
from tripcut import geo, exifw

TMP = "/tmp/claude-0/-root/122f76ce-780b-4053-ad8b-1ddb9eef00a2/scratchpad/tripcut_test"
os.makedirs(TMP, exist_ok=True)
SRC = os.path.join(TMP, "src.mp4")

# 1. тестовое видео: 20с, 30fps, keyint=30 (ключевой кадр раз в секунду), h264+aac
if not os.path.exists(SRC):
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
        "-c:v", "libx264", "-g", "30", "-keyint_min", "30", "-crf", "20",
        "-c:a", "aac", "-metadata", "creation_time=2026-07-15T14:00:00",
        "-shortest", SRC], check=True)

info = ft.probe(SRC)
print("probe:", info.vcodec, info.width, "x", info.height, f"{info.fps:.2f}fps",
      "dur", round(info.duration, 2), "ct", info.creation_time, "gpmd", info.gpmd_stream)
assert abs(info.duration - 20) < 0.5 and info.creation_time == datetime(2026, 7, 15, 14, 0, 0)

kf = ft.keyframes(SRC)
print("keyframes:", len(kf), kf[:5], "...")
assert len(kf) >= 19

clip = M.Clip(info=info, keyframes=kf, start_dt=info.creation_time)
prj = M.Project()
prj.add_clip(clip)

# 2. операции: e@5 (сплит), q@2 в левом (убрать 0-2), w@8 нет — глобал сместился!
assert prj.split(5.0)                      # [0,5) [5,20)
assert prj.trim_left(2.0)                  # [2,5) [5,20)  total=18
assert abs(prj.total - 18) < 0.01, prj.total
# глобальная 10.0 = 2-я часть src_t = 5 + (10-3) = 12; отрезаем правее
assert prj.trim_right(10.0)                # [2,5) [5,12)  total=10
assert abs(prj.total - 10) < 0.01, prj.total
seg, src_t, i = prj.locate(4.0)
assert i == 1 and abs(src_t - 6.0) < 0.01, (i, src_t)
assert prj.undo() and abs(prj.total - 18) < 0.01
assert prj.trim_right(10.0)
print("ops ok, segments:", [(round(s.start,2), round(s.end,2)) for s in prj.segments])

# 3. компиляция keyframe-режим
out1 = os.path.join(TMP, "out_kf.mp4")
prj.compile(out1, smart=False)
d1 = ft.probe(out1).duration
print("compiled kf:", round(d1, 2), "s (ожид ~10)")
assert abs(d1 - 10) < 1.5

# 4. компиляция smart (границы не на ключевых: сдвинем)
prj2 = M.Project(); clip2 = M.Clip(info=info, keyframes=kf, start_dt=info.creation_time)
prj2.add_clip(clip2)
prj2.split(5.4); prj2.trim_left(2.3); prj2.trim_right(9.6)
out2 = os.path.join(TMP, "out_smart.mp4")
prj2.compile(out2, smart=True)
i2 = ft.probe(out2)
print("compiled smart:", round(i2.duration, 2), "s (ожид ~9.6)", i2.vcodec)
assert abs(i2.duration - 9.6) < 0.4 and i2.vcodec == "h264"

# 5. кадр + EXIF + GPX
gpx_path = os.path.join(TMP, "track.gpx")
t0 = datetime(2026, 7, 15, 11, 0, 0, tzinfo=timezone.utc)  # 14:00 местного при UTC+3
pts = "".join(
    f'<trkpt lat="{55.75 + i*0.001:.6f}" lon="{37.61 + i*0.001:.6f}">'
    f'<ele>{150+i}</ele><time>{(t0 + timedelta(seconds=i*5)).strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>'
    for i in range(10))
open(gpx_path, "w").write(
    f'<?xml version="1.0"?><gpx version="1.1" creator="t"><trk><trkseg>{pts}</trkseg></trk></gpx>')
track = geo.load_gpx(gpx_path)
print("gpx:", len(track), "pts", track.start_utc, "-", track.end_utc)

cfg = M.GeoConfig(mode="track", utc_offset_h=3.0)
src_t = 7.5
loc = M.resolve_location(clip, src_t, track, cfg)
dt = M.capture_datetime(clip, src_t)
print("frame dt:", dt, "loc:", loc)
assert loc and abs(loc.lat - (55.75 + 0.0015)) < 1e-6, loc  # 7.5с = 1.5 интервала

photo = os.path.join(TMP, M.photo_filename(clip, src_t, "jpeg"))
ft.extract_frame(SRC, src_t, photo, "jpeg")
exifw.stamp_file(photo, dt, loc.lat, loc.lon, loc.ele,
                 dt_utc=datetime.fromtimestamp(loc.ts, tz=timezone.utc))
import piexif
ex = piexif.load(photo)
dto = ex["Exif"][piexif.ExifIFD.DateTimeOriginal].decode()
lat_dms = ex["GPS"][piexif.GPSIFD.GPSLatitude]
print("photo:", os.path.basename(photo), os.path.getsize(photo), "bytes; EXIF dto:", dto,
      "lat:", lat_dms)
assert dto == "2026:07:15 14:00:07"
lat_back = lat_dms[0][0]/lat_dms[0][1] + lat_dms[1][0]/lat_dms[1][1]/60 + lat_dms[2][0]/lat_dms[2][1]/3600
assert abs(lat_back - 55.7515) < 1e-4, lat_back
assert abs(os.path.getmtime(photo) - dt.timestamp()) < 2

# PNG-ветка
photo_png = os.path.join(TMP, M.photo_filename(clip, src_t, "png"))
ft.extract_frame(SRC, src_t, photo_png, "png")
exifw.stamp_file(photo_png, dt, loc.lat, loc.lon)
from PIL import Image
assert Image.open(photo_png).getexif().get(306) is not None  # DateTime
print("png ok:", os.path.getsize(photo_png), "bytes")

# 6. GPMF-парсер на синтетическом буфере (GPS5)
def klv(fourcc, tchar, ssize, repeat, payload):
    pad = (4 - len(payload) % 4) % 4
    return fourcc.encode() + bytes([0 if tchar is None else ord(tchar), ssize]) + \
           struct.pack(">H", repeat) + payload + b"\x00" * pad

gps5 = struct.pack(">5l", int(55.75e7), int(37.61e7), 150000, 0, 0)
strm_inner = (
    klv("GPSU", "U", 16, 1, b"260715110000.000") +
    klv("GPSF", "L", 4, 1, struct.pack(">L", 3)) +
    klv("SCAL", "l", 4, 5, struct.pack(">5l", 10**7, 10**7, 1000, 1000, 100)) +
    klv("GPS5", "l", 20, 1, gps5))
raw = klv("DEVC", None, 1, len(klv("STRM", None, 1, len(strm_inner), strm_inner)),
          klv("STRM", None, 1, len(strm_inner), strm_inner))
tr = geo.parse_gpmf(raw)
assert len(tr) == 1 and abs(tr.points[0].lat - 55.75) < 1e-6, tr.points
print("gpmf synthetic ok:", tr.points[0])

print("\nALL CORE TESTS PASSED")

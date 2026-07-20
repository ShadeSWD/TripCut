"""Обёртки над ffmpeg/ffprobe: пробинг, ключевые кадры, рез без перекодирования,
smart-cut, извлечение кадра, склейка."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# На Windows кладём ffmpeg.exe/ffprobe.exe рядом с программой (папка bin/)
def _tool(name: str) -> str:
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    exe = name + (".exe" if os.name == "nt" else "")
    for cand in (here / "bin" / exe, here / exe):
        if cand.exists():
            return str(cand)
    return name  # системный

FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")

_POPEN_KW: dict = {}
if os.name == "nt":  # не мигать консольными окнами
    _POPEN_KW["creationflags"] = 0x08000000  # CREATE_NO_WINDOW


def _run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          errors="replace", **_POPEN_KW)


@dataclass
class ClipInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    pix_fmt: str
    profile: str
    acodec: str | None
    sample_rate: int | None
    creation_time: datetime | None      # как записала камера (наивное локальное время)
    gpmd_stream: int | None             # индекс потока GoPro-телеметрии, если есть
    color: dict = field(default_factory=dict)


def probe(path: str) -> ClipInfo:
    p = _run([FFPROBE, "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", path])
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe: {p.stderr.strip()[:400]}")
    data = json.loads(p.stdout)
    v = next(s for s in data["streams"] if s.get("codec_type") == "video")
    a = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    gpmd = next((s["index"] for s in data["streams"]
                 if s.get("codec_tag_string") == "gpmd"), None)

    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    fps = (float(num) / float(den)) if float(den) else 0.0

    ct = None
    raw = (v.get("tags") or {}).get("creation_time") or \
          (data["format"].get("tags") or {}).get("creation_time")
    if raw:
        try:
            # камера пишет своё выставленное время; трактуем как локальное, зону отбрасываем
            ct = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass

    color = {k: v[k] for k in ("color_space", "color_transfer", "color_primaries", "color_range")
             if v.get(k)}

    return ClipInfo(
        path=path,
        duration=float(data["format"]["duration"]),
        width=v["width"], height=v["height"], fps=fps,
        vcodec=v["codec_name"], pix_fmt=v.get("pix_fmt", "yuv420p"),
        profile=v.get("profile", ""),
        acodec=a["codec_name"] if a else None,
        sample_rate=int(a["sample_rate"]) if a else None,
        creation_time=ct, gpmd_stream=gpmd, color=color,
    )


def keyframes(path: str, progress=None) -> list[float]:
    """Времена всех ключевых кадров видеопотока (сек). Быстрый проход по пакетам."""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time,dts_time,flags",
           "-of", "csv=p=0", path]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, errors="replace", **_POPEN_KW)
    kf: list[float] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        parts = line.strip().split(",")
        if len(parts) >= 3 and "K" in parts[2]:
            t = parts[0] if parts[0] not in ("N/A", "") else parts[1]
            try:
                kf.append(float(t))
            except ValueError:
                continue
            if progress and len(kf) % 200 == 0:
                progress(kf[-1])
    proc.wait()
    kf.sort()
    return kf


def snap_to_keyframe(kf: list[float], t: float, mode: str) -> float:
    """mode: 'floor' | 'ceil' | 'nearest'."""
    if not kf:
        return t
    import bisect
    i = bisect.bisect_right(kf, t + 1e-4)
    lo = kf[i - 1] if i > 0 else kf[0]
    hi = kf[i] if i < len(kf) else kf[-1]
    if mode == "floor":
        return lo
    if mode == "ceil":
        return hi if hi >= t - 1e-4 else lo
    return lo if (t - lo) <= (hi - t) else hi


def cut_copy(src: str, start: float, end: float, out: str) -> None:
    """Рез без перекодирования. start должен быть ключевым кадром."""
    p = _run([FFMPEG, "-y", "-v", "error",
              "-ss", f"{start:.6f}", "-to", f"{end:.6f}", "-i", src,
              "-map", "0:v:0?", "-map", "0:a:0?",
              "-c", "copy", "-avoid_negative_ts", "make_zero",
              "-movflags", "+faststart", out])
    if p.returncode != 0:
        raise RuntimeError(f"cut_copy: {p.stderr.strip()[:400]}")


def _encoder_args(info: ClipInfo) -> list[str]:
    if info.vcodec == "hevc":
        args = ["-c:v", "libx265", "-crf", "14", "-preset", "fast",
                "-tag:v", "hvc1"]
    else:
        args = ["-c:v", "libx264", "-crf", "14", "-preset", "fast"]
        prof = (info.profile or "").lower().replace(" ", "")
        if prof in ("baseline", "main", "high"):
            args += ["-profile:v", prof]
    args += ["-pix_fmt", info.pix_fmt]
    cs = info.color
    if cs.get("color_space"):
        args += ["-colorspace", cs["color_space"]]
    if cs.get("color_primaries"):
        args += ["-color_primaries", cs["color_primaries"]]
    if cs.get("color_transfer"):
        args += ["-color_trc", cs["color_transfer"]]
    return args


def cut_encode(src: str, start: float, end: float, out: str, info: ClipInfo) -> None:
    """Точный (перекодирующий) рез маленького кусочка для smart-cut."""
    cmd = [FFMPEG, "-y", "-v", "error",
           "-ss", f"{start:.6f}", "-to", f"{end:.6f}", "-i", src,
           "-map", "0:v:0?", "-map", "0:a:0?"] + _encoder_args(info)
    if info.acodec:
        cmd += ["-c:a", "aac", "-b:a", "256k"]
    cmd += ["-avoid_negative_ts", "make_zero", out]
    p = _run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"cut_encode: {p.stderr.strip()[:400]}")


def concat(parts: list[str], out: str) -> None:
    """Склейка однотипных частей без перекодирования (concat demuxer)."""
    lst = Path(out).with_suffix(".concat.txt")
    esc = lambda s: s.replace("'", r"'\''")
    lst.write_text("".join(f"file '{esc(os.path.abspath(x))}'\n" for x in parts),
                   encoding="utf-8")
    p = _run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
              "-i", str(lst), "-c", "copy", "-movflags", "+faststart", out])
    lst.unlink(missing_ok=True)
    if p.returncode != 0:
        raise RuntimeError(f"concat: {p.stderr.strip()[:400]}")


def extract_frame(src: str, t: float, out: str, fmt: str = "jpeg") -> None:
    """Кадр в максимальном качестве. fmt: 'jpeg' | 'png'."""
    cmd = [FFMPEG, "-y", "-v", "error", "-ss", f"{t:.6f}", "-i", src,
           "-map", "0:v:0", "-frames:v", "1"]
    if fmt == "jpeg":
        cmd += ["-q:v", "1", "-pix_fmt", "yuvj420p"]
    cmd += [out]
    p = _run(cmd, timeout=120)
    if p.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"extract_frame: {p.stderr.strip()[:400]}")


def extract_data_stream(src: str, stream_index: int, out_bin: str) -> None:
    """Выгрузка сырого потока телеметрии (gpmd) в файл."""
    p = _run([FFMPEG, "-y", "-v", "error", "-i", src,
              "-map", f"0:{stream_index}", "-c", "copy", "-f", "data", out_bin])
    if p.returncode != 0:
        raise RuntimeError(f"extract_data: {p.stderr.strip()[:400]}")

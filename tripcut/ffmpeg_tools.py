"""Обёртки над ffmpeg/ffprobe: пробинг, ключевые кадры, рез без перекодирования,
smart-cut, извлечение кадра, склейка."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    creation_time: datetime | None      # локальное время камеры (наивное), см. probe()
    gpmd_stream: int | None             # индекс потока GoPro-телеметрии, если есть
    color: dict = field(default_factory=dict)


def probe(path: str, utc_offset_h: float = 0.0) -> ClipInfo:
    """utc_offset_h — часовой пояс камеры: creation_time из контейнера приводится к нему."""
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
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            # MP4 хранит creation_time в UTC (DJI/GoPro пишут с "Z") — переводим в пояс
            # камеры. Если зоны нет, камера уже записала своё локальное время.
            if dt.tzinfo is not None:
                dt = (dt.astimezone(timezone.utc).replace(tzinfo=None)
                      + timedelta(hours=utc_offset_h))
            ct = dt

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


@dataclass(frozen=True)
class Format:
    """Параметры, которые обязаны совпадать у частей, чтобы их можно было
    склеить без перекодирования (concat demuxer + -c copy)."""
    width: int
    height: int
    fps: float
    vcodec: str
    pix_fmt: str
    acodec: str | None
    sample_rate: int | None

    def __str__(self) -> str:
        a = f"{self.acodec} {self.sample_rate}Hz" if self.acodec else "без звука"
        return f"{self.width}x{self.height} {self.fps:g}fps {self.vcodec} · {a}"


def format_of(info: ClipInfo) -> Format:
    return Format(width=info.width, height=info.height, fps=round(info.fps, 3),
                  vcodec=info.vcodec, pix_fmt=info.pix_fmt,
                  acodec=info.acodec, sample_rate=info.sample_rate)


def same_format(info: ClipInfo, target: Format) -> bool:
    return format_of(info) == target


def choose_target(items: list[tuple[ClipInfo, float]]) -> Format:
    """Целевой формат сборки: тот, которым снята наибольшая часть монтажа
    (по суммарной длительности). Остальное приводится к нему перекодированием."""
    if not items:
        raise ValueError("нечего собирать")
    weight: dict[Format, float] = {}
    order: list[Format] = []
    for info, dur in items:
        f = format_of(info)
        if f not in weight:
            weight[f] = 0.0
            order.append(f)
        weight[f] += max(dur, 0.0)
    return max(order, key=lambda f: (weight[f], -order.index(f)))


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


HW_ACCEL = True          # пробовать аппаратный кодировщик (ставится из настроек)
_HW_CACHE: dict[tuple, str | None] = {}

# кандидаты по убыванию качества/предпочтения; пригодность проверяется пробой
_HW_CANDIDATES = {
    "hevc": ["hevc_qsv", "hevc_nvenc", "hevc_amf"],
    "h264": ["h264_qsv", "h264_nvenc", "h264_amf"],
}


def _hw_quality_args(name: str) -> list[str]:
    if name.endswith("_qsv"):
        return ["-global_quality", "18"]
    if name.endswith("_nvenc"):
        return ["-preset", "p5", "-rc", "constqp", "-qp", "18"]
    return ["-rc", "cqp", "-qp_i", "18", "-qp_p", "18"]      # amf


def hw_encoder(vcodec: str, width: int, height: int, pix_fmt: str) -> str | None:
    """Аппаратный кодировщик, который реально работает на этой машине и на этом
    размере кадра. Наличия в списке ffmpeg мало: nvenc, например, отказывает при
    старом драйвере, а у QSV/AMF бывает предел разрешения — поэтому пробуем."""
    if not HW_ACCEL:
        return None
    key = (vcodec, width, height, pix_fmt)
    if key in _HW_CACHE:
        return _HW_CACHE[key]
    have = _run([FFMPEG, "-v", "error", "-hide_banner", "-encoders"]).stdout
    found = None
    for name in _HW_CANDIDATES.get(vcodec, []):
        if name not in have:
            continue
        p = _run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                  f"testsrc2=size={width}x{height}:rate=30:duration=0.2",
                  "-c:v", name, *_hw_quality_args(name), "-pix_fmt", pix_fmt,
                  "-frames:v", "2", "-f", "null", "-"], timeout=90)
        if p.returncode == 0:
            found = name
            break
    _HW_CACHE[key] = found
    return found


def used_encoder(target: Format) -> str:
    """Каким кодировщиком шло перекодирование (после компиляции — для отчёта)."""
    hw = _HW_CACHE.get((target.vcodec, target.width, target.height, target.pix_fmt))
    return hw or ("libx265" if target.vcodec == "hevc" else "libx264")


def _encoder_args(info: ClipInfo, target: Format | None = None) -> list[str]:
    vcodec = target.vcodec if target else info.vcodec
    pix_fmt = target.pix_fmt if target else info.pix_fmt
    hw = hw_encoder(vcodec, target.width if target else info.width,
                    target.height if target else info.height, pix_fmt)
    if hw:
        args = ["-c:v", hw, *_hw_quality_args(hw), "-pix_fmt", pix_fmt,
                "-color_range", "pc" if _range_of(pix_fmt) == "full" else "tv"]
        if vcodec == "hevc":
            args += ["-tag:v", "hvc1"]
        cs = info.color
        if cs.get("color_primaries"):
            args += ["-color_primaries", cs["color_primaries"]]
        if cs.get("color_transfer"):
            args += ["-color_trc", cs["color_transfer"]]
        if cs.get("color_space"):
            args += ["-colorspace", cs["color_space"]]
        return args
    if vcodec == "hevc":
        args = ["-c:v", "libx265", "-crf", "14", "-preset", "fast",
                "-tag:v", "hvc1"]
    else:
        args = ["-c:v", "libx264", "-crf", "14", "-preset", "fast"]
        prof = (info.profile or "").lower().replace(" ", "")
        if prof in ("baseline", "main", "high"):
            args += ["-profile:v", prof]
    args += ["-pix_fmt", pix_fmt,
             "-color_range", "pc" if _range_of(pix_fmt) == "full" else "tv"]
    cs = info.color
    if cs.get("color_space"):
        args += ["-colorspace", cs["color_space"]]
    if cs.get("color_primaries"):
        args += ["-color_primaries", cs["color_primaries"]]
    if cs.get("color_transfer"):
        args += ["-color_trc", cs["color_transfer"]]
    return args


def _range_of(pix_fmt: str) -> str:
    """yuvj420p и т.п. — полный диапазон (0-255), остальное — телевизионный."""
    return "full" if (pix_fmt or "").startswith("yuvj") else "limited"


def _video_filters(info: ClipInfo, target: Format | None, dur: float,
                   fade_in: float, fade_out: float) -> list[str]:
    """Приведение к целевому формату + затемнение на стыках."""
    vf: list[str] = []
    if target and (info.width, info.height) != (target.width, target.height):
        # вписываем кадр целиком и добиваем чёрным — без искажения пропорций
        vf.append(f"scale={target.width}:{target.height}"
                  ":force_original_aspect_ratio=decrease:flags=lanczos")
        vf.append(f"pad={target.width}:{target.height}:(ow-iw)/2:(oh-ih)/2:color=black")
        vf.append("setsar=1")
    if target and _range_of(info.pix_fmt) != _range_of(target.pix_fmt):
        # GoPro пишет полный диапазон, телефон — телевизионный. Без явного перевода
        # кодировщик оставляет диапазон источника, и на стыке скачут яркость с контрастом
        rng = _range_of(target.pix_fmt)
        if vf and vf[0].startswith("scale="):
            vf[0] += f":in_range=auto:out_range={rng}"
        else:
            vf.append(f"scale=in_range=auto:out_range={rng}")
    if target and target.fps > 0 and abs(info.fps - target.fps) > 0.01:
        vf.append(f"fps={target.fps:.6f}")
    if fade_in > 0:
        vf.append(f"fade=t=in:st=0:d={fade_in:.3f}:color=black")
    if fade_out > 0:
        vf.append(f"fade=t=out:st={max(dur - fade_out, 0.0):.3f}:d={fade_out:.3f}"
                  ":color=black")
    return vf


def _audio_filters(dur: float, fade_in: float, fade_out: float) -> list[str]:
    af: list[str] = []
    if fade_in > 0:
        af.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        af.append(f"afade=t=out:st={max(dur - fade_out, 0.0):.3f}:d={fade_out:.3f}")
    return af


def cut_encode(src: str, start: float, end: float, out: str, info: ClipInfo,
               target: Format | None = None,
               fade_in: float = 0.0, fade_out: float = 0.0) -> None:
    """Перекодирующий рез: smart-cut, приведение к целевому формату, затемнения.
    fade_in/fade_out — длительность затемнения у начала/конца получаемого кусочка."""
    dur = max(end - start, 0.0)
    cmd = [FFMPEG, "-y", "-v", "error",
           "-ss", f"{start:.6f}", "-to", f"{end:.6f}", "-i", src]
    silent = target is not None and target.acodec and not info.acodec
    if silent:                      # у источника нет звука, а дорожка нужна для склейки
        cmd += ["-f", "lavfi", "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={target.sample_rate or 48000}",
                "-shortest"]
        cmd += ["-map", "0:v:0?", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
    vf = _video_filters(info, target, dur, fade_in, fade_out)
    af = _audio_filters(dur, fade_in, fade_out)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if af and (info.acodec or silent):
        cmd += ["-af", ",".join(af)]
    cmd += _encoder_args(info, target)
    if info.acodec or silent:
        cmd += ["-c:a", "aac", "-b:a", "256k"]
        if target and target.sample_rate:
            cmd += ["-ar", str(target.sample_rate), "-ac", "2"]
    cmd += ["-avoid_negative_ts", "make_zero", out]
    p = _run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"cut_encode: {p.stderr.strip()[:400]}")


def transcode_to(src: str, out: str, target: Format, info: ClipInfo | None = None) -> None:
    """Перекодировать файл целиком в целевой формат (масштаб + поля, fps, звук)."""
    info = info or probe(src)
    cmd = [FFMPEG, "-y", "-v", "error", "-i", src]
    silent = bool(target.acodec) and not info.acodec
    if silent:
        cmd += ["-f", "lavfi", "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={target.sample_rate or 48000}",
                "-shortest", "-map", "0:v:0?", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
    vf = _video_filters(info, target, info.duration, 0.0, 0.0)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += _encoder_args(info, target)
    if info.acodec or silent:
        cmd += ["-c:a", "aac", "-b:a", "256k"]
        if target.sample_rate:
            cmd += ["-ar", str(target.sample_rate), "-ac", "2"]
    cmd += ["-avoid_negative_ts", "make_zero", out]
    p = _run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"transcode: {p.stderr.strip()[:400]}")


# что обязано совпадать у частей, иначе copy-склейка молча портит таймстампы
_JOIN_KEY = lambda f: (f.width, f.height, f.vcodec, f.pix_fmt, f.acodec, f.sample_rate)


def concat(parts: list[str], out: str, progress=None) -> None:
    """Склейка частей. Однотипные — без перекодирования; если формат разъехался
    (разное разрешение/fps-семейство/частота звука), выбивающиеся части сначала
    приводятся к преобладающему формату — иначе ffmpeg молча выдаёт битый файл."""
    infos = [probe(p) for p in parts]
    fmts = [format_of(i) for i in infos]
    weight: dict[tuple, float] = {}
    for f, i in zip(fmts, infos):
        weight[_JOIN_KEY(f)] = weight.get(_JOIN_KEY(f), 0.0) + max(i.duration, 0.0)
    if len(weight) > 1:
        win = max(weight, key=weight.get)
        target = next(f for f in fmts if _JOIN_KEY(f) == win)
        fixed: list[str] = []
        odd = [k for k, (f, _) in enumerate(zip(fmts, infos)) if _JOIN_KEY(f) != win]
        for k, (part, info, f) in enumerate(zip(parts, infos, fmts)):
            if _JOIN_KEY(f) == win:
                fixed.append(part)
                continue
            if progress:
                progress(f"Приведение части {odd.index(k) + 1}/{len(odd)} "
                         f"({f}) к {target}")
            norm = str(Path(part).with_name(Path(part).stem + "_norm.mp4"))
            transcode_to(part, norm, target, info)
            fixed.append(norm)
        parts = fixed

    lst = Path(out).with_suffix(".concat.txt")
    esc = lambda s: s.replace("'", r"'\''")
    lst.write_text("".join(f"file '{esc(os.path.abspath(x))}'\n" for x in parts),
                   encoding="utf-8")
    p = _run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
              "-i", str(lst), "-c", "copy", "-movflags", "+faststart", out])
    lst.unlink(missing_ok=True)
    if p.returncode != 0:
        raise RuntimeError(f"concat: {p.stderr.strip()[:400]}")
    check_result(out)


def check_result(path: str) -> None:
    """Проверка склейки: ffmpeg при рассинхроне таймстампов не возвращает ошибку,
    но видеодорожка обрывается раньше звука — ловим это здесь."""
    p = _run([FFPROBE, "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", path])
    if p.returncode != 0:
        raise RuntimeError(f"результат не читается: {p.stderr.strip()[:200]}")
    data = json.loads(p.stdout)
    total = float(data["format"].get("duration") or 0.0)
    v = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if v is None or total <= 0:
        raise RuntimeError("в результате нет видеодорожки")
    vdur = float(v.get("duration") or 0.0)
    if vdur and vdur < total - 1.0:
        raise RuntimeError(
            f"склейка испортила таймстампы: видео обрывается на {vdur:.1f} с "
            f"при общей длине {total:.1f} с. Обычно так бывает, когда куски сняты "
            f"в разных форматах")


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

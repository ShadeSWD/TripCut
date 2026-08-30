# -*- coding: utf-8 -*-
"""Тесты сборки: разнородные источники и переходы-затемнения.
Нужен ffmpeg/ffprobe (берутся из bin/ или из PATH). Запуск:
    python tests/test_compile.py
Материал генерируется через lavfi — маленький, тест идёт ~1-2 мин."""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tripcut import ffmpeg_tools as ft      # noqa: E402
from tripcut import model as M              # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "tripcut_test_compile")
os.makedirs(TMP, exist_ok=True)


def make(name, *, w, h, fps, sec, ar, color="red", codec="libx264"):
    """Тестовый ролик: движущаяся полоса, чтобы кадры отличались друг от друга."""
    path = os.path.join(TMP, name)
    if os.path.exists(path):
        return path
    subprocess.run([
        ft.FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={sec}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={sec}:sample_rate={ar}",
        "-c:v", codec, "-g", str(int(round(float(fps)))), "-keyint_min",
        str(int(round(float(fps)))), "-crf", "28", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", str(ar), "-ac", "2", "-shortest", path], check=True)
    return path


def clip(path):
    info = ft.probe(path)
    return M.Clip(info=info, keyframes=ft.keyframes(path), start_dt=info.creation_time)


def vinfo(path):
    p = subprocess.run([ft.FFPROBE, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", path],
                       capture_output=True, text=True)
    d = json.loads(p.stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return {"total": float(d["format"]["duration"]),
            "vdur": float(v.get("duration") or 0), "w": v["width"], "h": v["height"],
            "adur": float(a.get("duration") or 0) if a else 0.0,
            "ar": int(a["sample_rate"]) if a else 0}


def brightness(path, t):
    """Средняя яркость кадра в момент t (0..255) — так видно затемнение.
    Кадр забираем сырым в 32x32 grayscale, чтобы не связываться с фильтрами."""
    p = subprocess.run([ft.FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", path,
                        "-frames:v", "1", "-vf", "scale=32:32", "-pix_fmt", "gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"кадр {t:.3f} не читается: {p.stderr.decode(errors='replace')[:200]}")
    data = p.stdout[:32 * 32]
    return sum(data) / len(data)


# 1. разнородные источники — тот же случай, что сломал сборку: 4K60/44100 + 30fps/48000
a = make("a_1280x720_60_44100.mp4", w=1280, h=720, fps=60, sec=6, ar=44100)
b = make("b_960x720_30_48000.mp4", w=960, h=720, fps=30, sec=6, ar=48000)

prj = M.Project()
ca, cb = clip(a), clip(b)
prj.add_clip(ca)
prj.add_clip(cb)
tgt = prj.target_format()
print("target:", tgt)
assert len(prj.foreign_segments()) == 1, "один из двух кусков должен быть чужеродным"

out1 = os.path.join(TMP, "out_mixed.mp4")
prj.compile(out1, smart=False)
i1 = vinfo(out1)
print("mixed ->", i1)
# главное: видеодорожка не обрывается раньше звука (раньше было 68с против 317с)
assert abs(i1["vdur"] - i1["total"]) < 1.0, i1
assert abs(i1["total"] - 12) < 1.0, i1
assert (i1["w"], i1["h"]) == (tgt.width, tgt.height)
assert i1["ar"] == tgt.sample_rate
print("mixed-format compile: OK")

# 2. однородные источники по-прежнему склеиваются копированием (быстро, без потерь)
c = make("c_1280x720_60_44100.mp4", w=1280, h=720, fps=60, sec=6, ar=44100)
prj2 = M.Project()
prj2.add_clip(clip(a))
prj2.add_clip(clip(c))
assert prj2.foreign_segments() == []
out2 = os.path.join(TMP, "out_same.mp4")
prj2.compile(out2, smart=False)
i2 = vinfo(out2)
print("same ->", i2)
assert abs(i2["vdur"] - i2["total"]) < 0.5 and abs(i2["total"] - 12) < 0.7, i2
print("uniform compile: OK")

# 3. переходы: на стыке — затемнение, длина монтажа не меняется
prj3 = M.Project()
prj3.add_clip(clip(a))
prj3.add_clip(clip(c))
out3 = os.path.join(TMP, "out_fade.mp4")
prj3.compile(out3, smart=False, transition=0.4)
i3 = vinfo(out3)
print("fade ->", i3)
assert abs(i3["vdur"] - i3["total"]) < 0.5, i3
assert abs(i3["total"] - i2["total"]) < 0.35, (i3, i2)   # содержимое не съедено
join = i2["total"] / 2                                   # стык примерно посередине
dark = brightness(out3, join - 0.02)
mid_a = brightness(out3, join - 2.0)
mid_b = brightness(out3, join + 2.0)
print(f"яркость: середина A={mid_a:.1f} стык={dark:.1f} середина B={mid_b:.1f}")
assert dark < mid_a * 0.5 and dark < mid_b * 0.5, (dark, mid_a, mid_b)
assert brightness(out3, 0.05) > mid_a * 0.5, "начало фильма затемнять не должны"
assert brightness(out3, i3["total"] - 0.1) > mid_b * 0.5, "и конец тоже"
print("transitions: OK")

# 4. без переходов стык не темнеет — проверка, что тест ловит именно затемнение
assert brightness(out2, join - 0.02) > mid_a * 0.5
print("no-transition control: OK")

# 5. страховка в concat: части разного формата не дают молча битый файл
p_a = os.path.join(TMP, "part_a.mp4")
p_b = os.path.join(TMP, "part_b.mp4")
ft.cut_copy(a, 0.0, 3.0, p_a)
ft.cut_copy(b, 0.0, 3.0, p_b)
out5 = os.path.join(TMP, "out_guard.mp4")
ft.concat([p_a, p_b], out5)          # внутри приведёт часть к общему формату
i5 = vinfo(out5)
print("guard ->", i5)
assert abs(i5["vdur"] - i5["total"]) < 0.7, i5
print("concat guard: OK")

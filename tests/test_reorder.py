# -*- coding: utf-8 -*-
"""Тесты смены порядка кусков и клипов. ffmpeg не нужен; Qt-часть — offscreen
(пропускается, если PySide6 не установлен). Запуск: python tests/test_reorder.py"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tripcut import ffmpeg_tools as ft      # noqa: E402
from tripcut import model as M              # noqa: E402


def mk(name: str, dur: float) -> M.Clip:
    info = ft.ClipInfo(path=name, duration=dur, width=1920, height=1080, fps=30,
                       vcodec="h264", pix_fmt="yuv420p", profile="high", acodec="aac",
                       sample_rate=48000, creation_time=None, gpmd_stream=None)
    return M.Clip(info=info)


def names(prj):
    return [s.clip.name for s in prj.segments]


# ---------------------------------------------------------------- модель
prj = M.Project()
a, b, c = mk("a.mp4", 10), mk("b.mp4", 20), mk("c.mp4", 30)
for cl in (a, b, c):
    prj.add_clip(cl)
assert names(prj) == ["a.mp4", "b.mp4", "c.mp4"]

assert prj.move_segment(0, 2)                       # a в конец
assert names(prj) == ["b.mp4", "c.mp4", "a.mp4"], names(prj)
assert abs(prj.total - 60) < 1e-6                   # длительность не меняется
seg, src_t, i = prj.locate(55.0)                    # b 0-20, c 20-50, a 50-60
assert seg.clip is a and abs(src_t - 5.0) < 1e-6 and i == 2
assert prj.undo() and names(prj) == ["a.mp4", "b.mp4", "c.mp4"]

assert not prj.move_segment(1, 1)                   # на своё место — не операция
assert prj.move_segment(2, 0) and names(prj) == ["c.mp4", "a.mp4", "b.mp4"]
assert prj.move_segment(0, 99) and names(prj) == ["a.mp4", "b.mp4", "c.mp4"]   # клип по краю
assert prj.undo() and prj.undo()

prj2 = M.Project()
x, y = mk("x.mp4", 10), mk("y.mp4", 10)
prj2.add_clip(x)
prj2.add_clip(y)
assert prj2.split(5.0)                              # x[0,5) x[5,10) y[0,10)
assert prj2.move_segment(2, 1)                      # куски одного клипа могут разъехаться
assert names(prj2) == ["x.mp4", "y.mp4", "x.mp4"]

assert prj2.reorder_clips([y, x])                   # клипы: куски едут группой
assert names(prj2) == ["y.mp4", "x.mp4", "x.mp4"]
assert [round(s.start, 2) for s in prj2.segments] == [0.0, 0.0, 5.0]
assert not prj2.reorder_clips([y, x])               # тот же порядок
assert not prj2.reorder_clips([y])                  # неполный список
assert prj2.undo() and names(prj2) == ["x.mp4", "y.mp4", "x.mp4"] and prj2.clips == [x, y]

prj3 = M.Project()
prj3.add_clip(mk("z.mp4", 5))
assert not prj3.move_segment(0, 1)                  # единственный кусок не двигаем
print("model reorder: OK")

# ---------------------------------------------------------------- Qt
try:
    from PySide6.QtCore import Qt, QEvent, QModelIndex, QPoint, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QAbstractItemView, QApplication, QListWidgetItem
except ImportError:                                  # noqa: BLE001
    print("PySide6 нет — Qt-часть пропущена")
    raise SystemExit(0)

from tripcut.timeline import TimelineWidget, TOP, BLOCK_H   # noqa: E402
from tripcut.gui import ClipListWidget                      # noqa: E402

app = QApplication([])


def mev(kind, x, y, btn=Qt.LeftButton, buttons=Qt.LeftButton, mods=Qt.NoModifier):
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), btn, buttons, mods)


def drag(tl, x0, x1, y, mods=Qt.NoModifier):
    tl.mousePressEvent(mev(QEvent.MouseButtonPress, x0, y, mods=mods))
    tl.mouseMoveEvent(mev(QEvent.MouseMove, (x0 + x1) // 2, y, mods=mods))
    tl.mouseMoveEvent(mev(QEvent.MouseMove, x1, y, mods=mods))
    tl.render(tl.grab())                             # отрисовка призрака не падает
    tl.mouseReleaseEvent(mev(QEvent.MouseButtonRelease, x1, y, buttons=Qt.NoButton))


tprj = M.Project()
for n in ("a.mp4", "b.mp4", "c.mp4"):
    tprj.add_clip(mk(n, 10.0))
tl = TimelineWidget(tprj)
tl.resize(308, 78)                                   # рабочая ширина 300 -> по 100 px
moves, seeks = [], []
tl.reorder_requested.connect(lambda s, d: moves.append((s, d)))
tl.seek_requested.connect(seeks.append)
grip_y, body_y = TOP + 2, TOP + BLOCK_H - 4

tl.mousePressEvent(mev(QEvent.MouseButtonPress, 154, body_y))    # клик по телу = seek
tl.mouseReleaseEvent(mev(QEvent.MouseButtonRelease, 154, body_y, buttons=Qt.NoButton))
assert seeks and abs(seeks[-1] - 15.0) < 0.5 and not moves

drag(tl, 50, 290, grip_y)
assert moves == [(0, 2)], moves
moves.clear()
drag(tl, 250, 10, grip_y)
assert moves == [(2, 0)], moves
moves.clear()
drag(tl, 150, 160, grip_y)                           # бросили там же
assert moves == []
seeks.clear()
drag(tl, 50, 290, body_y, mods=Qt.AltModifier)       # Alt+тяга по телу
assert moves == [(0, 2)] and not seeks
assert tl._drag_from is None and tl._press_x is None

solo = M.Project()
solo.add_clip(mk("solo.mp4", 10))
tl1 = TimelineWidget(solo)
tl1.resize(308, 78)
one = []
tl1.reorder_requested.connect(lambda s, d: one.append((s, d)))
drag(tl1, 50, 250, grip_y)
assert not one                                       # один кусок — нечего переставлять
print("timeline drag: OK")


class _FakeDrop:
    """Минимальный QDropEvent для проверки арифметики позиции вставки."""

    def __init__(self, src):
        self._src, self.accepted, self.ignored, self.action = src, False, False, None

    def source(self):
        return self._src

    def position(self):
        return type("P", (), {"toPoint": staticmethod(lambda: QPoint(0, 0))})()

    def setDropAction(self, act):
        self.action = act

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


def drop(items, rows, at, below):
    """at = -1 — бросок в пустоту под списком."""
    lst = ClipListWidget()
    for n in items:
        it = QListWidgetItem(n)
        it.setData(Qt.UserRole, n)                   # вместо Clip — строка
        lst.addItem(it)
    for r in rows:
        lst.item(r).setSelected(True)
    lst.indexAt = lambda _p: lst.model().index(at, 0) if at >= 0 else QModelIndex()
    lst.dropIndicatorPosition = lambda: (QAbstractItemView.BelowItem if below
                                         else QAbstractItemView.AboveItem)
    out = []
    lst.order_changed.connect(out.append)
    e = _FakeDrop(lst)
    lst.dropEvent(e)
    # IgnoreAction обязателен: иначе Qt сам удалит перенесённые строки после drop
    assert e.accepted and not e.ignored and e.action == Qt.IgnoreAction
    return out[0] if out else None


L = ["a", "b", "c", "d"]
assert drop(L, [0], 2, False) == ["b", "a", "c", "d"]
assert drop(L, [0], 2, True) == ["b", "c", "a", "d"]
assert drop(L, [3], 0, False) == ["d", "a", "b", "c"]
assert drop(L, [0], -1, False) == ["b", "c", "d", "a"]        # в конец списка
assert drop(L, [0, 1], 3, True) == ["c", "d", "a", "b"]       # группой
assert drop(L, [1, 3], 0, False) == ["b", "d", "a", "c"]      # несмежная группа
assert drop(L, [1], 1, False) == ["a", "b", "c", "d"]         # на своё же место

foreign = ClipListWidget()
foreign.addItem(QListWidgetItem("x"))
ev_foreign = _FakeDrop(object())
foreign.dropEvent(ev_foreign)
assert ev_foreign.ignored and not ev_foreign.accepted          # чужой источник
print("clip list drop: OK")

# ---------------------------------------------------------------- окно целиком
from tripcut import gui as G                          # noqa: E402

G.PlayerWidget.init_mpv = lambda self: None           # без libmpv
G.PlayerWidget.load = lambda self, path, start=0.0: setattr(self, "current_path", path)
G.PlayerWidget.seek = lambda self, t: None

win = G.MainWindow()
for n in ("a.mp4", "b.mp4", "c.mp4"):
    clip = mk(n, 10.0)
    win.project.add_clip(clip)
    item = QListWidgetItem(win._clip_label(clip))
    item.setData(Qt.UserRole, clip)
    win.clip_list.addItem(item)

win._reorder_segment(0, 2)
assert names(win.project) == ["b.mp4", "c.mp4", "a.mp4"]
assert win.active_idx == 2 and win.cur_global > 19.9           # курсор поехал за куском

win.cur_global = 25.0                                          # b 0-10, c 10-20, a 20-30
win._nudge_segment(-1)
assert names(win.project) == ["b.mp4", "a.mp4", "c.mp4"], names(win.project)

win._reorder_clips([win.project.clips[2], win.project.clips[0], win.project.clips[1]])
assert [cl.name for cl in win.project.clips] == ["c.mp4", "a.mp4", "b.mp4"]
assert names(win.project) == ["c.mp4", "a.mp4", "b.mp4"]
assert [win.clip_list.item(i).data(Qt.UserRole).name for i in range(3)] == \
    ["c.mp4", "a.mp4", "b.mp4"]
win._undo()
assert [cl.name for cl in win.project.clips] == ["a.mp4", "b.mp4", "c.mp4"]

win.quick_mode = True                                          # в быстром режиме не режем
before = names(win.project)
win._reorder_segment(0, 2)
assert names(win.project) == before
print("main window reorder: OK")

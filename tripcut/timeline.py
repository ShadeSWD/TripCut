"""Таймлайн: сегменты всех клипов подряд (вырезанное схлопнуто), курсор, клик = seek."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QFontMetrics
from PySide6.QtWidgets import QWidget

_CLIP_COLORS = [QColor(70, 130, 200), QColor(90, 170, 110), QColor(190, 140, 70),
                QColor(160, 100, 180), QColor(200, 100, 100), QColor(100, 170, 170)]


def fmt_t(sec: float) -> str:
    sec = max(sec, 0)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int((sec % 1) * 10)
    return (f"{h}:{m:02d}:{s:02d}.{ms}" if h else f"{m:02d}:{s:02d}.{ms}")


class TimelineWidget(QWidget):
    seek_requested = Signal(float)   # глобальное время

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.cursor_t = 0.0          # глобальное время
        self.setMinimumHeight(64)
        self.setMaximumHeight(64)
        self.setMouseTracking(True)
        self._hover_x: int | None = None

    def set_cursor(self, gt: float):
        self.cursor_t = max(0.0, min(gt, self.project.total))
        self.update()

    # ---------------- события мыши
    def _x_to_t(self, x: int) -> float:
        total = self.project.total
        if total <= 0 or self.width() <= 8:
            return 0.0
        return (x - 4) / (self.width() - 8) * total

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.seek_requested.emit(self._x_to_t(int(ev.position().x())))

    def mouseMoveEvent(self, ev):
        self._hover_x = int(ev.position().x())
        if ev.buttons() & Qt.LeftButton:
            self.seek_requested.emit(self._x_to_t(self._hover_x))
        self.update()

    def leaveEvent(self, ev):
        self._hover_x = None
        self.update()

    # ---------------- отрисовка
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(28, 28, 32))
        total = self.project.total
        w = self.width() - 8
        top, h = 8, 34
        if total <= 0 or w <= 0:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignCenter, "Добавьте видео (кнопка или drag&drop)")
            p.end()
            return

        clip_idx = {id(c): i for i, c in enumerate(self.project.clips)}
        x = 4.0
        for seg in self.project.segments:
            sw = seg.duration / total * w
            col = _CLIP_COLORS[clip_idx.get(id(seg.clip), 0) % len(_CLIP_COLORS)]
            p.fillRect(QRectF(x, top, max(sw - 1.5, 1.0), h), col)
            if sw > 60:
                p.setPen(QColor(255, 255, 255, 200))
                fm = QFontMetrics(p.font())
                label = fm.elidedText(seg.clip.name, Qt.ElideMiddle, int(sw) - 8)
                p.drawText(QRectF(x + 4, top, sw - 8, h), Qt.AlignVCenter, label)
            x += sw

        # курсор
        cx = 4 + self.cursor_t / total * w
        p.setPen(QPen(QColor(255, 70, 70), 2))
        p.drawLine(int(cx), 2, int(cx), top + h + 6)

        # подпись времени
        p.setPen(QColor(220, 220, 220))
        p.drawText(6, self.height() - 6, f"{fmt_t(self.cursor_t)} / {fmt_t(total)}")
        if self._hover_x is not None:
            ht = self._x_to_t(self._hover_x)
            p.setPen(QColor(140, 140, 140))
            p.drawText(self.width() - 90, self.height() - 6, fmt_t(ht))
        p.end()

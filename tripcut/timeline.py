"""Таймлайн: сегменты всех клипов подряд (вырезанное схлопнуто), курсор, клик = seek,
перетаскивание сегмента за ручку сверху = смена порядка."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QFontMetrics
from PySide6.QtWidgets import QWidget

_CLIP_COLORS = [QColor(70, 130, 200), QColor(90, 170, 110), QColor(190, 140, 70),
                QColor(160, 100, 180), QColor(200, 100, 100), QColor(100, 170, 170)]

TOP = 8          # верх блоков сегментов
BLOCK_H = 40     # высота блока
GRIP_H = 14      # верхняя полоска блока — «ручка» для перетаскивания
DRAG_PX = 4      # порог, после которого нажатие на ручке становится перетаскиванием


def fmt_t(sec: float) -> str:
    sec = max(sec, 0)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int((sec % 1) * 10)
    return (f"{h}:{m:02d}:{s:02d}.{ms}" if h else f"{m:02d}:{s:02d}.{ms}")


class TimelineWidget(QWidget):
    seek_requested = Signal(float)          # глобальное время
    reorder_requested = Signal(int, int)    # (индекс сегмента, новая позиция)

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.cursor_t = 0.0          # глобальное время
        self.setMinimumHeight(78)
        self.setMaximumHeight(78)
        self.setMouseTracking(True)
        self._hover_x: int | None = None
        # перетаскивание
        self._press_x: int | None = None    # x нажатия на ручке (ещё не драг)
        self._drag_from: int | None = None  # индекс перетаскиваемого сегмента
        self._drag_x = 0                    # текущий x мыши при драге

    def set_cursor(self, gt: float):
        self.cursor_t = max(0.0, min(gt, self.project.total))
        self.update()

    # ---------------- геометрия
    def _w(self) -> int:
        return self.width() - 8

    def _widths(self) -> list[float]:
        """Ширины сегментов в пикселях, в текущем порядке."""
        total = self.project.total
        w = self._w()
        if total <= 0 or w <= 0:
            return []
        return [s.duration / total * w for s in self.project.segments]

    def _seg_at(self, x: int, y: int) -> int | None:
        if not (TOP <= y <= TOP + BLOCK_H):
            return None
        acc = 4.0
        for i, sw in enumerate(self._widths()):
            if acc <= x < acc + sw:
                return i
            acc += sw
        return None

    def _drop_index(self, x: int) -> int:
        """Куда встанет перетаскиваемый сегмент: сколько чужих центров левее курсора."""
        acc, dst = 4.0, 0
        for i, sw in enumerate(self._widths()):
            if i != self._drag_from and x > acc + sw / 2:
                dst += 1
            acc += sw
        return dst

    def _drop_x(self, dst: int) -> float:
        """Пиксельная позиция каретки вставки для позиции dst."""
        rest = [sw for i, sw in enumerate(self._widths()) if i != self._drag_from]
        return 4.0 + sum(rest[:dst])

    def _x_to_t(self, x: int) -> float:
        total = self.project.total
        if total <= 0 or self._w() <= 0:
            return 0.0
        return (x - 4) / self._w() * total

    # ---------------- события мыши
    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        x, y = int(ev.position().x()), int(ev.position().y())
        grab = (y <= TOP + GRIP_H) or bool(ev.modifiers() & Qt.AltModifier)
        if grab and len(self.project.segments) > 1 and self._seg_at(x, y) is not None:
            self._press_x = x           # драг начнётся после сдвига на DRAG_PX
            self._drag_from = None
            return
        self.seek_requested.emit(self._x_to_t(x))

    def mouseMoveEvent(self, ev):
        x, y = int(ev.position().x()), int(ev.position().y())
        self._hover_x = x
        if self._press_x is not None and self._drag_from is None \
                and abs(x - self._press_x) >= DRAG_PX:
            self._drag_from = self._seg_at(self._press_x, TOP + 1)
        if self._drag_from is not None:
            self._drag_x = x
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            return
        if self._press_x is None and (ev.buttons() & Qt.LeftButton):
            self.seek_requested.emit(self._x_to_t(x))
        on_grip = (y <= TOP + GRIP_H and len(self.project.segments) > 1
                   and self._seg_at(x, y) is not None)
        self.setCursor(Qt.OpenHandCursor if on_grip else Qt.ArrowCursor)
        self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        src, self._drag_from, self._press_x = self._drag_from, None, None
        self.unsetCursor()
        if src is not None:
            self._drag_from = src               # _drop_index считает без него
            dst = self._drop_index(int(ev.position().x()))
            self._drag_from = None
            if dst != src:
                self.reorder_requested.emit(src, dst)
        self.update()

    def leaveEvent(self, ev):
        self._hover_x = None
        self.update()

    # ---------------- отрисовка
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(28, 28, 32))
        total = self.project.total
        w = self._w()
        if total <= 0 or w <= 0:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignCenter, "Добавьте видео (кнопка или drag&drop)")
            p.end()
            return

        clip_idx = {id(c): i for i, c in enumerate(self.project.clips)}
        widths = self._widths()
        many = len(self.project.segments) > 1
        fm = QFontMetrics(p.font())
        x = 4.0
        for i, seg in enumerate(self.project.segments):
            sw = widths[i]
            base = _CLIP_COLORS[clip_idx.get(id(seg.clip), 0) % len(_CLIP_COLORS)]
            col = QColor(base)
            if i == self._drag_from:
                col.setAlpha(70)
            rect = QRectF(x, TOP, max(sw - 1.5, 1.0), BLOCK_H)
            p.fillRect(rect, col)
            if many:                                    # ручка перетаскивания
                p.fillRect(QRectF(rect.x(), TOP, rect.width(), GRIP_H),
                           QColor(255, 255, 255, 30))
                if sw > 26:
                    p.setPen(QColor(255, 255, 255, 130))
                    p.drawText(QRectF(rect.x(), TOP, rect.width(), GRIP_H),
                               Qt.AlignCenter, "= = =")
            if sw > 60:
                p.setPen(QColor(255, 255, 255, 200))
                label = fm.elidedText(seg.clip.name, Qt.ElideMiddle, int(sw) - 8)
                p.drawText(QRectF(x + 4, TOP + GRIP_H, sw - 8, BLOCK_H - GRIP_H),
                           Qt.AlignVCenter, label)
            x += sw

        if self._drag_from is not None:
            self._paint_drag(p, widths, clip_idx)

        # курсор
        cx = 4 + self.cursor_t / total * w
        p.setPen(QPen(QColor(255, 70, 70), 2))
        p.drawLine(int(cx), 2, int(cx), TOP + BLOCK_H + 6)

        # подпись времени
        if self._drag_from is None:
            p.setPen(QColor(220, 220, 220))
            p.drawText(6, self.height() - 6, f"{fmt_t(self.cursor_t)} / {fmt_t(total)}")
            if self._hover_x is not None:
                p.setPen(QColor(140, 140, 140))
                p.drawText(self.width() - 90, self.height() - 6,
                           fmt_t(self._x_to_t(self._hover_x)))
        p.end()

    def _paint_drag(self, p: QPainter, widths: list[float], clip_idx: dict):
        """Призрак перетаскиваемого сегмента + каретка вставки + подпись."""
        i = self._drag_from
        seg = self.project.segments[i]
        sw = max(widths[i] - 1.5, 6.0)
        gx = min(max(self._drag_x - sw / 2, 4.0), 4.0 + self._w() - sw)
        col = QColor(_CLIP_COLORS[clip_idx.get(id(seg.clip), 0) % len(_CLIP_COLORS)])
        col.setAlpha(215)
        ghost = QRectF(gx, TOP - 3, sw, BLOCK_H)
        p.fillRect(ghost, col)
        p.setPen(QPen(QColor(255, 255, 255, 180), 1))
        p.drawRect(ghost)

        dst = self._drop_index(self._drag_x)
        dx = self._drop_x(dst)
        p.setPen(QPen(QColor(255, 220, 90), 3))
        p.drawLine(int(dx), TOP - 6, int(dx), TOP + BLOCK_H + 3)
        p.setPen(QColor(255, 220, 90))
        p.drawText(6, self.height() - 6,
                   f"перенос: кусок {i + 1} → на позицию {dst + 1} "
                   f"из {len(self.project.segments)}")

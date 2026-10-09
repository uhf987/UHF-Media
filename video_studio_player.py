# -*- coding: utf-8 -*-
"""
Video Stüdyo & Oynatıcı (v1 - mpv motoru, HULU-Player + FFmpeg Video Studio birleşimi)
---------------------------------------------------------------------------------------
Bu dosya iki ayrı projeyi TEK bir uygulamada, TEK bir video motoru (mpv) üzerinde
birleştirir:

  - HULU-Player (main.py)          -> oynatma motoru, altyazı, ses kanalı, ekran
                                       görüntüsü, tam ekran, klavye/fare kısayolları
  - FFmpeg Video Studio (video_studio.py) -> döndürme + kırpma + ffmpeg ile dışa aktarma

Neden mpv?
----------
İki kaynak dosya iki farklı motor kullanıyordu: video_studio.py Qt Multimedia
(QMediaPlayer + QGraphicsVideoItem), main.py ise libmpv. Aynı anda iki motoru
aynı video alanına bağlamak mümkün olmadığından, mpv motoru seçildi ve
döndürme/kırpma özellikleri mpv'nin üzerine yeniden inşa edildi:

  - Döndürme artık mpv'nin GERÇEK "video-rotate" özelliğiyle yapılıyor
    (eskisi gibi sadece Qt view'ini döndüren bir numara değil).
  - Kırpma kutusu artık bir QGraphicsScene üzerinde değil, mpv'nin video
    widget'ının TAM ÜZERİNE bindirilmiş şeffaf bir katman (CropOverlay) olarak
    çiziliyor. Dışa aktarımda bu katmandaki (0..1 oranlı) kırpma alanı,
    o anki döndürme durumuna göre ORİJİNAL (döndürülmemiş) video piksel
    uzayına geri çevrilip ffmpeg'e crop+transpose filtre zinciri olarak
    veriliyor - video_studio.py'deki export mantığıyla birebir aynı sonucu
    üretir.

Gereksinimler:
    pip install PyQt5 mpv

    Sistemde **libmpv** kütüphanesi kurulu olmalı (Windows: libmpv-2.dll bu
    dosyayla / .exe ile aynı klasörde ya da PATH'te).
    Dışa aktarma (döndür/kırp) özelliği için ffmpeg ve ffprobe PATH'te ya da
    bu dosyayla aynı klasörde olmalı; ffmpeg yoksa oynatıcı yine çalışır,
    sadece "Dışa Aktar" devre dışı kalır.

NOT: Bu dosya bir GUI (PyQt5 + libmpv) uygulamasıdır; ekransız/headless bir
ortamda (ör. bu kodun üretildiği sanal ortam) çalıştırılıp görsel olarak
test edilememiştir. Gerçek bir video dosyasıyla kendi makinenizde deneyin.
"""

import os
import sys
import subprocess
import math
import functools
import asyncio
import wave
import re
import tempfile
from datetime import datetime

import numpy as np
from shazamio import Shazam


def _prepare_libmpv_search_path():
    """Windows'ta libmpv-2.dll'i bulabilmek için olası TÜM klasörleri PATH'e
    ekliyoruz. mpv modülünü import etmeden ÖNCE çağrılmalı.

    PyInstaller sürümüne göre dll'in beklenen konumu değişiyor:
      - PyInstaller 6+ onedir: dist/AppName/_internal/   (sys._MEIPASS)
      - PyInstaller 5 ve öncesi onedir: dist/AppName/     (sys._MEIPASS == exe klasörü)
      - onefile: geçici açılma klasörü                    (sys._MEIPASS)
      - .exe'nin kendisiyle aynı klasör                    (dirname(sys.executable))
      - script olarak çalıştırılıyorsa: .py dosyasının klasörü
    Hangi düzeni kullandığından emin olmamak için hepsini ekliyoruz; DLL
    bunlardan HANGİSİNDE olursa olsun bulunur.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(meipass)
    if getattr(sys, "frozen", False):
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    candidates.append(os.path.dirname(os.path.abspath(__file__)))

    existing = os.environ.get("PATH", "")
    for d in candidates:
        if d and d not in existing:
            existing = d + os.pathsep + existing
    os.environ["PATH"] = existing


if sys.platform == "win32":
    _prepare_libmpv_search_path()

import mpv  # noqa: E402  (PATH ayarından sonra import edilmeli)

from PyQt5.QtCore import (  # noqa: E402
    Qt, QTimer, QStandardPaths, pyqtSignal, QRect, QThread,
)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PyQt5.QtGui import (  # noqa: E402
    QPainter, QColor, QPen, QRegion, QPalette, QCursor,
)
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QFrame, QPushButton, QSlider, QLabel, QComboBox, QSizePolicy, QDialog,
    QDialogButtonBox, QStyle, QStyleOptionSlider, QCheckBox, QRadioButton,
    QButtonGroup, QFileDialog, QMessageBox, QProgressDialog,
)


# --------------------------------------------------------------------------- #
#  ffmpeg / ffprobe yardımcıları (video_studio.py'den; sadece dışa aktarımda
#  kullanılıyor - oynatma tamamen mpv üzerinden yapılıyor)
# --------------------------------------------------------------------------- #
def _binary_path(name: str) -> str:
    base = os.path.dirname(
        sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    )
    exe_name = name + (".exe" if os.name == "nt" else "")
    candidate = os.path.join(base, exe_name)
    if os.path.isfile(candidate):
        return candidate
    return name  # PATH üzerinden çözülsün


FFMPEG = _binary_path("ffmpeg")
FFPROBE = _binary_path("ffprobe")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# --------------------------------------------------------------------------- #
#  Şarkı tanıma (music_renamer.py'den; Shazam ile tanıyıp dosyayı yeniden
#  adlandırma) - PotPlayer izleme kısmı ATILDI çünkü artık zaten oynatılan
#  dosyanın yolunu (self.current_path) doğrudan biliyoruz. Ses okuma için
#  ayrıca bir ffmpeg aramaya da gerek yok; oynatıcının kendi FFMPEG'i
#  kullanılıyor.
# --------------------------------------------------------------------------- #
MUSIC_ID_SAMPLE_RATE = 44100
MUSIC_ID_READ_SECONDS = 10
MUSIC_ID_SILENCE_THRESH = 0.01

# Windows dosya ilişkilendirmesiyle (bir video dosyasına çift tıklama) her
# seferinde YENİ bir işlem başlar. Tek pencere kalması için bu isimde bir
# yerel sunucu (Windows'ta named pipe, Linux/Mac'te unix socket) kuruyoruz;
# ikinci bir örnek bu isme bağlanabiliyorsa demek ki uygulama zaten açık.
SINGLE_INSTANCE_KEY = "HuluMediaVideoStudioPlayer_v1_SingleInstance"


def check_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True,
                        creationflags=NO_WINDOW)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


@functools.lru_cache(maxsize=1)
def detect_hw_encoder():
    """Bu ffmpeg derlemesinin desteklediği donanım video kodlayıcılardan
    ilk uygun olanı döndürür (NVIDIA > Intel > AMD sırasıyla), hiçbiri
    yoksa None döndürür. Sonuç önbelleğe alınır (bu subprocess çağrısı
    her export'ta değil, uygulama başına sadece BİR KEZ çalışır).
    NOT: Bir kodlayıcının burada listelenmesi, o donanımın bu bilgisayarda
    GERÇEKTEN var olduğu anlamına gelmez (sürücü kurulu olmayabilir) - bu
    yüzden do_export() içinde gerçek kullanım başarısız olursa otomatik
    olarak CPU'ya (libx264) geri dönülüyor."""
    if not check_ffmpeg():
        return None
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=5,
        ).stdout
    except Exception:
        return None
    for name in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if name in out:
            return name
    return None


def fmt_time(seconds) -> str:
    if not seconds or seconds < 0:
        seconds = 0
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def fmt_time_hms(seconds) -> str:
    """Uzun videolarda (ör. 2 saatlik bir film) kesme başlangıç/bitiş
    zamanını netlik için SAAT:DAKİKA:SANİYE olarak gösterir."""
    if not seconds or seconds < 0:
        seconds = 0
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def aspect_ratio_str(w: int, h: int) -> str:
    if w <= 0 or h <= 0:
        return ""
    g = math.gcd(w, h)
    return f"{w // g}:{h // g}"


# --------------------------------------------------------------------------- #
#  Tıklanan konuma doğrudan atlayan slider (main.py)
# --------------------------------------------------------------------------- #
class ClickableSlider(QSlider):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            value = self._value_from_pos(event.pos())
            self.setValue(value)
            self.sliderMoved.emit(value)
        super().mousePressEvent(event)

    def _value_from_pos(self, pos):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)

        if self.orientation() == Qt.Horizontal:
            length = handle.width()
            smin = groove.x()
            smax = groove.right() - length + 1
            p = pos.x()
        else:
            length = handle.height()
            smin = groove.y()
            smax = groove.bottom() - length + 1
            p = pos.y()

        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), p - smin, smax - smin, opt.upsideDown
        )


# --------------------------------------------------------------------------- #
#  mpv'nin gömüldüğü video alanı (main.py)
# --------------------------------------------------------------------------- #
class VideoFrame(QFrame):
    resized = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        # mpv'nin winId() ile gömülebilmesi için gerçek bir native pencere
        # olmasını zorluyoruz.
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        # KRİTİK: Qt, bu widget'ın arka planını KENDİSİ tekrar tekrar boyamaya
        # çalışmasın diye bu üç bayrağı da eklemek şart. Aksi halde mpv
        # görüntüyü doğru şekilde bu pencereye çizse bile (ki log dosyasına
        # göre çiziyordu), Qt'nin kendi arka plan boyaması bunun üzerine
        # siyah olarak "geri çizilip" görüntüyü kapatıyor. Ayrıca bu widget'a
        # ASLA setStyleSheet ile arka plan rengi verilmemeli - stylesheet
        # verilmesi Qt'yi bu widget'ı kendi boyamasına zorluyor.
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_PaintOnScreen, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)
        # Açık (explicit) ok imleci: aksi halde CentralWidget'ın kenar
        # şeridinde ayarladığı yeniden boyutlandırma imleci bu native
        # pencereye miras kalıp takılı kalıyor.
        self.setCursor(Qt.ArrowCursor)

    def mousePressEvent(self, event):
        self.window().setFocus()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.window().toggle_fullscreen()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120
        self.window().adjust_volume(int(steps) * 5)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


# --------------------------------------------------------------------------- #
#  Özel (frameless) pencere için üst çubuk: buradan tutup pencere taşınabilir,
#  çift tıklayınca büyüt/eski boyut arasında geçiş yapar. Qt'nin Qt 5.15+ ile
#  gelen startSystemMove() API'sini kullanıyoruz - işletim sistemine devrediyor,
#  elle piksel piksel sürükleme koduna göre çok daha sağlam.
# --------------------------------------------------------------------------- #
class TitleBar(QWidget):
    doubleClicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Açık ok imleci: CentralWidget'ın yeniden boyutlandırma imleci
        # üst çubuğa (ve düğmelerine) miras kalmasın.
        self.setCursor(Qt.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


# --------------------------------------------------------------------------- #
#  Merkezi widget: pencere kenarlarından tutup yeniden boyutlandırabilmek için
#  ince bir boşluk (BORDER piksel) bırakır. Bu boşluk video_frame'in NATIVE
#  penceresinin DIŞINDA kaldığı için fare olaylarını normal şekilde alır -
#  video alanı native pencere olduğundan tam üzerinde bu iş yapılamaz.
#  startSystemResize() de Qt 5.15+ ile gelir, işletim sistemine devreder.
# --------------------------------------------------------------------------- #
class CentralWidget(QWidget):
    BORDER = 5

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window
        self.setMouseTracking(True)

    def _edge_at(self, pos):
        b = self.BORDER
        w, h = self.width(), self.height()
        left = pos.x() < b
        right = pos.x() > w - b
        top = pos.y() < b
        bottom = pos.y() > h - b
        edges = Qt.Edges()
        if top:
            edges |= Qt.TopEdge
        if bottom:
            edges |= Qt.BottomEdge
        if left:
            edges |= Qt.LeftEdge
        if right:
            edges |= Qt.RightEdge
        return edges if edges else None

    _CURSOR_MAP = {
        Qt.TopEdge: Qt.SizeVerCursor,
        Qt.BottomEdge: Qt.SizeVerCursor,
        Qt.LeftEdge: Qt.SizeHorCursor,
        Qt.RightEdge: Qt.SizeHorCursor,
        Qt.TopEdge | Qt.LeftEdge: Qt.SizeFDiagCursor,
        Qt.BottomEdge | Qt.RightEdge: Qt.SizeFDiagCursor,
        Qt.TopEdge | Qt.RightEdge: Qt.SizeBDiagCursor,
        Qt.BottomEdge | Qt.LeftEdge: Qt.SizeBDiagCursor,
    }

    def mouseMoveEvent(self, event):
        if self._window.isMaximized() or self._window.isFullScreen():
            self.unsetCursor()
        else:
            edge = self._edge_at(event.pos())
            cursor = self._CURSOR_MAP.get(edge) if edge else None
            if cursor is not None:
                self.setCursor(cursor)
            else:
                self.unsetCursor()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        # Fare kenar şeridinden hızlıca bir native çocuk pencereye (video,
        # üst çubuk vb.) geçerse buraya mouseMove gelmez; imleç takılı
        # kalmasın diye çıkışta açıkça sıfırlıyoruz.
        self.unsetCursor()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if (
            event.button() == Qt.LeftButton
            and not self._window.isMaximized()
            and not self._window.isFullScreen()
        ):
            edge = self._edge_at(event.pos())
            if edge:
                handle = self._window.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edge)
                    event.accept()
                    return
        super().mousePressEvent(event)


# --------------------------------------------------------------------------- #
#  Altyazı boyutu / konumu CANLI ayar penceresi (main.py)
# --------------------------------------------------------------------------- #
class SubtitleSettingsDialog(QDialog):
    def __init__(self, parent, scale, pos, on_change):
        super().__init__(parent)
        self.setWindowTitle("Altyazı Ayarları")
        self.on_change = on_change
        self._orig_scale = scale
        self._orig_pos = pos

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Altyazı Boyutu (%)"))
        self.scale_slider = QSlider(Qt.Horizontal)
        self.scale_slider.setRange(50, 300)
        self.scale_slider.setValue(scale)
        self.scale_slider.setFocusPolicy(Qt.NoFocus)
        self.scale_value_label = QLabel(str(scale))
        row1 = QHBoxLayout()
        row1.addWidget(self.scale_slider)
        row1.addWidget(self.scale_value_label)
        layout.addLayout(row1)

        layout.addWidget(QLabel("Altyazı Konumu (0 = üst, 100 = ekranın alt kenarı)"))
        self.pos_slider = QSlider(Qt.Horizontal)
        self.pos_slider.setRange(0, 100)
        self.pos_slider.setValue(pos)
        self.pos_slider.setFocusPolicy(Qt.NoFocus)
        self.pos_value_label = QLabel(str(pos))
        row2 = QHBoxLayout()
        row2.addWidget(self.pos_slider)
        row2.addWidget(self.pos_value_label)
        layout.addLayout(row2)

        self.scale_slider.valueChanged.connect(self._on_slider_changed)
        self.pos_slider.valueChanged.connect(self._on_slider_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Tamam")
        buttons.button(QDialogButtonBox.Cancel).setText("İptal (eski haline dön)")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_slider_changed(self, _value):
        scale = self.scale_slider.value()
        pos = self.pos_slider.value()
        self.scale_value_label.setText(str(scale))
        self.pos_value_label.setText(str(pos))
        self.on_change(scale, pos)

    def reject(self):
        self.on_change(self._orig_scale, self._orig_pos)
        super().reject()

    def values(self):
        return self.scale_slider.value(), self.pos_slider.value()


# --------------------------------------------------------------------------- #
#  Kırpma katmanı: mpv'nin video alanının TAM ÜZERİNE bindirilen şeffaf widget.
#  Koordinatlar bu widget'ın kendi piksel uzayında (video_frame ile birebir
#  aynı boyutta) tutulur. crop_ratio() ile 0..1 aralığında, "video_rect" (yani
#  harita/letterbox dışındaki gerçek video alanı) baz alınarak döndürülür.
# --------------------------------------------------------------------------- #
# 5x7'lik piksel-yazı tipi (retro/bit-map font) - sadece "UHF" için gereken
# harfler tanımlı. '1' = dolu piksel, '0' = boş.
_PIXEL_FONT_5X7 = {
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
}


def _draw_pixel_text(painter, text, x, y, pixel_size, color, letter_gap_cells=1):
    """Verilen metni gerçek bir yazı tipi yerine kare piksellerden oluşan
    bir bit-map font gibi çizer - referans görseldeki köşeli/retro "UHF"
    görünümünü taklit eder. Sonraki çizim için kullanılabilecek x
    konumunu (metnin bittiği yer) döndürür."""
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    cursor_x = x
    for ch in text:
        pattern = _PIXEL_FONT_5X7.get(ch.upper())
        if pattern:
            for row_idx, row in enumerate(pattern):
                for col_idx, bit in enumerate(row):
                    if bit == "1":
                        painter.drawRect(
                            cursor_x + col_idx * pixel_size,
                            y + row_idx * pixel_size,
                            pixel_size, pixel_size,
                        )
        cursor_x += (5 + letter_gap_cells) * pixel_size
    return cursor_x


class IdleSignalWidget(QWidget):
    """Eski TV'lerin UHF kanal arama ekranını taklit eden açılış logosu:
    piksel-yazı tipiyle kırmızı "UHF" yazısı + altında sarı bir "ayar
    çubuğu" satırı. Bu satır önce birkaç KALIN çubuktan oluşur ("IIIIIIII"
    gibi), ortadan sonra küçük NOKTALARA dönüşür ("............"); çubuk
    segmentinin SON çubuğu, sinyal aranıyor hissi vermesi için yanıp
    söner."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # video_frame gibi bu da NATIVE bir pencere olmalı, yoksa video_frame'in
        # (mpv'nin gömülü olduğu) native yüzeyinin ARKASINDA kalıp hiç görünmez.
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setCursor(Qt.ArrowCursor)

        self._bar_visible = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(500)
        self._blink_timer.timeout.connect(self._toggle_bar)
        self._blink_timer.start()

    def _toggle_bar(self):
        self._bar_visible = not self._bar_visible
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        logo_w, logo_h = 460, 180
        ox = (self.width() - logo_w) // 2
        oy = (self.height() - logo_h) // 2

        # "UHF" - piksel yazı tipiyle
        pixel_size = 10
        _draw_pixel_text(painter, "UHF", ox, oy, pixel_size, QColor(232, 20, 10))
        text_height = 7 * pixel_size

        # Ayar çubuğu satırı: solda KALIN çubuklar ("I I I I I I I I"),
        # ortadan itibaren küçük NOKTALAR ("."). Çubuk segmentinin SON
        # çubuğu yanıp sönüyor.
        row_top = oy + text_height + 26
        bar_count = 8
        bar_h = 34
        segment_w = logo_w / 2
        bar_pitch = segment_w / bar_count
        bar_w = max(4.0, bar_pitch * 0.4)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 221, 0))
        for i in range(bar_count):
            if i == bar_count - 1 and not self._bar_visible:
                continue  # son çubuk: yanıp sönme efekti
            bx = ox + i * bar_pitch
            painter.drawRect(int(bx), int(row_top), int(bar_w), int(bar_h))

        # Noktalar: segmentin ortasından logonun sonuna kadar küçük daireler,
        # çubuklarla aynı taban çizgisine hizalı.
        dot_d = 8
        dot_pitch = 16
        dot_y = row_top + bar_h - dot_d
        x = ox + segment_w + dot_pitch / 2
        while x + dot_d <= ox + logo_w:
            painter.drawEllipse(int(x), int(dot_y), dot_d, dot_d)
            x += dot_pitch


class CropOverlay(QWidget):
    HANDLE = 12
    changed = pyqtSignal()

    _CURSORS = {
        "topleft": Qt.SizeFDiagCursor, "bottomright": Qt.SizeFDiagCursor,
        "topright": Qt.SizeBDiagCursor, "bottomleft": Qt.SizeBDiagCursor,
        "top": Qt.SizeVerCursor, "bottom": Qt.SizeVerCursor,
        "left": Qt.SizeHorCursor, "right": Qt.SizeHorCursor,
        "move": Qt.SizeAllCursor,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        # ÖNEMLİ: WA_TranslucentBackground KASITLI OLARAK kullanılmıyor.
        # Windows'ta bu, widget'ı "katmanlı" (layered) bir native pencereye
        # çeviriyor ve mpv'nin D3D ile çizdiği görüntüyle birlikte doğru
        # alfa karıştırma yapmıyor - sonuç şeffaf olması gereken yerler de
        # dahil TÜM widget'ın opak siyah görünmesiydi. Onun yerine: widget'ı
        # gerçek bir native pencere yapıp (video_frame ile aynı teknik),
        # setMask() ile pencerenin GERÇEK ŞEKLİNİ kırpma kutusunun ETRAFI
        # olacak şekilde kesiyoruz (Windows'ta SetWindowRgn). Böylece kırpma
        # kutusunun içi bu pencerenin bir PARÇASI bile değil - altındaki
        # video hiçbir alfa karıştırmaya gerek kalmadan doğrudan görünür.
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)
        self._active = False
        self._video_rect = QRect()
        self._crop = QRect()
        self._mode = None
        self._start_mouse_pos = None
        self._start_crop = None

    # ---- dışarıdan çağrılan API ----
    def set_active(self, active: bool):
        self._active = active
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not active)
        self.setCursor(Qt.ArrowCursor)
        self.setVisible(active)
        if active:
            self._update_mask()
        self.update()

    def set_video_rect(self, rect: QRect):
        changed_geo = rect != self._video_rect
        self._video_rect = QRect(rect)
        if changed_geo or not self._crop.isValid() or not self._video_rect.contains(self._crop):
            self._reset_default_crop()
        if self._active:
            self._update_mask()
        self.update()

    def _update_mask(self):
        """Pencerenin gerçek şeklini kırpma kutusunun ETRAFI olacak şekilde
        keser; kutunun içi bu pencereden tamamen çıkarılır (gerçek bir delik),
        böylece altındaki video hiçbir alfa karıştırma olmadan görünür.
        Tutamaçlar (köşe/kenar/taşı) genel kenar payından BAĞIMSIZ olarak
        ayrıca maskeye geri ekleniyor - bu sayede genel kenar payını
        minimuma indirip ince bir çerçeve elde edebiliyoruz, tutamaçlar
        yine de tam çalışıyor."""
        if not self._crop.isValid():
            self.clearMask()
            return
        # Çerçevenin görsel kalınlığını belirleyen tek şey bu - ince bir
        # çerçeve için minimumda tutuyoruz.
        inset = 1
        hole = self._crop.adjusted(inset, inset, -inset, -inset)
        region = QRegion(self.rect())
        if hole.isValid() and hole.width() > 0 and hole.height() > 0:
            region = region.subtracted(QRegion(hole))
        # Tüm tutamaçları (köşe/kenar/taşı) - genel kenar payından bağımsız
        # olarak - her zaman pencerenin bir parçası (tıklanabilir/görünür)
        # olarak geri ekle.
        for hr in self._handles().values():
            region = region.united(QRegion(hr))
        self.setMask(region)

    def crop_ratio(self):
        """Kırpma kutusunu video_rect'e göre 0..1 aralığında (rx, ry, rw, rh) döndürür."""
        vr = self._video_rect
        if vr.width() <= 0 or vr.height() <= 0 or not self._crop.isValid():
            return None
        rx = (self._crop.left() - vr.left()) / vr.width()
        ry = (self._crop.top() - vr.top()) / vr.height()
        rw = self._crop.width() / vr.width()
        rh = self._crop.height() / vr.height()
        return rx, ry, rw, rh

    # ---- iç yardımcılar ----
    def _reset_default_crop(self):
        vr = self._video_rect
        if vr.width() <= 0 or vr.height() <= 0:
            self._crop = QRect()
            return
        cw, ch = int(vr.width() * 0.7), int(vr.height() * 0.7)
        cx = vr.left() + (vr.width() - cw) // 2
        cy = vr.top() + (vr.height() - ch) // 2
        self._crop = QRect(cx, cy, cw, ch)

    MOVEGRIP = 28

    def _handles(self):
        r, h = self._crop, self.HANDLE
        g = self.MOVEGRIP
        return {
            "topleft": QRect(r.left() - h // 2, r.top() - h // 2, h, h),
            "topright": QRect(r.right() - h // 2, r.top() - h // 2, h, h),
            "bottomleft": QRect(r.left() - h // 2, r.bottom() - h // 2, h, h),
            "bottomright": QRect(r.right() - h // 2, r.bottom() - h // 2, h, h),
            "top": QRect(r.center().x() - h // 2, r.top() - h // 2, h, h),
            "bottom": QRect(r.center().x() - h // 2, r.bottom() - h // 2, h, h),
            "left": QRect(r.left() - h // 2, r.center().y() - h // 2, h, h),
            "right": QRect(r.right() - h // 2, r.center().y() - h // 2, h, h),
            # Kutunun içi artık gerçek bir "delik" (pencerenin parçası değil),
            # bu yüzden içeriye tıklayıp sürükleyerek taşıma çalışmaz. Bunun
            # yerine ortada, delik AÇILMAYAN küçük bir "taşı" tutamacı var.
            "move": QRect(r.center().x() - g // 2, r.center().y() - g // 2, g, g),
        }

    def _handle_at(self, pos):
        for name, hr in self._handles().items():
            if hr.contains(pos):
                return name
        return None

    # ---- çizim ----
    def paintEvent(self, event):
        if not self._active or not self._crop.isValid():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # setMask() sayesinde kırpma kutusunun İÇİ zaten bu pencerenin bir
        # parçası değil (gerçek bir "delik"); burada sadece dışarıda kalan
        # (maskelenmiş) alanı düz, opak koyu bir renkle dolduruyoruz.
        painter.fillRect(self.rect(), QColor(15, 15, 15, 235))

        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self._crop)

        painter.setPen(QPen(QColor(30, 30, 30), 1))
        painter.setBrush(QColor(255, 255, 255))
        for hr in self._handles().values():
            painter.drawRect(hr)

    # ---- fare olayları ----
    def mousePressEvent(self, event):
        if not self._active:
            return
        handle = self._handle_at(event.pos())
        self._mode = handle if handle else ("move" if self._crop.contains(event.pos()) else None)
        self._start_mouse_pos = event.pos()
        self._start_crop = QRect(self._crop)

    def mouseMoveEvent(self, event):
        if not self._active:
            return
        if self._mode is None:
            handle = self._handle_at(event.pos())
            if handle:
                self.setCursor(self._CURSORS[handle])
            elif self._crop.contains(event.pos()):
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return

        delta = event.pos() - self._start_mouse_pos
        vr = self._video_rect
        min_size = 20
        r = QRect(self._start_crop)

        if self._mode == "move":
            r.translate(delta.x(), delta.y())
            if r.left() < vr.left():
                r.moveLeft(vr.left())
            if r.top() < vr.top():
                r.moveTop(vr.top())
            if r.right() > vr.right():
                r.moveRight(vr.right())
            if r.bottom() > vr.bottom():
                r.moveBottom(vr.bottom())
        else:
            if "left" in self._mode:
                r.setLeft(max(min(r.left() + delta.x(), r.right() - min_size), vr.left()))
            if "right" in self._mode:
                r.setRight(min(max(r.right() + delta.x(), r.left() + min_size), vr.right()))
            if "top" in self._mode:
                r.setTop(max(min(r.top() + delta.y(), r.bottom() - min_size), vr.top()))
            if "bottom" in self._mode:
                r.setBottom(min(max(r.bottom() + delta.y(), r.top() + min_size), vr.bottom()))

        self._crop = r
        self._update_mask()
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, event):
        self._mode = None


# --------------------------------------------------------------------------- #
#  KESME (trim) çubuğu - zaman çizgisinin altında görünen, başlangıç/bitiş
#  tutamaçlarıyla sürüklenerek ayarlanan aralık seçici. Sadece 0..1 arası
#  ORAN tutar; saniyeye çevirmek için süreyi (set_duration) bilmesi gerekir.
# --------------------------------------------------------------------------- #
class TrimBar(QWidget):
    changed = pyqtSignal()
    preview_seek = pyqtSignal(float)  # 0..1 oran - sürüklenen tutamacın anlık konumu
    MIN_GAP_RATIO = 0.005  # tutamaçların birbirine yapışmasını engeller

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(20)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._start = 0.0
        self._end = 1.0
        self._duration = 0.0
        self._drag_mode = None       # None | "start" | "end" | "range"
        self._drag_anchor_x = 0
        self._drag_anchor_vals = (0.0, 1.0)

    def set_duration(self, seconds):
        self._duration = float(seconds) if seconds else 0.0
        self.update()

    def reset(self):
        self._start, self._end = 0.0, 1.0
        self.update()
        self.changed.emit()

    def set_window_seconds(self, from_seconds, window_seconds):
        """Kesme aralığını, TAM OLARAK verilen andan BAŞLAYIP ileri doğru
        WINDOW_SECONDS uzunluğunda bir pencereye ayarlar (ör. "durduğum
        noktadan itibaren 1 dakikalık bir şerit"). Video sonuna çok
        yakınsa pencere kısalır (geriye taşmaz, başlangıç sabit kalır)."""
        if self._duration <= 0:
            self.reset()
            return
        start_s = max(0.0, min(from_seconds, self._duration))
        end_s = min(self._duration, start_s + window_seconds)
        self._start = start_s / self._duration
        self._end = end_s / self._duration
        self.update()
        self.changed.emit()

    def nudge_start(self, delta_seconds):
        """Başlangıç tutamacını SANİYE hassasiyetinde kaydırır - fareyle
        piksel piksel sürüklemek uzun bir videoda çok kaba kalabiliyor,
        bu tam olarak o hassasiyeti sağlıyor."""
        if self._duration <= 0:
            return
        delta_ratio = delta_seconds / self._duration
        self._start = max(0.0, min(self._start + delta_ratio, self._end - self.MIN_GAP_RATIO))
        self.update()
        self.changed.emit()
        # Sürüklemede olduğu gibi, +/- ile ayarlarken de videoyu o ana
        # canlı olarak atlatıyoruz - senkronize, ne kestiğini görebilesin.
        self.preview_seek.emit(self._start)

    def nudge_end(self, delta_seconds):
        if self._duration <= 0:
            return
        delta_ratio = delta_seconds / self._duration
        self._end = min(1.0, max(self._end + delta_ratio, self._start + self.MIN_GAP_RATIO))
        self.update()
        self.changed.emit()
        self.preview_seek.emit(self._end)

    def start_ratio(self):
        return self._start

    def end_ratio(self):
        return self._end

    def start_seconds(self):
        return self._start * self._duration

    def end_seconds(self):
        return self._end * self._duration

    def is_full_range(self):
        return self._start <= 0.0005 and self._end >= 0.9995

    def _handle_rects(self):
        w = max(1, self.width() - 1)
        x0 = int(self._start * w)
        x1 = int(self._end * w)
        left = QRect(x0 - 5, 1, 10, self.height() - 2)
        right = QRect(x1 - 5, 1, 10, self.height() - 2)
        return left, right, x0, x1

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        left, right, x0, x1 = self._handle_rects()
        pos = event.pos()
        if left.contains(pos):
            self._drag_mode = "start"
        elif right.contains(pos):
            self._drag_mode = "end"
        elif x0 < pos.x() < x1:
            self._drag_mode = "range"
            self._drag_anchor_x = pos.x()
            self._drag_anchor_vals = (self._start, self._end)
        else:
            # boş bir yere tıklanınca en yakın tutamacı oraya taşı
            w = max(1, self.width() - 1)
            ratio = max(0.0, min(1.0, pos.x() / w))
            if abs(ratio - self._start) <= abs(ratio - self._end):
                self._start = min(ratio, self._end - self.MIN_GAP_RATIO)
                self._drag_mode = "start"
            else:
                self._end = max(ratio, self._start + self.MIN_GAP_RATIO)
                self._drag_mode = "end"
            self.update()
            self.changed.emit()
        self._emit_preview_for_current_mode()

    def _emit_preview_for_current_mode(self):
        # Sürüklenen tutamacın (ya da aralık taşınıyorsa aralığın
        # başlangıcının) tam olarak hangi saniyede olduğunu oynatıcıya
        # bildiriyoruz ki kullanıcı sürüklerken filmi canlı görsün.
        if self._drag_mode == "start":
            self.preview_seek.emit(self._start)
        elif self._drag_mode == "end":
            self.preview_seek.emit(self._end)
        elif self._drag_mode == "range":
            self.preview_seek.emit(self._start)

    def mouseMoveEvent(self, event):
        if self._drag_mode is None:
            return
        w = max(1, self.width() - 1)
        ratio = max(0.0, min(1.0, event.pos().x() / w))
        if self._drag_mode == "start":
            self._start = max(0.0, min(ratio, self._end - self.MIN_GAP_RATIO))
        elif self._drag_mode == "end":
            self._end = min(1.0, max(ratio, self._start + self.MIN_GAP_RATIO))
        elif self._drag_mode == "range":
            dx = (event.pos().x() - self._drag_anchor_x) / w
            s0, e0 = self._drag_anchor_vals
            span = e0 - s0
            new_s = max(0.0, min(1.0 - span, s0 + dx))
            self._start, self._end = new_s, new_s + span
        self.update()
        self.changed.emit()
        self._emit_preview_for_current_mode()

    def mouseReleaseEvent(self, event):
        self._drag_mode = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        track = QRect(0, h // 2 - 3, w, 6)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(90, 90, 90))
        painter.drawRoundedRect(track, 3, 3)

        left, right, x0, x1 = self._handle_rects()
        selected = QRect(x0, h // 2 - 3, max(1, x1 - x0), 6)
        painter.setBrush(QColor(255, 59, 48, 190))
        painter.drawRoundedRect(selected, 3, 3)

        painter.setBrush(QColor(255, 255, 255))
        painter.drawRoundedRect(left, 3, 3)
        painter.drawRoundedRect(right, 3, 3)
        painter.end()


# --------------------------------------------------------------------------- #
#  Arka planda ffmpeg çalıştıran thread (video_studio.py)
# --------------------------------------------------------------------------- #
class ExportWorker(QThread):
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, cmd, output_path):
        super().__init__()
        self.cmd = cmd
        self.output_path = output_path
        self.process = None

    def run(self):
        try:
            self.process = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace",
            )
            out, _ = self.process.communicate()
            if self.process.returncode == 0:
                self.finished_ok.emit(self.output_path)
            else:
                self.finished_err.emit(out[-2000:] if out else "Bilinmeyen ffmpeg hatası")
        except FileNotFoundError:
            self.finished_err.emit(
                "ffmpeg bulunamadı. ffmpeg.exe / ffprobe.exe programı, bu uygulamayla "
                "aynı klasörde olmalı ya da sistem PATH'inde tanımlı olmalı."
            )
        except Exception as e:
            self.finished_err.emit(str(e))

    def cancel(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()


# --------------------------------------------------------------------------- #
#  Arka planda Shazam ile şarkı tanıyıp dosyayı yeniden adlandıran thread
#  (music_renamer.py'nin tanıma/adlandırma çekirdeği). UI donmasın diye
#  QThread üzerinde çalışır. Tanınamazsa ya da bir hata oluşursa dosyaya
#  KESİNLİKLE dokunmaz.
# --------------------------------------------------------------------------- #
class MusicIDWorker(QThread):
    finished_ok = pyqtSignal(str, str, str)     # new_path, artist, title
    finished_not_found = pyqtSignal()
    finished_err = pyqtSignal(str)

    def __init__(self, path, seek_to, seconds=MUSIC_ID_READ_SECONDS, do_rename=False):
        super().__init__()
        self.path = path
        self.seek_to = seek_to
        self.seconds = seconds
        self.do_rename = do_rename

    def run(self):
        wav_path = None
        try:
            audio = self._extract_audio(self.path, self.seek_to, self.seconds)

            vol = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
            if vol < MUSIC_ID_SILENCE_THRESH:
                self.finished_not_found.emit()
                return

            wav_path = os.path.join(tempfile.gettempdir(), "_hulumedia_musicid.wav")
            self._save_wav(audio, wav_path)

            result = asyncio.run(self._shazam_recognize(wav_path))
            if not result:
                self.finished_not_found.emit()
                return

            artist, title = result["artist"], result["title"]
            if self.do_rename:
                new_path = self._rename_file(self.path, artist, title)
            else:
                new_path = self.path
            self.finished_ok.emit(new_path, artist, title)

        except Exception as e:
            # Herhangi bir hata (ffmpeg okuyamadı, Shazam'a ulaşılamadı,
            # dosya yeniden adlandırılamadı - ör. başka bir program dosyayı
            # kilitliyor - vb.) durumunda dosya olduğu gibi bırakılır.
            self.finished_err.emit(str(e))
        finally:
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass

    @staticmethod
    def _extract_audio(path, seek_to, seconds):
        cmd = [
            FFMPEG, "-v", "quiet",
            "-ss", str(seek_to), "-t", str(seconds),
            "-i", path,
            "-ac", "2", "-ar", str(MUSIC_ID_SAMPLE_RATE),
            "-f", "f32le", "-",
        ]
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW,
        )
        if not result.stdout:
            raise RuntimeError(
                "Ses okunamadı: " + result.stderr.decode(errors="replace")[:200]
            )
        audio = np.frombuffer(result.stdout, dtype="float32")
        if len(audio) % 2 != 0:
            audio = audio[:-1]
        return audio.reshape(-1, 2)

    @staticmethod
    def _save_wav(audio, path):
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).flatten()
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(MUSIC_ID_SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())

    @staticmethod
    async def _shazam_recognize(wav_path):
        shazam = Shazam()
        result = await shazam.recognize(wav_path)
        track = result.get("track")
        if track:
            return {
                "title": track.get("title", "Unknown"),
                "artist": track.get("subtitle", "Unknown Artist"),
            }
        return None

    @staticmethod
    def _rename_file(old_path, artist, title):
        directory, base = os.path.split(old_path)
        ext = os.path.splitext(base)[1]
        safe = re.sub(r'[\\/*?:"<>|]', "", f"{artist} - {title}").strip()
        new_path = os.path.join(directory, safe + ext)
        if os.path.abspath(new_path) == os.path.abspath(old_path):
            return old_path
        counter = 1
        while os.path.exists(new_path):
            new_path = os.path.join(directory, f"{safe} ({counter}){ext}")
            counter += 1
        os.rename(old_path, new_path)
        return new_path


# --------------------------------------------------------------------------- #
#  Ana pencere: HULU-Player oynatma özellikleri + döndür/kırp/dışa aktar
# --------------------------------------------------------------------------- #
class VideoStudioPlayer(QMainWindow):
    fullscreen_toggle_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("HuluMedia")
        # Native (Windows'un kendi) başlık çubuğunu tamamen kaldırıyoruz;
        # pencereyi taşıma/yeniden boyutlandırma artık nativeEvent() içinde
        # ELLE yönetiliyor (bkz. aşağıdaki nativeEvent metodu).
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.resize(1100, 780)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self.current_path = None
        self.native_w = None
        self.native_h = None
        self._last_tracks = []
        self.is_seeking = False
        self.sub_scale = 100
        self.sub_pos = 100
        self.worker = None
        self.progress = None
        self._pending_open_path = None
        self._export_filters = None
        self._export_output_path = None
        self._export_used_hw = False
        self._export_cancelled = False
        self._export_preserve_interlace = False
        self._export_field_order = None
        self._music_id_worker = None

        # Tam ekranda hareketsiz kalan fare imlecini gizleme (bkz.
        # _update_cursor_autohide / _cursor_autohide_tick).
        self._cursor_hidden = False
        self._cursor_idle_ticks = 0
        self._cursor_last_pos = None
        self._cursor_idle_timer = QTimer(self)
        self._cursor_idle_timer.setInterval(100)
        self._cursor_idle_timer.timeout.connect(self._cursor_autohide_tick)

        self._build_ui()
        self._connect_signals()
        self._disable_child_focus()

        self.player = None
        self.timer = None
        # mpv'yi HEMEN burada değil, pencere gerçekten ekrana çıktıktan
        # (show() çağrıldıktan) SONRA kuruyoruz. Aksi halde video_frame'in
        # winId()'si henüz "gerçek" hale gelmeden yakalanmış olur; Qt
        # pencereyi ilk gösterirken native pencereyi yeniden oluşturabilir
        # ve mpv geçersiz kalmış eski bir HWND'ye render etmeye çalışır -
        # bunun sonucu tam olarak "ses var, görüntü simsiyah" hatasıdır.
        QTimer.singleShot(0, self._init_player)

    def _init_player(self):
        wid = str(int(self.video_frame.winId()))
        self.player = mpv.MPV(
            wid=wid,
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            keep_open=True,
            hwdec="no",
            vo="gpu",
            # KRİTİK (Windows): mpv varsayılan olarak D3D11'de "flip-model"
            # sunum kullanıyor. Bu üst düzey pencerelerde sorunsuz çalışır
            # ama bir Qt widget'ının İÇİNE gömülü (child HWND) bir pencerede
            # DWM çoğu zaman görüntüyü doğru bileştiremiyor - mpv'nin kendi
            # logu "başarıyla çizdim" dese bile ekranda hiçbir şey görünmez.
            # Klasik "blt-model" sunuma zorlamak bunu çözer.
            d3d11_flip="no",
        )
        self.player.volume = self.volume_slider.value()

        self.fullscreen_toggle_requested.connect(self.toggle_fullscreen)
        self.player.on_key_press("MBTN_LEFT_DBL")(self.fullscreen_toggle_requested.emit)

        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._update_ui)
        self.timer.start()

        self.crop_overlay.set_active(False)
        self.setFocus()

        # Eğer kullanıcı mpv hazır olmadan bir dosya seçtiyse (çok kısa bir
        # pencere), bekleyen açma isteğini şimdi işleme al.
        if getattr(self, "_pending_open_path", None):
            path, self._pending_open_path = self._pending_open_path, None
            self.open_file(path)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.central_widget = CentralWidget(self)
        self.setCentralWidget(self.central_widget)
        main_layout = QVBoxLayout(self.central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(4)
        self.main_layout = main_layout

        self.btn_open = QPushButton("📂")
        self.btn_open.setFixedWidth(40)
        self.btn_open.setToolTip("Video Aç")

        # ---- pencere kontrol düğmeleri (native başlık çubuğunun yerine) ----
        self.btn_pin = QPushButton("📌")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setToolTip("Her Zaman Üstte Tut")
        self.btn_min = QPushButton("─")
        self.btn_max = QPushButton("▢")
        self.btn_close = QPushButton("✕")
        self.btn_min.setToolTip("Küçült")
        self.btn_max.setToolTip("Büyüt")
        self.btn_close.setToolTip("Kapat")
        for b in (self.btn_pin, self.btn_min, self.btn_max, self.btn_close):
            b.setFixedSize(40, 32)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(
                "QPushButton{background:transparent;border:none;border-radius:0;font-size:14px;}"
                "QPushButton:hover{background:#4a4a4a;}"
                "QPushButton:checked{background:#3a6ea5;}"
            )
        self.btn_close.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:0;font-size:14px;}"
            "QPushButton:hover{background:#e81123;color:white;}"
        )

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(6, 0, 0, 0)
        top_bar.setSpacing(0)
        top_bar.addWidget(self.btn_open)
        top_bar.addStretch(1)
        top_bar.addWidget(self.btn_pin)
        top_bar.addWidget(self.btn_min)
        top_bar.addWidget(self.btn_max)
        top_bar.addWidget(self.btn_close)
        self.top_bar_widget = TitleBar()
        self.top_bar_widget.setFixedHeight(36)
        self.top_bar_widget.setLayout(top_bar)
        self.top_bar_widget.doubleClicked.connect(self._toggle_maximize)
        main_layout.addWidget(self.top_bar_widget)

        # ---- video alanı: mpv + kırpma katmanı aynı hücrede üst üste ----
        self.video_frame = VideoFrame()
        # NOT: bu widget'a KASITLI olarak stylesheet/arka plan rengi VERİLMİYOR.
        # Nedeni VideoFrame.__init__ içindeki yorumda açıklanıyor.
        self.video_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.crop_overlay = CropOverlay()

        # ---- "sinyal yok" logosu: video açılana kadar ortada duran, eski
        # televizyonların UHF kanal arama ekranını andıran kırmızı piksel
        # yazı. video_frame NATIVE bir pencere olduğu için (mpv buraya
        # gömülüyor), sıradan bir QLabel bunun ÜZERİNDE hiç görünmez -
        # CropOverlay'de olduğu gibi bunu da native yapıp raise_() ile üste
        # çıkarmamız gerekiyor.
        self.idle_logo = IdleSignalWidget()

        video_area = QGridLayout()
        video_area.setContentsMargins(0, 0, 0, 0)
        video_area.addWidget(self.video_frame, 0, 0)
        video_area.addWidget(self.crop_overlay, 0, 0)
        video_area.addWidget(self.idle_logo, 0, 0)
        video_container = QWidget()
        video_container.setLayout(video_area)
        main_layout.addWidget(video_container, 1)
        video_container.setCursor(Qt.ArrowCursor)
        self.idle_logo.raise_()

        self.lbl_dim = QLabel(video_container)
        self.lbl_dim.setStyleSheet(
            "background: rgba(0,0,0,170); color: white; padding: 5px 10px;"
            "border-radius: 5px; font-weight: bold; font-size: 12px;"
        )
        self.lbl_dim.move(10, 10)
        self.lbl_dim.hide()

        # ---- oynatma kontrolleri (tam ekranda gizlenir) ----
        self.controls_panel = QWidget()
        controls_panel_layout = QVBoxLayout(self.controls_panel)
        controls_panel_layout.setContentsMargins(6, 4, 6, 0)

        seek_layout = QHBoxLayout()
        self.seek_slider = ClickableSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.time_label = QLabel("00:00 / 00:00")

        # ---- kesme (trim) çubuğu: makas işaretlenince zaman çizgisinin
        # TAM ÜZERİNE biner (aynı hücrede üst üste) - kullanıcı tutamaçları
        # doğrudan zaman çizgisi üzerinde sürükler, sürüklerken video o
        # ana canlı olarak atlar (preview_seek sinyali).
        self.trim_bar = TrimBar()
        self.trim_bar.hide()
        seek_stack = QGridLayout()
        seek_stack.setContentsMargins(0, 0, 0, 0)
        seek_stack.addWidget(self.seek_slider, 0, 0)
        seek_stack.addWidget(self.trim_bar, 0, 0)
        seek_stack_widget = QWidget()
        seek_stack_widget.setLayout(seek_stack)

        seek_layout.addWidget(seek_stack_widget, 1)
        seek_layout.addWidget(self.time_label)
        controls_panel_layout.addLayout(seek_layout)

        # ---- kesme hassas ayar satırı: fareyle piksel piksel sürüklemek
        # uzun bir videoda çok kaba kalıyor (ör. 2 saatlik bir filmde her
        # piksel ~9 saniyeye denk gelebilir) - bu yüzden SANİYE hassasiyetinde
        # ince ayar için küçük +/- düğmeleri ekliyoruz. Sadece kesme aktifken
        # görünür.
        self.lbl_trim_start = QLabel("00:00:00")
        self.lbl_trim_end = QLabel("00:00:00")
        self.btn_trim_start_minus = QPushButton("−1s")
        self.btn_trim_start_plus = QPushButton("+1s")
        self.btn_trim_end_minus = QPushButton("−1s")
        self.btn_trim_end_plus = QPushButton("+1s")
        for b in (self.btn_trim_start_minus, self.btn_trim_start_plus,
                  self.btn_trim_end_minus, self.btn_trim_end_plus):
            b.setFixedWidth(40)
        trim_controls = QHBoxLayout()
        trim_controls.setContentsMargins(0, 0, 0, 0)
        trim_controls.addWidget(QLabel("Başlangıç:"))
        trim_controls.addWidget(self.btn_trim_start_minus)
        trim_controls.addWidget(self.lbl_trim_start)
        trim_controls.addWidget(self.btn_trim_start_plus)
        trim_controls.addSpacing(20)
        trim_controls.addWidget(QLabel("Bitiş:"))
        trim_controls.addWidget(self.btn_trim_end_minus)
        trim_controls.addWidget(self.lbl_trim_end)
        trim_controls.addWidget(self.btn_trim_end_plus)
        trim_controls.addStretch(1)
        self.trim_controls_row = QWidget()
        self.trim_controls_row.setLayout(trim_controls)
        self.trim_controls_row.hide()
        controls_panel_layout.addWidget(self.trim_controls_row)

        controls_layout = QHBoxLayout()
        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedWidth(40)
        controls_layout.addWidget(self.play_btn)

        self.screenshot_btn = QPushButton("📷")
        self.screenshot_btn.setFixedWidth(40)
        self.screenshot_btn.setToolTip("Ekran görüntüsü al (Masaüstüne kaydedilir)")
        controls_layout.addWidget(self.screenshot_btn)

        self.music_id_btn = QPushButton("🎵")
        self.music_id_btn.setFixedWidth(40)
        self.music_id_btn.setToolTip(
            "Şarkıyı Tanı (Shazam)\n"
            "Tanınamazsa dosyaya hiç dokunulmaz."
        )
        controls_layout.addWidget(self.music_id_btn)

        self.chk_auto_rename = QCheckBox("✓")
        self.chk_auto_rename.setChecked(False)
        self.chk_auto_rename.setToolTip(
            "Tanınınca dosyayı otomatik yeniden adlandır\n"
            "(işaretli değilse sadece sonucu gösterir, dosyaya dokunmaz)"
        )
        controls_layout.addWidget(self.chk_auto_rename)

        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setMaximumWidth(150)
        controls_layout.addWidget(self.volume_slider)

        self.btn_mono_l = QPushButton("L")
        self.btn_mono_r = QPushButton("R")
        self.btn_mono_l.setCheckable(True)
        self.btn_mono_r.setCheckable(True)
        self.btn_mono_l.setFixedWidth(28)
        self.btn_mono_r.setFixedWidth(28)
        self.btn_mono_l.setToolTip(
            "Sol Kanalı Mono Yap\n"
            "Sağ kanalda sorun varsa (parazit/sessiz), her iki hoparlörden "
            "de SOL kanalın sesini dinlemenizi sağlar."
        )
        self.btn_mono_r.setToolTip(
            "Sağ Kanalı Mono Yap\n"
            "Sol kanalda sorun varsa (parazit/sessiz), her iki hoparlörden "
            "de SAĞ kanalın sesini dinlemenizi sağlar."
        )
        self.btn_mono_l.setStyleSheet(
            "QPushButton{background:transparent;border:1px solid #555;border-radius:4px;}"
            "QPushButton:checked{background:#3a6ea5;border-color:#3a6ea5;}"
        )
        self.btn_mono_r.setStyleSheet(
            "QPushButton{background:transparent;border:1px solid #555;border-radius:4px;}"
            "QPushButton:checked{background:#3a6ea5;border-color:#3a6ea5;}"
        )
        controls_layout.addWidget(self.btn_mono_l)
        controls_layout.addWidget(self.btn_mono_r)

        controls_layout.addStretch(1)
        controls_panel_layout.addLayout(controls_layout)

        tracks_layout = QHBoxLayout()
        self.audio_combo = QComboBox()
        self.audio_combo.setPlaceholderText("🔊")
        self.audio_combo.setToolTip("Ses Kanalı")
        tracks_layout.addWidget(self.audio_combo)
        self.subtitle_combo = QComboBox()
        self.subtitle_combo.setPlaceholderText("💬")
        self.subtitle_combo.setToolTip("Altyazı")
        tracks_layout.addWidget(self.subtitle_combo)
        self.subtitle_settings_btn = QPushButton("⚙")
        self.subtitle_settings_btn.setFixedWidth(40)
        self.subtitle_settings_btn.setToolTip("Altyazı Ayarları")
        tracks_layout.addWidget(self.subtitle_settings_btn)
        controls_panel_layout.addLayout(tracks_layout)

        main_layout.addWidget(self.controls_panel)

        # ---- düzenleme paneli: döndür + kırp + dışa aktar ----
        self.separator_line = QFrame()
        line = self.separator_line
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#555;")
        main_layout.addWidget(line)

        self.chk_rotate = QCheckBox("🔄")
        self.chk_rotate.setToolTip("Videoyu Döndür")
        self.radio_cw = QRadioButton("↻ 90")
        self.radio_cw.setToolTip("Saat Yönünde 90°")
        self.radio_ccw = QRadioButton("↺ 90")
        self.radio_ccw.setToolTip("Saat Yönü Tersine 90°")
        self.radio_180 = QRadioButton("⤾ 180")
        self.radio_180.setToolTip("180° (Tepetaklak)")
        self.radio_cw.setChecked(True)
        self.rotate_group = QButtonGroup(self)
        self.rotate_group.addButton(self.radio_cw)
        self.rotate_group.addButton(self.radio_ccw)
        self.rotate_group.addButton(self.radio_180)

        self.chk_crop = QCheckBox("⛶")
        self.chk_crop.setToolTip("Kırpmayı Uygula")

        self.chk_trim = QCheckBox("✂")
        self.chk_trim.setToolTip(
            "Kesme (Trim) Aralığı Seç - zaman çizgisinde başlangıç/bitiş "
            "tutamaçlarını sürükleyip yalnızca o aralığı dışa aktarın."
        )

        self.btn_export = QPushButton("💾")
        self.btn_export.setFixedWidth(44)
        self.btn_export.setToolTip("Dışa Aktar")
        self.btn_export.setStyleSheet(
            "QPushButton{background:#ff3b30;color:white;font-weight:bold;padding:8px;border-radius:6px;}"
            "QPushButton:disabled{background:#666;}"
        )

        edit_row = QHBoxLayout()
        edit_row.setContentsMargins(6, 4, 6, 6)
        edit_row.addWidget(self.chk_rotate)
        edit_row.addWidget(self.radio_cw)
        edit_row.addWidget(self.radio_ccw)
        edit_row.addWidget(self.radio_180)
        edit_row.addSpacing(16)
        edit_row.addWidget(self.chk_crop)
        edit_row.addWidget(self.chk_trim)
        edit_row.addStretch(1)
        edit_row.addWidget(self.btn_export)

        self.edit_panel = QWidget()
        self.edit_panel.setLayout(edit_row)
        main_layout.addWidget(self.edit_panel)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#4caf50; padding: 4px 10px;")
        main_layout.addWidget(self.status_label)

        # Kenar şeridindeki yeniden boyutlandırma imleci bu bölümlere miras
        # kalmasın diye hepsine açık ok imleci veriyoruz (çocuk widget'lar
        # - slider, düğme vb. - bunu miras alır; kendi imleci olanlar, ör.
        # TrimBar, kendi imlecini korur).
        for w in (self.controls_panel, self.separator_line,
                  self.edit_panel, self.status_label):
            w.setCursor(Qt.ArrowCursor)

        self._apply_content_margins()

    def _connect_signals(self):
        self.btn_open.clicked.connect(self.open_dialog)
        self.btn_pin.toggled.connect(self._toggle_always_on_top)
        self.btn_min.clicked.connect(self.showMinimized)
        self.btn_max.clicked.connect(self._toggle_maximize)
        self.btn_close.clicked.connect(self.close)
        self.play_btn.clicked.connect(self.toggle_play)
        self.screenshot_btn.clicked.connect(self.take_screenshot)
        self.music_id_btn.clicked.connect(self.identify_and_rename_track)
        self.btn_mono_l.toggled.connect(self._on_mono_l_toggled)
        self.btn_mono_r.toggled.connect(self._on_mono_r_toggled)
        self.volume_slider.valueChanged.connect(self.set_volume)
        self.seek_slider.sliderPressed.connect(self._seek_start)
        self.seek_slider.sliderReleased.connect(self._seek_end)
        self.audio_combo.currentIndexChanged.connect(self._on_audio_changed)
        self.subtitle_combo.currentIndexChanged.connect(self._on_subtitle_changed)
        self.subtitle_settings_btn.clicked.connect(self.open_subtitle_settings)

        self.chk_rotate.stateChanged.connect(self._on_rotate_settings_changed)
        self.radio_cw.toggled.connect(self._on_rotate_settings_changed)
        self.radio_ccw.toggled.connect(self._on_rotate_settings_changed)
        self.radio_180.toggled.connect(self._on_rotate_settings_changed)
        self.chk_crop.stateChanged.connect(self._on_crop_toggle)
        self.chk_trim.stateChanged.connect(self._on_trim_toggle)
        self.trim_bar.changed.connect(self._update_trim_label)
        self.trim_bar.preview_seek.connect(self._on_trim_preview_seek)
        self.btn_trim_start_minus.clicked.connect(lambda: self.trim_bar.nudge_start(-1))
        self.btn_trim_start_plus.clicked.connect(lambda: self.trim_bar.nudge_start(1))
        self.btn_trim_end_minus.clicked.connect(lambda: self.trim_bar.nudge_end(-1))
        self.btn_trim_end_plus.clicked.connect(lambda: self.trim_bar.nudge_end(1))
        self.btn_export.clicked.connect(self.do_export)

        self.video_frame.resized.connect(self._update_video_rect)
        self.crop_overlay.changed.connect(self._update_dim_label)

    def _disable_child_focus(self):
        """Ok tuşlarının HER ZAMAN pencereye (ileri/geri sarma) gitmesi için
        tüm kontrol widget'larının klavye odağını kapatıyoruz."""
        for w in (
            self.btn_open, self.btn_pin, self.btn_min, self.btn_max, self.btn_close,
            self.play_btn, self.screenshot_btn, self.music_id_btn, self.chk_auto_rename,
            self.btn_mono_l, self.btn_mono_r, self.volume_slider,
            self.seek_slider, self.audio_combo, self.subtitle_combo,
            self.subtitle_settings_btn, self.chk_rotate, self.radio_cw,
            self.radio_ccw, self.radio_180, self.chk_crop, self.chk_trim,
            self.trim_bar, self.btn_trim_start_minus, self.btn_trim_start_plus,
            self.btn_trim_end_minus, self.btn_trim_end_plus, self.btn_export,
        ):
            w.setFocusPolicy(Qt.NoFocus)

    # ------------------------------------------------------------- video açma
    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Video Seç", "", "Video Dosyaları (*.mp4 *.mov *.mkv *.avi *.m4v *.webm)"
        )
        if path:
            self.open_file(path)

    # ------------------------------------------------------------- tek örnek
    def activate_and_focus(self):
        """İkinci bir örnek (ör. dosya ilişkilendirmesiyle çift tıklanan
        yeni bir video) algılandığında çağrılır: pencere simge durumunda
        veya arka plandaysa geri getirip öne alır."""
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def open_file(self, path):
        if not os.path.isfile(path):
            return
        if self.player is None:
            # mpv henüz kurulmadı (çok nadir bir zamanlama durumu) - dosyayı
            # kaydedip mpv hazır olur olmaz otomatik açacağız.
            self._pending_open_path = path
            return
        self.current_path = path
        self.idle_logo.hide()
        self.native_w = self.native_h = None
        self.status_label.setText("")
        self.lbl_dim.hide()
        self.chk_trim.setChecked(False)
        self.trim_bar.hide()
        self.trim_bar.reset()
        self.btn_mono_l.setChecked(False)
        self.btn_mono_r.setChecked(False)
        self._apply_audio_pan(None)

        try:
            self.player.command("loadfile", path, "replace")
            self.player.pause = False
            self.player.sid = False  # altyazı her açılışta varsayılan olarak KAPALI
        except Exception as e:
            print("Video açılamadı:", e)
            return

        # Taramalı (interlaced) kaynaklarda OYNATMA sırasında hareketli
        # alanlarda (ör. kayan yazı, kamera hareketi) "tarak/çizgi çizgi"
        # (combing) görüntüsü oluşur, çünkü mpv varsayılan olarak
        # deinterlace yapmıyor (hwdec="no" + vo="gpu" ile otomatik
        # devreye girmiyor). mpv'nin kendi deinterlace desteğini SADECE
        # kaynak gerçekten taramalıysa açıyoruz; progressive kaynaklarda
        # dokunulmuyor. Bu sadece OYNATMAYI etkiler - dışa aktarılan
        # dosya do_export()'ta kaynağın tarama tipini zaten kendi başına
        # koruyor/yönetiyor.
        try:
            self.player.deinterlace = self._get_source_field_order(path) is not None
        except Exception:
            pass

        self.play_btn.setText("⏸")
        self.setWindowTitle(f"HuluMedia - {os.path.basename(path)}")

        self._apply_subtitle_live(self.sub_scale, self.sub_pos)
        self._apply_rotation_to_player()

        QTimer.singleShot(700, self._on_video_ready)
        self.setFocus()

    def _on_video_ready(self):
        try:
            w, h = self.player.width, self.player.height
        except Exception:
            w = h = None
        if w and h:
            self.native_w, self.native_h = w, h
        self.populate_tracks()
        self._update_video_rect()

    # ------------------------------------------------------------- sürükle bırak
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if not path:
            return
        if path.lower().endswith(".srt"):
            if self.current_path:
                self.load_external_subtitle(path)
        else:
            self.open_file(path)

    def load_external_subtitle(self, path):
        try:
            self.player.sub_add(path, flags="select")
        except Exception as e:
            print("Altyazı eklenemedi:", e)
        QTimer.singleShot(500, self.populate_tracks)

    # ------------------------------------------------------------ oynatma
    def toggle_play(self):
        try:
            self.player.pause = not self.player.pause
            self.play_btn.setText("▶" if self.player.pause else "⏸")
        except Exception:
            pass

    def take_screenshot(self):
        if not self.current_path:
            return
        desktop_dir = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        if not desktop_dir:
            desktop_dir = os.path.expanduser("~")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(desktop_dir, f"ekran_goruntusu_{timestamp}.png")
        try:
            self.player.screenshot_to_file(filename, includes="subtitles")
        except Exception as e:
            print("Ekran görüntüsü alınamadı:", e)

    # ------------------------------------------------------- şarkı tanıma
    def identify_and_rename_track(self):
        """Şu an TIMELINE'DA NEREDEYSE (oynatma konumundan) ~10 saniyelik
        ses alıp Shazam ile tanımaya çalışır. "Otomatik yeniden adlandır"
        işaretliyse ve tanırsa dosyayı "Sanatçı - Şarkı" olarak yeniden
        adlandırır; işaretli değilse sadece sonucu gösterir, dosyaya
        dokunmaz. Tanıyamazsa veya bir hata olursa dosyaya HİÇ dokunmaz."""
        if not self.current_path:
            self.status_label.setText("Önce bir dosya açın.")
            return
        if self._music_id_worker is not None:
            return  # zaten devam eden bir tanıma var

        try:
            seek_to = self.player.time_pos or 0.0
        except Exception:
            seek_to = 0.0

        self.music_id_btn.setEnabled(False)
        self.status_label.setText("🎵 Şarkı tanınıyor…")

        do_rename = self.chk_auto_rename.isChecked()
        self._music_id_worker = MusicIDWorker(self.current_path, seek_to, do_rename=do_rename)
        self._music_id_worker.finished_ok.connect(self._on_music_id_ok)
        self._music_id_worker.finished_not_found.connect(self._on_music_id_not_found)
        self._music_id_worker.finished_err.connect(self._on_music_id_err)
        self._music_id_worker.start()

    def _music_id_cleanup(self):
        self.music_id_btn.setEnabled(True)
        self._music_id_worker = None

    def _on_music_id_ok(self, new_path, artist, title):
        # Dosya yeniden adlandırıldıysa (auto-rename işaretliyse); mpv aynı
        # dosya tanıtıcısını (handle) kullanmaya devam ettiği için oynatma
        # kesintiye uğramaz - sadece kendi takip ettiğimiz yolu ve pencere
        # başlığını güncelliyoruz. İşaretli değilse new_path == eski yol
        # olur, bu satırlar zararsız birer no-op olur.
        renamed = new_path != self.current_path
        self.current_path = new_path
        self.setWindowTitle(f"HuluMedia - {os.path.basename(new_path)}")
        if renamed:
            self.status_label.setText(f"✓ Tanındı ve yeniden adlandırıldı: {artist} - {title}")
        else:
            self.status_label.setText(f"✓ Tanındı: {artist} - {title}")
        self._music_id_cleanup()

    def _on_music_id_not_found(self):
        self.status_label.setText("Şarkı tanınamadı. Dosyaya dokunulmadı.")
        self._music_id_cleanup()

    def _on_music_id_err(self, message):
        self.status_label.setText("Tanıma başarısız oldu. Dosyaya dokunulmadı.")
        print("Şarkı tanıma hatası:", message)
        self._music_id_cleanup()

    def seek_relative(self, seconds):
        try:
            self.player.seek(seconds, reference="relative")
        except Exception:
            pass

    def _seek_start(self):
        self.is_seeking = True

    def _seek_end(self):
        try:
            duration = self.player.duration
        except Exception:
            duration = None
        if duration:
            new_time = (self.seek_slider.value() / 1000) * duration
            try:
                self.player.time_pos = new_time
            except Exception:
                pass
        self.is_seeking = False

    def _update_ui(self):
        if self.is_seeking:
            return
        try:
            duration = self.player.duration
            current = self.player.time_pos
        except Exception:
            duration, current = None, None

        # Video sona erdiğinde (keep_open nedeniyle mpv orada duraklıyor)
        # son karede takılı kalmak yerine başa sar ve duraklat.
        try:
            if self.player.eof_reached:
                self.player.command("seek", 0, "absolute")
                self.player.pause = True
        except Exception:
            pass

        if duration and current is not None:
            pos = int((current / duration) * 1000)
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(max(0, min(1000, pos)))
            self.seek_slider.blockSignals(False)

        # Kesme (trim) modu aktifken zaman etiketi seçili aralığı gösterir
        # (_update_trim_label), normal oynatma zamanını değil.
        if self.chk_trim.isChecked():
            if duration:
                self.trim_bar.set_duration(duration)
            self._update_trim_label()
        else:
            self.time_label.setText(f"{fmt_time(current)} / {fmt_time(duration)}")

        try:
            self.play_btn.setText("▶" if self.player.pause else "⏸")
        except Exception:
            pass

    # ------------------------------------------------------------- klavye
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Right:
            self.seek_relative(3)
        elif event.key() == Qt.Key_Left:
            self.seek_relative(-3)
        elif event.key() == Qt.Key_Space:
            self.toggle_play()
        elif event.key() == Qt.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
        else:
            super().keyPressEvent(event)

    def toggle_fullscreen(self):
        chrome_widgets = (
            self.top_bar_widget, self.controls_panel,
            self.separator_line, self.edit_panel, self.status_label,
        )
        if self.isFullScreen():
            self.showNormal()
            for w in chrome_widgets:
                w.setVisible(True)
        else:
            for w in chrome_widgets:
                w.setVisible(False)
            self.showFullScreen()
        self._apply_content_margins()
        self._update_cursor_autohide()
        self.setFocus()

    # Tam ekranda imleç bu kadar süre (100 ms'lik tik) hareketsiz kalırsa
    # gizlenir; fare oynayınca hemen geri gelir. 20 tik = 2 saniye.
    CURSOR_HIDE_TICKS = 20

    def _update_cursor_autohide(self):
        """Tam ekrana girince imleç gizlemeyi başlatır, çıkınca durdurur ve
        imleci eski haline getirir. İki katmanlı çalışır: mpv'nin kendi
        'cursor-autohide' seçeneği (mpv penceresi imleci kendi yönettiği
        için) + Qt tarafında zamanlayıcıyla video alanının imleci."""
        fs = self.isFullScreen()
        try:
            if self.player is not None:
                self.player["cursor-autohide"] = 2000 if fs else "no"
        except Exception:
            pass

        self._cursor_idle_ticks = 0
        self._cursor_last_pos = QCursor.pos()
        if fs:
            self._cursor_idle_timer.start()
        else:
            self._cursor_idle_timer.stop()
            self._show_cursor()

    def _hide_cursor(self):
        if not self._cursor_hidden:
            self._cursor_hidden = True
            self.video_frame.setCursor(Qt.BlankCursor)

    def _show_cursor(self):
        if self._cursor_hidden:
            self._cursor_hidden = False
            self.video_frame.setCursor(Qt.ArrowCursor)

    def _cursor_autohide_tick(self):
        pos = QCursor.pos()
        if pos != self._cursor_last_pos:
            self._cursor_last_pos = pos
            self._cursor_idle_ticks = 0
            self._show_cursor()
            return
        self._cursor_idle_ticks += 1
        if self._cursor_idle_ticks >= self.CURSOR_HIDE_TICKS:
            self._hide_cursor()

    def _apply_content_margins(self):
        """Pencere normal (ne büyütülmüş ne tam ekran) haldeyken kenarlarda
        ince bir boşluk bırakır - buradan tutup yeniden boyutlandırılabilir
        (bkz. CentralWidget). Büyütülmüş/tam ekranken boşluğa gerek yok,
        video kenara kadar dolsun diye 0'a çekiyoruz."""
        if self.isMaximized() or self.isFullScreen():
            self.main_layout.setContentsMargins(0, 0, 0, 0)
        else:
            b = CentralWidget.BORDER
            self.main_layout.setContentsMargins(b, b, b, b)

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _toggle_always_on_top(self, checked):
        # setWindowFlags() pencereyi native olarak yeniden oluşturur ve bu
        # sırada GİZLER; bu yüzden mevcut durumu (büyütülmüş/tam ekran)
        # hatırlayıp değişiklikten sonra aynı durumda tekrar göstermemiz
        # gerekiyor, yoksa pencere normale dönmüş/kaybolmuş gibi görünür.
        was_fullscreen = self.isFullScreen()
        was_maximized = self.isMaximized()
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        if was_fullscreen:
            self.showFullScreen()
        elif was_maximized:
            self.showMaximized()
        else:
            self.show()
        self.setFocus()

    def changeEvent(self, event):
        if event.type() == event.WindowStateChange and hasattr(self, "btn_max"):
            if self.isMaximized():
                self.btn_max.setText("❐")
                self.btn_max.setToolTip("Eski Boyuta Getir")
            else:
                self.btn_max.setText("▢")
                self.btn_max.setToolTip("Büyüt")
        if hasattr(self, "central_widget"):
            self._apply_content_margins()
        super().changeEvent(event)

    # ------------------------------------------------- ses / altyazı track
    def populate_tracks(self):
        try:
            tracks = self.player.track_list
        except Exception:
            tracks = []

        # Dışa aktarımda (özellikle çoklu ses izli MKV'lerde) tam olarak
        # HANGİ kanalın seçili olduğunu bilebilmek için ham track listesini
        # saklıyoruz - "ff-index" alanı mpv'nin bu iz için bildirdiği,
        # ffmpeg'in de "-map 0:<ff-index>" ile doğrudan anlayacağı ham akış
        # numarasıdır.
        self._last_tracks = tracks

        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        audio_current_index = -1
        for t in tracks:
            if t.get("type") == "audio":
                label = t.get("title") or t.get("lang") or f"Track {t.get('id')}"
                self.audio_combo.addItem(label, t.get("id"))
                if t.get("selected"):
                    audio_current_index = self.audio_combo.count() - 1
        if audio_current_index == -1 and self.audio_combo.count() > 0:
            audio_current_index = 0
        self.audio_combo.setCurrentIndex(audio_current_index)
        self.audio_combo.blockSignals(False)

        self.subtitle_combo.blockSignals(True)
        self.subtitle_combo.clear()
        self.subtitle_combo.addItem("Kapalı", -1)
        subtitle_current_index = 0
        for t in tracks:
            if t.get("type") == "sub":
                label = t.get("title") or t.get("lang") or f"Track {t.get('id')}"
                self.subtitle_combo.addItem(label, t.get("id"))
                if t.get("selected"):
                    subtitle_current_index = self.subtitle_combo.count() - 1
        self.subtitle_combo.setCurrentIndex(subtitle_current_index)
        self.subtitle_combo.blockSignals(False)

    def _on_audio_changed(self, index):
        if index < 0:
            return
        track_id = self.audio_combo.itemData(index)
        if track_id is not None:
            try:
                self.player.aid = track_id
            except Exception:
                pass
        self.setFocus()

    def _on_subtitle_changed(self, index):
        if index < 0:
            return
        track_id = self.subtitle_combo.itemData(index)
        try:
            self.player.sid = False if track_id == -1 else track_id
        except Exception:
            pass
        self.setFocus()

    def open_subtitle_settings(self):
        dlg = SubtitleSettingsDialog(self, self.sub_scale, self.sub_pos, self._apply_subtitle_live)
        result = dlg.exec_()
        if result == QDialog.Accepted:
            self.sub_scale, self.sub_pos = dlg.values()
        self.setFocus()

    def _apply_subtitle_live(self, scale_percent, pos_value):
        try:
            self.player.sub_scale = scale_percent / 100.0
            self.player.sub_pos = pos_value
        except Exception:
            pass

    # --------------------------------------------------------------- ses
    def set_volume(self, value):
        try:
            self.player.volume = value
        except Exception:
            pass

    def adjust_volume(self, delta):
        new_value = max(0, min(100, self.volume_slider.value() + delta))
        self.volume_slider.setValue(new_value)

    # ------------------------------------------------------------------ #
    #  SOL/SAĞ KANAL MONO - bir kanalda sorun olduğunda (parazit/sessiz),
    #  sağlam kanalı HER İKİ hoparlörden birden dinlemeyi sağlar. mpv'nin
    #  "af" (audio filter) özelliğine ffmpeg'in "pan" filtresini vererek
    #  yapılıyor. Tek bir "M" düğmesi değil iki ayrı düğme (L/R) olmasının
    #  sebebi: hangi kanalın bozuk olduğunu önceden bilemiyoruz, kullanıcı
    #  kulağıyla karar verip ilgili düğmeye basıyor.
    # ------------------------------------------------------------------ #
    def _on_mono_l_toggled(self, checked):
        if checked:
            self.btn_mono_r.setChecked(False)
            self._apply_audio_pan("left")
        elif not self.btn_mono_r.isChecked():
            self._apply_audio_pan(None)

    def _on_mono_r_toggled(self, checked):
        if checked:
            self.btn_mono_l.setChecked(False)
            self._apply_audio_pan("right")
        elif not self.btn_mono_l.isChecked():
            self._apply_audio_pan(None)

    def _apply_audio_pan(self, mode):
        # mpv'nin kendi yerleşik filtre listesinde "pan" YOK - bu bir
        # ffmpeg/libavfilter filtresi. mpv'de libavfilter filtrelerini
        # kullanmak için "lavfi=[...]" sarmalayıcısı ŞART; bare "pan=..."
        # mpv tarafından tanınmayan bir filtre adı olarak reddediliyordu
        # (ve hatayı sessizce yutuyorduk, bu yüzden fark edilmemişti).
        try:
            if mode == "left":
                self.player.af = "lavfi=[pan=stereo|c0=c0|c1=c0]"
            elif mode == "right":
                self.player.af = "lavfi=[pan=stereo|c0=c1|c1=c1]"
            else:
                self.player.af = ""
        except Exception as e:
            print("Mono ses filtresi uygulanamadı:", e)

    # ------------------------------------------------------------------ #
    #  DÖNDÜRME (gerçek zamanlı, mpv "video-rotate" ile)
    # ------------------------------------------------------------------ #
    def _on_rotate_settings_changed(self, *_):
        self._apply_rotation_to_player()
        self._update_video_rect()

    def _rotation_mode(self):
        """'none' | 'cw' | 'ccw' | '180' döndürür."""
        if not self.chk_rotate.isChecked():
            return "none"
        if self.radio_cw.isChecked():
            return "cw"
        if self.radio_ccw.isChecked():
            return "ccw"
        return "180"

    def _apply_rotation_to_player(self):
        angle = {"none": 0, "cw": 90, "ccw": 270, "180": 180}[self._rotation_mode()]
        try:
            self.player["video-rotate"] = angle
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  KIRPMA - görüntülenen (letterbox dışı) video alanını hesaplayıp
    #  CropOverlay'e bildirir
    # ------------------------------------------------------------------ #
    def _on_crop_toggle(self, *_):
        self.crop_overlay.set_active(self.chk_crop.isChecked())
        self._update_video_rect()

    # ------------------------------------------------------------------ #
    #  KESME (trim) - zaman çizgisinin altında bir aralık çubuğu gösterip
    #  saklar; dışa aktarımda -ss/-t olarak ffmpeg'e verilir.
    # ------------------------------------------------------------------ #
    def _on_trim_toggle(self, *_):
        checked = self.chk_trim.isChecked()
        self.trim_bar.setVisible(checked)
        self.trim_controls_row.setVisible(checked)
        if checked:
            try:
                duration = self.player.duration
            except Exception:
                duration = None
            try:
                current = self.player.time_pos or 0.0
            except Exception:
                current = 0.0
            if duration:
                self.trim_bar.set_duration(duration)
                # Tam olarak durduğun/izlediğin noktadan BAŞLAYIP ileri
                # doğru 1 dakikalık bir şerit aç - tüm videoyu seçili
                # göstermek yerine, "makas"ı aktif ettiğin andan itibaren
                # küçük, kullanışlı bir aralık.
                self.trim_bar.set_window_seconds(current, 60.0)
            else:
                self.trim_bar.reset()
        self._update_trim_label()

    def _update_trim_label(self):
        if not self.chk_trim.isChecked():
            return
        start_s = self.trim_bar.start_seconds()
        end_s = self.trim_bar.end_seconds()
        self.time_label.setText(f"✂ {fmt_time(start_s)} - {fmt_time(end_s)}")
        self.lbl_trim_start.setText(fmt_time_hms(start_s))
        self.lbl_trim_end.setText(fmt_time_hms(end_s))

    def _on_trim_preview_seek(self, ratio):
        """Kullanıcı trim tutamacını sürüklerken çağrılır: videoyu o ana
        canlı olarak atlatır, böylece hangi sahnede olduğu görülür."""
        try:
            duration = self.player.duration
        except Exception:
            duration = None
        if not duration:
            return
        try:
            self.player.pause = True
            self.player.time_pos = max(0.0, min(duration, ratio * duration))
        except Exception:
            pass

    def _compute_display_rect(self) -> QRect:
        if not self.native_w or not self.native_h:
            return QRect()
        # 90°/270° boyutları YER DEĞİŞTİRİR (genişlik<->yükseklik);
        # 0° ve 180° boyutları AYNI kalır (sadece görüntü çevrilir).
        if self._rotation_mode() in ("cw", "ccw"):
            disp_w, disp_h = self.native_h, self.native_w
        else:
            disp_w, disp_h = self.native_w, self.native_h
        ww, wh = self.video_frame.width(), self.video_frame.height()
        if ww <= 0 or wh <= 0 or disp_w <= 0 or disp_h <= 0:
            return QRect()
        scale = min(ww / disp_w, wh / disp_h)
        show_w, show_h = disp_w * scale, disp_h * scale
        x = (ww - show_w) / 2
        y = (wh - show_h) / 2
        return QRect(int(x), int(y), int(show_w), int(show_h))

    def _update_video_rect(self):
        self.crop_overlay.set_video_rect(self._compute_display_rect())
        self._update_dim_label()

    def _compute_crop_rect(self):
        """Kırpma dikdörtgenini ORİJİNAL (döndürülmemiş) video piksel
        uzayında (x, y, w, h) olarak döndürür - ffmpeg crop filtresi bu
        uzayda çalışır, transpose/180° filtresi zincirde SONRA uygulanır."""
        if not self.native_w or not self.native_h:
            return None
        ratio = self.crop_overlay.crop_ratio()
        if ratio is None:
            return None
        rx_r, ry_r, rw_r, rh_r = ratio

        mode = self._rotation_mode()
        if mode in ("cw", "ccw"):
            disp_w, disp_h = self.native_h, self.native_w
        else:
            disp_w, disp_h = self.native_w, self.native_h
        rx, ry = rx_r * disp_w, ry_r * disp_h
        rw, rh = rw_r * disp_w, rh_r * disp_h

        w0, h0 = self.native_w, self.native_h

        if mode == "none":
            x, y, w, h = rx, ry, rw, rh
        elif mode == "cw":
            # 90° saat yönü: orijinal -> ekran dönüşümünün tersi
            x = ry
            y = h0 - rx - rw
            w = rh
            h = rw
        elif mode == "ccw":
            # 90° saat yönü tersi (270°)
            x = w0 - ry - rh
            y = rx
            w = rh
            h = rw
        else:
            # 180° (tepetaklak): her iki eksen de ters çevrilir, boyutlar
            # AYNI kalır (genişlik<->yükseklik yer değiştirmez).
            x = w0 - rx - rw
            y = h0 - ry - rh
            w = rw
            h = rh

        x, y = max(0, int(x)), max(0, int(y))
        w, h = int(w), int(h)
        w -= w % 2
        h -= h % 2
        if x + w > w0:
            x -= (x + w - w0)
        if y + h > h0:
            y -= (y + h - h0)
        x, y = max(0, x), max(0, y)
        return x, y, w, h

    def _update_dim_label(self):
        if not self.chk_crop.isChecked():
            self.lbl_dim.hide()
            return
        dims = self._compute_crop_rect()
        if not dims:
            self.lbl_dim.hide()
            return
        _, _, w, h = dims
        out_w, out_h = (h, w) if self._rotation_mode() in ("cw", "ccw") else (w, h)
        ratio = aspect_ratio_str(out_w, out_h)
        self.lbl_dim.setText(f"{out_w} x {out_h}  ({ratio})")
        self.lbl_dim.adjustSize()
        self.lbl_dim.show()
        self.lbl_dim.raise_()

    # ------------------------------------------------------------------ #
    #  Bitrate okuma (ffprobe) - video_studio.py
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    #  Dışa aktarımda SADECE seçili video + seçili ses izini eşliyoruz.
    #  Neden: -map hiç belirtilmezse ffmpeg kendi "en iyi" akışı seçme
    #  sezgisiyle bir iz seçer - çoklu ses izli (İngilizce stereo + 5.1 +
    #  Atmos/TrueHD gibi) MKV dosyalarında bu çoğunlukla oynatıcıda dinlemek
    #  istediğiniz iz DEĞİL, en çok kanala sahip iz (ör. Atmos) olur; bu izin
    #  "-c:a copy" ile kopyalanması bazı durumlarda ffmpeg'i başarısız
    #  ediyor. Kullanıcının player'da seçtiği ses izini mpv'nin bildirdiği
    #  "ff-index" (ffmpeg'in "-map 0:<N>" ile anladığı ham akış numarası)
    #  üzerinden doğrudan eşleyerek bunu garanti altına alıyoruz.
    # ------------------------------------------------------------------ #
    def _get_export_stream_maps(self):
        tracks = self._last_tracks or []
        video_ff = None
        for t in tracks:
            if t.get("type") == "video" and t.get("selected"):
                video_ff = t.get("ff-index")
                break

        selected_audio_id = self.audio_combo.currentData()
        audio_ff = None
        for t in tracks:
            if t.get("type") == "audio" and t.get("id") == selected_audio_id:
                audio_ff = t.get("ff-index")
                break

        maps = [
            "-map", f"0:{video_ff}" if video_ff is not None else "0:v:0",
            "-map", f"0:{audio_ff}" if audio_ff is not None else "0:a:0?",
            # Altyazılar varsa oldukları gibi (kopyalanarak) taşınır; -map
            # hiç belirtilmediğinde ffmpeg zaten benzer şekilde davranıyordu,
            # bu davranışı koruyoruz. "?" işareti, hiç altyazı yoksa
            # ffmpeg'in hata vermeden devam etmesini sağlar.
            "-map", "0:s?", "-c:s", "copy",
        ]
        return maps

    def _get_source_bitrate(self, path):
        def probe(entries):
            try:
                out = subprocess.check_output(
                    [FFPROBE, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", entries, "-of", "default=noprint_wrappers=1:nokey=1", path],
                    creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace",
                ).strip().splitlines()
                for v in out:
                    if v.isdigit():
                        return int(v)
            except Exception:
                return None
            return None

        rate = probe("stream=bit_rate")
        if not rate:
            try:
                out = subprocess.check_output(
                    [FFPROBE, "-v", "error", "-show_entries", "format=bit_rate",
                     "-of", "default=noprint_wrappers=1:nokey=1", path],
                    creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace",
                ).strip()
                if out.isdigit():
                    rate = int(out)
            except Exception:
                rate = None
        return rate

    # ------------------------------------------------------------------ #
    #  Tarama tipi (interlaced/progressive) okuma (ffprobe)
    # ------------------------------------------------------------------ #
    def _get_source_field_order(self, path):
        """Kaynağın alan sırasını döndürür: 'tff' (üstten taramalı),
        'bff' (alttan taramalı) ya da progressive/bilinmiyor ise None.
        ffprobe 'field_order' alanı tipik olarak "tt"/"tb" (top field
        first) ya da "bb"/"bt" (bottom field first) ya da "progressive"
        değerlerinden birini döndürür."""
        try:
            out = subprocess.check_output(
                [FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=field_order",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace",
            ).strip().lower()
        except Exception:
            return None
        if out.startswith("t"):
            return "tff"
        if out.startswith("b"):
            return "bff"
        return None  # "progressive", "unknown" ya da boş çıktı

    def _get_source_fps(self, path):
        """Kaynağın kare hızını ffmpeg'in anlayacağı kesirli metin olarak
        döndürür (ör. '25/1', '30000/1001'), ya da okunamıyorsa None.
        Kesirli haliyle kullanmak, ondalık yuvarlamadan (ör. 29.97'yi
        29.970000 gibi yazıp uzun videolarda ses/görüntü kaymasına yol
        açmaktan) kaçınır. Taramayı korurken bunu ffmpeg'e AÇIKÇA
        bildirmek gerekiyor: aksi halde x264 MBAFF (interlaced) modunda
        çıktıya net bir sabit kare hızı (CFR) damgalanmıyor, bu yüzden
        MediaInfo gibi araçlar "Frame rate mode: Variable" gösterip
        somut bir fps değeri vermiyor - içerik aslında sabit hızda olsa
        bile."""
        try:
            out = subprocess.check_output(
                [FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace",
            ).strip()
        except Exception:
            return None
        if not out or out in ("0/0", "N/A"):
            return None
        return out

    def _get_source_bit_depth(self, path):
        """Kaynağın piksel derinliğini (8, 10, 12...) döndürür, okunamazsa
        8 varsayar. H.264 donanım kodlayıcılar (nvenc/qsv/amf) çoğu sürücüde
        10-bit+ girdiyi (çoğu HDR HEVC/AV1 içeriğin formatı) DESTEKLEMEZ -
        H.264 standardının "High 10" profili donanımda neredeyse hiç
        bulunmaz. Bu yüzden 10-bit+ kaynaklarda GPU'yu hiç DENEMEDEN
        doğrudan CPU'ya (libx264) gidiyoruz - aksi halde önce başarısız
        olması beklenen bir GPU denemesi için boşuna zaman kaybedilir,
        sonra zaten CPU ile baştan başlanır (net sonuç: iki kat bekleme)."""
        try:
            out = subprocess.check_output(
                [FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=bits_per_raw_sample,pix_fmt",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace",
            ).strip().splitlines()
        except Exception:
            return 8
        for line in out:
            line = line.strip()
            if line.isdigit() and int(line) > 0:
                return int(line)
        # bits_per_raw_sample boş dönmüş olabilir (bazı konteynerlerde
        # yazılmaz) - piksel formatı adından tahmin et (ör. "yuv420p10le").
        for line in out:
            if "10le" in line or "10be" in line:
                return 10
            if "12le" in line or "12be" in line:
                return 12
        return 8

    # ------------------------------------------------------------------ #
    #  DIŞA AKTARMA (tek ffmpeg çağrısında kırpma + döndürme) - video_studio.py
    # ------------------------------------------------------------------ #
    def do_export(self):
        if not self.current_path or not self.native_w:
            return
        try:
            self.player.pause = True
        except Exception:
            pass

        do_rotate = self.chk_rotate.isChecked()
        do_crop = self.chk_crop.isChecked() and self.crop_overlay.crop_ratio() is not None
        do_trim = self.chk_trim.isChecked() and not self.trim_bar.is_full_range()

        if not do_rotate and not do_crop and not do_trim:
            QMessageBox.information(self, "Bilgi",
                                     "Ne döndürme, ne kırpma, ne de kesme (trim) "
                                     "seçili. Uygulanacak bir işlem yok.")
            return

        trim_start = trim_end = None
        if do_trim:
            trim_start = self.trim_bar.start_seconds()
            trim_end = self.trim_bar.end_seconds()
            if trim_end - trim_start < 0.1:
                QMessageBox.warning(self, "Hata", "Kesme aralığı çok kısa.")
                return

        if not check_ffmpeg():
            QMessageBox.critical(
                self, "ffmpeg bulunamadı",
                "Dışa aktarım için ffmpeg gereklidir (PATH'te ya da bu dosyayla "
                "aynı klasörde bulunmalı)."
            )
            return

        # Kaynak taramalı (interlaced) mı? Döndürme YOKSA tarama tipini
        # (alan sırasını) doğrudan koruyoruz. HERHANGİ bir döndürmede
        # (90°/180°/270° fark etmez) artık "bob" deinterlace uyguluyoruz:
        # her alan kendi başına tam bir progressive kareye çevrilir (kare
        # hızı 2 katına çıkar) - weave'in aksine taraklanma (combing)
        # OLUŞMAZ. (Not: 180° için önce "tff/bff bayrağını ters çevirerek
        # taramalı kalsın" diye bir yol denendi; teoride doğru görünse de
        # pratikte decoder'larda "field order" hatasına - hareketin
        # sekmesine/atlamasına - yol açtığı görüldüğü için kaldırıldı.
        # Bob deinterlace daha az kod, daha güvenilir sonuç veriyor.)
        rotate_mode = self._rotation_mode() if do_rotate else None
        field_order = self._get_source_field_order(self.current_path)
        is_interlaced_source = field_order in ("tff", "bff")

        preserve_interlace = is_interlaced_source and not do_rotate
        bob_deinterlace = is_interlaced_source and do_rotate

        filters = []
        if bob_deinterlace:
            # yadif'i EN BAŞA koyuyoruz: transpose/crop'tan önce alanları
            # tam progressive karelere ayırıyor, böylece sonraki crop/
            # rotate filtreleri sıradan progressive görüntü üzerinde
            # çalışıyor - alan hizası (field parity) derdi kalmıyor.
            # deint=interlaced -> sadece gerçekten taramalı işaretli
            # kareleri işler, progressive kareye dokunmaz.
            filters.append("yadif=mode=send_field:parity=auto:deint=interlaced")
            self.status_label.setText(
                "Not: Döndürme + taramalı kaynak -> bob deinterlace "
                "uygulanıyor, çıktı progressive olacak ve kare hızı 2 katına çıkacak."
            )

        if do_crop:
            dims = self._compute_crop_rect()
            if not dims:
                return
            x, y, w, h = dims
            if preserve_interlace:
                if y % 2 != 0:
                    # Alan (field) hizasını bozmamak için üst kırpma
                    # sınırını çift satıra yuvarla (aksi halde tepe/alt
                    # alan sırası (tff/bff) değişebilir).
                    y -= 1
                if h % 4 != 0:
                    # libx264 taramalı (MBAFF/interlaced) kodlamada
                    # yüksekliğin 4'e bölünebilir olmasını zorunlu tutuyor
                    # ("height not divisible by 4" hatası); aksi halde
                    # kodlayıcı hiç açılamıyor ve export tamamen
                    # başarısız oluyor.
                    h -= h % 4
            if w < 2 or h < 2:
                QMessageBox.warning(self, "Hata", "Kırpma alanı çok küçük.")
                return
            filters.append(f"crop={w}:{h}:{x}:{y}")
        if do_rotate:
            mode = rotate_mode
            if mode == "cw":
                filters.append("transpose=1")
            elif mode == "ccw":
                filters.append("transpose=2")
            elif mode == "180":
                # ffmpeg'de tek adımlık "180°" filtresi yok; yatay+dikey
                # ayna (hflip+vflip) matematiksel olarak 180° döndürmenin
                # tam karşılığıdır ve boyutu değiştirmez.
                filters.append("hflip")
                filters.append("vflip")

        base, ext = os.path.splitext(self.current_path)
        ext = ext if ext else ".mp4"
        suffix = (
            ("_cropped" if do_crop else "")
            + ("_rotated" if do_rotate else "")
            + ("_kesildi" if do_trim else "")
        )
        output_path = f"{base}{suffix}{ext}"

        # Sonraki export'ta (ya da donanım kodlayıcı başarısız olup CPU'ya
        # düşerken) tekrar kullanmak için sakla.
        self._export_filters = filters
        self._export_output_path = output_path
        self._export_preserve_interlace = preserve_interlace
        self._export_field_order = field_order if preserve_interlace else None
        self._export_trim = (trim_start, trim_end) if do_trim else None

        # Donanım kodlayıcılarda (nvenc/qsv/amf) taramalı (interlaced)
        # kodlama VE 10-bit+ (çoğu HDR HEVC/AV1 kaynağı) girdi desteği
        # güvenilir/tutarlı değil; bu durumlarda GPU'yu hiç denemeden
        # doğrudan libx264 (CPU) yoluna gidiyoruz - aksi halde önce
        # başarısız olması beklenen bir GPU denemesi için boşuna
        # beklenir, sonra zaten CPU ile baştan başlanır.
        bit_depth = self._get_source_bit_depth(self.current_path)
        hw_encoder = detect_hw_encoder()
        use_hw = bool(hw_encoder) and not preserve_interlace and bit_depth <= 8
        self._start_export(filters, output_path, use_hw=use_hw)

    def _build_export_cmd(self, filters, output_path, use_hw: bool):
        """ffmpeg komutunu oluşturur. use_hw=True ve bir donanım kodlayıcı
        tespit edildiyse GPU kodlama dener; aksi halde (ya da GPU kodlama
        çalışma anında başarısız olup CPU'ya düşüldüyse) yazılımsal
        libx264 kullanılır. Donanım yolu yalnızca kaynak bitrate'i
        bilindiğinde kullanılır - kalite (crf) modu yalnızca libx264'te
        güvenilir şekilde çalışır."""
        bitrate = self._get_source_bitrate(self.current_path)
        hw_encoder = detect_hw_encoder() if use_hw else None

        # KESME (trim): İKİ AŞAMALI seek kullanıyoruz.
        #   1) "-ss" -i'DEN ÖNCE (girdi/input seek) -> ffmpeg en yakın
        #      anahtar kareye HIZLICA atlar, dosyanın başından o ana kadar
        #      TÜM kareleri çözümlemek ZORUNDA KALMAZ. Uzun bir filmde
        #      geç bir dakikadan kesim yaparken bekleme süresini saniyelere
        #      indiren asıl kısım burası.
        #   2) İkinci küçük "-ss" -i'DEN SONRA (çıktı/output seek) -> 1.
        #      adımdaki anahtar kareyle gerçek hedef an arasındaki birkaç
        #      saniyelik farkı KARE HASSASİYETİYLE kapatır. Zaten yeniden
        #      kodladığımız için bu küçük parçayı çözmek neredeyse anlık.
        trim = getattr(self, "_export_trim", None)
        cmd = [FFMPEG, "-y"]
        if trim:
            trim_start, trim_end = trim
            # NOT: Daha önce burada "-ss BÜYÜK -i dosya -ss KÜÇÜK" şeklinde
            # iki aşamalı bir seek vardı ("hızlı kabaca atla + ince ayarla"
            # tekniği). Bu, video AKIŞINI OLDUĞU GİBİ KOPYALAYARAK (-c copy)
            # kesen durumlar için gereklidir - ama biz zaten HER ZAMAN
            # görüntüyü yeniden kodluyoruz (crop/rotate filtreleri var).
            # Yeniden kodlarken ffmpeg'in TEK bir "-ss" (girdi tarafında)
            # kullanımı da hem HIZLIDIR (en yakın anahtar kareye dosyanın
            # başından hiç okumadan atlar) HEM DE KARE HASSASİYETİNDEDİR
            # (o anahtar kareden hedef ana kadar kareleri çözüp atar).
            # İki aşamalı yöntem, kaynağa/konteynere göre birkaç saniye
            # ERKEN başlama hatasına yol açabiliyordu - tek aşamalı seek
            # bunu ortadan kaldırır.
            cmd += ["-ss", f"{trim_start:.3f}", "-i", self.current_path,
                    "-t", f"{max(0.0, trim_end - trim_start):.3f}"]
        else:
            cmd += ["-i", self.current_path]

        video_filters = list(filters)
        if hw_encoder and bitrate:
            # Güvenlik önlemi: bit derinliği tespiti yanlış çıksa bile
            # (ör. beklenmedik bir konteyner/etiketleme durumunda),
            # donanım kodlayıcıya giden görüntüyü filtre zincirinde
            # AÇIKÇA 8-bit'e çeviriyoruz. Sondaki "-pix_fmt" çıktı
            # seçeneğine güvenmek yerine bunu filtergraph içinde yapmak,
            # donanım kodlayıcılarla ilgili format uyumsuzluğu
            # hatalarını daha güvenilir şekilde önlüyor.
            video_filters.append("format=yuv420p")
        if video_filters:
            cmd += ["-filter:v", ",".join(video_filters)]

        cmd += self._get_export_stream_maps()

        if hw_encoder and bitrate:
            bufsize = bitrate * 2
            cmd += ["-c:v", hw_encoder, "-b:v", str(bitrate),
                    "-maxrate", str(bitrate), "-bufsize", str(bufsize)]
            if hw_encoder == "h264_nvenc":
                cmd += ["-rc:v", "vbr", "-preset", "p4"]
        elif bitrate:
            bufsize = bitrate * 2
            cmd += ["-c:v", "libx264", "-b:v", str(bitrate), "-minrate", str(bitrate),
                    "-maxrate", str(bitrate), "-bufsize", str(bufsize), "-preset", "medium"]
        else:
            cmd += ["-c:v", "libx264", "-crf", "16", "-preset", "slow"]

        # Tarama tipini (interlaced) koruma: sadece libx264 yolunda (donanım
        # kodlayıcılarda güvenilir değil, do_export() zaten bu durumda
        # use_hw=False gönderiyor). "-flags +ildct+ilme" x264'e alan bazlı
        # (field-based) DCT/hareket kestirimi yaptırır, "-x264opts tff/bff"
        # ise çıktı bitakışına doğru alan sırasını (SEI/VUI) yazar; aksi
        # halde x264 varsayılan olarak progressive kodlar.
        field_order = getattr(self, "_export_field_order", None)
        if self._export_preserve_interlace and field_order in ("tff", "bff") and not (hw_encoder and bitrate):
            cmd += ["-flags:v", "+ildct+ilme",
                    "-x264opts", "tff=1" if field_order == "tff" else "bff=1"]
            # x264 MBAFF (interlaced) modunda kare hızını açıkça
            # belirtmezsek, çıktıya net bir CFR (sabit kare hızı) bilgisi
            # yazılmıyor ve MediaInfo gibi araçlar "Frame rate mode:
            # Variable" gösterip somut bir fps değeri veremiyor. Kaynağın
            # kare hızını okuyup hem -r ile hem de -fps_mode cfr ile
            # zorluyoruz ki çıktıda net ve doğru fps bilgisi olsun.
            source_fps = self._get_source_fps(self.current_path)
            if source_fps:
                cmd += ["-r", source_fps]
            cmd += ["-fps_mode", "cfr"]

        cmd += ["-pix_fmt", "yuv420p", "-c:a", "copy", "-map_metadata", "0", output_path]
        return cmd, bool(hw_encoder and bitrate)

    def _start_export(self, filters, output_path, use_hw: bool):
        cmd, used_hw = self._build_export_cmd(filters, output_path, use_hw)
        self._export_used_hw = used_hw
        self._export_cancelled = False

        self.progress = QProgressDialog("Video işleniyor, lütfen bekleyin...", "İptal", 0, 0, self)
        self.progress.setWindowTitle("Dışa Aktar")
        self.progress.setWindowModality(Qt.WindowModal)
        self.progress.setMinimumDuration(0)
        self.progress.canceled.connect(self._cancel_export)

        self.worker = ExportWorker(cmd, output_path)
        self.worker.finished_ok.connect(self._export_done)
        self.worker.finished_err.connect(self._export_failed)
        self.worker.start()
        self.progress.show()

    def _cancel_export(self):
        # Kullanıcı BİLEREK iptal etti - bu bir hata değil. İşaretliyoruz
        # ki hem yanlış "başarısız oldu" mesajı çıkmasın, hem de GPU->CPU
        # otomatik tekrar deneme mekanizması burada YANLIŞLIKLA devreye
        # girip export'u istemeden CPU ile baştan başlatmasın.
        self._export_cancelled = True
        if self.worker:
            self.worker.cancel()

    def _export_done(self, output_path):
        if self.progress:
            self.progress.close()
        QMessageBox.information(self, "Tamamlandı", f"Video başarıyla kaydedildi:\n{output_path}")

    def _export_failed(self, message):
        if self.progress:
            self.progress.close()
        if getattr(self, "_export_cancelled", False):
            self.status_label.setText("Dışa aktarma iptal edildi.")
            return
        # GPU kodlama listede görünse bile o bilgisayarda gerçekten
        # çalışacağının garantisi yok (sürücü sorunu vb.) - başarısız
        # olduysa kullanıcıyı hiç rahatsız etmeden CPU ile sessizce
        # tekrar deniyoruz.
        if getattr(self, "_export_used_hw", False):
            self.status_label.setText("GPU ile kodlama başarısız oldu, CPU ile tekrar deneniyor…")
            self._start_export(self._export_filters, self._export_output_path, use_hw=False)
            return
        QMessageBox.critical(self, "Hata", f"İşlem başarısız oldu:\n\n{message}")



def apply_dark_theme(app: QApplication):
    """Koyu gri tema. BİLİNÇLİ OLARAK setStyleSheet() KULLANMIYORUZ -
    uygulama genelinde bir QSS, video_frame gibi native-gömülü widget'lara
    da otomatik olarak "styled background" davranışı bulaştırır ve bu da
    daha önce çözülen "video simsiyah" hatasını GERİ GETİRİR. QPalette ise
    bu native/özel çizilen widget'lara hiç dokunmadan geri kalan tüm arayüzü
    (butonlar, kutucuklar, etiketler, iletişim pencereleri) koyulaştırır."""
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(43, 43, 43))
    palette.setColor(QPalette.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.Base, QColor(30, 30, 30))
    palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ToolTipBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ToolTipText, QColor(255, 255, 255))
    palette.setColor(QPalette.Text, QColor(255, 255, 255))
    palette.setColor(QPalette.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ButtonText, QColor(255, 255, 255))
    palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.Link, QColor(100, 170, 255))
    palette.setColor(QPalette.Highlight, QColor(65, 105, 190))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 120))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 120))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(120, 120, 120))
    app.setPalette(palette)


def main():
    app = QApplication(sys.argv)
    apply_dark_theme(app)

    candidate = None
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        candidate = sys.argv[1]

    # --- Tek örnek (single instance) kontrolü ---------------------------
    # Video dosyaları bu uygulamayla ilişkilendirildiğinde, kullanıcı her
    # video dosyasına çift tıkladığında işletim sistemi YENİ bir işlem
    # başlatır. Zaten çalışan bir örnek varsa bu yeni işlem, dosya yolunu
    # ona bir yerel soket üzerinden iletip HEMEN kendi kapanır; böylece
    # hep TEK pencere kalır ve yeni video o pencerenin ÜZERİNE açılır.
    probe_sock = QLocalSocket()
    probe_sock.connectToServer(SINGLE_INSTANCE_KEY)
    if probe_sock.waitForConnected(200):
        payload = candidate if candidate else "__FOCUS__"
        probe_sock.write(payload.encode("utf-8"))
        probe_sock.flush()
        probe_sock.waitForBytesWritten(500)
        probe_sock.disconnectFromServer()
        return  # bu işlem burada sonlanır; yeni pencere AÇILMAZ
    probe_sock.abort()

    if not check_ffmpeg():
        QMessageBox.warning(
            None, "ffmpeg bulunamadı",
            "ffmpeg bulunamadı; oynatma yine de çalışır, ancak 'Dışa Aktar' "
            "(döndürme/kırpma) özelliği kullanılamayacak.\n\n"
            "Windows:  winget install ffmpeg\n"
            "macOS:    brew install ffmpeg\n"
            "Linux:    sudo apt install ffmpeg"
        )

    win = VideoStudioPlayer()
    win.show()
    win.setFocus()

    # Bu örnek "birincil" örnek oldu: gelecekteki dosya açma isteklerini
    # (yeni video çift tıklamalarını) dinlemek için yerel sunucuyu kur.
    # removeServer(): önceki bir çökme yüzünden isim kalıntısı kalmışsa
    # temizler; aksi halde listen() sessizce başarısız olabilir.
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer()
    server.listen(SINGLE_INSTANCE_KEY)

    def _handle_new_connection():
        client = server.nextPendingConnection()
        if not client:
            return
        if client.bytesAvailable() == 0:
            client.waitForReadyRead(300)
        data = bytes(client.readAll()).decode("utf-8", errors="ignore").strip()
        client.disconnectFromServer()
        win.activate_and_focus()
        if data and data != "__FOCUS__" and os.path.isfile(data):
            win.open_file(data)

    server.newConnection.connect(_handle_new_connection)
    # Referansları canlı tut (yerel değişkenler fonksiyon bitince silinip
    # çöp toplanmasın diye pencereye iliştiriyoruz).
    win._single_instance_server = server

    # Windows dosya ilişkilendirmesi (bir video dosyasına çift tıklayınca)
    # uygulamayı "HuluMedia.exe C:\...\video.mp4" şeklinde, dosya yolunu
    # komut satırı argümanı olarak vererek çalıştırır. Bunu okuyup otomatik
    # açmazsak pencere açılır ama hiçbir video yüklenmez.
    if candidate:
        win.open_file(candidate)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

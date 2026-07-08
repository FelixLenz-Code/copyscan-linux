#!/usr/bin/env python3
"""
Kopierer - Scan-, Druck- und PDF-Werkzeug für Linux.

Verbindet einen beliebigen SANE-Scanner (Flachbett oder mit automatischem
Seiteneinzug/ADF) mit einem CUPS-Drucker, damit man wie an einem Kopierer
arbeiten kann:
Blatt einscannen  ->  Vorschau prüfen  ->  drucken und/oder als PDF speichern.

Abhängigkeiten (unter Ubuntu/Debian bereits vorhanden):
    - scanimage (Paket: sane-utils)
    - lp / lpstat (Paket: cups-client)
    - python3-pyqt5, python3-pil, img2pdf
"""

import os
import re
import sys
import glob
import shutil
import tempfile
import subprocess

import img2pdf

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer, QSettings, QRectF
from PyQt5.QtGui import QPixmap, QIcon, QImage, QPen, QColor, QPainter
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox,
    QSpinBox, QListWidget, QListWidgetItem, QVBoxLayout, QHBoxLayout,
    QFormLayout, QGroupBox, QScrollArea, QFileDialog, QMessageBox,
    QProgressBar, QFrame, QSizePolicy, QStyle, QTabWidget, QSlider,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem,
)

# --- Papiergrößen in Millimetern (Breite x Höhe) ---------------------------
# "Ganze Fläche" nutzt die volle A4-Breite; Scanner mit kleinerem oder
# größerem Maximum beschneiden bzw. füllen das automatisch.
PAGE_SIZES = {
    "A4 (210 × 297 mm)":     (210.0, 297.0),
    "A5 (148 × 210 mm)":     (148.0, 210.0),
    "Letter (216 × 279 mm)": (216.0, 279.0),
    "Ganze Fläche":          (216.0, 297.0),
}

# Zuordnung Format -> CUPS-Medienname (für `lp -o media=...`)
CUPS_MEDIA = {
    "A4 (210 × 297 mm)":     "A4",
    "A5 (148 × 210 mm)":     "A5",
    "Letter (216 × 279 mm)": "Letter",
    "Ganze Fläche":          "A4",
}

RESOLUTIONS = ["150", "300", "600", "1200"]

SCAN_MODES = {
    "Farbe":        "Color",
    "Graustufen":   "Gray",
    "Schwarz/Weiß": "Lineart",
}

# Druckqualität -> IPP-Wert für `lp -o print-quality=...`
# 3 = Entwurf, 4 = Normal, 5 = Hoch. Standardisiertes IPP-Attribut, das die
# meisten CUPS-Treiber auf ihre eigene Auflösung/Qualität abbilden.
PRINT_QUALITY = {
    "Entwurf (schnell)": "3",
    "Normal":            "4",
    "Hoch (beste)":      "5",
}


def source_is_adf(name):
    """True, wenn eine SANE-Quelle ein automatischer Einzug ist (ADF/Duplex)."""
    return bool(name) and bool(re.search(r"adf|feeder|duplex", name, re.I))


# ---------------------------------------------------------------------------
#  Hintergrund-Worker
# ---------------------------------------------------------------------------
class DeviceScanWorker(QThread):
    """Sucht per `scanimage -L` nach verfügbaren Scannern (langsam -> Thread)."""
    finished_ok = pyqtSignal(list)   # Liste von (device_id, beschreibung)

    def run(self):
        devices = []
        try:
            out = subprocess.run(
                ["scanimage", "-L"],
                capture_output=True, text=True, timeout=60,
            ).stdout
            # Zeilen der Form:  device `backend:id' is a Hersteller Modell ...
            for m in re.finditer(r"device `([^']+)' is a (.+)", out):
                devices.append((m.group(1), m.group(2).strip()))
        except Exception:
            pass
        self.finished_ok.emit(devices)


class SourceProbeWorker(QThread):
    """Fragt für ein Gerät die verfügbaren Quellen (Flachbett/ADF/Duplex) ab."""
    finished_ok = pyqtSignal(list)   # Liste von SANE-Quellnamen (Strings)

    def __init__(self, device):
        super().__init__()
        self.device = device

    def run(self):
        sources = []
        try:
            out = subprocess.run(
                ["scanimage", "-A", "-d", self.device],
                capture_output=True, text=True, timeout=30,
            ).stdout
            # Zeile der Form:  --source Flatbed|Automatic Document Feeder [Flatbed]
            m = re.search(r"--source\s+([^\[\n]+)", out)
            if m:
                for s in m.group(1).split("|"):
                    s = s.strip()
                    if s:
                        sources.append(s)
        except Exception:
            pass
        self.finished_ok.emit(sources)


def _embed_dpi(path, resolution):
    """Bettet die Scan-Auflösung als DPI ins PNG ein.

    scanimage schreibt keine DPI-Information. Ohne sie nimmt img2pdf 96 dpi an,
    wodurch Druck/PDF unabhängig von der gewählten Auflösung gleich aussehen.
    Mit korrektem DPI ist die Seite physikalisch richtig groß (z. B. A4) und
    höhere dpi = schärfer."""
    try:
        from PIL import Image
        dpi = int(resolution)
        im = Image.open(path)
        im.load()
        im.save(path, dpi=(dpi, dpi))
    except (ValueError, OSError):
        pass   # 'auto' o. Ä.: dann eben ohne DPI-Einbettung


class ScanWorker(QThread):
    """Scannt eine oder – beim automatischen Einzug (ADF) – mehrere Seiten.

    Flachbett:  eine Seite mit Fortschrittsanzeige.
    ADF/Duplex: Batch-Scan, der so lange einzieht, bis der Einzug leer ist;
                jede fertige Seite wird sofort über `page_done` gemeldet."""
    progress = pyqtSignal(int)
    page_done = pyqtSignal(str)       # Pfad je fertig gescannter Seite
    finished_ok = pyqtSignal(int)     # Gesamtzahl der Seiten
    failed = pyqtSignal(str)

    def __init__(self, device, resolution, mode, source, width_mm, height_mm,
                 prefix):
        super().__init__()
        self.device = device
        self.resolution = resolution
        self.mode = mode
        self.source = source          # SANE-Quellname oder None (nicht angeben)
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.prefix = prefix          # Dateipfad-Präfix für die PNG(s)

    def _base_cmd(self):
        cmd = [
            "scanimage",
            "-d", self.device,
            "--resolution", self.resolution,
            "--mode", self.mode,
            "--format=png",
            "-x", f"{self.width_mm}",
            "-y", f"{self.height_mm}",
        ]
        if self.source:
            cmd += ["--source", self.source]
        return cmd

    def run(self):
        try:
            if source_is_adf(self.source):
                self._run_adf()
            else:
                self._run_single()
        except Exception as e:
            self.failed.emit(str(e))

    def _run_single(self):
        out_path = self.prefix + "001.png"
        cmd = self._base_cmd() + ["--progress"]
        with open(out_path, "wb") as out_file:
            proc = subprocess.Popen(cmd, stdout=out_file, stderr=subprocess.PIPE)
            # stderr enthält "Progress: NN.N%" (mit \r getrennt)
            buf = b""
            while True:
                chunk = proc.stderr.read(64)
                if not chunk:
                    break
                buf += chunk
                for part in re.split(rb"[\r\n]", buf)[:-1]:
                    m = re.search(rb"([\d.]+)%", part)
                    if m:
                        try:
                            self.progress.emit(int(float(m.group(1))))
                        except ValueError:
                            pass
                buf = re.split(rb"[\r\n]", buf)[-1]
            err = proc.stderr.read().decode(errors="ignore")
            proc.wait()

        if proc.returncode != 0 or not os.path.getsize(out_path):
            raise RuntimeError(err or f"scanimage endete mit Code {proc.returncode}")

        _embed_dpi(out_path, self.resolution)
        self.progress.emit(100)
        self.page_done.emit(out_path)
        self.finished_ok.emit(1)

    def _run_adf(self):
        # Batch-Modus: scanimage zieht Blatt für Blatt ein und schreibt je Seite
        # eine Datei, bis der Einzug leer ist. Duplex-Quellen liefern automatisch
        # doppelt so viele Seiten (Vorder-/Rückseite).
        pattern = self.prefix + "%03d.png"
        cmd = self._base_cmd() + [f"--batch={pattern}"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        produced = sorted(glob.glob(self.prefix + "[0-9]" * 3 + ".png"))
        # Verlässlicher als der Rückgabecode (SANE meldet den leeren Einzug als
        # "Fehler"): entscheidend ist, ob mindestens eine Seite entstanden ist.
        if not produced:
            raise RuntimeError(
                (res.stderr or "").strip()
                or "Keine Seite eingezogen. Liegt Papier im Einzug?")
        for i, p in enumerate(produced, 1):
            _embed_dpi(p, self.resolution)
            self.page_done.emit(p)
        self.finished_ok.emit(len(produced))


class PrintWorker(QThread):
    """Schickt eine fertige PDF-Datei via `lp` an den Drucker."""
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, pdf_path, printer, copies, options=None):
        super().__init__()
        self.pdf_path = pdf_path
        self.printer = printer
        self.copies = copies
        self.options = options or []

    def run(self):
        cmd = ["lp", "-d", self.printer, "-n", str(self.copies)]
        for opt in self.options:
            cmd += ["-o", opt]
        cmd.append(self.pdf_path)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(res.stderr.strip() or "lp fehlgeschlagen")
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
#  Seite mit nicht-destruktiver Bildbearbeitung
# ---------------------------------------------------------------------------
def pil_to_qpixmap(img):
    """Wandelt ein PIL-Bild in ein QPixmap (immer über RGB)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())   # .copy() -> eigener Puffer


# ---------------------------------------------------------------------------
#  Intelligente Scan-Verbesserung (reines Pillow – keine schweren Extra-Deps)
# ---------------------------------------------------------------------------
def _row_activity_variance(gray):
    """Maß dafür, wie stark sich die Zeilenmittelwerte unterscheiden.

    Ein Bild auf 1 px Breite verkleinern liefert je Zeile den Mittelwert; die
    Varianz dieser Werte ist maximal, wenn Textzeilen sauber waagerecht liegen
    (dunkle Zeilen / helle Zwischenräume). Grundlage der Schräglagen-Erkennung."""
    from PIL import Image
    h = gray.height
    col = gray.resize((1, h), Image.BILINEAR)
    vals = list(col.getdata())
    n = len(vals) or 1
    mean = sum(vals) / n
    return sum((v - mean) ** 2 for v in vals) / n


def detect_skew(gray, limit=8.0, step=0.4):
    """Erkennt die Schräglage einer Textseite in Grad (0, wenn unsicher)."""
    from PIL import Image, ImageOps
    # klein rechnen (Tempo) und Kanten betonen, damit Textzeilen dominieren
    small = gray.copy()
    small.thumbnail((700, 700), Image.BILINEAR)
    small = ImageOps.autocontrast(small, cutoff=2)
    base_score = _row_activity_variance(small)
    best_angle, best_score = 0.0, base_score
    a = -limit
    while a <= limit + 1e-9:
        if abs(a) >= 1e-6:
            rot = small.rotate(a, resample=Image.BILINEAR, fillcolor=255)
            score = _row_activity_variance(rot)
            if score > best_score:
                best_score, best_angle = score, a
        a += step
    # nur anwenden, wenn deutlich besser als ungedreht und nicht winzig
    if abs(best_angle) < 0.3 or best_score < base_score * 1.05:
        return 0.0
    return best_angle


def white_balance(img):
    """Gray-World-Weißabgleich: Farbstich entfernen, indem die Kanalmittelwerte
    aneinander angeglichen werden."""
    from PIL import Image, ImageStat
    means = ImageStat.Stat(img).mean[:3]
    gray = sum(means) / 3.0
    chans = []
    for ch, m in zip(img.split()[:3], means):
        s = gray / m if m > 1 else 1.0
        s = min(max(s, 0.6), 1.6)          # Übersteuern begrenzen
        chans.append(ch.point(lambda p, s=s: int(min(255, p * s))))
    return Image.merge("RGB", chans)


def flatten_background(img):
    """Ungleichmäßige Ausleuchtung ausgleichen und das Papier weiß ziehen:
    jeden Kanal durch einen grob geschätzten Hintergrund teilen."""
    from PIL import Image, ImageFilter, ImageMath
    w, h = img.size
    gray = img.convert("L")
    # Hintergrund schätzen: stark verkleinern (Text verschwindet), glätten, zurück
    sw, sh = max(1, w // 24), max(1, h // 24)
    bg = gray.resize((sw, sh), Image.BILINEAR).filter(ImageFilter.GaussianBlur(2))
    bg = bg.resize((w, h), Image.BILINEAR)
    out = []
    for ch in img.split()[:3]:
        norm = ImageMath.eval(
            "convert(min(a * 255 / (b + 1), 255), 'L')", a=ch, b=bg)
        out.append(norm)
    return Image.merge("RGB", out)


def smart_enhance(img, skew_angle=0.0):
    """Vollständige Dokument-Autoverbesserung: entzerren, Weißabgleich,
    Hintergrund-/Beleuchtungsausgleich, Kontrast, Nachschärfen."""
    from PIL import Image, ImageOps, ImageFilter
    if abs(skew_angle) >= 0.3:
        img = img.rotate(skew_angle, resample=Image.BILINEAR,
                         fillcolor=(255, 255, 255), expand=True)
    img = white_balance(img)
    img = flatten_background(img)
    img = ImageOps.autocontrast(img, cutoff=1)
    # UnsharpMask mit Schwellwert schärft Kanten, ohne feines Rauschen zu betonen
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=110, threshold=3))
    return img


class Page:
    """Eine gescannte Seite samt nicht-destruktiver Bearbeitung.

    Das rohe Scan-PNG (``original``) bleibt unangetastet. Alle Änderungen sind
    nur Parameter, die bei Bedarf frisch auf das Original angewandt werden. Für
    die Ausgabe (Miniatur/PDF/Druck) wird das Ergebnis nach ``work_path``
    gerendert."""

    PREVIEW_MAX = 1600   # Kantenlänge des schnellen Vorschau-Basisbildes

    def __init__(self, original_path, fmt_name):
        self.original = original_path
        self.fmt_name = fmt_name
        self.work_path = os.path.splitext(original_path)[0] + "_edit.png"
        self.rotation = 0        # 0/90/180/270 Grad im Uhrzeigersinn
        self.crop = None         # (l, t, r, b) als Anteile 0..1 (nach Drehung)
        self.color = "color"     # "color" | "gray" | "bw"
        self.contrast = 1.0
        self.brightness = 1.0
        self.auto = False        # automatische Dokumentverbesserung an/aus
        self._base = None        # gecachtes, verkleinertes Rohbild (für Vorschau)
        self._dpi = None
        # Zwischenspeicher für den teuren Geometrie-/Enhance-Schritt, damit er
        # beim Ziehen der Helligkeit/Kontrast-Regler nicht ständig neu läuft.
        self._geo_cache = {}     # 'base'/'full' -> (signatur, PIL-Bild)
        self._skew_sig = None    # self.rotation, für die _skew_val gilt
        self._skew_val = 0.0
        self.render()

    # -- interne Bild-Pipeline ----------------------------------------------
    def _open_original(self):
        from PIL import Image
        im = Image.open(self.original)
        im.load()
        self._dpi = im.info.get("dpi")
        return im.convert("RGB")

    def _base_image(self):
        if self._base is None:
            base = self._open_original()
            base.thumbnail((self.PREVIEW_MAX, self.PREVIEW_MAX))
            self._base = base
        return self._base

    def _skew_angle(self):
        """Erkannte Schräglage (aus dem Basisbild, gecacht je 90°-Drehung),
        damit Vorschau und Vollbild exakt denselben Winkel verwenden."""
        if self._skew_sig != self.rotation:
            base = self._base_image()
            if self.rotation:
                base = base.rotate(-self.rotation, expand=True)
            self._skew_val = detect_skew(base.convert("L"))
            self._skew_sig = self.rotation
        return self._skew_val

    def _geo(self, which):
        """Drehung (90°-Schritte) + optionale Auto-Verbesserung – der teure
        Teil, zwischengespeichert nach (Drehung, Auto)."""
        sig = (self.rotation, self.auto)
        cached = self._geo_cache.get(which)
        if cached and cached[0] == sig:
            return cached[1]
        img = self._base_image() if which == "base" else self._open_original()
        if self.rotation:
            img = img.rotate(-self.rotation, expand=True)   # im Uhrzeigersinn
        if self.auto:
            img = smart_enhance(img, self._skew_angle())
        self._geo_cache[which] = (sig, img)
        return img

    def _apply(self, geo_img, skip_crop=False):
        """Auf das (gedrehte/verbesserte) Bild noch Zuschnitt, Helligkeit,
        Kontrast und Farbmodus anwenden – die günstigen, live veränderbaren
        Schritte."""
        from PIL import ImageEnhance, ImageOps
        img = geo_img
        if self.crop and not skip_crop:
            w, h = img.size
            l, t, r, b = self.crop
            box = (int(l * w), int(t * h), int(r * w), int(b * h))
            if box[2] > box[0] and box[3] > box[1]:
                img = img.crop(box)
        if self.brightness != 1.0:
            img = ImageEnhance.Brightness(img).enhance(self.brightness)
        if self.contrast != 1.0:
            img = ImageEnhance.Contrast(img).enhance(self.contrast)
        if self.color == "gray":
            img = ImageOps.grayscale(img).convert("RGB")
        elif self.color == "bw":
            g = ImageOps.grayscale(img)
            img = g.point(lambda p: 255 if p >= 128 else 0, mode="1").convert("RGB")
        return img

    # -- öffentlich ----------------------------------------------------------
    def render(self):
        """Rendert das bearbeitete Vollbild nach work_path (für die Ausgabe)."""
        img = self._apply(self._geo("full"))
        kwargs = {"dpi": self._dpi} if self._dpi else {}
        img.save(self.work_path, **kwargs)
        return self.work_path

    def preview_pixmap(self, skip_crop=False):
        """Schnelle Vorschau aus dem verkleinerten Basisbild."""
        return pil_to_qpixmap(self._apply(self._geo("base"), skip_crop=skip_crop))

    def pixel_size(self):
        """(Breite, Höhe) der Ausgabedatei in Pixeln."""
        img = QImage(self.work_path)
        return img.width(), img.height()

    def thumb_icon(self):
        return QIcon(QPixmap(self.work_path).scaled(
            96, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def reset(self):
        self.rotation = 0
        self.crop = None
        self.color = "color"
        self.contrast = 1.0
        self.brightness = 1.0
        self.auto = False

    def output_entry(self):
        """(Pfad, Formatname) für die Ausgabe. Zugeschnittene Seiten weichen vom
        Standardformat ab -> Formatname None, damit die natürliche Größe (per
        DPI) statt einer A4-Streckung verwendet wird."""
        return (self.work_path, None if self.crop else self.fmt_name)


# ---------------------------------------------------------------------------
#  Vorschau: zoom-/verschiebbar, mit optionalem Zuschnitt-Rechteck
# ---------------------------------------------------------------------------
class PreviewView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self._scene.addItem(self._item)
        self.setRenderHints(QPainter.SmoothPixmapTransform | QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QColor("#14171f"))
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumSize(400, 500)
        self._has_image = False
        self._crop_mode = False
        self._rubber = None
        self._crop_origin = None

    # -- Bild setzen / löschen ----------------------------------------------
    def set_pixmap(self, pixmap, fit=True):
        old_w = self._item.pixmap().width()
        self._item.setPixmap(pixmap)
        self._item.setOffset(0, 0)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._has_image = not pixmap.isNull()
        if fit:
            self.fit()
        elif old_w and pixmap.width() and old_w != pixmap.width():
            # Nur die Pixelzahl hat sich geändert (schnelle Vorschau <-> volle
            # Auflösung). Bildschirmgröße konstant halten, damit der Zoom nicht
            # springt.
            r = old_w / pixmap.width()
            self.scale(r, r)
        self.viewport().update()

    def clear_image(self):
        self._item.setPixmap(QPixmap())
        self._has_image = False
        self.viewport().update()

    def fit(self):
        if self._has_image:
            self.fitInView(self._item, Qt.KeepAspectRatio)

    def zoom(self, factor):
        if self._has_image:
            self.scale(factor, factor)

    def zoom_actual(self):
        """Originalgröße (1 Bildpixel = 1 Bildschirmpixel) – zeigt die volle
        Scan-Auflösung."""
        if self._has_image:
            self.resetTransform()
            self.centerOn(self._item)

    def wheelEvent(self, event):
        if self._has_image and not self._crop_mode:
            self.scale(1.25 if event.angleDelta().y() > 0 else 0.8,
                       1.25 if event.angleDelta().y() > 0 else 0.8)
        else:
            super().wheelEvent(event)

    def drawForeground(self, painter, rect):
        if not self._has_image:
            painter.resetTransform()
            painter.setPen(QColor("#8b93a7"))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter,
                             "Noch keine Seite gescannt.\n\n"
                             "Lege ein Dokument ein und\nklicke „Seite scannen“.")

    # -- Zuschnitt -----------------------------------------------------------
    def is_cropping(self):
        return self._crop_mode

    def enter_crop_mode(self, initial_frac=None):
        self._crop_mode = True
        self.setDragMode(QGraphicsView.NoDrag)
        w = self._item.pixmap().width()
        h = self._item.pixmap().height()
        self._rubber = QGraphicsRectItem()
        pen = QPen(QColor("#4a6cf7"))
        pen.setCosmetic(True)
        pen.setWidth(2)
        self._rubber.setPen(pen)
        self._rubber.setBrush(QColor(74, 108, 247, 55))
        self._scene.addItem(self._rubber)
        if initial_frac:
            l, t, r, b = initial_frac
            self._rubber.setRect(QRectF(l * w, t * h, (r - l) * w, (b - t) * h))
        else:
            self._rubber.setRect(QRectF(0, 0, w, h))

    def exit_crop_mode(self):
        self._crop_mode = False
        self._crop_origin = None
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        if self._rubber is not None:
            self._scene.removeItem(self._rubber)
            self._rubber = None

    def crop_fractions(self):
        """Aktuelles Zuschnitt-Rechteck als Anteile 0..1 oder None (zu klein)."""
        if self._rubber is None:
            return None
        r = self._rubber.rect().normalized()
        w = self._item.pixmap().width()
        h = self._item.pixmap().height()
        if not w or not h:
            return None
        l = max(0.0, r.left() / w)
        t = max(0.0, r.top() / h)
        rr = min(1.0, r.right() / w)
        bb = min(1.0, r.bottom() / h)
        if rr - l < 0.02 or bb - t < 0.02:
            return None
        return (l, t, rr, bb)

    def mousePressEvent(self, event):
        if self._crop_mode and event.button() == Qt.LeftButton:
            self._crop_origin = self.mapToScene(event.pos())
            self._rubber.setRect(QRectF(self._crop_origin, self._crop_origin))
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._crop_mode and self._crop_origin is not None:
            self._rubber.setRect(
                QRectF(self._crop_origin, self.mapToScene(event.pos())).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._crop_mode and self._crop_origin is not None:
            self._crop_origin = None
            return
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
#  Miniaturliste mit robustem Umsortieren per Drag & Drop
# ---------------------------------------------------------------------------
class ThumbnailList(QListWidget):
    """Waagerechte Miniaturleiste. Das Umsortieren per Ziehen wird selbst
    behandelt (takeItem/insertItem), weil Qts eingebauter InternalMove im
    IconMode unzuverlässig ist (Seiten landen an falschen Positionen oder
    werden nicht verschoben)."""
    reordered = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(False)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)      # Position steuern wir selbst
        self.setSelectionMode(QListWidget.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QListWidget.InternalMove)
        self.viewport().setAcceptDrops(True)

    def dropEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return
        src_item = self.currentItem()
        if src_item is None:
            event.ignore()
            return
        src_row = self.row(src_item)

        # Zielposition robust bestimmen: erste Seite, deren Mitte rechts vom
        # Cursor liegt -> davor einfügen. (indexAt() liefert am linken Rand /
        # in Lücken einen ungültigen Index, wodurch "Seite 2 vor Seite 1"
        # fälschlich ans Ende sprang.)
        x = event.pos().x()
        target_row = self.count()
        for i in range(self.count()):
            rect = self.visualItemRect(self.item(i))
            if x < rect.center().x():
                target_row = i
                break

        # Index nach dem Entnehmen korrigieren
        if target_row > src_row:
            target_row -= 1
        if target_row == src_row:
            event.ignore()
            return

        item = self.takeItem(src_row)
        self.insertItem(target_row, item)
        self.setCurrentItem(item)
        event.accept()
        self.reordered.emit()


# ---------------------------------------------------------------------------
#  Combobox, deren Popup-Container mitgefärbt wird
# ---------------------------------------------------------------------------
class ResetSlider(QSlider):
    """Schieberegler, der bei Doppelklick auf den Mittelwert (100 = 1.0)
    zurückspringt."""
    def mouseDoubleClickEvent(self, event):
        self.setValue(100)
        super().mouseDoubleClickEvent(event)


class StyledComboBox(QComboBox):
    """Ohne dies zeigt Qt am aufgeklappten Popup oben/unten weiße Streifen:
    Der Popup-Container (QFrame mit Scrollbereichen) bleibt sonst ungestylt.
    Wir färben ihn bei jedem Öffnen passend zum Dark-Theme ein."""
    def showPopup(self):
        super().showPopup()
        container = self.view().parentWidget()
        container.setStyleSheet("background-color: #262b3b;")


# ---------------------------------------------------------------------------
#  Hauptfenster
# ---------------------------------------------------------------------------
class Kopierer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kopierer")
        self.resize(1150, 780)

        self.tmpdir = tempfile.mkdtemp(prefix="kopierer_")
        self.page_counter = 0
        self.scan_worker = None
        self.print_worker = None
        self._pending_quick = False   # True -> nach dem Scan sofort drucken

        self.settings = QSettings("Kopierer", "Kopierer")
        self._loading_settings = False   # Guard gegen Signal-Rückkopplung

        # Bildbearbeitung: verzögertes Voll-Rendern beim Ziehen der Regler
        self._loading_edits = False
        self._pending_render_page = None
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._flush_render)

        self._ensure_assets()
        self._build_ui()
        self._apply_style()
        self._detect_devices()
        self._load_printers()

    # -- Pfeil-Symbole für Dropdowns/Spinbox erzeugen -----------------------
    def _ensure_assets(self):
        """Erzeugt einmalig kleine Chevron-PNGs für Comboboxen und Spinbox.
        Sie landen im beschreibbaren Temp-Ordner, damit die App auch aus
        einem read-only AppImage heraus funktioniert."""
        from PIL import Image, ImageDraw
        self.assets_dir = os.path.join(self.tmpdir, "assets")
        os.makedirs(self.assets_dir, exist_ok=True)
        self.arrow_down = os.path.join(self.assets_dir, "chevron_down.png")
        self.arrow_up = os.path.join(self.assets_dir, "chevron_up.png")

        scale, size = 4, 16
        color = (176, 186, 214, 255)   # #b0bad6

        def draw(path, up):
            big = size * scale
            img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            pad, top, bot = 4 * scale, 6 * scale, 11 * scale
            mid, lw = big // 2, max(1, int(1.8 * scale))
            if up:
                pts = [(pad, bot), (mid, top), (big - pad, bot)]
            else:
                pts = [(pad, top), (mid, bot), (big - pad, top)]
            d.line(pts, fill=color, width=lw, joint="curve")
            img.resize((size, size), Image.LANCZOS).save(path)

        draw(self.arrow_down, up=False)
        draw(self.arrow_up, up=True)

    # -- UI-Aufbau ----------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(14)

        # ---- linke Steuerspalte ----
        sidebar = QVBoxLayout()
        sidebar.setSpacing(12)

        title = QLabel("Kopierer")
        title.setObjectName("appTitle")
        sidebar.addWidget(title)

        # Scanner-Einstellungen
        scan_box = QGroupBox("Scanner")
        scan_form = QFormLayout(scan_box)
        scan_form.setLabelAlignment(Qt.AlignRight)

        self.device_combo = StyledComboBox()
        self.device_combo.addItem("Suche Scanner …", None)
        self.device_combo.setEnabled(False)

        self.res_combo = StyledComboBox()
        for r in RESOLUTIONS:
            self.res_combo.addItem(f"{r} dpi", r)
        self.res_combo.setCurrentText("300 dpi")

        self.mode_combo = StyledComboBox()
        self.mode_combo.addItems(SCAN_MODES.keys())

        self.source_combo = StyledComboBox()
        self.source_combo.addItem("Flachbett", None)
        self.source_combo.setToolTip(
            "Vorlagenquelle. „Automatischer Einzug“ zieht bei ADF-Scannern "
            "alle eingelegten Blätter nacheinander ein.")

        self.size_combo = StyledComboBox()
        self.size_combo.addItems(PAGE_SIZES.keys())

        scan_form.setHorizontalSpacing(12)
        scan_form.setVerticalSpacing(10)
        scan_form.addRow("Gerät:", self.device_combo)
        scan_form.addRow("Quelle:", self.source_combo)
        scan_form.addRow("Auflösung:", self.res_combo)
        scan_form.addRow("Modus:", self.mode_combo)
        scan_form.addRow("Format:", self.size_combo)
        sidebar.addWidget(scan_box)

        self.scan_btn = QPushButton(" Seite scannen")
        self.scan_btn.setObjectName("primary")
        self.scan_btn.setIcon(self.style().standardIcon(QStyle.SP_ArrowDown))
        self.scan_btn.setMinimumHeight(46)
        self.scan_btn.clicked.connect(self.start_scan)
        sidebar.addWidget(self.scan_btn)

        self.quick_btn = QPushButton(" Schnellkopie (scannen + drucken)")
        self.quick_btn.setObjectName("quick")
        self.quick_btn.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.quick_btn.setMinimumHeight(42)
        self.quick_btn.setToolTip(
            "Scannt eine Seite und druckt sie sofort auf den gewählten Drucker.")
        self.quick_btn.clicked.connect(self.quick_copy)
        sidebar.addWidget(self.quick_btn)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.hide()
        sidebar.addWidget(self.progress)

        # Ausgabe-Einstellungen
        out_box = QGroupBox("Drucken")
        out_form = QFormLayout(out_box)
        out_form.setLabelAlignment(Qt.AlignRight)

        self.printer_combo = StyledComboBox()
        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 99)
        self.copies_spin.setValue(1)

        self.quality_combo = StyledComboBox()
        for label, val in PRINT_QUALITY.items():
            self.quality_combo.addItem(label, val)
        # Gespeicherte Druckqualität übernehmen, sonst „Normal“
        saved_q = self.settings.value("print_quality", "4")
        qi = self.quality_combo.findData(saved_q)
        self.quality_combo.setCurrentIndex(qi if qi >= 0 else 1)
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)

        out_form.addRow("Drucker:", self.printer_combo)
        out_form.addRow("Druckqualität:", self.quality_combo)
        out_form.addRow("Exemplare:", self.copies_spin)
        sidebar.addWidget(out_box)

        self.print_btn = QPushButton(" Drucken")
        self.print_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogYesButton))
        self.print_btn.setMinimumHeight(40)
        self.print_btn.clicked.connect(self.do_print)

        self.pdf_btn = QPushButton(" Als PDF speichern")
        self.pdf_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.pdf_btn.setMinimumHeight(40)
        self.pdf_btn.clicked.connect(self.save_pdf)

        sidebar.addWidget(self.print_btn)
        sidebar.addWidget(self.pdf_btn)

        sidebar.addStretch(1)

        self.status_label = QLabel("Bereit.")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        sidebar.addWidget(self.status_label)

        sidebar_widget = QWidget()
        sidebar_widget.setLayout(sidebar)
        sidebar_widget.setFixedWidth(340)
        root.addWidget(sidebar_widget)

        # ---- rechte Seite: Vorschau + Miniaturen ----
        right = QVBoxLayout()
        right.setSpacing(12)

        # ---- Bearbeiten-Werkzeugleiste ----
        right.addLayout(self._build_edit_toolbar())

        preview_frame = QFrame()
        preview_frame.setObjectName("previewFrame")
        pf_layout = QVBoxLayout(preview_frame)
        pf_layout.setContentsMargins(10, 10, 10, 10)
        self.preview = PreviewView()
        pf_layout.addWidget(self.preview)
        right.addWidget(preview_frame, stretch=1)

        # ---- Kontrast / Helligkeit ----
        right.addLayout(self._build_adjust_row())

        # Miniaturleiste
        thumbs_row = QHBoxLayout()
        thumbs_label = QLabel("Seiten:")
        thumbs_label.setObjectName("sectionLabel")
        thumbs_row.addWidget(thumbs_label)
        thumbs_row.addStretch(1)

        self.del_btn = QPushButton("Seite löschen")
        self.del_btn.clicked.connect(self.delete_current)
        self.clear_btn = QPushButton("Alle löschen")
        self.clear_btn.clicked.connect(self.clear_all)
        thumbs_row.addWidget(self.del_btn)
        thumbs_row.addWidget(self.clear_btn)
        right.addLayout(thumbs_row)

        self.thumbs = ThumbnailList()
        self.thumbs.setIconSize(QSize(96, 128))
        self.thumbs.setGridSize(QSize(118, 166))
        self.thumbs.setFixedHeight(196)
        self.thumbs.setSpacing(8)
        self.thumbs.setToolTip("Seiten per Ziehen umsortieren.")
        self.thumbs.currentItemChanged.connect(self._on_thumb_changed)
        # Nach dem Umsortieren die Seitennummern neu vergeben
        self.thumbs.reordered.connect(self._on_reordered)
        right.addWidget(self.thumbs)

        root.addLayout(right, stretch=1)

        # ---- Reiter: Kopierer + Einstellungen ----
        self.tabs = QTabWidget()
        self.tabs.addTab(central, "Kopierer")
        self.tabs.addTab(self._build_settings_tab(), "Einstellungen")
        self.setCentralWidget(self.tabs)

        self._update_actions()

    # -- Bearbeiten-Werkzeugleiste (über der Vorschau) ----------------------
    def _build_edit_toolbar(self):
        bar = QHBoxLayout()
        bar.setSpacing(6)

        def tool(text, tip, slot, checkable=False):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            if checkable:
                b.clicked.connect(slot)
            else:
                b.clicked.connect(slot)
            return b

        self.rotate_l_btn = tool("↺", "Seite 90° gegen den Uhrzeigersinn drehen",
                                 lambda: self._rotate(-90))
        self.rotate_r_btn = tool("↻", "Seite 90° im Uhrzeigersinn drehen",
                                 lambda: self._rotate(90))
        self.crop_btn = tool("Zuschnitt", "Bereich zum Zuschneiden aufziehen",
                             self._toggle_crop, checkable=True)
        self.crop_ok_btn = tool("✓ Übernehmen", "Zuschnitt anwenden", self._apply_crop)
        self.crop_cancel_btn = tool("✗ Abbrechen", "Zuschnitt verwerfen",
                                    self._cancel_crop)
        self.crop_ok_btn.setObjectName("quick")
        self.crop_ok_btn.hide()
        self.crop_cancel_btn.hide()

        self.color_combo = StyledComboBox()
        self.color_combo.addItem("Farbe", "color")
        self.color_combo.addItem("Graustufen", "gray")
        self.color_combo.addItem("Schwarz/Weiß", "bw")
        self.color_combo.setToolTip("Farbfilter für diese Seite")
        self.color_combo.currentIndexChanged.connect(self._on_color_changed)

        self.enhance_btn = tool("✨ Enhance",
                                "Dokument automatisch verbessern: geraderücken, "
                                "Weißabgleich, Hintergrund weißen, Kontrast, "
                                "Entrauschen und Schärfen",
                                self._toggle_enhance, checkable=True)
        self.enhance_btn.setObjectName("enhance")

        self.reset_edit_btn = tool("Zurücksetzen",
                                   "Alle Bearbeitungen dieser Seite verwerfen",
                                   self._reset_edits)

        for w in (self.rotate_l_btn, self.rotate_r_btn):
            w.setFixedWidth(40)

        bar.addWidget(self.rotate_l_btn)
        bar.addWidget(self.rotate_r_btn)
        bar.addWidget(self.crop_btn)
        bar.addWidget(self.crop_ok_btn)
        bar.addWidget(self.crop_cancel_btn)
        bar.addWidget(self.color_combo)
        bar.addWidget(self.enhance_btn)
        bar.addWidget(self.reset_edit_btn)
        bar.addStretch(1)

        # Zoom
        self.zoom_out_btn = tool("−", "Verkleinern", lambda: self.preview.zoom(0.8))
        self.zoom_fit_btn = tool("Anpassen", "An Fenster anpassen", self.preview_fit)
        self.zoom_11_btn = tool("1:1", "Originalgröße (100 %) – zeigt die volle "
                                "Scan-Auflösung", lambda: self.preview.zoom_actual())
        self.zoom_in_btn = tool("+", "Vergrößern", lambda: self.preview.zoom(1.25))
        for w in (self.zoom_out_btn, self.zoom_in_btn):
            w.setFixedWidth(36)
        bar.addWidget(self.zoom_out_btn)
        bar.addWidget(self.zoom_fit_btn)
        bar.addWidget(self.zoom_11_btn)
        bar.addWidget(self.zoom_in_btn)

        # Bearbeiten-Bedienelemente, die eine ausgewählte Seite brauchen
        self._edit_widgets = [
            self.rotate_l_btn, self.rotate_r_btn, self.crop_btn,
            self.color_combo, self.enhance_btn, self.reset_edit_btn,
        ]
        return bar

    def _build_adjust_row(self):
        row = QHBoxLayout()
        row.setSpacing(10)

        def slider(tip):
            s = ResetSlider(Qt.Horizontal)
            s.setRange(0, 200)      # Faktor 0.0 .. 2.0
            s.setValue(100)         # 1.0 = unverändert
            s.setToolTip(tip)
            s.setMinimumWidth(120)
            return s

        c_lbl = QLabel("Kontrast:")
        c_lbl.setObjectName("sectionLabel")
        self.contrast_slider = slider("Kontrast (Doppelklick setzt zurück)")
        self.contrast_slider.valueChanged.connect(
            lambda v: self._on_adjust("contrast", v))

        b_lbl = QLabel("Helligkeit:")
        b_lbl.setObjectName("sectionLabel")
        self.bright_slider = slider("Helligkeit (Doppelklick setzt zurück)")
        self.bright_slider.valueChanged.connect(
            lambda v: self._on_adjust("brightness", v))

        row.addWidget(c_lbl)
        row.addWidget(self.contrast_slider, 1)
        row.addSpacing(8)
        row.addWidget(b_lbl)
        row.addWidget(self.bright_slider, 1)

        self._edit_widgets += [self.contrast_slider, self.bright_slider]
        return row

    def _build_settings_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(16)

        title = QLabel("Einstellungen")
        title.setObjectName("appTitle")
        lay.addWidget(title)

        box = QGroupBox("Standardgeräte")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)

        self.def_scanner_combo = StyledComboBox()
        self.def_scanner_combo.setMinimumWidth(360)
        self.def_printer_combo = StyledComboBox()
        self.def_printer_combo.setMinimumWidth(360)
        form.addRow("Standard-Scanner:", self.def_scanner_combo)
        form.addRow("Standard-Drucker:", self.def_printer_combo)
        lay.addWidget(box)

        hint = QLabel("Diese Auswahl wird gespeichert und beim nächsten Start "
                      "automatisch verwendet.")
        hint.setObjectName("status")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        lay.addStretch(1)

        self.def_scanner_combo.currentIndexChanged.connect(
            self._on_default_scanner_changed)
        self.def_printer_combo.currentIndexChanged.connect(
            self._on_default_printer_changed)
        return page

    def _apply_style(self):
        down = self.arrow_down.replace("\\", "/")
        up = self.arrow_up.replace("\\", "/")
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: #1e2230; color: #e6e9f0;
                font-family: 'Segoe UI', 'Ubuntu', 'Cantarell', sans-serif;
                font-size: 13px; }}
            #appTitle {{ font-size: 24px; font-weight: 700; color: #ffffff;
                padding: 2px 0 6px 2px; }}

            /* --- Reiter --- */
            QTabWidget::pane {{ border: 0; }}
            QTabBar {{ qproperty-drawBase: 0; }}
            QTabBar::tab {{ background: #262b3b; color: #9aa4bf;
                padding: 9px 28px; margin-right: 4px; font-weight: 600;
                min-width: 90px;
                border: 1px solid #333a4d; border-bottom: 0;
                border-top-left-radius: 9px; border-top-right-radius: 9px; }}
            QTabBar::tab:selected {{ background: #4a6cf7; color: #ffffff;
                border-color: #4a6cf7; }}
            QTabBar::tab:hover:!selected {{ background: #313849; color: #e6e9f0; }}

            /* --- Gruppenrahmen --- */
            QGroupBox {{ border: 1px solid #333a4d; border-radius: 10px;
                margin-top: 16px; padding: 16px 12px 12px 12px;
                background: #262b3b; font-weight: 600; }}
            QGroupBox::title {{ subcontrol-origin: margin;
                subcontrol-position: top left; left: 14px; top: 2px;
                padding: 0 6px; color: #9aa4bf; background: #1e2230; }}

            /* --- Formular-Beschriftungen --- */
            QLabel {{ color: #cfd5e6; }}

            /* --- Comboboxen (Dropdowns) --- */
            QComboBox {{ background: #313849; border: 1px solid #3d4661;
                border-radius: 8px; padding: 7px 10px; min-height: 22px;
                color: #e6e9f0; }}
            QComboBox:hover {{ border-color: #4a6cf7; background: #353d52; }}
            QComboBox:focus {{ border-color: #4a6cf7; }}
            QComboBox:disabled {{ color: #6b7288; background: #2a2f3d; }}
            QComboBox::drop-down {{ subcontrol-origin: padding;
                subcontrol-position: center right; width: 30px;
                border-left: 1px solid #3d4661;
                border-top-right-radius: 8px; border-bottom-right-radius: 8px;
                background: #2a3143; }}
            QComboBox::down-arrow {{ image: url({down}); width: 14px; height: 14px; }}
            QComboBox QAbstractItemView {{ background: #262b3b; color: #e6e9f0;
                border: 1px solid #3d4661; border-radius: 8px; outline: 0;
                padding: 4px; selection-background-color: #4a6cf7; }}
            QComboBox QAbstractItemView::item {{ min-height: 28px;
                padding: 4px 10px; border-radius: 6px; }}
            QComboBox QAbstractItemView::item:selected {{ background: #4a6cf7; }}

            /* --- Spinbox (Exemplare) --- */
            QSpinBox {{ background: #313849; border: 1px solid #3d4661;
                border-radius: 8px; padding: 7px 10px; min-height: 22px;
                color: #e6e9f0; }}
            QSpinBox:hover {{ border-color: #4a6cf7; }}
            QSpinBox::up-button {{ subcontrol-origin: border;
                subcontrol-position: top right; width: 26px;
                border-left: 1px solid #3d4661; border-bottom: 1px solid #3d4661;
                border-top-right-radius: 8px; background: #2a3143; }}
            QSpinBox::down-button {{ subcontrol-origin: border;
                subcontrol-position: bottom right; width: 26px;
                border-left: 1px solid #3d4661;
                border-bottom-right-radius: 8px; background: #2a3143; }}
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: #3a4256; }}
            QSpinBox::up-arrow {{ image: url({up}); width: 12px; height: 12px; }}
            QSpinBox::down-arrow {{ image: url({down}); width: 12px; height: 12px; }}

            /* --- Buttons --- */
            QPushButton {{ background: #313849; border: 1px solid #3d4661;
                border-radius: 8px; padding: 9px 14px; color: #e6e9f0; }}
            QPushButton:hover {{ background: #3a4256; border-color: #4a6cf7; }}
            QPushButton:pressed {{ background: #2a3143; }}
            QPushButton:disabled {{ color: #6b7288; background: #2a2f3d;
                border-color: #333a4d; }}
            QPushButton#primary {{ background: #4a6cf7; border: 0; font-weight: 700;
                font-size: 14px; }}
            QPushButton#primary:hover {{ background: #5b7bff; }}
            QPushButton#primary:pressed {{ background: #3f5ce0; }}
            QPushButton#primary:disabled {{ background: #384063; color: #9aa4bf; }}
            QPushButton#quick {{ background: #1f6f54; border: 0; font-weight: 700; }}
            QPushButton#quick:hover {{ background: #268264; }}
            QPushButton#quick:pressed {{ background: #195b45; }}
            QPushButton#quick:disabled {{ background: #2c4a41; color: #9aa4bf; }}
            /* Enhance-Umschalter: dezent, im aktiven Zustand violett hervorgehoben */
            QPushButton#enhance {{ font-weight: 600; }}
            QPushButton#enhance:checked {{ background: qlineargradient(x1:0, y1:0,
                x2:1, y2:0, stop:0 #8b5cf6, stop:1 #a855f7); border: 0;
                color: #ffffff; font-weight: 700; }}
            QPushButton#enhance:checked:hover {{ background: qlineargradient(x1:0,
                y1:0, x2:1, y2:0, stop:0 #9b6cff, stop:1 #b866ff); }}

            /* --- Vorschau & Miniaturen --- */
            #previewFrame {{ background: #14171f; border: 1px solid #333a4d;
                border-radius: 12px; }}
            #sectionLabel {{ color: #9aa4bf; font-weight: 600; }}
            #status {{ color: #8b93a7; padding: 4px 2px; }}
            QListWidget {{ background: #14171f; border: 1px solid #333a4d;
                border-radius: 10px; padding: 4px; }}
            QListWidget::item {{ border: 2px solid transparent; border-radius: 6px;
                padding: 3px; margin: 2px; }}
            QListWidget::item:selected {{ border: 2px solid #4a6cf7; background: #23293a; }}
            QListWidget::item:hover {{ border: 2px solid #3d4661; }}

            /* --- Fortschrittsbalken --- */
            QProgressBar {{ background: #14171f; border: 1px solid #333a4d;
                border-radius: 7px; text-align: center; height: 22px; color: #e6e9f0; }}
            QProgressBar::chunk {{ background: #4a6cf7; border-radius: 6px; }}

            /* --- Scrollbalken --- */
            QScrollBar:horizontal {{ background: #14171f; height: 10px;
                border-radius: 5px; }}
            QScrollBar::handle:horizontal {{ background: #3d4661; border-radius: 5px;
                min-width: 30px; }}
            QScrollBar::handle:horizontal:hover {{ background: #4a6cf7; }}
            QScrollBar:vertical {{ background: #14171f; width: 10px;
                border-radius: 5px; }}
            QScrollBar::handle:vertical {{ background: #3d4661; border-radius: 5px;
                min-height: 30px; }}
            QScrollBar::handle:vertical:hover {{ background: #4a6cf7; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}

            /* --- Schieberegler (Kontrast/Helligkeit) --- */
            QSlider {{ min-height: 26px; }}
            QSlider::groove:horizontal {{ height: 8px; border-radius: 4px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #10131b, stop:1 #1b1f2b);
                border: 1px solid #2b3140; }}
            QSlider::sub-page:horizontal {{ border-radius: 4px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #3f5ce0, stop:1 #6a8bff);
                border: 1px solid #3f5ce0; }}
            QSlider::add-page:horizontal {{ border-radius: 4px;
                background: #14171f; border: 1px solid #2b3140; }}
            QSlider::handle:horizontal {{ width: 18px; height: 18px;
                margin: -7px 0; border-radius: 9px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #ffffff, stop:1 #d7ddec);
                border: 1px solid #2b3140; }}
            QSlider::handle:horizontal:hover {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #ffffff, stop:1 #eef1f8);
                border: 2px solid #6a8bff; margin: -7px 0; }}
            QSlider::handle:horizontal:pressed {{ border: 2px solid #4a6cf7;
                background: #eef1f8; }}
        """)

    # -- Geräte / Drucker ---------------------------------------------------
    def _detect_devices(self):
        self.dev_worker = DeviceScanWorker()
        self.dev_worker.finished_ok.connect(self._on_devices_found)
        self.dev_worker.start()

    def _on_devices_found(self, devices):
        self._loading_settings = True
        self.device_combo.clear()
        self.def_scanner_combo.clear()
        if not devices:
            self.device_combo.addItem("Kein Scanner gefunden", None)
            self.def_scanner_combo.addItem("Kein Scanner gefunden", None)
            self.device_combo.setEnabled(False)
            self.def_scanner_combo.setEnabled(False)
            self.set_status("Kein Scanner gefunden. Ist das Gerät angeschlossen?")
        else:
            for dev_id, desc in devices:
                self.device_combo.addItem(desc, dev_id)
                self.def_scanner_combo.addItem(desc, dev_id)
            # Gespeicherten Standard anwenden, sonst das erste gefundene Gerät.
            saved = self.settings.value("default_scanner", "")
            idx = self.device_combo.findData(saved) if saved else -1
            if idx < 0:
                idx = 0
            self.device_combo.setCurrentIndex(idx)
            self.def_scanner_combo.setCurrentIndex(idx)
            self.device_combo.setEnabled(True)
            self.def_scanner_combo.setEnabled(True)
            self.set_status("Scanner bereit.")
        self._loading_settings = False
        # Quellen (Flachbett/Einzug) des aktiven Geräts abfragen
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._probe_sources()
        self._update_actions()

    def _on_device_changed(self, _idx):
        if not self._loading_settings:
            self._probe_sources()

    def _probe_sources(self):
        """Fragt im Hintergrund ab, welche Quellen das gewählte Gerät anbietet."""
        device = self.device_combo.currentData()
        self.source_combo.setEnabled(False)
        if not device:
            return
        self.src_worker = SourceProbeWorker(device)
        self.src_worker.finished_ok.connect(self._on_sources_found)
        self.src_worker.start()

    def _on_sources_found(self, sources):
        self.source_combo.clear()
        if not sources:
            # Backend meldet keine Quellen -> ohne --source scannen (Flachbett)
            self.source_combo.addItem("Flachbett", None)
            self.source_combo.setEnabled(False)
            return
        for s in sources:
            label = s
            if source_is_adf(s):
                tag = "Duplex-Einzug" if "duplex" in s.lower() else "Automatischer Einzug"
                label = f"{tag} ({s})"
            self.source_combo.addItem(label, s)
        self.source_combo.setEnabled(True)

    def _on_quality_changed(self, _idx):
        val = self.quality_combo.currentData()
        if val:
            self.settings.setValue("print_quality", val)

    def _load_printers(self):
        try:
            out = subprocess.run(["lpstat", "-p"], capture_output=True,
                                 text=True, timeout=10).stdout
            printers = re.findall(r"^(?:Drucker|printer)\s+(\S+)", out, re.MULTILINE)
        except Exception:
            printers = []
        self._loading_settings = True
        self.printer_combo.clear()
        self.def_printer_combo.clear()
        if printers:
            self.printer_combo.addItems(printers)
            self.def_printer_combo.addItems(printers)
            # Gespeicherten Standard bevorzugen, sonst CUPS-Standarddrucker
            target = self.settings.value("default_printer", "")
            if target not in printers:
                target = ""
                try:
                    default = subprocess.run(["lpstat", "-d"], capture_output=True,
                                             text=True, timeout=10).stdout
                    m = re.search(r":\s*(\S+)", default)
                    if m and m.group(1) in printers:
                        target = m.group(1)
                except Exception:
                    pass
            if target:
                self.printer_combo.setCurrentText(target)
                self.def_printer_combo.setCurrentText(target)
        else:
            self.printer_combo.addItem("Kein Drucker gefunden")
            self.def_printer_combo.addItem("Kein Drucker gefunden")
            self.printer_combo.setEnabled(False)
            self.def_printer_combo.setEnabled(False)
        self._loading_settings = False

    # -- Standardgeräte (Einstellungen) -------------------------------------
    def _on_default_scanner_changed(self, _idx):
        if self._loading_settings:
            return
        dev = self.def_scanner_combo.currentData()
        if dev:
            self.settings.setValue("default_scanner", dev)
            i = self.device_combo.findData(dev)
            if i >= 0:
                self.device_combo.setCurrentIndex(i)
            self.set_status("Standard-Scanner gespeichert.")

    def _on_default_printer_changed(self, _idx):
        if self._loading_settings:
            return
        name = self.def_printer_combo.currentText()
        if name and self.def_printer_combo.isEnabled():
            self.settings.setValue("default_printer", name)
            i = self.printer_combo.findText(name)
            if i >= 0:
                self.printer_combo.setCurrentIndex(i)
            self.set_status("Standard-Drucker gespeichert.")

    # -- Scannen ------------------------------------------------------------
    def start_scan(self):
        device = self.device_combo.currentData()
        if not device:
            self.set_status("Kein Scanner ausgewählt.")
            return

        self._scan_fmt_name = self.size_combo.currentText()
        width, height = PAGE_SIZES[self._scan_fmt_name]
        mode = SCAN_MODES[self.mode_combo.currentText()]
        resolution = self.res_combo.currentData()
        source = self.source_combo.currentData()
        is_adf = source_is_adf(source)

        self.page_counter += 1
        prefix = os.path.join(self.tmpdir, f"seite_{self.page_counter:03d}_")

        self._scanned_new = []   # Pfade der in diesem Lauf gescannten Seiten

        if is_adf:
            # ADF: Seitenzahl unbekannt -> unbestimmter Fortschritt
            self.progress.setRange(0, 0)
            self.set_status("Ziehe Blätter aus dem Einzug … bitte warten.")
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.set_status("Scanne … bitte den Scanner nicht öffnen.")
        self.progress.show()
        self.scan_btn.setEnabled(False)
        self.quick_btn.setEnabled(False)

        self.scan_worker = ScanWorker(device, resolution, mode, source,
                                      width, height, prefix)
        self.scan_worker.progress.connect(self.progress.setValue)
        self.scan_worker.page_done.connect(self._on_page_scanned)
        self.scan_worker.finished_ok.connect(self._on_scan_done)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.start()

    def _on_page_scanned(self, path):
        """Eine (von evtl. mehreren) Seiten ist fertig -> Miniatur anlegen."""
        page = Page(path, self._scan_fmt_name)
        self._scanned_new.append(page)

        item = QListWidgetItem(page.thumb_icon(), "")
        item.setData(Qt.UserRole, page)
        item.setTextAlignment(Qt.AlignHCenter)
        self.thumbs.addItem(item)
        self.thumbs.setCurrentItem(item)   # zeigt sie sofort in der Vorschau
        self._renumber()

    def _on_scan_done(self, count):
        self.progress.setRange(0, 100)
        self.progress.hide()

        if self._pending_quick:
            # Schnellkopie: die frisch gescannten Seiten direkt drucken
            self._pending_quick = False
            entries = [p.output_entry() for p in self._scanned_new]
            self.set_status("Gescannt – sende an Drucker …")
            if not self._send_to_printer(entries):
                self._set_idle()
        else:
            self._set_idle()
            if count == 1 and self._scanned_new:
                w, h = self._scanned_new[-1].pixel_size()
                self.set_status(
                    f"Seite {self.thumbs.count()} gescannt ({w}×{h} px).")
            else:
                self.set_status(f"{count} Seiten aus dem Einzug gescannt.")

    def _on_scan_failed(self, msg):
        self.progress.setRange(0, 100)
        self.progress.hide()
        self.page_counter -= 1
        self._pending_quick = False
        self._set_idle()
        QMessageBox.critical(self, "Scan fehlgeschlagen", msg)
        self.set_status("Scan fehlgeschlagen.")

    def quick_copy(self):
        """Scannt eine Seite und druckt sie sofort auf den gewählten Drucker."""
        if not self._printer_or_warn():
            return
        self._pending_quick = True
        self.start_scan()

    # -- Miniaturen ---------------------------------------------------------
    def _on_thumb_changed(self, current, _previous):
        # laufende Bearbeitung sichern, evtl. Zuschnitt-Modus beenden
        self._flush_render()
        if self.preview.is_cropping():
            self._cancel_crop()
        if current:
            page = current.data(Qt.UserRole)
            self._sync_edit_controls(page)
            self._show_page(page, fit=True)
        else:
            self.preview.clear_image()
        self._update_actions()

    # -- Bildbearbeitung der aktuellen Seite --------------------------------
    def _current_page(self):
        it = self.thumbs.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _show_page(self, page, fit=True):
        # Volle Auflösung anzeigen, damit Zoom die echte Scan-Schärfe zeigt.
        self.preview.set_pixmap(QPixmap(page.work_path), fit=fit)

    def preview_fit(self):
        self.preview.fit()

    def _refresh_thumb(self, page):
        for i in range(self.thumbs.count()):
            it = self.thumbs.item(i)
            if it.data(Qt.UserRole) is page:
                it.setIcon(page.thumb_icon())
                break

    def _sync_edit_controls(self, page):
        """Regler/Combo auf die gespeicherten Werte der Seite setzen (ohne dabei
        die Änderungssignale als Benutzeraktion misszuverstehen)."""
        self._loading_edits = True
        self.color_combo.setCurrentIndex(
            {"color": 0, "gray": 1, "bw": 2}.get(page.color, 0))
        self.contrast_slider.setValue(int(round(page.contrast * 100)))
        self.bright_slider.setValue(int(round(page.brightness * 100)))
        self.enhance_btn.setChecked(page.auto)
        self._loading_edits = False

    def _flush_render(self):
        """Verzögertes Voll-Rendern (Thumbnail/Ausgabedatei) sofort ausführen."""
        self._render_timer.stop()
        page = self._pending_render_page
        if page is not None:
            page.render()
            self._refresh_thumb(page)
            self._pending_render_page = None
            # Vorschau von der schnellen Näherung auf die scharfe Vollauflösung
            # heben, sofern diese Seite gerade aktiv ist.
            if page is self._current_page() and not self.preview.is_cropping():
                self.preview.set_pixmap(QPixmap(page.work_path), fit=False)

    def _render_busy(self, page, text):
        """Rendert eine Seite mit Wartecursor + Statusmeldung – für die wenigen
        rechenintensiven Schritte (Auto-Verbesserung), die kurz blockieren."""
        self.set_status(text)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()   # Status/Cursor sichtbar machen
        try:
            page.render()
        finally:
            QApplication.restoreOverrideCursor()

    def _rotate(self, deg):
        page = self._current_page()
        if not page:
            return
        page.rotation = (page.rotation + deg) % 360
        page.crop = None   # Zuschnitt passt nach dem Drehen nicht mehr
        if page.auto:
            self._render_busy(page, "Verbessere Dokument …")
        else:
            page.render()
        self._refresh_thumb(page)
        self._show_page(page, fit=True)
        self.set_status("Seite gedreht.")

    def _on_color_changed(self, _idx):
        if self._loading_edits:
            return
        page = self._current_page()
        if not page:
            return
        page.color = self.color_combo.currentData()
        page.render()
        self._refresh_thumb(page)
        self._show_page(page, fit=False)

    def _toggle_enhance(self, checked):
        if self._loading_edits:
            return
        page = self._current_page()
        if not page:
            self.enhance_btn.setChecked(False)
            return
        page.auto = checked
        if checked:
            self._render_busy(page, "Verbessere Dokument …")
        else:
            page.render()
        self._refresh_thumb(page)
        self._show_page(page, fit=False)
        self.set_status("Auto-Verbesserung aktiviert." if checked
                        else "Auto-Verbesserung aus.")

    def _on_adjust(self, attr, value):
        if self._loading_edits:
            return
        page = self._current_page()
        if not page:
            return
        setattr(page, attr, value / 100.0)
        # Sofort die schnelle Vorschau aktualisieren, das teure Voll-Rendern
        # (Ausgabedatei + Thumbnail) erst nach kurzer Pause.
        self.preview.set_pixmap(page.preview_pixmap(), fit=False)
        self._pending_render_page = page
        self._render_timer.start(200)

    def _reset_edits(self):
        page = self._current_page()
        if not page:
            return
        page.reset()
        page.render()
        self._refresh_thumb(page)
        self._sync_edit_controls(page)
        self._show_page(page, fit=True)
        self.set_status("Bearbeitung zurückgesetzt.")

    # -- Zuschnitt ----------------------------------------------------------
    def _toggle_crop(self, checked):
        page = self._current_page()
        if not page:
            self.crop_btn.setChecked(False)
            return
        if checked:
            self._flush_render()
            # ungeschnittenes (aber gedrehtes) Bild zeigen, damit frei neu
            # ausgewählt werden kann
            self.preview.set_pixmap(page.preview_pixmap(skip_crop=True), fit=True)
            self.preview.enter_crop_mode(page.crop)
            self._set_crop_ui(True)
            self.set_status("Rechteck aufziehen, dann „Übernehmen“.")
        else:
            self._cancel_crop()

    def _apply_crop(self):
        page = self._current_page()
        if not page:
            return
        frac = self.preview.crop_fractions()
        self.preview.exit_crop_mode()
        self._set_crop_ui(False)
        self.crop_btn.setChecked(False)
        if frac:
            page.crop = frac
            page.render()
            self._refresh_thumb(page)
            self.set_status("Zuschnitt übernommen.")
        else:
            self.set_status("Kein gültiger Bereich – Zuschnitt verworfen.")
        self._show_page(page, fit=True)

    def _cancel_crop(self):
        self.preview.exit_crop_mode()
        self._set_crop_ui(False)
        self.crop_btn.setChecked(False)
        page = self._current_page()
        if page:
            self._show_page(page, fit=True)

    def _set_crop_ui(self, active):
        self.crop_ok_btn.setVisible(active)
        self.crop_cancel_btn.setVisible(active)
        # übrige Bearbeiten-Elemente während des Zuschnitts sperren
        for w in self._edit_widgets:
            if w is not self.crop_btn:
                w.setEnabled(not active)

    def _renumber(self):
        """Vergibt fortlaufende Seitennummern gemäß aktueller Reihenfolge."""
        for i in range(self.thumbs.count()):
            self.thumbs.item(i).setText(f"Seite {i + 1}")

    def _on_reordered(self):
        self._renumber()
        self.set_status("Seitenreihenfolge geändert.")

    def delete_current(self):
        row = self.thumbs.currentRow()
        if row < 0:
            return
        if self.preview.is_cropping():
            self._cancel_crop()
        item = self.thumbs.takeItem(row)
        page = item.data(Qt.UserRole)
        if self._pending_render_page is page:
            self._pending_render_page = None
        for p in (page.original, page.work_path):
            try:
                os.remove(p)
            except OSError:
                pass
        del item
        if self.thumbs.count() == 0:
            self.preview.clear_image()
        self._renumber()
        self.set_status("Seite gelöscht.")
        self._update_actions()

    def clear_all(self):
        if self.thumbs.count() == 0:
            return
        if QMessageBox.question(self, "Alle löschen",
                                "Wirklich alle Seiten verwerfen?") != QMessageBox.Yes:
            return
        if self.preview.is_cropping():
            self._cancel_crop()
        self._pending_render_page = None
        self._render_timer.stop()
        self.thumbs.clear()
        self.preview.clear_image()
        for f in os.listdir(self.tmpdir):
            if f.startswith("seite_"):
                try:
                    os.remove(os.path.join(self.tmpdir, f))
                except OSError:
                    pass
        self.set_status("Alle Seiten gelöscht.")
        self._update_actions()

    # -- Ausgabe ------------------------------------------------------------
    def _page_entries(self):
        """Liste (Pfad, Formatname) in aktueller Reihenfolge.
        Sichert zuvor eine evtl. noch ausstehende Bearbeitung."""
        self._flush_render()
        entries = []
        for i in range(self.thumbs.count()):
            page = self.thumbs.item(i).data(Qt.UserRole)
            entries.append(page.output_entry())
        return entries

    def _pdf_from(self, entries, target_path):
        """Baut die PDF. Wenn alle Seiten dasselbe Format haben, wird die
        PDF-Seitengröße deterministisch auf dieses Format gesetzt (unabhängig
        von Pixelzahl/DPI) – so ist die Ausgabe echtes A4/A5/Letter.
        `auto_orient` dreht die Seite bei quer gedrehten Scans mit, damit ein
        Querformat-Bild nicht in eine Hochkant-Seite gequetscht wird."""
        paths = [e[0] for e in entries]
        formats = {e[1] for e in entries}
        kwargs = {}
        if len(formats) == 1:
            name = next(iter(formats))
            if name in PAGE_SIZES:
                w_mm, h_mm = PAGE_SIZES[name]
                kwargs["layout_fun"] = img2pdf.get_layout_fun(
                    (img2pdf.mm_to_pt(w_mm), img2pdf.mm_to_pt(h_mm)),
                    auto_orient=True)
        with open(target_path, "wb") as f:
            f.write(img2pdf.convert(paths, **kwargs))

    def _build_pdf(self, target_path):
        self._pdf_from(self._page_entries(), target_path)

    def _printer_or_warn(self):
        printer = self.printer_combo.currentText()
        if not self.printer_combo.isEnabled() or not printer:
            QMessageBox.warning(self, "Kein Drucker",
                                "Es ist kein Drucker verfügbar.")
            return None
        return printer

    def _send_to_printer(self, entries):
        """Baut ein PDF aus (Pfad, Format)-Einträgen und druckt es.
        Gibt True zurück, wenn der Druckauftrag gestartet wurde."""
        printer = self._printer_or_warn()
        if not printer:
            return False
        pdf_path = os.path.join(self.tmpdir, "_druck.pdf")
        try:
            self._pdf_from(entries, pdf_path)
        except Exception as e:
            QMessageBox.critical(self, "Fehler", str(e))
            return False

        # fit-to-page + passendes Medium, damit die Seite das Blatt füllt und
        # nicht verkleinert gedruckt wird.
        options = ["fit-to-page"]
        formats = {e[1] for e in entries}
        if len(formats) == 1:
            media = CUPS_MEDIA.get(next(iter(formats)))
            if media:
                options.append(f"media={media}")

        quality = self.quality_combo.currentData()
        if quality:
            options.append(f"print-quality={quality}")

        copies = self.copies_spin.value()
        self.scan_btn.setEnabled(False)
        self.quick_btn.setEnabled(False)
        self.print_btn.setEnabled(False)
        self.set_status(f"Sende {len(entries)} Seite(n) an {printer} …")

        self.print_worker = PrintWorker(pdf_path, printer, copies, options)
        self.print_worker.finished_ok.connect(lambda: self._on_print_done(printer))
        self.print_worker.failed.connect(self._on_print_failed)
        self.print_worker.start()
        return True

    def save_pdf(self):
        if self.thumbs.count() == 0:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Als PDF speichern",
            os.path.join(os.path.expanduser("~"), "scan.pdf"),
            "PDF-Dateien (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            self._build_pdf(path)
            self.set_status(f"Gespeichert: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler beim Speichern", str(e))

    def do_print(self):
        if self.thumbs.count() == 0:
            return
        self._send_to_printer(self._page_entries())

    def _on_print_done(self, printer):
        self._set_idle()
        self.set_status(f"Druckauftrag an {printer} gesendet.")

    def _on_print_failed(self, msg):
        self._set_idle()
        QMessageBox.critical(self, "Druck fehlgeschlagen", msg)
        self.set_status("Druck fehlgeschlagen.")

    # -- Hilfen -------------------------------------------------------------
    def _set_idle(self):
        """Aktiviert die Scan-Buttons wieder und aktualisiert die Aktionen."""
        self.scan_btn.setEnabled(True)
        self.quick_btn.setEnabled(True)
        self._update_actions()

    def _update_actions(self):
        has_pages = self.thumbs.count() > 0
        self.print_btn.setEnabled(has_pages)
        self.pdf_btn.setEnabled(has_pages)
        self.del_btn.setEnabled(self.thumbs.currentRow() >= 0)
        self.clear_btn.setEnabled(has_pages)
        # Bearbeiten-Elemente nur bei ausgewählter Seite (und nicht im Zuschnitt)
        has_cur = self.thumbs.currentItem() is not None
        if not self.preview.is_cropping():
            for w in self._edit_widgets:
                w.setEnabled(has_cur)
        for w in (self.zoom_out_btn, self.zoom_fit_btn, self.zoom_11_btn,
                  self.zoom_in_btn):
            w.setEnabled(has_cur)

    def set_status(self, text):
        self.status_label.setText(text)

    def closeEvent(self, event):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = Kopierer()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

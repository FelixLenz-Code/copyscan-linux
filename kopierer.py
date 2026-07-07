#!/usr/bin/env python3
"""
Kopierer - Scan-, Druck- und PDF-Werkzeug für Linux.

Verbindet einen SANE-Flachbettscanner (z. B. Canon CanoScan 8800F) mit einem
CUPS-Drucker, damit man wie an einem Kopierer arbeiten kann:
Blatt einscannen  ->  Vorschau prüfen  ->  drucken und/oder als PDF speichern.

Abhängigkeiten (unter Ubuntu/Debian bereits vorhanden):
    - scanimage (Paket: sane-utils)
    - lp / lpstat (Paket: cups-client)
    - python3-pyqt5, python3-pil, img2pdf
"""

import os
import re
import sys
import shutil
import tempfile
import subprocess

import img2pdf

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt5.QtGui import QPixmap, QIcon, QImage
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox,
    QSpinBox, QListWidget, QListWidgetItem, QVBoxLayout, QHBoxLayout,
    QFormLayout, QGroupBox, QScrollArea, QFileDialog, QMessageBox,
    QProgressBar, QFrame, QSizePolicy, QStyle,
)

# --- Papiergrößen in Millimetern (Breite x Höhe) ---------------------------
# Der 8800F kann max. 216.069 x 297.011 mm scannen.
PAGE_SIZES = {
    "A4 (210 × 297 mm)":     (210.0, 297.0),
    "A5 (148 × 210 mm)":     (148.0, 210.0),
    "Letter (216 × 279 mm)": (216.0, 279.0),
    "Ganze Fläche":          (216.069, 297.011),
}

RESOLUTIONS = ["150", "300", "600", "1200"]

SCAN_MODES = {
    "Farbe":        "Color",
    "Graustufen":   "Gray",
    "Schwarz/Weiß": "Lineart",
}


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
            # Zeilen der Form:  device `pixma:04A91901' is a CANON Canoscan 8800F ...
            for m in re.finditer(r"device `([^']+)' is a (.+)", out):
                devices.append((m.group(1), m.group(2).strip()))
        except Exception:
            pass
        self.finished_ok.emit(devices)


class ScanWorker(QThread):
    """Scannt eine Seite nach PNG und meldet Fortschritt über stderr."""
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(str)     # Pfad zur PNG-Datei
    failed = pyqtSignal(str)

    def __init__(self, device, resolution, mode, width_mm, height_mm, out_path):
        super().__init__()
        self.device = device
        self.resolution = resolution
        self.mode = mode
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.out_path = out_path

    def run(self):
        cmd = [
            "scanimage",
            "-d", self.device,
            "--resolution", self.resolution,
            "--mode", self.mode,
            "--source", "Flatbed",
            "--format=png",
            "-x", f"{self.width_mm}",
            "-y", f"{self.height_mm}",
            "--progress",
        ]
        try:
            with open(self.out_path, "wb") as out_file:
                proc = subprocess.Popen(
                    cmd, stdout=out_file, stderr=subprocess.PIPE,
                )
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

            if proc.returncode != 0 or not os.path.getsize(self.out_path):
                raise RuntimeError(err or f"scanimage endete mit Code {proc.returncode}")
            self.progress.emit(100)
            self.finished_ok.emit(self.out_path)
        except Exception as e:
            self.failed.emit(str(e))


class PrintWorker(QThread):
    """Schickt eine fertige PDF-Datei via `lp` an den Drucker."""
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, pdf_path, printer, copies):
        super().__init__()
        self.pdf_path = pdf_path
        self.printer = printer
        self.copies = copies

    def run(self):
        cmd = ["lp", "-d", self.printer, "-n", str(self.copies), self.pdf_path]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(res.stderr.strip() or "lp fehlgeschlagen")
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(str(e))


# ---------------------------------------------------------------------------
#  Vorschau-Widget (skaliert das Bild passend zur Fenstergröße)
# ---------------------------------------------------------------------------
class PreviewLabel(QLabel):
    def __init__(self):
        super().__init__()
        self._pixmap = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(400, 500)
        self.setText("Noch keine Seite gescannt.\n\nLege ein Dokument auf den Scanner\nund klicke „Seite scannen“.")
        self.setStyleSheet("color: #8b93a7; font-size: 15px;")

    def set_image(self, path):
        self._pixmap = QPixmap(path)
        self._rescale()

    def clear_image(self):
        self._pixmap = None
        self.setText("Noch keine Seite gescannt.")

    def resizeEvent(self, event):
        self._rescale()
        super().resizeEvent(event)

    def _rescale(self):
        if self._pixmap and not self._pixmap.isNull():
            self.setPixmap(self._pixmap.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


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
        self.setCentralWidget(central)
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

        self.device_combo = QComboBox()
        self.device_combo.addItem("Suche Scanner …", None)
        self.device_combo.setEnabled(False)

        self.res_combo = QComboBox()
        for r in RESOLUTIONS:
            self.res_combo.addItem(f"{r} dpi", r)
        self.res_combo.setCurrentText("300 dpi")

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(SCAN_MODES.keys())

        self.size_combo = QComboBox()
        self.size_combo.addItems(PAGE_SIZES.keys())

        scan_form.setHorizontalSpacing(12)
        scan_form.setVerticalSpacing(10)
        scan_form.addRow("Gerät:", self.device_combo)
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

        self.printer_combo = QComboBox()
        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 99)
        self.copies_spin.setValue(1)

        out_form.addRow("Drucker:", self.printer_combo)
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

        preview_frame = QFrame()
        preview_frame.setObjectName("previewFrame")
        pf_layout = QVBoxLayout(preview_frame)
        pf_layout.setContentsMargins(10, 10, 10, 10)
        self.preview = PreviewLabel()
        pf_layout.addWidget(self.preview)
        right.addWidget(preview_frame, stretch=1)

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

        self.thumbs = QListWidget()
        self.thumbs.setViewMode(QListWidget.IconMode)
        self.thumbs.setIconSize(QSize(96, 128))
        self.thumbs.setGridSize(QSize(118, 166))
        self.thumbs.setFixedHeight(196)
        self.thumbs.setFlow(QListWidget.LeftToRight)
        self.thumbs.setWrapping(False)
        # Seiten per Drag & Drop umsortieren
        self.thumbs.setMovement(QListWidget.Snap)
        self.thumbs.setDragEnabled(True)
        self.thumbs.setAcceptDrops(True)
        self.thumbs.setDragDropMode(QListWidget.InternalMove)
        self.thumbs.setDefaultDropAction(Qt.MoveAction)
        self.thumbs.setSelectionMode(QListWidget.SingleSelection)
        self.thumbs.setSpacing(8)
        self.thumbs.setToolTip("Seiten per Ziehen umsortieren.")
        self.thumbs.currentItemChanged.connect(self._on_thumb_changed)
        # Nach dem Umsortieren die Seitennummern neu vergeben
        self.thumbs.model().rowsMoved.connect(self._on_rows_moved)
        right.addWidget(self.thumbs)

        root.addLayout(right, stretch=1)

        self._update_actions()

    def _apply_style(self):
        down = self.arrow_down.replace("\\", "/")
        up = self.arrow_up.replace("\\", "/")
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: #1e2230; color: #e6e9f0;
                font-family: 'Segoe UI', 'Ubuntu', 'Cantarell', sans-serif;
                font-size: 13px; }}
            #appTitle {{ font-size: 24px; font-weight: 700; color: #ffffff;
                padding: 2px 0 6px 2px; }}

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
            QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        """)

    # -- Geräte / Drucker ---------------------------------------------------
    def _detect_devices(self):
        self.dev_worker = DeviceScanWorker()
        self.dev_worker.finished_ok.connect(self._on_devices_found)
        self.dev_worker.start()

    def _on_devices_found(self, devices):
        self.device_combo.clear()
        if not devices:
            self.device_combo.addItem("Kein Scanner gefunden", None)
            self.device_combo.setEnabled(False)
            self.set_status("Kein Scanner gefunden. Ist das Gerät angeschlossen?")
        else:
            for dev_id, desc in devices:
                self.device_combo.addItem(f"{desc}", dev_id)
            # Canon bevorzugt vorauswählen
            for i, (dev_id, _) in enumerate(devices):
                if "pixma" in dev_id.lower() or "canon" in dev_id.lower():
                    self.device_combo.setCurrentIndex(i)
                    break
            self.device_combo.setEnabled(True)
            self.set_status("Scanner bereit.")
        self._update_actions()

    def _load_printers(self):
        try:
            out = subprocess.run(["lpstat", "-p"], capture_output=True,
                                 text=True, timeout=10).stdout
            printers = re.findall(r"^(?:Drucker|printer)\s+(\S+)", out, re.MULTILINE)
        except Exception:
            printers = []
        self.printer_combo.clear()
        if printers:
            self.printer_combo.addItems(printers)
            # Standarddrucker markieren
            try:
                default = subprocess.run(["lpstat", "-d"], capture_output=True,
                                         text=True, timeout=10).stdout
                m = re.search(r":\s*(\S+)", default)
                if m and m.group(1) in printers:
                    self.printer_combo.setCurrentText(m.group(1))
            except Exception:
                pass
        else:
            self.printer_combo.addItem("Kein Drucker gefunden")
            self.printer_combo.setEnabled(False)

    # -- Scannen ------------------------------------------------------------
    def start_scan(self):
        device = self.device_combo.currentData()
        if not device:
            self.set_status("Kein Scanner ausgewählt.")
            return

        width, height = PAGE_SIZES[self.size_combo.currentText()]
        mode = SCAN_MODES[self.mode_combo.currentText()]
        resolution = self.res_combo.currentData()

        self.page_counter += 1
        out_path = os.path.join(self.tmpdir, f"seite_{self.page_counter:03d}.png")

        self.progress.setValue(0)
        self.progress.show()
        self.scan_btn.setEnabled(False)
        self.quick_btn.setEnabled(False)
        self.set_status("Scanne … bitte den Scanner nicht öffnen.")

        self.scan_worker = ScanWorker(device, resolution, mode, width, height, out_path)
        self.scan_worker.progress.connect(self.progress.setValue)
        self.scan_worker.finished_ok.connect(self._on_scan_done)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.start()

    def _on_scan_done(self, path):
        self.progress.hide()

        # Miniatur erzeugen
        pix = QPixmap(path)
        icon = QIcon(pix.scaled(96, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        item = QListWidgetItem(icon, "")
        item.setData(Qt.UserRole, path)
        item.setTextAlignment(Qt.AlignHCenter)
        self.thumbs.addItem(item)
        self.thumbs.setCurrentItem(item)   # zeigt sie sofort in der Vorschau
        self._renumber()

        if self._pending_quick:
            # Schnellkopie: frisch gescannte Seite direkt drucken
            self._pending_quick = False
            self.set_status("Gescannt – sende an Drucker …")
            if not self._send_to_printer([path]):
                self._set_idle()
        else:
            self._set_idle()
            self.set_status(f"Seite {self.thumbs.count()} gescannt.")

    def _on_scan_failed(self, msg):
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
        if current:
            self.preview.set_image(current.data(Qt.UserRole))
        else:
            self.preview.clear_image()
        self._update_actions()

    def _renumber(self):
        """Vergibt fortlaufende Seitennummern gemäß aktueller Reihenfolge."""
        for i in range(self.thumbs.count()):
            self.thumbs.item(i).setText(f"Seite {i + 1}")

    def _on_rows_moved(self, *args):
        self._renumber()
        self.set_status("Seitenreihenfolge geändert.")

    def delete_current(self):
        row = self.thumbs.currentRow()
        if row < 0:
            return
        item = self.thumbs.takeItem(row)
        path = item.data(Qt.UserRole)
        try:
            os.remove(path)
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
    def _page_paths(self):
        return [self.thumbs.item(i).data(Qt.UserRole)
                for i in range(self.thumbs.count())]

    def _pdf_from(self, paths, target_path):
        with open(target_path, "wb") as f:
            f.write(img2pdf.convert(paths))

    def _build_pdf(self, target_path):
        self._pdf_from(self._page_paths(), target_path)

    def _printer_or_warn(self):
        printer = self.printer_combo.currentText()
        if not self.printer_combo.isEnabled() or not printer:
            QMessageBox.warning(self, "Kein Drucker",
                                "Es ist kein Drucker verfügbar.")
            return None
        return printer

    def _send_to_printer(self, paths):
        """Baut ein PDF aus den Pfaden und schickt es an den Drucker.
        Gibt True zurück, wenn der Druckauftrag gestartet wurde."""
        printer = self._printer_or_warn()
        if not printer:
            return False
        pdf_path = os.path.join(self.tmpdir, "_druck.pdf")
        try:
            self._pdf_from(paths, pdf_path)
        except Exception as e:
            QMessageBox.critical(self, "Fehler", str(e))
            return False

        copies = self.copies_spin.value()
        self.scan_btn.setEnabled(False)
        self.quick_btn.setEnabled(False)
        self.print_btn.setEnabled(False)
        self.set_status(f"Sende {len(paths)} Seite(n) an {printer} …")

        self.print_worker = PrintWorker(pdf_path, printer, copies)
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
        self._send_to_printer(self._page_paths())

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

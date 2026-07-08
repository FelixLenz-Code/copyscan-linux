# Kopierer

Eine kleine Kopierer-App für Linux: Sie verbindet einen beliebigen
SANE-Scanner (Flachbett **oder** mit automatischem Seiteneinzug/ADF) mit einem
CUPS-Drucker. Dokument einscannen → Vorschau prüfen → drucken **und/oder** als
PDF speichern.

## Starten

```bash
./start_kopierer.sh
```

oder direkt:

```bash
python3 kopierer.py
```

Optional als Menü-Eintrag installieren:

```bash
cp kopierer.desktop ~/.local/share/applications/
```

## AppImage (ohne Installation)

Ein GitHub-Actions-Workflow baut das AppImage für jedes **Versions-Tag**. Es
enthält Python, PyQt5, Pillow und img2pdf – nur `scanimage` (SANE) und `lp`
(CUPS) müssen auf dem System vorhanden sein.

- **Neue Version veröffentlichen:** einen `v*`-Tag pushen, z. B.

  ```bash
  git tag v1.0.0 && git push origin v1.0.0
  ```

  Der Workflow baut das AppImage und legt automatisch ein festes
  **[Versions-Release](../../releases)** mit diesem Tag an. Zum Testen ohne Tag
  lässt sich der Workflow auch manuell starten (*Run workflow*); das AppImage
  liegt dann als Artefakt beim jeweiligen
  [Actions-Lauf](../../actions/workflows/build-appimage.yml).

  Das AppImage wird bewusst mit der klassischen AppImage-Runtime gepackt, damit
  auch AppImageLauncher/libappimage es problemlos registrieren kann.

- **Ausführen:**

  ```bash
  chmod +x Kopierer-x86_64.AppImage
  ./Kopierer-x86_64.AppImage
  ```

> Hinweis: Auf sehr schlanken Systemen können für Qt noch X11-Bibliotheken
> fehlen (z. B. `libxcb-xinerama0`, `libxcb-cursor0`). Auf üblichen
> Desktop-Installationen sind sie vorhanden.

## Bedienung

1. **Gerät** wählen (das erste gefundene ist vorausgewählt; ein anderer
   Standard lässt sich im Reiter *Einstellungen* festlegen).
2. **Quelle** wählen: *Flachbett* oder – falls der Scanner es anbietet –
   *Automatischer Einzug (ADF)* bzw. *Duplex-Einzug*. Die verfügbaren Quellen
   werden je Gerät automatisch erkannt.
3. **Auflösung**, **Modus** (Farbe/Graustufen/SW) und **Format** (A4, A5,
   Letter, ganze Fläche) einstellen.
4. Dokument auflegen und **„Seite scannen“** klicken – die Seite erscheint
   sofort groß in der Vorschau und als Miniatur unten.
   - **Mit ADF** zieht ein Klick automatisch **alle eingelegten Blätter**
     nacheinander ein; ein Duplex-Einzug liefert Vorder- und Rückseite.
5. Für weitere Seiten das nächste Blatt auflegen und erneut scannen; alle
   Seiten sammeln sich in der Leiste. Miniaturen lassen sich per Ziehen
   umsortieren, einzeln oder komplett löschen.
6. **Seite bearbeiten** (Werkzeugleiste über der Vorschau, wirkt jeweils auf
   die ausgewählte Seite):
   - **Drehen** links/rechts (↺ ↻),
   - **Zuschnitt**: Rechteck in der Vorschau aufziehen und *Übernehmen*,
   - **Farbfilter**: Farbe / Graustufen / Schwarz-Weiß,
   - **✨ Enhance**: verbessert den Scan automatisch in mehreren Schritten –
     **geraderücken** (Schräglage per Textzeilen-Analyse erkennen und
     ausgleichen), **Weißabgleich** (Farbstich entfernen),
     **Hintergrund-/Beleuchtungsausgleich** (ungleichmäßige Ausleuchtung
     glätten, Papier weiß ziehen), **Kontrast** anheben und **nachschärfen**;
     lässt sich pro Seite an-/ausschalten,
   - **Kontrast** und **Helligkeit** über die Regler (Doppelklick = zurück),
   - **Zurücksetzen** verwirft alle Änderungen der Seite.

   Die Bearbeitung ist nicht-destruktiv – der Rohscan bleibt erhalten, das
   Ergebnis landet in Vorschau, Druck und PDF.
7. **Vorschau ansehen**: mit dem **Mausrad zoomen**, bei Vergrößerung durch
   **Ziehen verschieben**; *Anpassen* setzt die Ansicht wieder aufs Fenster,
   *1:1* zeigt die Originalgröße (100 %). Die Vorschau nutzt die volle
   Scan-Auflösung – beim Hineinzoomen bzw. bei *1:1* sieht man daher direkt,
   wie scharf eine höhere **Auflösung** wirklich ist (die gescannte Pixelzahl
   steht nach dem Scan in der Statuszeile).
8. Rechts unten: **Drucken** (Drucker, **Druckqualität** – Entwurf/Normal/Hoch
   – und Exemplare wählbar) oder **Als PDF speichern**.

## Voraussetzungen

Unter Ubuntu/Debian normalerweise schon vorhanden – falls nicht:

```bash
sudo apt install sane-utils cups-client python3-pyqt5 python3-pil python3-img2pdf
```

Der Scanner wird über SANE angesprochen (beliebiges Backend). Test:

```bash
scanimage -L          # listet die verfügbaren Scanner auf
```

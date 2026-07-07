# Kopierer

Eine kleine Kopierer-App für Linux: Sie verbindet deinen Flachbettscanner
(Canon **CanoScan 8800F**) mit einem CUPS-Drucker. Dokument einscannen →
Vorschau prüfen → drucken **und/oder** als PDF speichern.

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

## Bedienung

1. **Gerät** wählen (der Canon-Scanner ist automatisch vorausgewählt).
2. **Auflösung**, **Modus** (Farbe/Graustufen/SW) und **Format** (A4, A5,
   Letter, ganze Fläche) einstellen.
3. Dokument auflegen und **„Seite scannen“** klicken – die Seite erscheint
   sofort groß in der Vorschau und als Miniatur unten.
4. Für mehrere Seiten einfach das nächste Blatt auflegen und erneut scannen;
   alle Seiten sammeln sich in der Leiste. Miniaturen lassen sich per
   Ziehen umsortieren, einzeln oder komplett löschen.
5. Rechts unten: **Drucken** (Drucker + Exemplare wählbar) oder
   **Als PDF speichern**.

## Voraussetzungen

Unter Ubuntu/Debian normalerweise schon vorhanden – falls nicht:

```bash
sudo apt install sane-utils cups-client python3-pyqt5 python3-pil python3-img2pdf
```

Der Scanner wird über das SANE-**pixma**-Backend angesprochen. Test:

```bash
scanimage -L          # sollte den CanoScan 8800F auflisten
```

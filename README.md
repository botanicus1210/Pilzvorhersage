# 🍄 Pilzvorhersage Österreich

Eine interaktive Karte, die **Pilzwachstum prognostiziert** — nicht nur das Wetter anzeigt.
Für sechs Arten wird pro Waldzelle ein Wachstumsindex (0–100) berechnet, inklusive
Reifezeit-Verzug, Bodenfeuchte-Modell und Wettervorhersage.

**➡️ Live-Karte:** https://botanicus1210.github.io/Pilzvorhersage/

Läuft komplett serverlos auf GitHub Pages und aktualisiert sich täglich von selbst.

---

## Was die Karte kann

- **6 Arten**: Herbst- und Sommersteinpilz, Eierschwammerl, Herbsttrompete, Morchel, Parasol
- **Wachstumsindex 0–100** je 5×5-km-Waldzelle, farbcodiert
- **Prognose-Slider** von *heute* bis *+21 Tage* — zeigt, wann frischer Regen „reif" wird
- **Punkte- oder Heatmap-Ansicht**, Deckkraft-Regler, Ortssuche
- **Fund-Overlay** aus iNaturalist (Marker oder Dichte-Heatmap), nach ökologischer Gilde
- Klick auf eine Zelle zeigt alle Einflussfaktoren

---

## Wie das Modell funktioniert

Pro Waldzelle und Art:

```
Index = Saison × Bodenfeuchte × Temperatur × Datenabdeckung
```

- **Saison** — art-spezifisches Kalenderfenster (verschiebt sich mit dem Prognose-Slider)
- **Bodenfeuchte** — ein *Antecedent Precipitation Index*: der Tagesregen wird täglich
  abgebaut (`Feuchte = k·Feuchte_gestern + Regen`). Das unterscheidet Starkregen
  (spikt, klingt ab) von durchgehender Feuchte (Plateau) und lässt Trockenphasen absinken.
- **Reifezeit-Verzug (Lag)** — Fruchtkörper erscheinen 1–2 Wochen *nach* dem Regen.
  Der Index für „heute" wird deshalb vom Regen vor ~2 Wochen getrieben, nicht vom frischen.
- **Temperatur** — art-spezifisches Optimum (Gauß-Glocke)
- **Waldtyp-Filter** — CORINE Laub-/Nadel-/Mischwald; z.B. Sommersteinpilz nur im Laubwald
- **Wettervorhersage** — GeoSphere AROME (2,5 Tage) wird an die gemessene Reihe angehängt;
  durch den Reifezeit-Lag reicht das für sinnvolle Prognosen bis ~+16 Tage.

Der Index wird **im Browser** aus den Rohreihen gerechnet — dadurch ist der Slider live.

### Fund-Gilden

Ein Fund einer Art belegt die Bedingungen für die ganze **ökologische Gilde**. Trick gegen
den Melde-Bias: den Fliegenpilz meldet jeder, den Steinpilz keiner — da beide dieselben
Bedingungen brauchen, wird der gut gemeldete Indikator zum Signal für die Speisepilze.
Die Gilden fassen ökologisch gleichartige Taxa (z.B. alle ektomykorrhizalen Familien).

---

## Datenquellen

| Quelle | Verwendung | Lizenz |
|--------|-----------|--------|
| GeoSphere Austria **INCA** | Niederschlag + Temperatur, 1 km, 30 Tage | CC BY 4.0 |
| GeoSphere Austria **AROME** | Niederschlagsprognose, 2,5 Tage | CC BY 4.0 |
| **CORINE Land Cover** (EEA) | Waldtyp-Maske | © European Union, Copernicus |
| **iNaturalist** | Fundmeldungen (research grade) | jeweilige Beobachter-Lizenzen |
| **OpenStreetMap** / **Esri** | Kartenhintergrund | © OSM contributors, © Esri |

Kein API-Key nötig.

---

## Repo-Struktur

```
pilzwachstum.py          Tageslauf: INCA + AROME -> pilzwachstum.geojson; baut/nutzt die Wald-Maske
funde.py                 iNaturalist-Funde nach Gilde -> funde.geojson
pilz_forest_mask.json    vorgebaute Wald-Maske (Waldtyp je Zelle) — einmal erzeugt, dann statisch
site/
  index.html             das Frontend (Leaflet-Karte + Modell)
  pilzwachstum.geojson   wird vom Workflow erzeugt (nicht eingecheckt)
  funde.geojson          wird vom Workflow erzeugt (nicht eingecheckt)
.github/workflows/
  update.yml             täglicher Lauf + Deploy auf GitHub Pages
```

---

## Einrichtung (serverlos)

1. Repo **öffentlich** anlegen, die obigen Dateien hochladen.
2. **Settings → Pages → Source: „GitHub Actions"**.
3. **Actions → „Pilzkarte aktualisieren" → Run workflow** (erster Lauf).
4. Nach ~5–10 Min ist die Karte live unter `https://<name>.github.io/<repo>/`.

Danach aktualisiert der Cron (`update.yml`) die Karte täglich. Die vorgebaute
`pilz_forest_mask.json` sorgt dafür, dass der Tageslauf die Maske nicht neu bauen muss.

> Hinweis: GitHub deaktiviert geplante Workflows nach 60 Tagen ohne Repo-Aktivität —
> gelegentlich etwas committen oder manuell starten hält den Cron wach.

### Wald-Maske neu bauen (einmalig)

Falls die Maske fehlt oder das Raster geändert wird: in `pilzwachstum.py`
`build_mask()` einmal laufen lassen (CORINE-Abfrage pro Rasterpunkt, ~1 h, resümierbar).
Ergebnis als `pilz_forest_mask.json` ins Repo.

---

## Lokal ausführen

Reine Standardbibliothek, keine Abhängigkeiten. Pfade über Umgebungsvariablen:

```bash
PILZ_OUTPUT=site/pilzwachstum.geojson PILZ_MASK=pilz_forest_mask.json python3 pilzwachstum.py --run
PILZ_FUNDE=site/funde.geojson python3 funde.py --run
```

Endpunkte vorab testen: `python3 pilzwachstum.py --probe` bzw. `funde.py --probe`.

---

## Grenzen & Ehrlichkeit

- Die Artparameter sind **fundierte Schätzungen, nicht aus Funddaten kalibriert**.
  Es ist ein transparentes Heuristik-Modell — bewusst der Mittelweg zwischen
  „hat's geregnet?" und einem datenkalibrierten Modell.
- **Nicht** im Modell: Höhenlage (Kalenderverschiebung), Bodenart/pH, Grundwasser,
  Hangrichtung, Bestandsalter — dafür fehlen offene Daten bzw. es sprengt den Rahmen.
- **Wettervorhersage** ist gerade bei konvektiven Sommergewittern räumlich fleckig und
  unsicher; jenseits ~+16 Tagen blendet der Abdeckungs-Term aus.
- **Fundmeldungen** sind nur Positivfunde und sammler-verzerrt; iNat verschleiert manche
  Koordinaten (die werden herausgefiltert). Es ist Bestätigung, keine flächige Abdeckung.

---

## Mögliche Weiterentwicklungen

- **Kalibrierung** der Artparameter aus historischen Funddaten (GBIF/iNaturalist) —
  mit Korrektur der bekannten Biases (nur Positivfunde, Sammler-Bias, kein Alter/Dichte).
- Zweite Fundquelle (**Observation.org**) für höhere Dichte.
- **Kälteschock-Trigger** für Herbstarten, Unsicherheits-Dämpfung des Prognose-Regens.

---

## Lizenz

Code frei verwendbar (z.B. MIT). Bei Nutzung die **Datenlizenzen der Quellen** beachten
und entsprechend attribuieren (siehe Tabelle oben).

*Kein Bestimmungswerkzeug — im Zweifel keinen Pilz essen. Die Karte sagt, wo sich die
Suche lohnen könnte, nicht was essbar ist.*

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pilzwachstum.py  —  Pilzwachstums-Vorhersage fuer Oesterreich

Was es tut
----------
1. Spannt ein grobes Punktraster ueber Oesterreich (Default 5 km).
2. Holt pro Punkt taegliche Werte:
      - Niederschlag (RR) und 2m-Temperatur (T2M) von GeoSphere INCA  (1km, CC BY 4.0)
      - Bodenfeuchte + ET0 von Open-Meteo  (INCA hat keine Bodenfeuchte)
   Beide APIs ohne Key, beide werden gebatcht (mehrere lat_lon pro Request).
3. Rechnet pro Punkt einen Wachstums-Index je Art (Steinpilz, Eierschwammerl,
   Parasol) ueber ein einstellbares Fenster und schreibt das Ergebnis als
   kompaktes GeoJSON in HAs www/-Ordner.

Wichtig: gegen die echten APIs konnte ich es nicht live testen. Vor dem ersten
Volllauf einmal den Probe-Modus fahren, der die Feldnamen/Struktur dumpt:

      python3 pilzwachstum.py --probe

Falls INCA die Parameter klein schreibt (t2m statt T2M) o.ae., siehst du das
dort sofort und korrigierst INCA_PARAMS unten.

Standalone-Lauf (schreibt das GeoJSON):

      python3 pilzwachstum.py --run

Als AppDaemon-App: siehe Klasse PilzwachstumApp am Ende.

Lizenz-Hinweis fuer die Karte: "Wetterdaten: GeoSphere Austria (INCA) CC BY 4.0,
Open-Meteo. Kartengrundlage OpenStreetMap."
"""

from __future__ import annotations
import sys
import json
import time
import math
import datetime as dt
from urllib import request, parse, error

# --------------------------------------------------------------------------
# KONFIG
# --------------------------------------------------------------------------

# Bounding-Box Oesterreich (etwas grosszuegig). lat/lon in Grad.
AT_LAT_MIN, AT_LAT_MAX = 46.30, 49.05
AT_LON_MIN, AT_LON_MAX = 9.40, 17.20

GRID_KM = 5.0          # Rasterabstand in km. 5 = ~1500-3000 Punkte. Pi-tauglich.
MAX_WINDOW_DAYS = 30   # so viel Historie holen wir; Frontend-Slider <= das
BATCH_POINTS = 200     # lat_lon-Paare pro INCA-Request
OM_BATCH_POINTS = 50   # Open-Meteo ist empfindlicher beim Rate-Limit
REQUEST_PAUSE = 0.8    # Pause zwischen INCA-Requests (s)
OM_REQUEST_PAUSE = 2.0 # Pause zwischen Open-Meteo-Requests (s)
HTTP_TIMEOUT = 60

# Ausgabepfad: HAs www/-Ordner -> erreichbar unter /local/pilz/pilzwachstum.geojson
# Auf HAOS sieht AppDaemon HAs config je nach Add-on-Version unterschiedlich:
#   - aeltere AD-Versionen: /config  (= HA config)
#   - AD v15+ (addon_configs): HA config liegt unter /homeassistant
# resolve_www_dir() probiert die Kandidaten durch und nimmt den ersten, der
# existiert (bzw. dessen Elternordner existiert). Du kannst den Pfad in
# apps.yaml via output_path immer fest ueberschreiben.
WWW_CANDIDATES = ["/homeassistant/www", "/config/www"]
OUTPUT_NAME = "pilz/pilzwachstum.geojson"


def resolve_output_path():
    import os
    env = os.environ.get("PILZ_OUTPUT")   # z.B. GitHub Actions / cron
    if env:
        return env
    for base in WWW_CANDIDATES:
        # Elternordner (z.B. /homeassistant oder /config) muss existieren
        if os.path.isdir(os.path.dirname(base)):
            return os.path.join(base, OUTPUT_NAME)
    # Fallback: relativ zum Arbeitsverzeichnis (Standalone-Test)
    return os.path.join("www", OUTPUT_NAME)


OUTPUT_PATH = None  # wird in run()/probe zur Laufzeit aufgeloest

INCA_BASE = "https://dataset.api.hub.geosphere.at/v1/timeseries/historical/inca-v1-1h-1km"
INCA_PARAMS = ["RR", "T2M"]   # <-- bei Bedarf nach --probe anpassen (Gross/Kleinschreibung!)

OM_BASE = "https://api.open-meteo.com/v1/forecast"  # forecast-Endpoint kann past_days bis ~92
OM_HOURLY = ["soil_moisture_7_to_28cm", "soil_temperature_7_to_28cm"]
OM_DAILY = ["et0_fao_evapotranspiration"]

# --- Wald-Maske (CORINE Land Cover 2018, EEA ArcGIS) ----------------------
# Einmalig via --buildmask (oder build_mask: true in apps.yaml) erzeugen.
# Danach fragt run() nur noch Waldpunkte ab.
CLC_SERVICE = ("https://image.discomap.eea.europa.eu/arcgis/rest/services/"
               "Corine/CLC2018_LAEA/MapServer/0/query")
FOREST_CODES = {"311", "312", "313", "324"}  # Laub/Nadel/Misch/Uebergangswald
MASK_BUFFER_M = 2000   # Puffer um Punkt: Zelle gilt als Wald wenn Wald in <=2km
MASK_PATH = None       # None -> neben dem Script (resolve_mask_path())
MASK_PAUSE = 0.3       # Pause zwischen CORINE-Requests (s)
MASK_SAVE_EVERY = 100  # Zwischenspeichern alle N Punkte (resumierbar)

# --------------------------------------------------------------------------
# ARTEN-PARAMETER (heuristisch, v1 - spaeter mit eigenen Funddaten kalibrieren)
#   t_opt/t_sigma : Optimum & Breite der Tagesmittel-Temp [degC]
#   doy_*         : saisonales Fenster (day-of-year, weiche Flanken)
#   r_ref         : Niederschlags-Referenz fuer 14d in mm (saettigt tanh)
#   w_rain/w_moist: Gewicht Regen- vs Bodenfeuchte-Term
#   cold_bonus    : Staerke des Kaelteschock-Triggers (0 = aus)
# --------------------------------------------------------------------------
SPECIES = {
    "steinpilz": {
        "label": "Herbststeinpilz",
        "t_opt": 15.0, "t_sigma": 5.0,
        "doy_start": 196, "doy_peak1": 225, "doy_peak2": 285, "doy_end": 315,
        "r_ref14": 28.0, "w_rain": 0.45, "w_moist": 0.55, "cold_bonus": 0.35,
    },
    "sommersteinpilz": {
        "label": "Sommersteinpilz",   # Boletus reticulatus - waermeliebend, frueher
        "t_opt": 17.5, "t_sigma": 5.5,
        "doy_start": 150, "doy_peak1": 175, "doy_peak2": 240, "doy_end": 270,
        "r_ref14": 27.0, "w_rain": 0.45, "w_moist": 0.55, "cold_bonus": 0.15,
    },
    "eierschwammerl": {
        "label": "Eierschwammerl",
        "t_opt": 17.0, "t_sigma": 6.0,
        "doy_start": 152, "doy_peak1": 196, "doy_peak2": 270, "doy_end": 300,
        "r_ref14": 30.0, "w_rain": 0.55, "w_moist": 0.45, "cold_bonus": 0.10,
    },
    "herbsttrompete": {
        "label": "Herbsttrompete",    # Craterellus cornucopioides - Buche/Eiche, kuehl
        "t_opt": 13.5, "t_sigma": 5.0,
        "doy_start": 220, "doy_peak1": 250, "doy_peak2": 295, "doy_end": 325,
        "r_ref14": 30.0, "w_rain": 0.50, "w_moist": 0.50, "cold_bonus": 0.20,
    },
    "morchel": {
        "label": "Morchel",           # Morchella - FRUEHLING, waermt nach Schneeschmelze
        "t_opt": 12.0, "t_sigma": 4.5,
        "doy_start": 74, "doy_peak1": 95, "doy_peak2": 125, "doy_end": 150,
        "r_ref14": 24.0, "w_rain": 0.50, "w_moist": 0.50, "cold_bonus": 0.05,
    },
    "parasol": {
        "label": "Parasol",
        "t_opt": 16.0, "t_sigma": 6.0,
        "doy_start": 210, "doy_peak1": 240, "doy_peak2": 285, "doy_end": 315,
        "r_ref14": 26.0, "w_rain": 0.50, "w_moist": 0.50, "cold_bonus": 0.25,
    },
}

# Waldtyp-Affinitaet je Art (CORINE-Codes: 311 Laub, 312 Nadel, 313 Misch,
# 324 Uebergangswald). Eine Zelle zeigt eine Art nur, wenn ihr Waldtyp passt.
# 313 (Mischwald) enthaelt beide -> fuer Laub- UND Nadel-Arten gueltig.
FOREST_AFFINITY = {
    "steinpilz":       {"311", "312", "313"},        # B. edulis: Nadel + Laub
    "sommersteinpilz": {"311", "313"},               # B. reticulatus: nur Laub/Misch
    "eierschwammerl":  {"311", "312", "313"},         # Nadel + Laub
    "herbsttrompete":  {"311", "313"},               # Buche/Eiche -> Laub/Misch
    "morchel":         {"311", "313", "324"},         # Laub/Au/gestoerte Flaechen
    "parasol":         {"311", "312", "313", "324"},  # Generalist / Waldrand
}

# --------------------------------------------------------------------------
# RASTER
# --------------------------------------------------------------------------

def build_grid(grid_km=GRID_KM):
    """Gleichmaessiges lat/lon-Raster. Laengengrad-Abstand wird mit cos(lat)
    korrigiert, damit die Zellen real ~quadratisch sind."""
    dlat = grid_km / 111.0
    points = []
    lat = AT_LAT_MIN
    while lat <= AT_LAT_MAX:
        dlon = grid_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
        lon = AT_LON_MIN
        while lon <= AT_LON_MAX:
            points.append((round(lat, 4), round(lon, 4)))
            lon += dlon
        lat += dlat
    return points


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _get_json(url):
    req = request.Request(url, headers={"User-Agent": "pilzwachstum/1.0 (HomeAssistant)"})
    with request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------
# INCA  (Niederschlag + Temperatur, stuendlich -> taeglich)
# --------------------------------------------------------------------------

def fetch_inca(points, start, end):
    """Liefert dict {(lat,lon): {'rain': [taeglich mm], 'tmean': [taeglich degC]}}.
    Aggregiert die Stundenwerte zu Tageswerten anhand der timestamps."""
    out = {}
    par = ",".join(INCA_PARAMS)
    for batch in chunked(points, BATCH_POINTS):
        latlon = "&".join(f"lat_lon={lat},{lon}" for lat, lon in batch)
        url = (f"{INCA_BASE}?parameters={par}"
               f"&start={start.strftime('%Y-%m-%dT00:00')}"
               f"&end={end.strftime('%Y-%m-%dT23:00')}"
               f"&output_format=geojson&{latlon}")
        try:
            data = _get_json(url)
        except (error.URLError, error.HTTPError, ValueError) as e:
            print(f"[INCA] Batch-Fehler: {e}", file=sys.stderr)
            continue
        ts = data.get("timestamps", [])
        days = [t[:10] for t in ts]  # 'YYYY-MM-DD'
        feats = data.get("features", [])
        for (lat, lon), feat in zip(batch, feats):
            props = feat.get("properties", {}).get("parameters", {})
            # robust gegen Gross/Klein-Schreibung der Parameternamen
            rr = _param_array(props, "RR")
            tt = _param_array(props, "T2M")
            out[(lat, lon)] = {
                "rain": _to_daily(days, rr, how="sum"),
                "tmean": _to_daily(days, tt, how="mean"),
            }
        time.sleep(REQUEST_PAUSE)
    return out


def _param_array(props, name):
    """Hole das data-Array zu einem Parameter, case-insensitiv."""
    for key in (name, name.lower(), name.upper()):
        if key in props and isinstance(props[key], dict):
            return props[key].get("data", []) or []
    # Fallback: erstes data-Array das passt
    return []


def _to_daily(days, hourly, how="sum"):
    """Aggregiere stuendliche Werte zu Tageswerten. days und hourly gleich lang."""
    if not hourly:
        return []
    acc = {}
    cnt = {}
    for d, v in zip(days, hourly):
        if v is None:
            continue
        acc[d] = acc.get(d, 0.0) + float(v)
        cnt[d] = cnt.get(d, 0) + 1
    res = []
    for d in sorted(acc.keys()):
        if how == "mean":
            res.append(acc[d] / max(1, cnt[d]))
        else:
            res.append(acc[d])
    return res


# --------------------------------------------------------------------------
# OPEN-METEO  (Bodenfeuchte + ET0)  - optional, mit Fallback
# --------------------------------------------------------------------------

def fetch_openmeteo(points, past_days):
    """Liefert dict {(lat,lon): {'soil': [taeglicher Mittel m3/m3], 'et0': [mm]}}.
    Open-Meteo akzeptiert kommaseparierte Koordinaten -> Liste von Bloecken.
    Bricht nach 3 aufeinanderfolgenden 429-Fehlern ab statt alle Batches
    durchzuhangeln."""
    out = {}
    consecutive_429 = 0
    for batch in chunked(points, OM_BATCH_POINTS):
        lats = ",".join(str(lat) for lat, lon in batch)
        lons = ",".join(str(lon) for lat, lon in batch)
        q = {
            "latitude": lats, "longitude": lons,
            "hourly": ",".join(OM_HOURLY),
            "daily": ",".join(OM_DAILY),
            "past_days": str(min(92, past_days + 1)),
            "forecast_days": "1",
            "timezone": "Europe/Vienna",
        }
        url = OM_BASE + "?" + parse.urlencode(q)
        try:
            data = _get_json(url)
            consecutive_429 = 0  # reset bei Erfolg
        except error.HTTPError as e:
            if e.code == 429:
                consecutive_429 += 1
                print(f"[OpenMeteo] 429 Rate-Limit ({consecutive_429}/3)", file=sys.stderr)
                if consecutive_429 >= 3:
                    print("[OpenMeteo] 3x 429 -> Bodenfeuchte wird fuer diesen Lauf uebersprungen",
                          file=sys.stderr)
                    break
            else:
                print(f"[OpenMeteo] Batch-Fehler: {e}", file=sys.stderr)
            continue
        except (error.URLError, ValueError) as e:
            print(f"[OpenMeteo] Batch-Fehler: {e}", file=sys.stderr)
            continue
        blocks = data if isinstance(data, list) else [data]
        for (lat, lon), blk in zip(batch, blocks):
            hourly = blk.get("hourly", {})
            soil_h = hourly.get("soil_moisture_7_to_28cm", []) or []
            htimes = hourly.get("time", []) or []
            hdays = [t[:10] for t in htimes]
            soil_daily = _to_daily(hdays, soil_h, how="mean")
            daily = blk.get("daily", {})
            et0 = daily.get("et0_fao_evapotranspiration", []) or []
            out[(lat, lon)] = {"soil": soil_daily, "et0": [float(x or 0) for x in et0]}
        time.sleep(OM_REQUEST_PAUSE)
    return out


# --------------------------------------------------------------------------
# WALD-MASKE  (CORINE Land Cover, einmalig)
# --------------------------------------------------------------------------

def resolve_mask_path():
    import os
    env = os.environ.get("PILZ_MASK")
    if env:
        return env
    if MASK_PATH:
        return MASK_PATH
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "pilz_forest_mask.json")


def _clc_forest_codes(lat, lon):
    """Set der CORINE-Waldcodes (311/312/313/324) die in <=MASK_BUFFER_M um
    (lat,lon) vorkommen. Leeres Set = kein Wald.
    Nutzt eine Distinct-Query (kleine Antwort, nennt die vorhandenen Typen),
    faellt bei Bedarf auf generisches Datensatz-Scannen zurueck.
    Retries mit Backoff bei Timeout/Netzfehler."""
    base = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "distance": str(MASK_BUFFER_M),
        "units": "esriSRUnit_Meter",
        "spatialRel": "esriSpatialRelIntersects",
        "f": "json",
    }
    codes_in = ",".join(f"'{c}'" for c in sorted(FOREST_CODES))

    def _extract(data):
        found = set()
        for feat in data.get("features", []):
            for v in (feat.get("attributes", {}) or {}).values():
                s = "".join(ch for ch in str(v)[:3] if ch.isdigit())
                if s in FOREST_CODES:
                    found.add(s)
        return found

    for attempt in range(3):
        try:
            # 1) Distinct-Query: nur die vorhandenen Wald-Codes (kleine Antwort)
            q = dict(base)
            q["where"] = f"Code_18 IN ({codes_in})"
            q["outFields"] = "Code_18"
            q["returnDistinctValues"] = "true"
            q["returnGeometry"] = "false"
            data = _get_json(CLC_SERVICE + "?" + parse.urlencode(q))
            if "features" in data and "error" not in data:
                return _extract(data)
            # 2) Fallback: generischer Scan aller Flaechen im Puffer
            q = dict(base)
            q["outFields"] = "*"
            q["returnGeometry"] = "false"
            q["resultRecordCount"] = "100"
            data = _get_json(CLC_SERVICE + "?" + parse.urlencode(q))
            return _extract(data)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))   # Backoff: 1.5s, 3s
    return set()


def _clc_forest_at(lat, lon):
    """True wenn ueberhaupt Wald in Reichweite (fuer probe_mask)."""
    return bool(_clc_forest_codes(lat, lon))


def build_mask(grid_km=GRID_KM):
    """Einmaliger Lauf: prueft jeden Rasterpunkt gegen CORINE, speichert
    resumierbar. Bei Unterbrechung einfach erneut starten -> macht weiter."""
    import os
    path = resolve_mask_path()
    points = build_grid(grid_km)

    # vorhandenen Fortschritt laden
    results = {}
    if os.path.exists(path):
        try:
            prev = json.load(open(path, encoding="utf-8"))
            results = prev.get("results", {})
            # v1 -> v2 Migration: alte Maske speicherte 0/1 statt Waldcodes.
            #   0/"0"  -> "" (kein Wald, muss NICHT neu abgefragt werden)
            #   1/"1"  -> loeschen (Wald, aber Typ unbekannt -> neu abfragen)
            migrated = 0
            for k in list(results.keys()):
                v = results[k]
                if v in (0, "0"):
                    results[k] = ""
                elif v in (1, "1"):
                    del results[k]
                    migrated += 1
            if migrated:
                print(f"[mask] v1-Maske erkannt: {migrated} Waldzellen werden "
                      f"fuer Waldtyp neu abgefragt (Nicht-Wald bleibt gespart)")
            print(f"[mask] {len(results)} Punkte bereits bekannt, setze fort")
        except Exception:
            results = {}

    def save():
        tmp = path + ".tmp"
        body = {"version": 2, "grid_km": grid_km, "buffer_m": MASK_BUFFER_M,
                "codes": sorted(FOREST_CODES), "results": results}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, separators=(",", ":"))
        os.replace(tmp, path)

    total = len(points)
    done = 0
    forest = sum(1 for v in results.values() if v)
    for (lat, lon) in points:
        key = f"{lat},{lon}"
        if key in results:        # schon geprueft (resume)
            done += 1
            continue
        try:
            codes = _clc_forest_codes(lat, lon)
        except Exception as e:
            print(f"[mask] Fehler {key}: {e} -> ueberspringe (spaeter erneut)",
                  file=sys.stderr)
            time.sleep(MASK_PAUSE * 3)
            continue
        results[key] = ",".join(sorted(codes))   # "" = kein Wald
        if codes:
            forest += 1
        done += 1
        if done % MASK_SAVE_EVERY == 0:
            save()
            print(f"[mask] {done}/{total} geprueft, davon {forest} Wald")
        time.sleep(MASK_PAUSE)

    save()
    print(f"[mask] FERTIG: {forest}/{total} Waldpunkte -> {path}")
    return forest


def load_mask(grid_km=GRID_KM):
    """Liefert dict {(lat,lon): set(waldcodes)} der Waldpunkte, oder None wenn
    keine Maske existiert. v1-Masken (0/1) werden als 'Wald unbekannten Typs'
    interpretiert (leeres Set -> keine Typ-Filterung fuer diese Zelle)."""
    import os
    path = resolve_mask_path()
    if not os.path.exists(path):
        return None
    try:
        body = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    res = body.get("results", {})
    if not res:
        return None
    out = {}
    for key, v in res.items():
        la, lo = key.split(",")
        pt = (float(la), float(lo))
        if isinstance(v, str) and v:                 # v2: "311,313"
            out[pt] = set(v.split(","))
        elif v in (1, "1"):                          # v1: Wald, Typ unbekannt
            out[pt] = set()                          # leeres Set = alle Arten ok
        # 0 / "" -> kein Wald -> nicht aufnehmen
    return out or None


def probe_mask():
    """Einen Wald- und einen Nicht-Wald-Punkt testen + Browser-URL ausgeben."""
    tests = [("Wienerwald (Wald)", 48.16, 16.20),
             ("Neusiedler See (Wasser)", 47.84, 16.76),
             ("Wien Zentrum (Stadt)", 48.21, 16.37)]
    q = {"geometry": "16.20,48.16", "geometryType": "esriGeometryPoint",
         "inSR": "4326", "distance": str(MASK_BUFFER_M), "units": "esriSRUnit_Meter",
         "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
         "returnGeometry": "false", "resultRecordCount": "100", "f": "json"}
    print("CORINE Test-URL (im Browser oeffnen):\n", CLC_SERVICE + "?" + parse.urlencode(q), "\n")
    for name, la, lo in tests:
        try:
            print(f"  {name:28s} -> Wald={_clc_forest_at(la, lo)}")
        except Exception as e:
            print(f"  {name:28s} -> Fehler: {e}")


# --------------------------------------------------------------------------
# MODELL
# --------------------------------------------------------------------------

def _logistic(x):
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def _season_factor(doy, sp):
    """Weiches saisonales Fenster: 0 ausserhalb, ~1 im Plateau zw. peak1..peak2,
    weiche Flanken via Logistik an start/end."""
    rise = _logistic((doy - sp["doy_start"]) / 6.0)
    fall = _logistic((sp["doy_end"] - doy) / 6.0)
    base = rise * fall
    # leichte Anhebung im Kern-Plateau
    if sp["doy_peak1"] <= doy <= sp["doy_peak2"]:
        base = min(1.0, base * 1.1)
    return base


def _cold_shock(tmean, window):
    """Erkenne einen Temperatursturz im Trigger-Fenster (vor ~10-21 Tagen).
    Rueckgabe 0..1: groesser bei groesserem Sturz."""
    n = len(tmean)
    if n < 22:
        return 0.0
    early = tmean[-21:-14]   # ~21..14 Tage zurueck
    late = tmean[-14:-7]     # ~14..7 Tage zurueck
    if not early or not late:
        return 0.0
    drop = (sum(early) / len(early)) - (sum(late) / len(late))
    # 0 degC Sturz -> 0 ; 6 degC Sturz -> ~1
    return max(0.0, min(1.0, drop / 6.0))


def growth_index(rain, tmean, soil, species_key, window):
    """Kern-Modell. Liefert 0..100 (gerundet) plus Detail-Terme."""
    sp = SPECIES[species_key]
    if not rain or not tmean:
        return None

    w = max(1, min(window, len(rain)))
    rain_w = sum(rain[-w:])
    tmean_w = sum(tmean[-w:]) / w

    # Regen-Term: auf 14d-Referenz skaliert, saettigend
    r_ref = sp["r_ref14"] * (w / 14.0)
    f_rain = math.tanh(rain_w / max(1.0, r_ref))

    # Bodenfeuchte-Term (falls vorhanden), sonst Fallback = f_rain
    if soil and len(soil) >= 1:
        recent = soil[-min(7, len(soil)):]
        sm = sum(recent) / len(recent)
        # m3/m3: ~0.15 trocken, ~0.30 feucht -> logistisch zentriert 0.22
        f_moist = _logistic((sm - 0.22) / 0.04)
    else:
        f_moist = f_rain

    f_water = sp["w_rain"] * f_rain + sp["w_moist"] * f_moist

    # Temperatur-Optimum (Gauss)
    f_temp = math.exp(-((tmean_w - sp["t_opt"]) ** 2) / (2 * sp["t_sigma"] ** 2))

    # Saison
    doy = dt.date.today().timetuple().tm_yday
    f_season = _season_factor(doy, sp)

    # Kaelteschock-Bonus
    f_trig = _cold_shock(tmean, window)

    index = f_season * f_water * f_temp * (1.0 + sp["cold_bonus"] * f_trig)
    index = max(0.0, min(1.0, index))
    return {
        "index": round(index * 100),
        "rain_mm": round(rain_w, 1),
        "tmean": round(tmean_w, 1),
        "f_rain": round(f_rain, 2),
        "f_moist": round(f_moist, 2),
        "f_temp": round(f_temp, 2),
        "f_season": round(f_season, 2),
        "f_trigger": round(f_trig, 2),
    }


# --------------------------------------------------------------------------
# GEOJSON-OUTPUT
# --------------------------------------------------------------------------

def build_geojson(points, inca, window, mask=None):
    """Speichert pro Waldzelle die taeglichen Reihen (Regen, Temperatur) +
    Waldtyp-Codes. Der Wachstumsindex wird im Frontend gerechnet (Lag-Kernel,
    Prognose-Slider). Reihen sind chronologisch: [0]=aeltester Tag, [-1]=gestern."""
    feats = []
    for (lat, lon) in points:
        d = inca.get((lat, lon))
        if not d or not d.get("rain"):
            continue
        cell_codes = mask.get((lat, lon), set()) if mask else set()
        rain = [round(float(x), 1) for x in d["rain"]]
        temp = [round(float(x)) for x in d["tmean"]]
        props = {
            "forest": ",".join(sorted(cell_codes)),   # "" = unbekannt -> alle Arten
            "rain": rain,
            "t": temp,
        }
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })
    # ref_doy = day-of-year des letzten Datentags (= gestern)
    ref_date = dt.date.today() - dt.timedelta(days=1)
    return {
        "type": "FeatureCollection",
        "metadata": {
            "generated": dt.datetime.now().isoformat(timespec="minutes"),
            "ref_date": ref_date.isoformat(),
            "ref_doy": ref_date.timetuple().tm_yday,
            "days": MAX_WINDOW_DAYS,
            "grid_km": GRID_KM,
            "species": {k: SPECIES[k]["label"] for k in SPECIES},
            "attribution": "GeoSphere Austria INCA (CC BY 4.0); OpenStreetMap",
        },
        "features": feats,
    }


def run(window=14, output_path=None, grid_km=GRID_KM):
    import os
    if output_path is None:
        output_path = resolve_output_path()
    points = build_grid(grid_km)

    mask = load_mask(grid_km)
    if mask is not None:
        before = len(points)
        points = [p for p in points if p in mask]
        print(f"[run] Wald-Maske aktiv: {len(points)}/{before} Punkte")
    else:
        print("[run] keine Wald-Maske gefunden -> alle Punkte "
              "(einmal build_mask laufen lassen)")

    end = dt.date.today() - dt.timedelta(days=1)   # INCA: Vortag ist verlaesslich da
    start = end - dt.timedelta(days=MAX_WINDOW_DAYS - 1)
    print(f"[run] {len(points)} Punkte, {start}..{end}, Fenster={window}d")

    inca = fetch_inca(points, start, end)
    print(f"[run] INCA: {len(inca)} Punkte mit Daten")

    gj = build_geojson(points, inca, window, mask=mask)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gj, f, separators=(",", ":"))
    os.replace(tmp, output_path)
    print(f"[run] geschrieben: {output_path}  ({len(gj['features'])} Zellen)")
    return gj


def probe():
    """Einen einzigen Punkt holen und die Rohstruktur dumpen, um Feldnamen
    zu verifizieren bevor man den Volllauf startet."""
    p = [(48.21, 16.37)]  # Wien
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=2)
    par = ",".join(INCA_PARAMS)
    url = (f"{INCA_BASE}?parameters={par}"
           f"&start={start.strftime('%Y-%m-%dT00:00')}"
           f"&end={end.strftime('%Y-%m-%dT23:00')}"
           f"&output_format=geojson&lat_lon=48.21,16.37")
    print("INCA URL:\n", url, "\n")
    try:
        data = _get_json(url)
        feat = data["features"][0]
        params = feat["properties"]["parameters"]
        print("INCA Parameter-Keys:", list(params.keys()))
        for k, v in params.items():
            arr = v.get("data", [])
            print(f"  {k}: unit={v.get('unit')}  n={len(arr)}  head={arr[:3]}")
        print("timestamps head:", data.get("timestamps", [])[:3])
    except Exception as e:
        print("INCA-Probe fehlgeschlagen:", e)


# --------------------------------------------------------------------------
# APPDAEMON-WRAPPER
# --------------------------------------------------------------------------
try:
    import appdaemon.plugins.hass.hassapi as hass  # noqa

    class PilzwachstumApp(hass.Hass):
        """apps.yaml:
        pilzwachstum:
          module: pilzwachstum
          class: PilzwachstumApp
          run_time: "07:30:00"
          window_days: 14
          run_on_start: true          # 1x ~30s nach Add-on-Start laufen (zum Testen)
          # build_mask: true          # EINMALIG: Wald-Maske bauen (~60-90 min!),
          #                           # danach wieder rausnehmen
          # output_path: "/homeassistant/www/pilz/pilzwachstum.geojson"  # optional fix
        """
        def initialize(self):
            t = self.args.get("run_time", "07:30:00")
            self.window = int(self.args.get("window_days", 14))
            self.outp = self.args.get("output_path")  # None -> auto-detect
            self.run_daily(self._cb, self.parse_time(t))
            self.log("Pilzwachstum-App initialisiert, laeuft taeglich " + t)

            if str(self.args.get("build_mask", False)).lower() in ("true", "1", "yes"):
                self.run_in(self._build_mask_cb, 30)
                self.log("build_mask aktiv -> Wald-Maske wird in 30s gebaut "
                         "(dauert ~60-90 min, danach build_mask wieder entfernen)")

            if str(self.args.get("run_on_start", False)).lower() in ("true", "1", "yes"):
                self.run_in(self._cb, 30)
                self.log("run_on_start aktiv -> Testlauf in 30s")

        def _build_mask_cb(self, kwargs):
            try:
                n = build_mask()
                self.log(f"Wald-Maske fertig: {n} Waldpunkte")
            except Exception as e:
                self.error(f"build_mask fehlgeschlagen: {e}")

        def _cb(self, kwargs):
            try:
                gj = run(window=self.window, output_path=self.outp)
                self.log(f"Pilzwachstum: {len(gj['features'])} Zellen geschrieben")
                self.set_state("sensor.pilzwachstum_update",
                               state=dt.datetime.now().isoformat(timespec="minutes"),
                               attributes={"cells": len(gj["features"])})
            except Exception as e:
                self.error(f"Pilzwachstum-Lauf fehlgeschlagen: {e}")
except ImportError:
    pass  # ausserhalb AppDaemon -> nur Standalone


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif "--probe-mask" in sys.argv:
        probe_mask()
    elif "--buildmask" in sys.argv:
        build_mask()
    elif "--run" in sys.argv:
        w = 14
        for a in sys.argv:
            if a.startswith("--window="):
                w = int(a.split("=")[1])
        run(window=w)
    else:
        print(__doc__)

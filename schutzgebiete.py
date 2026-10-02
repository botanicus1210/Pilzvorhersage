#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
schutzgebiete.py  —  Schutzgebiete (Nationalparks + Naturschutzgebiete)

Holt die Grenzen aus OpenStreetMap (Overpass, kein Key, KEINE Zusatzpakete)
und schreibt schutzgebiete.geojson als zuschaltbares Overlay.
Baut Polygone direkt aus 'out geom' zusammen (Relationen-Mitglieder + Wege).

Standalone:  python3 schutzgebiete.py --run
Pfad ueber PILZ_SCHUTZ (env) oder site/schutzgebiete.geojson.
"""
from __future__ import annotations
import os, sys, json, time
from urllib import request, parse

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
UA = "Pilzkarte/1.0 (Hobbyprojekt)"
TIMEOUT = 200

QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="AT"][admin_level=2]->.a;
(
  relation["boundary"="national_park"](area.a);
  relation["leisure"="nature_reserve"](area.a);
  way["leisure"="nature_reserve"](area.a);
  relation["boundary"="protected_area"]["protect_class"~"^(1|1a|1b|2)$"](area.a);
);
out geom;
"""


def resolve_output_path():
    env = os.environ.get("PILZ_SCHUTZ")
    if env:
        return env
    for base in ("/homeassistant/www/pilz", "/config/www/pilz"):
        if os.path.isdir(os.path.dirname(base)):
            return os.path.join(base, "schutzgebiete.geojson")
    return os.path.join("site", "schutzgebiete.geojson")


def _fetch():
    body = parse.urlencode({"data": QUERY}).encode()
    for ep in OVERPASS:
        for attempt in range(2):
            try:
                req = request.Request(ep, data=body, headers={"User-Agent": UA})
                with request.urlopen(req, timeout=TIMEOUT) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                print(f"[schutz] {ep} Versuch {attempt+1}: {e}", file=sys.stderr)
                time.sleep(3)
    return None


def _ring(geom):
    """geom: Liste {lat,lon} -> geschlossener Ring [[lon,lat],...] oder None."""
    r = [[round(p["lon"], 4), round(p["lat"], 4)]
         for p in geom if isinstance(p, dict) and "lat" in p and "lon" in p]
    if len(r) < 4:
        return None
    if r[0] != r[-1]:
        r.append(r[0])
    return r


def _kind(tags):
    if tags.get("boundary") == "national_park":
        return "Nationalpark"
    return "Naturschutzgebiet"


def run():
    out = resolve_output_path()
    raw = _fetch()
    if not raw:
        print("[schutz] Overpass nicht erreichbar - Abbruch", file=sys.stderr)
        return
    feats = []
    for el in raw.get("elements", []):
        t = el.get("type")
        tags = el.get("tags", {}) or {}
        if t == "way" and el.get("geometry"):
            ring = _ring(el["geometry"])
            if ring:
                feats.append({"type": "Feature",
                              "geometry": {"type": "Polygon", "coordinates": [ring]},
                              "properties": {"name": tags.get("name", "Schutzgebiet"),
                                             "kind": _kind(tags)}})
        elif t == "relation":
            name = tags.get("name", "Schutzgebiet")
            k = _kind(tags)
            for m in el.get("members", []):
                if m.get("type") == "way" and m.get("role") in ("outer", "") \
                        and m.get("geometry"):
                    ring = _ring(m["geometry"])
                    if ring:
                        feats.append({"type": "Feature",
                                      "geometry": {"type": "Polygon", "coordinates": [ring]},
                                      "properties": {"name": name, "kind": k}})
    fc = {"type": "FeatureCollection",
          "metadata": {"count": len(feats), "source": "OpenStreetMap (Overpass); ODbL",
                       "note": "Sammeln eingeschraenkt/verboten — vor Ort pruefen"},
          "features": feats}
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, out)
    print(f"[schutz] geschrieben: {out}  ({len(feats)} Flaechen)")


if __name__ == "__main__":
    if "--run" in sys.argv:
        run()
    else:
        print(__doc__)
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
schutzgebiete.py  —  Schutzgebiete (Nationalparks + Naturschutzgebiete) fuer die Karte

Holt die Grenzen aus OpenStreetMap (Overpass, kein Key) und schreibt
schutzgebiete.geojson als zuschaltbares Overlay: "hier Sammeln eingeschraenkt
oder verboten — vor Ort pruefen".

Statisch (Grenzen aendern sich kaum). Braucht osm2geojson zum Zusammenbauen der
Multipolygon-Relationen:   pip install osm2geojson

Standalone:  python3 schutzgebiete.py --run
Pfad ueber PILZ_SCHUTZ (env) oder site/schutzgebiete.geojson.
"""

from __future__ import annotations
import os, sys, json
from urllib import request, parse

OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "Pilzkarte/1.0 (Hobbyprojekt)"
TIMEOUT = 200

# Nationalparks + Naturschutzgebiete in Oesterreich
QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="AT"][admin_level=2]->.a;
(
  relation["boundary"="national_park"](area.a);
  relation["leisure"="nature_reserve"](area.a);
  way["leisure"="nature_reserve"](area.a);
  relation["boundary"="protected_area"]["protect_class"~"^(1|1a|1b|2)$"](area.a);
);
out geom;
"""


def resolve_output_path():
    env = os.environ.get("PILZ_SCHUTZ")
    if env:
        return env
    for base in ("/homeassistant/www/pilz", "/config/www/pilz"):
        if os.path.isdir(os.path.dirname(base)):
            return os.path.join(base, "schutzgebiete.geojson")
    return os.path.join("site", "schutzgebiete.geojson")


def _round_coords(obj, nd=4):
    """Koordinaten auf ~11 m runden -> kleinere Datei."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], (int, float)) and len(obj) == 2:
            return [round(obj[0], nd), round(obj[1], nd)]
        return [_round_coords(x, nd) for x in obj]
    return obj


def _kind(tags):
    if tags.get("boundary") == "national_park":
        return "Nationalpark"
    if tags.get("leisure") == "nature_reserve" or tags.get("boundary") == "protected_area":
        return "Naturschutzgebiet"
    return "Schutzgebiet"


def run():
    try:
        import osm2geojson
    except ImportError:
        print("[schutz] osm2geojson fehlt -> pip install osm2geojson", file=sys.stderr)
        return

    out = resolve_output_path()
    data = parse.urlencode({"data": QUERY}).encode()
    req = request.Request(OVERPASS, data=data, headers={"User-Agent": UA})
    try:
        with request.urlopen(req, timeout=TIMEOUT) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[schutz] Overpass-Fehler: {e}", file=sys.stderr)
        return

    gj = osm2geojson.json2geojson(raw)

    feats = []
    for f in gj.get("features", []):
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        tags = (f.get("properties") or {}).get("tags", {}) or {}
        name = tags.get("name") or tags.get("official_name") or "Schutzgebiet"
        feats.append({
            "type": "Feature",
            "geometry": {"type": geom["type"], "coordinates": _round_coords(geom["coordinates"])},
            "properties": {"name": name, "kind": _kind(tags)},
        })

    fc = {
        "type": "FeatureCollection",
        "metadata": {"count": len(feats),
                     "source": "OpenStreetMap (Overpass); ODbL",
                     "note": "Sammeln eingeschraenkt/verboten — vor Ort pruefen"},
        "features": feats,
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, out)
    print(f"[schutz] geschrieben: {out}  ({len(feats)} Gebiete)")


if __name__ == "__main__":
    if "--run" in sys.argv:
        run()
    else:
        print(__doc__)

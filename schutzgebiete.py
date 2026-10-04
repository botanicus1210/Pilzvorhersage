#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
schutzgebiete.py  —  Schutzgebiete (Nationalparks + Naturschutzgebiete)

Holt die Grenzen aus OpenStreetMap (Overpass, kein Key) und schreibt
schutzgebiete.geojson als zuschaltbares Overlay.
osm2geojson setzt die Multipolygon-Relationen korrekt zusammen; Overpass
wird mit mehreren Mirrors + Retry abgefragt (gegen 504).

  pip install osm2geojson
  python3 schutzgebiete.py --run

Pfad ueber PILZ_SCHUTZ (env) oder site/schutzgebiete.geojson.
"""
from __future__ import annotations
import os, sys, json, time
from urllib import request, parse

OVERPASS = [
    "https://overpass.kumi.systems/api/interpreter",   # meist stabiler zuerst
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
UA = "Pilzkarte/1.0 (Hobbyprojekt)"
TIMEOUT = 330

QUERY = """
[out:json][timeout:300];
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
    """Nur eine Antwort MIT Elementen und ohne Laufzeitfehler zaehlt.
    Overpass liefert bei Ueberlastung oft HTTP 200 + leere Liste + 'remark'."""
    body = parse.urlencode({"data": QUERY}).encode()
    for ep in OVERPASS:
        for attempt in range(2):
            try:
                req = request.Request(ep, data=body, headers={"User-Agent": UA})
                with request.urlopen(req, timeout=TIMEOUT) as r:
                    raw = json.loads(r.read().decode("utf-8"))
                n = len(raw.get("elements", []))
                remark = raw.get("remark", "")
                if n == 0 or "error" in remark.lower():
                    print(f"[schutz] {ep} Versuch {attempt+1}: unbrauchbar "
                          f"({n} Elemente) {remark[:120]}", file=sys.stderr)
                    time.sleep(5)
                    continue
                print(f"[schutz] {ep}: {n} Elemente erhalten")
                return raw
            except Exception as e:
                print(f"[schutz] {ep} Versuch {attempt+1}: {e}", file=sys.stderr)
                time.sleep(5)
    return None


def _round(obj, nd=4):
    if isinstance(obj, list):
        if obj and isinstance(obj[0], (int, float)) and len(obj) == 2:
            return [round(obj[0], nd), round(obj[1], nd)]
        return [_round(x, nd) for x in obj]
    return obj


def _kind(tags):
    if tags.get("boundary") == "national_park":
        return "Nationalpark"
    return "Naturschutzgebiet"


def run():
    try:
        import osm2geojson
    except ImportError:
        print("[schutz] osm2geojson fehlt -> pip install osm2geojson", file=sys.stderr)
        return
    out = resolve_output_path()
    raw = _fetch()
    if not raw:
        print("[schutz] Overpass nicht erreichbar - Abbruch", file=sys.stderr)
        return
    gj = osm2geojson.json2geojson(raw)
    feats = []
    for f in gj.get("features", []):
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        tags = (f.get("properties") or {}).get("tags", {}) or {}
        feats.append({
            "type": "Feature",
            "geometry": {"type": geom["type"], "coordinates": _round(geom["coordinates"])},
            "properties": {"name": tags.get("name") or "Schutzgebiet", "kind": _kind(tags)},
        })
    if not feats:
        types = {}
        for f in gj.get("features", []):
            ty = (f.get("geometry") or {}).get("type")
            types[ty] = types.get(ty, 0) + 1
        print(f"[schutz] 0 Flaechen erzeugt (Geometrietypen: {types}) - "
              f"vorhandene Datei bleibt unveraendert", file=sys.stderr)
        sys.exit(1)
    fc = {"type": "FeatureCollection",
          "metadata": {"count": len(feats), "source": "OpenStreetMap (Overpass); ODbL",
                       "note": "Sammeln eingeschraenkt/verboten — vor Ort pruefen"},
          "features": feats}
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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
funde.py  —  Fundmeldungen von iNaturalist fuer die Pilzkarte

Holt die Pilz-Funde der letzten Wochen fuer Oesterreich (nur "research grade",
nur offene Koordinaten) und schreibt sie als funde.geojson, nach Gilde getaggt.
Ein Fund einer Gilde belegt, dass die Bedingungen fuer die ganze Gilde passen
(Indikator-Prinzip: der gut gemeldete Fliegenpilz zeigt Steinpilz-Bedingungen).

Kein API-Key noetig. Taxon-Namen werden zur Laufzeit in iNat-IDs aufgeloest,
damit nichts geraten werden muss.

Standalone:  python3 funde.py --run        -> schreibt funde.geojson
Test:        python3 funde.py --probe      -> Test-URLs + eine Aufloesung
Pfad ueber PILZ_FUNDE (env) oder neben pilzwachstum.geojson.
"""

from __future__ import annotations
import os, sys, json, time, datetime as dt
from urllib import request, parse, error

# Oesterreich-Bounding-Box (wie im Hauptskript)
AT = dict(swlat=46.30, swlng=9.40, nelat=49.05, nelng=17.20)
DAYS_BACK = 21           # wie weit zurueck Funde gelten
QUALITY = "research"     # nur community-bestaetigte Bestimmungen
PER_PAGE = 200
MAX_PAGES = 8
PAUSE = 1.0
TIMEOUT = 45
UA = "Pilzkarte/1.0 (Home-Assistant Hobbyprojekt; kontakt via GitHub)"

INAT = "https://api.inaturalist.org/v1"

# Gilden: Gilde -> Liste iNat-Taxon-Namen (Gattung ODER Art).
# iNat matcht bei einer Taxon-Abfrage automatisch alle Unterarten -> Gattungen
# geben maximale Meldungsdichte.
GUILDS = {
    "mykorrhiza_wald": [   # Herbststeinpilz, Sommersteinpilz, Marone
        "Boletus", "Imleria badia", "Amanita muscaria", "Amanita rubescens",
        "Laccaria", "Russula", "Lactarius", "Cortinarius", "Leccinum",
        "Xerocomellus", "Neoboletus", "Suillellus", "Paxillus involutus",
        "Scleroderma", "Tylopilus felleus",
    ],
    "feucht_mykorrhiza": [ # Eierschwammerl, Herbsttrompete
        "Cantharellus", "Craterellus", "Hydnum", "Laccaria amethystina",
    ],
    "fruehling_au": [      # Morchel
        "Morchella", "Verpa", "Disciotis venosa",
    ],
    "wiese_saprob": [      # Parasol
        "Macrolepiota", "Chlorophyllum", "Coprinus comatus",
        "Marasmius oreades", "Agaricus", "Lycoperdon", "Bovista", "Calvatia",
    ],
}


def resolve_output_path():
    env = os.environ.get("PILZ_FUNDE")
    if env:
        return env
    for base in ("/homeassistant/www/pilz", "/config/www/pilz"):
        if os.path.isdir(os.path.dirname(base)):
            return os.path.join(base, "funde.geojson")
    return os.path.join("site", "funde.geojson")


def _get(url):
    req = request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _resolve_one(name):
    """Exakter Namensabgleich statt 'erstes Ergebnis' (sonst matcht z.B.
    Boletus faelschlich auf Trametes versicolor). Bevorzugt aktive Taxa und
    den passenden Rang (ein Wort -> Gattung, zwei Woerter -> Art)."""
    data = _get(f"{INAT}/taxa?" + parse.urlencode(
        {"q": name, "per_page": 20, "is_active": "true"}))
    res = data.get("results", [])
    if not res:
        return None
    ql = name.strip().lower()
    want_rank = "species" if " " in name.strip() else "genus"
    # 1) exakter wissenschaftlicher Name + passender Rang
    for r in res:
        if r.get("name", "").lower() == ql and r.get("rank") == want_rank:
            return r
    # 2) exakter Name, egal welcher Rang
    for r in res:
        if r.get("name", "").lower() == ql:
            return r
    # 3) kein exakter Treffer -> nichts (lieber weglassen als falsch)
    return None


def resolve_taxa():
    """Namen -> {taxon_id: guild}. Ein Name kann Gattung (viele Arten) sein."""
    id2guild = {}
    name2id = {}
    for guild, names in GUILDS.items():
        for name in names:
            if name in name2id:
                id2guild.setdefault(name2id[name], guild)
                continue
            try:
                r = _resolve_one(name)
                if not r:
                    print(f"[funde] kein exakter Treffer fuer '{name}' - uebersprungen",
                          file=sys.stderr)
                    continue
                tid = r["id"]
                name2id[name] = tid
                id2guild[tid] = guild
                print(f"[funde] {name:22s} -> id {tid} ({r.get('name')}, {r.get('rank')})")
            except Exception as e:
                print(f"[funde] Aufloesung '{name}' fehlgeschlagen: {e}", file=sys.stderr)
            time.sleep(0.3)
    return id2guild


def _guild_for(obs, id2guild):
    """Gilde eines Fundes ueber die Taxon-Abstammung bestimmen."""
    taxon = obs.get("taxon") or {}
    ids = set(taxon.get("ancestor_ids") or [])
    ids.add(taxon.get("id"))
    for tid, guild in id2guild.items():
        if tid in ids:
            return guild
    return None


def fetch(id2guild):
    ids = ",".join(str(t) for t in id2guild)
    d1 = (dt.date.today() - dt.timedelta(days=DAYS_BACK)).isoformat()
    feats = []
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        q = {
            "taxon_id": ids, "d1": d1, "quality_grade": QUALITY,
            "swlat": AT["swlat"], "swlng": AT["swlng"],
            "nelat": AT["nelat"], "nelng": AT["nelng"],
            "geo": "true", "geoprivacy": "open",      # nur offene Koordinaten
            "per_page": PER_PAGE, "page": page,
            "order_by": "observed_on", "order": "desc",
        }
        try:
            data = _get(f"{INAT}/observations?" + parse.urlencode(q))
        except Exception as e:
            print(f"[funde] Seite {page} Fehler: {e}", file=sys.stderr)
            break
        results = data.get("results", [])
        if not results:
            break
        for obs in results:
            if obs.get("obscured") or obs.get("coordinates_obscured"):
                continue
            gj = obs.get("geojson") or {}
            coords = gj.get("coordinates")
            if not coords or len(coords) != 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            oid = obs.get("id")
            if oid in seen:
                continue
            seen.add(oid)
            guild = _guild_for(obs, id2guild)
            if not guild:
                continue
            on = obs.get("observed_on")
            if not on:
                continue
            try:
                days_ago = (dt.date.today() - dt.date.fromisoformat(on)).days
            except Exception:
                days_ago = None
            taxon = obs.get("taxon") or {}
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]},
                "properties": {
                    "guild": guild,
                    "name": taxon.get("preferred_common_name") or taxon.get("name") or "?",
                    "sci": taxon.get("name"),
                    "date": on,
                    "days_ago": days_ago,
                },
            })
        print(f"[funde] Seite {page}: {len(results)} geladen, {len(feats)} gesamt")
        if len(results) < PER_PAGE:
            break
        time.sleep(PAUSE)
    return feats


def run():
    out = resolve_output_path()
    id2guild = resolve_taxa()
    if not id2guild:
        print("[funde] keine Taxa aufgeloest - Abbruch", file=sys.stderr)
        return
    feats = fetch(id2guild)
    gj = {
        "type": "FeatureCollection",
        "metadata": {
            "generated": dt.datetime.now().isoformat(timespec="minutes"),
            "days_back": DAYS_BACK,
            "count": len(feats),
            "source": "iNaturalist (research grade, offene Koordinaten)",
        },
        "features": feats,
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gj, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, out)
    print(f"[funde] geschrieben: {out}  ({len(feats)} Funde)")


def probe():
    d1 = (dt.date.today() - dt.timedelta(days=DAYS_BACK)).isoformat()
    print("Taxa-Aufloesung Test (Boletus):")
    print(f"  {INAT}/taxa?q=Boletus&per_page=1\n")
    print("Beobachtungs-Query (im Browser oeffnen, Fliegenpilz=Amanita muscaria id 48701):")
    q = {"taxon_id": "48701", "d1": d1, "quality_grade": "research",
         "swlat": AT["swlat"], "swlng": AT["swlng"], "nelat": AT["nelat"], "nelng": AT["nelng"],
         "geoprivacy": "open", "per_page": 5}
    print(f"  {INAT}/observations?" + parse.urlencode(q))
    try:
        n = _get(f"{INAT}/taxa?q=Boletus&per_page=1")["results"][0]
        print(f"\n  -> Boletus = id {n['id']} ({n['name']})  ✓ Verbindung ok")
    except Exception as e:
        print("  Fehler:", e)


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif "--run" in sys.argv:
        run()
    else:
        print(__doc__)

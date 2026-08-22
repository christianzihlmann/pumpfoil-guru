#!/usr/bin/env python3
"""
Holt drei Dinge und schreibt sie nach data/level.json:

  1. die Seepegel-Prognose von Groupe E (Min/Max-Band, keine Messung)
  2. BAFU 2215, Saane – Laupen: Abfluss UNTERHALB der Staumauer
  3. BAFU 2119, Sarine – Fribourg: Pegel und Abfluss OBERHALB des Sees

Alles zusammen wandert stuendlich als eine Zeile nach data/hydro.csv:

    timestamp,gE_min,gE_max,h2119,q2119,h2215,q2215

Warum in EINER Zeile: nur so laesst sich spaeter die Korrelation zwischen dem
gemessenen Pegel bei Fribourg (h2119) und dem Prognoseband von Groupe E rechnen,
ohne Dateien zusammenfuegen zu muessen. Findet sich ein stabiler Offset, waere
h2119 eine echte, oeffentliche, viertelstuendlich aktualisierte Messung — und
die eigene Drucksonde teilweise erspart. Ob der Offset stabil ist, weiss man
erst mit Wochen an Daten. Deshalb ab jetzt sammeln.

q2119 (Zufluss) gegen q2215 (Abfluss) zeigt ausserdem direkt, ob der See gerade
steigt oder faellt — mehr raus als rein heisst sinkend.

Die Datei ist append-only, waechst ~500 kB im Jahr und wird nie gekuerzt.

Faellt BAFU aus, bleibt der letzte bekannte Stand erhalten und der Prognoseteil
laeuft normal weiter. Nur ein Fehler bei Groupe E ist fatal.

Laeuft stuendlich als GitHub Action. Keine Abhaengigkeiten ausser requests.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

URL = "https://www.groupe-e.ch/de/ueber-groupe-e/wasserstand-seen/schiffenen"
LAKE_OUT = "2215"          # Saane – Laupen, unterhalb der Staumauer
LAKE_IN = "2119"           # Sarine – Fribourg, oberhalb des Sees
HYDRO = ("https://api.existenz.ch/apiv1/hydro/latest"
         f"?locations={LAKE_IN},{LAKE_OUT}&parameters=height,flow"
         "&app=pumpfoil.guru&version=1.0")

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
OUT = os.path.join(DATA, "level.json")
CSV = os.path.join(DATA, "hydro.csv")
HEAD = "timestamp,gE_min,gE_max,h2119,q2119,h2215,q2215\n"

UA = {"User-Agent": "pumpfoil-guru/1.0 (hobby project; contact via GitHub)"}

# Ab so vielen Messwerten gilt die Abflussstatistik als aussagekraeftig.
# Die Seite zeigt das Wort erst dann — vorher steht nur die Zahl da.
MIN_SAMPLES = 300

# Manuelle Schalter. Werden in data/level.json direkt auf GitHub umgelegt und
# hier nie ueberschrieben — nur angelegt, falls sie fehlen.
MANUAL_DEFAULTS = {"rowing_competition": False, "dock4_installed": False}

# Tabellenzeile:  22.08.2026 | 531.29 m ü. M. | 531.55 m ü. M.
ROW = re.compile(
    r"(\d{2}\.\d{2}\.\d{4})\s*\|?\s*"
    r"(\d{3}[.,]\d{1,2})\s*m[^|]*\|?\s*"
    r"(\d{3}[.,]\d{1,2})\s*m",
    re.I,
)


def strip_tags(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"</t[dh]>", " | ", html, flags=re.I)
    html = re.sub(r"</tr>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&uuml;", "ü")
    return re.sub(r"[ \t]+", " ", html)


def fetch_forecast() -> list:
    r = requests.get(URL, timeout=30, headers=UA)
    r.raise_for_status()
    forecast = []
    for date, lo, hi in ROW.findall(strip_tags(r.text)):
        lo, hi = float(lo.replace(",", ".")), float(hi.replace(",", "."))
        forecast.append({"date": date, "min": min(lo, hi), "max": max(lo, hi)})
    return forecast


def fetch_hydro() -> dict:
    """{'2119': {'height': .., 'flow': .., 'timestamp': ..}, '2215': {...}}"""
    r = requests.get(HYDRO, timeout=30, headers=UA)
    r.raise_for_status()
    out = {}
    for row in r.json().get("payload", []):
        loc, par = str(row.get("loc")), row.get("par")
        if par in ("height", "flow"):
            out.setdefault(loc, {})[par] = float(row["val"])
            out[loc]["timestamp"] = int(row["timestamp"])
    return out


def read_archive() -> list:
    """Alle Zeilen als dicts. Leere Felder bleiben None."""
    if not os.path.exists(CSV):
        return []
    cols = HEAD.strip().split(",")
    rows = []
    with open(CSV) as f:
        next(f, None)
        for line in f:
            parts = line.rstrip("\n").split(",")
            if len(parts) != len(cols):
                continue                       # kaputte Zeile ueberspringen
            row = {}
            for c, v in zip(cols, parts):
                if v == "":
                    row[c] = None
                else:
                    try:
                        row[c] = int(v) if c == "timestamp" else float(v)
                    except ValueError:
                        row[c] = None
            if row["timestamp"] is not None:
                rows.append(row)
    return rows


def append_archive(ts: int, vals: dict, rows: list) -> list:
    """Haengt an, falls der Zeitstempel neu ist. BAFU aktualisiert nicht
    zwingend stuendlich, und GitHubs Cron ist unpuenktlich — ohne diese
    Pruefung stuende derselbe Messwert mehrfach im Archiv."""
    if any(r["timestamp"] == ts for r in rows[-48:]):
        return rows
    cols = HEAD.strip().split(",")
    os.makedirs(DATA, exist_ok=True)
    new = not os.path.exists(CSV)
    with open(CSV, "a") as f:
        if new:
            f.write(HEAD)
        f.write(",".join("" if vals.get(c) is None else str(vals[c])
                         for c in cols) + "\n")
    return rows + [{c: vals.get(c) for c in cols}]


def flow_stats(rows: list):
    """Perzentile des Abflusses bei Laupen. None, solange zu duenn."""
    vals = sorted(r["q2215"] for r in rows if r.get("q2215") is not None)
    if len(vals) < 2:
        return None

    def p(q):
        i = (len(vals) - 1) * q
        lo = int(i)
        hi = min(lo + 1, len(vals) - 1)
        return round(vals[lo] + (vals[hi] - vals[lo]) * (i - lo), 2)

    ts = [r["timestamp"] for r in rows]
    return {
        "n": len(vals),
        "days": round((max(ts) - min(ts)) / 86400, 1),
        "enough": len(vals) >= MIN_SAMPLES,
        "min": vals[0], "p10": p(.10), "p25": p(.25), "median": p(.50),
        "p75": p(.75), "p90": p(.90), "max": vals[-1],
    }


def main() -> int:
    forecast = fetch_forecast()
    if not forecast:
        print("Keine Prognosezeilen gefunden — Seitenstruktur geaendert?", file=sys.stderr)
        return 1

    try:
        with open(OUT) as f:
            old = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        old = {}

    payload = {
        "source": "Groupe E — Seepegel-Prognose Schiffenensee",
        "source_url": URL,
        "kind": "forecast_band",
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "forecast": forecast,
    }

    # Manuelle Schalter und Sensormessung nie ueberschreiben
    payload["manual"] = {**MANUAL_DEFAULTS, **(old.get("manual") or {})}
    if "measured" in old:
        payload["measured"] = old["measured"]

    # BAFU ist Beiwerk: faellt es aus, bleibt der letzte Stand stehen
    try:
        h = fetch_hydro()
        out_s, in_s = h.get(LAKE_OUT, {}), h.get(LAKE_IN, {})
        if "flow" not in out_s and "height" not in in_s:
            raise ValueError("weder 2215 noch 2119 lieferten Werte")

        ts = out_s.get("timestamp") or in_s.get("timestamp")
        today = forecast[0]
        rows = append_archive(ts, {
            "timestamp": ts,
            "gE_min": today["min"], "gE_max": today["max"],
            "h2119": in_s.get("height"), "q2119": in_s.get("flow"),
            "h2215": out_s.get("height"), "q2215": out_s.get("flow"),
        }, read_archive())

        payload["hydro"] = {
            "source": "BAFU / FOEN Hydrologie via api.existenz.ch",
            "station": LAKE_OUT,
            "name": "Saane – Laupen",
            "unit": "m3/s",
            "flow": out_s.get("flow"),
            "time": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"),
            "stats": flow_stats(rows),
        }
        # Oberhalb des Sees. Noch nichts fuer die Anzeige — wird gesammelt, um
        # den Zusammenhang zum Groupe-E-Band zu pruefen.
        payload["sarine"] = {
            "station": LAKE_IN,
            "name": "Sarine – Fribourg",
            "height": in_s.get("height"),
            "flow": in_s.get("flow"),
            "note": "oberhalb des Sees; Pegel in m ü. M., noch nicht kalibriert",
        }

        st = payload["hydro"]["stats"]
        bal = (in_s.get("flow") or 0) - (out_s.get("flow") or 0)
        print(f"Laupen {out_s.get('flow')} m³/s · Fribourg {in_s.get('height')} m ü. M. "
              f"/ {in_s.get('flow')} m³/s · Bilanz {bal:+.2f} m³/s "
              f"({'steigend' if bal > 0 else 'sinkend'})")
        if st:
            print(f"Archiv {st['n']} Werte über {st['days']} Tage"
                  + ("" if st["enough"] else f", Wortangabe ab {MIN_SAMPLES}"))
    except Exception as e:                                  # noqa: BLE001
        print(f"BAFU nicht geholt ({e}) — alter Stand bleibt", file=sys.stderr)
        for k in ("hydro", "sarine"):
            if k in old:
                payload[k] = old[k]

    os.makedirs(DATA, exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"{len(forecast)} Tage geschrieben, heute "
          f"{forecast[0]['min']:.2f}–{forecast[0]['max']:.2f} m ü. M.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

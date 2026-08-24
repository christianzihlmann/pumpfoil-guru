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
WARM = "2467"              # Saane – Guemmenen, einzige Station der Kette mit Temperatur
HYDRO = ("https://api.existenz.ch/apiv1/hydro/latest"
         f"?locations={LAKE_IN},{LAKE_OUT},{WARM}&parameters=height,flow,temperature"
         "&app=pumpfoil.guru&version=1.0")

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
OUT = os.path.join(DATA, "level.json")
CSV = os.path.join(DATA, "hydro.csv")
HEAD = ("timestamp,gE_min,gE_max,h2119,q2119,h2215,q2215,h2467,t2467,uv,aqi,"
        "wind,gust,wdir,smn_ff,smn_fx,smn_dd,smn_tt\n")

UA = {"User-Agent": "pumpfoil-guru/1.0 (hobby project; contact via GitHub)"}

# Pensier. Open-Meteo braucht keinen Schluessel.
LAT, LON = 46.8261, 7.1256
METEO = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
         "&current=uv_index,wind_speed_10m,wind_gusts_10m,wind_direction_10m"
         "&timezone=Europe/Zurich&forecast_days=1")

# MeteoSchweiz-Station Grangeneuve, 8.7 km vom Steg. Anders als Open-Meteo ist
# das eine ECHTE Messung, alle 10 Minuten. Wird mitgeschrieben, damit sich in
# ein paar Wochen sagen laesst, wie weit das Modell danebenliegt.
# Unwetterwarnungen. MeteoAlarm ist die europaeische Warnzentrale von EUMETNET;
# fuer die Schweiz sendet MeteoSchweiz selbst (meteoalarm.cap@meteoswiss.ch) im
# CAP-Standard. Offiziell und dokumentiert, im Gegensatz zur App-Schnittstelle.
# Die Antwort ist ~8 MB, davon 82 % Polygone in fuenf Sprachkopien — das laedt
# nur dieser Job, nie der Browser. Uebrig bleiben drei Zeilen in level.json.
WARN = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-switzerland"
DOCK_LAT, DOCK_LON = 46.8473, 7.1414      # Dock 2, mitten in der Steggruppe

# Die Antwort ist 8 MB, der Server komprimiert nicht und schickt weder ETag noch
# Last-Modified — bedingte Abfragen gehen also nicht. Bei halbstuendlichem Takt
# waeren das 11.5 GB pro Monat auf MeteoAlarms Kosten, fuer einen einzigen Steg.
# Deshalb hoechstens einmal pro Stunde. Gewitterwarnungen gelten ohnehin ueber
# Stunden, 30 Minuten Frische bringen nichts.
WARN_MAX_AGE_MIN = 50

SMN_STATION = "GRA"
SMN = ("https://api.existenz.ch/apiv1/smn/latest"
       f"?locations={SMN_STATION}&parameters=ff,fx,dd,tt"
       "&app=pumpfoil.guru&version=1.0")
AIR = (f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={LAT}"
       f"&longitude={LON}&current=european_aqi&timezone=Europe/Zurich")

# Ab so vielen Messwerten gilt die Abflussstatistik als aussagekraeftig.
# Die Seite zeigt das Wort erst dann — vorher steht nur die Zahl da.
MIN_SAMPLES = 300

# Ist das Tagesband schmaler als das, laesst sich eine Messung am Steg fast
# eindeutig einem Pegel zuordnen — der ideale Kalibriertag. Der Job meldet ihn
# dann als GitHub-Issue. Bisher gesehen: 24 und 44 cm. Feuert das nie, hier
# hochsetzen; die Verteilung steht in hydro.csv (gE_max minus gE_min).
NARROW_BAND_CM = 10

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
        if par in ("height", "flow", "temperature"):
            out.setdefault(loc, {})[par] = float(row["val"])
            out[loc]["timestamp"] = int(row["timestamp"])
    return out


def fetch_current(url: str, *keys):
    """Momentanwerte aus einer Open-Meteo-Adresse. Leeres dict statt Ausnahme —
    Wetter ist Beiwerk und darf den Lauf nie kippen."""
    try:
        r = requests.get(url, timeout=30, headers=UA)
        r.raise_for_status()
        cur = r.json().get("current") or {}
        return {k: (None if cur.get(k) is None else float(cur[k])) for k in keys}
    except Exception as e:                                  # noqa: BLE001
        print(f"Open-Meteo nicht geholt ({e})", file=sys.stderr)
        return {k: None for k in keys}


def _in_polygon(lat, lon, pts) -> bool:
    """Strahlenverfahren. pts sind (lat, lon)-Paare."""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        yi, xi = pts[i]
        yj, xj = pts[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def fetch_thunderstorm():
    """Aktive oder bevorstehende GEWITTERwarnung fuer den Steg, sonst None.

    Nur Gewitter — Hitze, Regen, Waldbrand werden bewusst verworfen. Gefiltert
    wird ueber den deutschen Ereignisnamen, nicht ueber die Zahlencodes: die
    Namen ("Heftiges Gewitter", "Sehr heftiges Gewitter", "Verbreitet heftige
    Gewitter moeglich") sind sprechend und ueberleben eine Code-Aenderung.
    """
    try:
        r = requests.get(WARN, timeout=90, headers=UA)
        r.raise_for_status()
        now = datetime.now(timezone.utc)
        best = None
        for w in r.json().get("warnings", []):
            de = next((i for i in w.get("alert", {}).get("info", [])
                       if i.get("language") == "de"), None)
            if not de or "gewitter" not in (de.get("event") or "").lower():
                continue
            expires = de.get("expires")
            if expires and datetime.fromisoformat(expires) < now:
                continue                       # abgelaufen
            for area in de.get("area", []):
                for poly in area.get("polygon", []):
                    pts = [tuple(map(float, q.split(","))) for q in poly.split()]
                    if not _in_polygon(DOCK_LAT, DOCK_LON, pts):
                        continue
                    onset = de.get("onset")
                    cand = {
                        "event": de.get("event"),
                        "severity": de.get("severity"),
                        "area": area.get("areaDesc"),
                        "onset": onset,
                        "expires": expires,
                        "source": "MeteoSchweiz via MeteoAlarm (EUMETNET)",
                    }
                    # Die schwerste gewinnt, bei Gleichstand die frueheste
                    rank = {"Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}
                    if best is None or rank.get(cand["severity"], 0) > rank.get(best["severity"], 0):
                        best = cand
                    break
        return best
    except Exception as e:                                  # noqa: BLE001
        print(f"Warnungen nicht geholt ({e})", file=sys.stderr)
        return None


def fetch_smn():
    """Echte Messwerte der MeteoSchweiz-Station. ff/fx in km/h, dd in Grad."""
    try:
        r = requests.get(SMN, timeout=30, headers=UA)
        r.raise_for_status()
        out = {}
        for row in r.json().get("payload", []):
            out[row["par"]] = float(row["val"])
        return out
    except Exception as e:                                  # noqa: BLE001
        print(f"SMN nicht geholt ({e})", file=sys.stderr)
        return {}


def read_archive() -> list:
    """Alle Zeilen als dicts, gelesen nach der Kopfzeile der DATEI — nicht nach
    HEAD. So bleiben aeltere Archive lesbar, wenn Spalten dazukommen."""
    if not os.path.exists(CSV):
        return []
    with open(CSV) as f:
        first = f.readline().strip()
        cols = first.split(",") if first else HEAD.strip().split(",")
        rows = []
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
            if row.get("timestamp") is not None:
                rows.append(row)
    return rows


def migrate(rows: list) -> None:
    """Schreibt die Datei mit der aktuellen Kopfzeile neu, falls Spalten
    dazugekommen sind. Fehlende alte Werte bleiben leer."""
    if not os.path.exists(CSV):
        return
    with open(CSV) as f:
        if f.readline().strip() == HEAD.strip():
            return
    cols = HEAD.strip().split(",")
    with open(CSV, "w") as f:
        f.write(HEAD)
        for r in rows:
            f.write(",".join("" if r.get(c) is None else str(r[c]) for c in cols) + "\n")
    print(f"hydro.csv auf neues Spaltenschema gebracht ({len(rows)} Zeilen)")


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

        warm_s = h.get(WARM, {})
        om = fetch_current(METEO, "uv_index", "wind_speed_10m",
                           "wind_gusts_10m", "wind_direction_10m")
        uv = om.get("uv_index")
        aqi = fetch_current(AIR, "european_aqi").get("european_aqi")
        smn = fetch_smn()

        # Warnung nur holen, wenn der letzte Stand alt genug ist. Das ist
        # robuster als am Zeitplan zu haengen — GitHubs Cron ist unpuenktlich.
        now = datetime.now(timezone.utc)
        last = old.get("warning_checked")
        stale = True
        if last:
            try:
                stale = (now - datetime.fromisoformat(last)).total_seconds() > WARN_MAX_AGE_MIN * 60
            except ValueError:
                stale = True

        if stale:
            storm = fetch_thunderstorm()
            payload["warning_checked"] = now.isoformat(timespec="seconds")
        else:
            storm = old.get("warning")          # letzten Stand weiterreichen
            payload["warning_checked"] = last
            # Der uebernommene Stand kann inzwischen abgelaufen sein
            if storm and storm.get("expires"):
                try:
                    if datetime.fromisoformat(storm["expires"]) < now:
                        storm = None
                except ValueError:
                    storm = None
        ts = out_s.get("timestamp") or in_s.get("timestamp")
        today = forecast[0]
        archive = read_archive()
        migrate(archive)
        rows = append_archive(ts, {
            "timestamp": ts,
            "gE_min": today["min"], "gE_max": today["max"],
            "h2119": in_s.get("height"), "q2119": in_s.get("flow"),
            "h2215": out_s.get("height"), "q2215": out_s.get("flow"),
            "h2467": warm_s.get("height"), "t2467": warm_s.get("temperature"),
            "uv": uv, "aqi": aqi,
            "wind": om.get("wind_speed_10m"), "gust": om.get("wind_gusts_10m"),
            "wdir": om.get("wind_direction_10m"),
            "smn_ff": smn.get("ff"), "smn_fx": smn.get("fx"),
            "smn_dd": smn.get("dd"), "smn_tt": smn.get("tt"),
        }, archive)

        payload["hydro"] = {
            "source": "BAFU / FOEN Hydrologie via api.existenz.ch",
            "station": LAKE_OUT,
            "name": "Saane – Laupen",
            "unit": "m3/s",
            "flow": out_s.get("flow"),
            "time": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"),
            "stats": flow_stats(rows),
        }
        # Saane bei Guemmenen, unterhalb von Laupen. Einzige Station der Kette
        # mit Wassertemperatur. Der Wert ist NICHT die Seetemperatur — es ist
        # Tiefenwasser aus der Staumauer plus Erwaermung auf dem Weg. Wird
        # gesammelt, um ihn spaeter gegen eine echte Oberflaechenmessung zu
        # halten und den Zusammenhang zu bestimmen.
        # Nur setzen, wenn es wirklich eine Gewitterwarnung gibt. Fehlt der
        # Block, zeigt die Seite gar nichts an — kein "keine Warnung".
        if storm:
            payload["warning"] = storm
            print(f"GEWITTERWARNUNG: {storm['event']} ({storm['severity']}) "
                  f"bis {storm['expires']}")

        payload["air"] = {
            "source": "Open-Meteo (Modell) + MeteoSchweiz " + SMN_STATION + " (Messung)",
            "uv_index": uv,
            "european_aqi": aqi,
            "wind_model": om.get("wind_speed_10m"),
            "gust_model": om.get("wind_gusts_10m"),
            "wind_dir_model": om.get("wind_direction_10m"),
            "wind_measured": smn.get("ff"),
            "gust_measured": smn.get("fx"),
            "wind_dir_measured": smn.get("dd"),
            "temp_measured": smn.get("tt"),
            "measured_station": SMN_STATION + " Fribourg/Grangeneuve, 8.7 km",
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        payload["water"] = {
            "station": WARM,
            "name": "Saane – Gümmenen",
            "temperature": warm_s.get("temperature"),
            "height": warm_s.get("height"),
            "note": "Flusstemperatur unterhalb der Staumauer, nicht die Seetemperatur",
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
        print(f"Wind Modell {om.get('wind_speed_10m')} / gemessen {smn.get('ff')} km/h · "
              f"UV {uv} · Luft {aqi} · Gümmenen {warm_s.get('temperature')} °C · "
              f"Laupen {out_s.get('flow')} m³/s · Fribourg {in_s.get('height')} m ü. M. "
              f"/ {in_s.get('flow')} m³/s · Bilanz {bal:+.2f} m³/s "
              f"({'steigend' if bal > 0 else 'sinkend'})")
        if st:
            print(f"Archiv {st['n']} Werte über {st['days']} Tage"
                  + ("" if st["enough"] else f", Wortangabe ab {MIN_SAMPLES}"))
    except Exception as e:                                  # noqa: BLE001
        print(f"BAFU nicht geholt ({e}) — alter Stand bleibt", file=sys.stderr)
        for k in ("hydro", "sarine", "water", "air"):
            if k in old:
                payload[k] = old[k]

    os.makedirs(DATA, exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    today = forecast[0]
    band_cm = round((today["max"] - today["min"]) * 100)
    print(f"{len(forecast)} Tage geschrieben, heute "
          f"{today['min']:.2f}–{today['max']:.2f} m ü. M. (Spanne {band_cm} cm)")

    # An GitHub Actions weiterreichen; ausserhalb davon passiert nichts.
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"band_cm={band_cm}\n")
            f.write(f"narrow={'true' if band_cm <= NARROW_BAND_CM else 'false'}\n")
            f.write(f"lo={today['min']:.2f}\n")
            f.write(f"hi={today['max']:.2f}\n")
    if band_cm <= NARROW_BAND_CM:
        print(f"KALIBRIERFENSTER: nur {band_cm} cm Spanne heute")

    return 0


if __name__ == "__main__":
    sys.exit(main())

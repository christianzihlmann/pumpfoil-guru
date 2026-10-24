#!/usr/bin/env python3
"""Tages-Assessment fuer heute, morgen und uebermorgen.

Der Code rechnet, das Modell formuliert. Dieses Skript ordnet Wind, Wetter
und Temperatur der Open-Meteo-Prognose ein, bestimmt die Overrides und legt
pro Tag eine Liste fertiger Aussagen an ("points"). Das Sprachmodell macht
daraus nur noch Titel und Zusatz, auf Englisch. Rechnen darf es nichts:
jede Zahl und jede Uhrzeit im Text muss aus diesen Aussagen stammen.

Laeuft im Workflow nach fetch_level.py, jede halbe Stunde:
  - Overrides (Gewitterwarnung, UV, Luftqualitaet) bei jedem Lauf neu
  - Text nur in drei Fenstern: ab 05:30, ab 12:00, ab Sonnenuntergang + 30 min
  - und pro Tag nur, wenn sich seine Aussagen seither geaendert haben

Schreibt data/assessment.json (aktueller Stand, liest die Testseite) und
haengt jeden neuen Text und jeden Override-Wechsel an
data/assessment_log.jsonl an. Das Log ist append-only wie hydro.csv: nie
kuerzen, nie squashen.

Ohne ANTHROPIC_API_KEY oder wenn die API versagt, steht der feste Satz aus
der Regeltabelle da. Ein Fehler hier darf den Datenlauf nie aufhalten; im
Workflow laeuft der Schritt deshalb mit continue-on-error.

Testen, ohne etwas zu schreiben oder das Modell zu fragen:
    python scripts/assess.py --dry
    python scripts/assess.py --dry --now 2026-10-25T05:40
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from time import sleep
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Zurich")
LAT, LON = 46.8261, 7.1256            # Pensier, wie in fetch_level.py
UA = {"User-Agent": "pumpfoil-guru/1.0 (hobby project; contact via GitHub)"}

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
PROMPT = os.path.join(HERE, "assessment_prompt.md")

# Zeiten als Unix-Sekunden, NICHT als Ortszeit-Text: Open-Meteo rechnet alle
# Zeiten einer Abfrage mit dem UTC-Versatz des Abfragezeitpunkts. Ueber eine
# Zeitumstellung hinweg liegt die Ortszeit dann eine Stunde daneben — am
# 24.10.2026 meldete es fuer Montag nach der Umstellung den Sonnenaufgang
# um 08:04 statt 07:04. Umgerechnet wird hier mit zoneinfo.
FORECAST = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
            "&hourly=temperature_2m,weather_code,visibility,precipitation,"
            "wind_speed_10m,wind_direction_10m,wind_gusts_10m,uv_index"
            "&daily=sunrise,sunset&timezone=Europe/Zurich&timeformat=unixtime"
            "&forecast_days=4")
# Luftqualitaet gibt es entgegen assessment.md auch im Voraus (geprueft
# 24.10.2026: drei Tage stuendlich). Der Override greift also auch morgen.
AIRQ = (f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={LAT}"
        f"&longitude={LON}&hourly=european_aqi&timezone=Europe/Zurich"
        "&timeformat=unixtime&forecast_days=4")

# Abrufe bei Open-Meteo: bis zu so viele Versuche, wie HYDRO_TRIES in
# fetch_level.py. Am 24.10.2026 kam die Luftqualitaet im ersten Lauf nicht
# an, waehrend fetch_level.py acht Sekunden vorher denselben Dienst erreichte.
FETCH_TRIES = 3
FETCH_PAUSE_S = 5

MODEL = "claude-opus-5-5"
EFFORT = "medium"

# --- Schwellen. Alle Windwerte sind Mittelwind des Modells in km/h. ---------
#
# Modell statt Messung: am 02.10.2026 meldete Grangeneuve fast Windstille
# (1-3 km/h), das Modell fuer Pensier 12 km/h Bise — und am Steg war Chop.
# Fuer morgen gibt es ohnehin nur das Modell.
#
# Grundlage sind die Fahrtenbuch-Eintraege des Besitzers vom 27.09. bis
# 24.10.2026, gehalten gegen die archivierten Modellwerte in hydro.csv:
#   07.10.  2-6 km/h           "dead flat, perfect"
#   15.10.  2-3 km/h           "perfect flat"
#   05.10.  5-6 km/h aus NNO   "a bit of chop ... still sucks"
#   14.10.  9-11 km/h aus NO   "chop, onshore wind a bit"
#   02.10.  12 km/h aus NO     "a bit of sideways chop, not huge"
#   10.10.  16-18 km/h aus SW  "bit offshore wind but not much"
#   24.10.  25 km/h aus SW     "strong, doable, you feel it pumping back,
#                               winging would have been ok"
AXIS = 65              # Seeachse in Grad (Besitzer: 60-70°)
ONSHORE_MAX = 55       # bis so weit neben der Achse: Bise, volle Seelaenge Anlauf
OFFSHORE_MIN = 125     # ab so weit: Suedwest, weht vom Ufer weg
CALM = 5               # darunter spiegelglatt, egal woher
CHOPPY_ON = 15         # Bise ab hier mehr als "a bit of chop"
CHOP_CROSS = 10        # Querwind hat wenig Anlauf, braucht mehr fuer Chop
CHOPPY_CROSS = 20
OFFSHORE_BIT = 12      # Suedwest ab hier spuerbar, am Steg aber glatt
OFFSHORE_STRONG = 22   # ab hier "you feel it pumping back"
WINGABLE = 25          # ab hier geht Wingfoilen
# Obergrenze fuers Wingen. NICHT vom Besitzer beurteilt, sondern geschaetzt:
# am 25.10.2026 sagte das Modell Suedwest mit 26-37 km/h und Boeen bis
# 72 km/h voraus, und ohne Grenze stand da "Go winging instead!". Nachziehen,
# sobald jemand an so einem Tag draussen war.
GALE_GUST = 60         # Boeen ab hier: Sturm
GALE_WIND = 45         # Mittelwind ab hier: Sturm

# Lufttemperatur des Modells in °C
ICE = 2                # "Ice, ice baby." — Tageshoechstwert hoechstens so
FROSTY = 2             # kalter Start, wenn das Minimum so tief liegt
PARADISE_T = 22        # "Another day in paradise." ab hier ...
HOT = 30               # ... und darunter. Ab hier "gettin' hot".
PARADISE_SUN = 0.6     # Anteil sonniger Stunden
PARADISE_VIS = 10000   # Sicht in m, in jeder Stunde

FOG_VIS = 1000         # Sicht in m darunter heisst Nebel, auch ohne Code 45/48
# Code 45 bei 9.5 km Sicht kommt vor (Prognose fuer den 26.10.2026). Dann
# ist der Nebel unsicher und heisst "patchy"; die Sicht wird nur genannt,
# wenn sie ihn auch bestaetigt.
FOG_SURE = 2000
# Regen nach Menge, nicht nach Wettercode: das Modell setzt Code 61
# ("leichter Regen") auch bei 0.0 mm. Am 07.10.2026 hiess es so Schauer,
# und der Besitzer notierte "dead flat, perfect water".
RAIN_MM = 0.2          # mm in der Stunde

# Overrides: fester, sachlicher Satz statt Modelltext
UV_MAX = 11
AQI_MAX = 80

LAKE_MAX_AGE_H = 12    # wie CONFIG.lakeTempMaxAgeH in index.html

# Textfenster. Es zaehlt der erste Lauf nach dem Beginn; Verspaetungen von
# GitHub schaden also nicht. Abends nur noch morgen und uebermorgen.
SLOT_MORNING = time(5, 30)                  # damit um 6 Uhr der Text steht
SLOT_MIDDAY = time(12, 0)
SLOT_EVENING_AFTER = timedelta(minutes=30)  # nach Sonnenuntergang
SLOT_TRIES = 3         # so oft probieren, wenn die API streikt, dann fester Satz
RECENT_DAYS = 3        # so lange soll sich ein Titel nicht wiederholen

HEAD_MAX, SUB_MAX = 60, 110                 # Zeichen; laenger heisst unbrauchbar

FOG_CODES = {45, 48}
STORM_CODES = {95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
SUN_CODES = {0, 1}
# Auch starker Offshore ist am Steg glatt: am 24.10.2026 bei 25 km/h
# "still doable since offshore, but you feel it when pumping back".
PUMPABLE = {"flat", "off", "off_strong"}

# Feste Saetze aus der Regeltabelle (assessment.md). Sie stehen da, wenn das
# Modell nicht erreichbar ist; das Modell darf sie aufgreifen.
SIGNATURE = {
    "STORM": "Thunderstorms possible.",
    "GALE": "Too windy today.",
    "ICE": "Ice, ice baby.",
    "WING": "Go winging instead!",
    "FOG": "Ready for a session in the fog?",
    "RAIN": "Rain on and off.",
    "SNOW": "Snow on the lake.",
    "CHOP": "Onshore wind. A bit choppy.",
    "CROSS": "Sideways chop.",
    "OFFSHORE": "Offshore wind.",
    "PARADISE": "Another day in paradise.",
    "FLAT": "Dead Flat Water. Go Pumping!",
    "HOT": "It's gettin' hot in here.",
    "DEFAULT": "See you at the docks!",
}
OVERRIDE_THEMES = {"WARNING", "UV", "AQI"}

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
RELATIVE = {0: "today", 1: "tomorrow", 2: "day after tomorrow"}

SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "headline": {"type": "string"},
                    "subline": {"type": "string"},
                },
                "required": ["date", "headline", "subline"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["days"],
    "additionalProperties": False,
}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def local_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, TZ).date()


def read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_json(url: str) -> dict:
    for i in range(FETCH_TRIES):
        try:
            r = requests.get(url, timeout=30, headers=UA)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if i == FETCH_TRIES - 1:
                raise
            sleep(FETCH_PAUSE_S)


def sector(dd: float) -> str:
    """Lage des Windes zur Seeachse: 'on' (Bise, volle Seelaenge Anlauf),
    'off' (Suedwest, vom Ufer weg) oder 'cross'."""
    d = (dd - AXIS + 180) % 360 - 180
    if abs(d) <= ONSHORE_MAX:
        return "on"
    if abs(d) >= OFFSHORE_MIN:
        return "off"
    return "cross"


def surface(ws: float, dd) -> str:
    """Was der Wind am Steg aus dem Wasser macht."""
    if ws < CALM:
        return "flat"
    s = sector(dd) if dd is not None else "cross"
    if s == "on":
        return "choppy" if ws >= CHOPPY_ON else "chop"
    if s == "cross":
        if ws >= CHOPPY_CROSS:
            return "choppy_x"
        return "chop_x" if ws >= CHOP_CROSS else "flat"
    if ws >= OFFSHORE_STRONG:
        return "off_strong"
    return "off" if ws >= OFFSHORE_BIT else "flat"


def classify(r: dict) -> dict:
    c = r["code"]
    r["fog"] = c in FOG_CODES or (r["vis"] is not None and r["vis"] < FOG_VIS)
    r["storm"] = c in STORM_CODES
    r["snow"] = c in SNOW_CODES
    r["rain"] = r["mm"] >= RAIN_MM and not r["snow"]
    r["sun"] = c in SUN_CODES
    r["sector"] = sector(r["wd"]) if r["wd"] is not None else "cross"
    r["surf"] = surface(r["ws"], r["wd"])
    r["gale"] = r["ws"] >= GALE_WIND or (r["wg"] or 0) >= GALE_GUST
    r["wing"] = r["ws"] >= WINGABLE and not r["gale"]
    r["ok"] = (r["surf"] in PUMPABLE and not r["gale"]
               and not (r["fog"] or r["storm"] or r["snow"] or r["rain"]))
    return r


def build_hours(fc: dict, aq) -> dict:
    """Stundenwerte nach Ortsdatum: {date: [stunde, ...]}."""
    h = fc["hourly"]
    aqi = dict(zip(aq["hourly"]["time"], aq["hourly"]["european_aqi"])) if aq else {}
    out = {}
    for i, t in enumerate(h["time"]):
        ws = h["wind_speed_10m"][i]
        if ws is None or h["weather_code"][i] is None:
            continue
        out.setdefault(local_date(t), []).append({
            "t": t,
            "T": h["temperature_2m"][i],
            "code": h["weather_code"][i],
            "vis": h["visibility"][i],
            "mm": h["precipitation"][i] or 0,
            "ws": ws,
            "wd": h["wind_direction_10m"][i],
            "wg": h["wind_gusts_10m"][i],
            "uv": h["uv_index"][i],
            "aqi": aqi.get(t),
        })
    return out


def sun_times(fc: dict) -> dict:
    """{date: (sonnenaufgang, sonnenuntergang)} in Unix-Sekunden."""
    d = fc["daily"]
    return {local_date(sr): (sr, ss) for sr, ss in zip(d["sunrise"], d["sunset"])}


def lake_temp(level: dict, now: datetime):
    """Wassertemperatur von schiffenen.guru, wenn sie jung genug ist."""
    lake = level.get("lake") or {}
    try:
        t = datetime.fromisoformat(lake["time"])
        if (now - t).total_seconds() <= LAKE_MAX_AGE_H * 3600:
            return float(lake["temperature"])
    except (KeyError, TypeError, ValueError):
        pass
    return None


def analyse(day: date, hours: list, sun, now: datetime, today: date, lake, warning):
    """Aussagen fuer einen Tag, oder None, wenn dort kein Tageslicht mehr ist."""
    if not sun or not hours:
        return None
    sr, ss = sun
    start = max(sr, now.timestamp()) if day == today else sr
    end = ss
    if end - start < 1800:
        return None
    rest = start > sr + 1800                 # heute, Tag schon angebrochen
    hs = [classify(dict(r)) for r in hours if r["t"] + 3600 > start and r["t"] < end]
    if not hs:
        return None
    # Eine einzelne wingbare Stunde praegt keinen Tag (24.10.2026: eine Stunde
    # mit 26 km/h, sonst 24 — der Titel hiess "Go winging instead!")
    if sum(r["wing"] for r in hs) < 2:
        for r in hs:
            r["wing"] = False

    def when(a, b):
        a, b = max(a, start), min(b, end)
        if a <= start + 900 and b >= end - 900:
            return "for the rest of the day" if rest else "all day"
        if a <= start + 900:
            return f"until {hhmm(b)}"
        if b >= end - 900:
            return f"from {hhmm(a)}"
        return f"{hhmm(a)}–{hhmm(b)}"

    def runs(pred):
        """Zusammenhaengende Stunden, fuer die pred gilt: [(von, bis, stunden)]."""
        out, cur = [], []
        for r in hs:
            if pred(r):
                cur.append(r)
            elif cur:
                out.append(cur)
                cur = []
        if cur:
            out.append(cur)
        return [(g[0]["t"], g[-1]["t"] + 3600, g) for g in out]

    def periods(rs):
        return " and ".join(when(a, b) for a, b, _ in rs[:3])

    def members(rs):
        return [r for _, _, g in rs for r in g]

    def kmh(m):
        lo, hi = min(r["ws"] for r in m), max(r["ws"] for r in m)
        return f"{hi:.0f} km/h" if hi - lo < 2 else f"{lo:.0f}–{hi:.0f} km/h"

    def gusts(m):
        g = [r["wg"] for r in m if r["wg"] is not None]
        return f", gusts to {max(g):.0f}" if g else ""

    temps = [r["T"] for r in hs if r["T"] is not None]
    tmin, tmax = (min(temps), max(temps)) if temps else (None, None)
    n = len(hs)
    ok_share = sum(r["ok"] for r in hs) / n
    sun_share = sum(r["sun"] for r in hs) / n
    uv_max = max((r["uv"] or 0) for r in hs)
    aqis = [r["aqi"] for r in hs if r["aqi"] is not None]
    aqi_max = max(aqis) if aqis else None

    pts = []

    def add(theme, text, rs=None):
        pts.append({"theme": theme, "text": text,
                    "from": hhmm(max(rs[0][0], start)) if rs else None,
                    "to": hhmm(min(rs[-1][1], end)) if rs else None})

    ov = override(start, end, when, warning, uv_max, aqi_max)
    if ov:
        add(ov["theme"], ov["point"])

    rs = runs(lambda r: r["storm"])
    if rs:
        add("STORM", f"Thunderstorms possible {periods(rs)}.", rs)
    gale = runs(lambda r: r["gale"])
    if gale:
        m = members(gale)
        add("GALE", f"Stormy {periods(gale)}: {kmh(m)}{gusts(m)}.", gale)
    if tmax is not None and tmax <= ICE:
        add("ICE", f"Freezing: {tmax:.0f} °C at most.")
    # An einem Sturmtag kein Wingen vorschlagen, auch nicht in den
    # ruhigeren Randstunden davor und danach
    rs = runs(lambda r: r["wing"])
    if rs and not gale:
        m = members(rs)
        sek = max(("on", "off", "cross"), key=lambda s: sum(r["sector"] == s for r in m))
        word = {"on": "Bise", "off": "offshore", "cross": "cross"}[sek]
        extra = " Flat at the dock, tough pumping back." if sek == "off" else ""
        add("WING", f"Wingable {word} wind {periods(rs)}: {kmh(m)}{gusts(m)}.{extra}", rs)
    rs = runs(lambda r: r["fog"])
    if rs:
        vis = [r["vis"] for r in members(rs) if r["vis"] is not None]
        if vis and min(vis) < FOG_SURE:
            add("FOG", f"Fog {periods(rs)}, visibility down to {min(vis) / 1000:.1f} km.", rs)
        else:
            add("FOG", f"Patchy fog possible {periods(rs)}.", rs)
    # Regen fast den ganzen Tag praegt ihn; einzelne Schauer sind ein Detail
    # und kommen erst nach dem Wasser (24.10.2026: Schauer am Morgen, aber der
    # Tag hiess "strong offshore")
    rain = runs(lambda r: r["rain"])
    if rain and len(members(rain)) / n >= 0.7:
        add("RAIN", "Rain most of the day.", rain)
        rain = []
    rs = runs(lambda r: r["snow"])
    if rs:
        add("SNOW", f"Snow {periods(rs)}.", rs)
    rs = runs(lambda r: r["surf"] in ("chop", "choppy") and not (r["wing"] or r["gale"]))
    if rs:
        m = members(rs)
        how = "choppy" if any(r["surf"] == "choppy" for r in m) else "a bit of chop"
        add("CHOP", f"Bise (onshore) {periods(rs)}, {kmh(m)}: {how}. "
                    "Too much for clean pumping, too little for winging.", rs)
    rs = runs(lambda r: r["surf"] in ("chop_x", "choppy_x") and not (r["wing"] or r["gale"]))
    if rs:
        m = members(rs)
        how = "choppy from the side" if any(r["surf"] == "choppy_x" for r in m) else "sideways chop"
        add("CROSS", f"Cross wind {periods(rs)}, {kmh(m)}: {how}.", rs)
    rs = runs(lambda r: r["surf"] == "off_strong" and not (r["wing"] or r["gale"]))
    if rs:
        m = members(rs)
        add("OFFSHORE", f"Strong offshore wind {periods(rs)}, {kmh(m)}{gusts(m)}: "
                        "flat at the dock, tough pumping back.", rs)

    paradise = (ok_share == 1 and sun_share >= PARADISE_SUN and tmax is not None
                and PARADISE_T <= tmax < HOT
                and all((r["vis"] or 0) >= PARADISE_VIS for r in hs))
    flat = runs(lambda r: r["surf"] == "flat")
    if paradise:
        add("PARADISE", f"Paradise: sunny, up to {tmax:.0f} °C, flat water {when(start, end)}.")
    elif flat:
        m = members(flat)
        if max(r["ws"] for r in m) < CALM:
            add("FLAT", f"Dead flat {periods(flat)}: wind under {CALM} km/h.", flat)
        else:
            add("FLAT", f"Flat water {periods(flat)}: {kmh(m)}, no chop at the dock.", flat)
    rs = runs(lambda r: r["surf"] == "off")
    if rs and not paradise:
        add("OFFSHORE", f"Light offshore breeze {periods(rs)}, {kmh(members(rs))}: "
                        "flat at the dock.", rs)
    if len(rain) > 1:
        add("RAIN", f"Changeable: showers {periods(rain)}, dry in between.", rain)
    elif rain:
        add("RAIN", f"Rain {periods(rain)}.", rain)

    poor = (any(r["storm"] or r["gale"] for r in hs) or (tmax is not None and tmax <= ICE)
            or ok_share < 0.25 or ov is not None)
    mood = "poor" if poor else ("good" if ok_share >= 0.75 else "mixed")

    # Bestes Fenster nur an gemischten Tagen, und nur wenn es mehr sagt als
    # die Flachwasser-Aussage selbst — sonst stuende dasselbe zweimal da.
    good = runs(lambda r: r["ok"])
    if mood == "mixed" and good:
        a, b, g = max(good, key=lambda x: len(x[2]))
        if len(g) >= 2 and not (len(flat) == 1 and flat[0][0] == a and flat[0][1] == b):
            best = {"theme": "BEST", "text": f"Best window {when(a, b)}.",
                    "from": hhmm(max(a, start)), "to": hhmm(min(b, end))}
            first = next((i for i, p in enumerate(pts) if p["theme"] not in OVERRIDE_THEMES), 0)
            pts.insert(first + 1, best)

    if tmax is not None and tmax >= HOT:
        add("HOT", f"Hot: up to {tmax:.0f} °C.")
    if tmin is not None and tmin <= FROSTY and not (tmax <= ICE):
        add("COLD", f"Frosty start: {tmin:.0f} °C.")
    pts = pts[:5]
    if tmin is not None:
        txt = f"Air {tmin:.0f}–{tmax:.0f} °C." if round(tmax) != round(tmin) else f"Air {tmax:.0f} °C."
        if lake is not None:
            txt += f" Lake {lake:.0f} °C."
        pts.append({"theme": "TEMP", "text": txt, "from": None, "to": None})

    def bucket(hm):
        if not hm:
            return ""
        h = int(hm[:2])
        return "m" if h < 11 else "d" if h < 14 else "a" if h < 17 else "e"

    # Aendert sich nur eine Zahl, bleibt der Text. Neu geschrieben wird erst,
    # wenn ein Thema dazukommt, wegfaellt oder in einen anderen Tagesteil rutscht.
    sig = json.dumps([mood] + [[p["theme"], bucket(p["from"]), bucket(p["to"])]
                               for p in pts if p["theme"] != "TEMP"])
    winds = [r["ws"] for r in hs]
    gust_v = [r["wg"] for r in hs if r["wg"] is not None]
    return {
        "points": pts,
        "mood": mood,
        "rest": rest,
        "sig": sig,
        "override": {k: ov[k] for k in ("kind", "headline", "subline", "event", "severity")
                     if ov.get(k) is not None} if ov else None,
        "facts": {
            "window": f"{hhmm(start)}–{hhmm(end)}",
            "mood": mood,
            "tmin": None if tmin is None else round(tmin, 1),
            "tmax": None if tmax is None else round(tmax, 1),
            "wind": [round(min(winds)), round(max(winds))],
            "gust_max": round(max(gust_v)) if gust_v else None,
            "uv_max": round(uv_max, 1),
            "aqi_max": aqi_max,
            "lake": lake,
            "ok_share": round(ok_share, 2),
            "sun_share": round(sun_share, 2),
        },
    }


def override(start, end, when, warning, uv_max, aqi_max):
    """Gewitterwarnung, UV oder Luftqualitaet: fester, sachlicher Satz."""
    if warning:
        try:
            on = datetime.fromisoformat(warning["onset"]).timestamp() if warning.get("onset") else start
            ex = datetime.fromisoformat(warning["expires"]).timestamp() if warning.get("expires") else end
        except ValueError:
            on, ex = start, end
        if on < end and ex > start:
            w = when(on, ex)
            return {"kind": "storm", "theme": "WARNING",
                    "headline": "Thunderstorm warning.",
                    "subline": f"{w[0].upper()}{w[1:]}. Stay off the water.",
                    "point": f"Official thunderstorm warning {w}.",
                    "event": warning.get("event"), "severity": warning.get("severity")}
    if uv_max >= UV_MAX:
        return {"kind": "uv", "theme": "UV", "headline": "Extreme UV index.",
                "subline": "Outdoor activities not recommended.",
                "point": f"UV index up to {uv_max:.0f}: extreme."}
    if aqi_max is not None and aqi_max >= AQI_MAX:
        return {"kind": "aqi", "theme": "AQI", "headline": "Bad air quality.",
                "subline": "Outdoor activities not recommended.",
                "point": f"Air quality index up to {aqi_max:.0f}: very poor."}
    return None


def fallback(a: dict):
    """Der feste Satz: Leitsatz des wichtigsten Themas, dazu die naechste Aussage."""
    pts = [p for p in a["points"] if p["theme"] not in OVERRIDE_THEMES]
    head = SIGNATURE.get(pts[0]["theme"], SIGNATURE["DEFAULT"]) if pts else SIGNATURE["DEFAULT"]
    sub = ""
    for p in pts[1:]:
        sub = p["text"].split(". ")[0].rstrip(".") + "."
        break
    return head, sub


def load_prompt() -> str:
    with open(PROMPT, encoding="utf-8") as f:
        return re.sub(r"<!--.*?-->", "", f.read(), flags=re.S).strip()


def render(days: list, analyses: dict, today: date, recent: list) -> str:
    """Die Anfrage ans Modell: pro Tag die fertigen Aussagen."""
    lines = ["Write the assessment for each day below. Return one entry per day, "
             "with the date exactly as given."]
    if recent:
        lines += ["", "Headlines of the last days — don't reuse these word for word:"]
        lines += [f"- {h}" for h in recent]
    for d in days:
        a = analyses[d]
        rel = RELATIVE.get((d - today).days, "")
        span = "from now until sunset" if a["rest"] else "sunrise to sunset"
        lines += ["",
                  f"DAY {d.isoformat()}: {WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} ({rel})",
                  f"Window: {a['facts']['window']} ({span}). Mood: {a['mood']}.",
                  "Points, most important first:"]
        lines += [f"{i}. [{p['theme']}] {p['text']}" for i, p in enumerate(a["points"], 1)]
    return "\n".join(lines)


def clean(s, limit):
    s = " ".join(str(s or "").split()).strip().strip('"“”')
    return s if 0 < len(s) <= limit else None


NOKEY = "kein ANTHROPIC_API_KEY"
BADKEY = ("ANTHROPIC_API_KEY beginnt nicht mit sk-ant- — im Secret steht vermutlich "
          "der Name oder die Kennung (apikey_…) statt des Schluessels")


def api_key() -> str:
    """Der Key aus dem Secret, ohne Leerraum und Anfuehrungszeichen.

    Am 24.10.2026 scheiterte der erste echte Aufruf mit "APIConnectionError:
    Connection error." — so meldet sich das SDK, wenn im Key ein Zeilenumbruch
    steckt, etwa vom Einfuegen. Ein Key enthaelt nie Leerraum, also darf alles
    weg, auch mitten drin.
    """
    return "".join((os.environ.get("ANTHROPIC_API_KEY") or "").split()).strip("\"'")


def why(e: Exception) -> str:
    """Fehlertext fuer den oeffentlichen Stand: Meldung des SDK, dazu nur die
    KLASSEN der Ursachen — deren Meldungen koennten Teile des Keys enthalten."""
    causes, c = [], e.__cause__ or e.__context__
    while c is not None and len(causes) < 3:
        causes.append(type(c).__name__)
        c = c.__cause__ or c.__context__
    return (f"{type(e).__name__}: {e}"[:200]
            + (f" (Ursache: {' < '.join(causes)})" if causes else ""))


def ask_model(days: list, analyses: dict, today: date, recent: list):
    import anthropic

    client = anthropic.Anthropic(api_key=api_key(), timeout=180, max_retries=2)
    r = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        # Lehnt das Modell ab (bei Wettertext kaum denkbar), springt
        # serverseitig das empfohlene Ersatzmodell ein.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": EFFORT,
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        system=load_prompt(),
        messages=[{"role": "user", "content": render(days, analyses, today, recent)}],
    )
    if r.stop_reason != "end_turn":
        raise ValueError(f"stop_reason {r.stop_reason}")
    text = next(b.text for b in r.content if b.type == "text")
    out = {}
    for e in json.loads(text).get("days", []):
        h, s = clean(e.get("headline"), HEAD_MAX), clean(e.get("subline"), SUB_MAX)
        if h and s is not None:
            out[e.get("date")] = (h, s)
    meta = {"model": r.model, "request_id": r._request_id,
            "in": r.usage.input_tokens, "out": r.usage.output_tokens}
    return out, meta


def write_texts(days: list, analyses: dict, today: date, recent: list, dry: bool):
    """Titel und Zusatz je Tag: {date: (titel, zusatz, quelle)} und Metadaten.
    meta["final"] sagt, ob ein neuer Versuch im naechsten Lauf etwas aendern koennte."""
    texts = {d: (*fallback(analyses[d]), "fallback") for d in days}
    if dry:
        print("--- Anfrage ans Modell ---\n" + render(days, analyses, today, recent) + "\n---")
        return texts, {"dry": True}
    if not api_key():
        return texts, {"error": NOKEY, "final": True}
    if not api_key().startswith("sk-ant-"):
        return texts, {"error": BADKEY, "final": True}
    try:
        import anthropic
    except ImportError:
        return texts, {"error": "Paket anthropic fehlt", "final": True}
    try:
        got, meta = ask_model(days, analyses, today, recent)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
            anthropic.NotFoundError, anthropic.BadRequestError) as e:
        # Schluessel oder Anfrage falsch: ein neuer Versuch aendert nichts
        return texts, {"error": why(e), "final": True}
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        return texts, {"error": why(e), "final": False}
    except (ValueError, KeyError, StopIteration) as e:
        return texts, {"error": f"Antwort unbrauchbar: {e}"[:300], "final": False}
    missing = []
    for d in days:
        if d.isoformat() in got:
            texts[d] = (*got[d.isoformat()], "model")
        else:
            missing.append(d.isoformat())
    if missing:
        meta["error"] = "kein brauchbarer Text fuer " + ", ".join(missing)
        meta["final"] = True
    return texts, meta


def recent_headlines(path: str, now: datetime) -> list:
    """Titel der letzten Tage, damit sich keiner zu bald wiederholt."""
    cutoff = now - timedelta(days=RECENT_DAYS)
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if (e.get("type") == "text" and e.get("source") == "model"
                            and datetime.fromisoformat(e["time"]) >= cutoff
                            and e.get("headline") not in out):
                        out.append(e["headline"])
                except (ValueError, KeyError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return out[-20:]


def due_slot(now: datetime, suns: dict, today: date):
    """Juengstes Textfenster, das schon begonnen hat: (id, art, beginn, tag)."""
    loc = now.astimezone(TZ)
    cands = []
    for back in (1, 0):
        d = today - timedelta(days=back)
        for name, t in (("morning", SLOT_MORNING), ("midday", SLOT_MIDDAY)):
            cands.append((f"{d}-{name}", name, datetime.combine(d, t, TZ), d))
        sun = suns.get(d) or (suns.get(today) and tuple(x - 86400 * back for x in suns[today]))
        if sun:
            ev = datetime.fromtimestamp(sun[1], TZ) + SLOT_EVENING_AFTER
            cands.append((f"{d}-evening", "evening", ev, d))
    past = [c for c in cands if c[2] <= loc]
    return max(past, key=lambda c: c[2]) if past else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry", action="store_true",
                    help="nichts schreiben, das Modell nicht fragen, die Anfrage zeigen")
    ap.add_argument("--now", help="Ortszeit vortaeuschen, z. B. 2026-10-25T05:40")
    ap.add_argument("--data", default=DATA, help="Verzeichnis mit level.json")
    ap.add_argument("--force", action="store_true",
                    help="aktuelles Fenster sofort neu schreiben, fuer alle Tage")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    if args.now:
        now = datetime.fromisoformat(args.now).replace(tzinfo=TZ).astimezone(timezone.utc)
    state_path = os.path.join(args.data, "assessment.json")
    log_path = os.path.join(args.data, "assessment_log.jsonl")
    level = read_json(os.path.join(args.data, "level.json")) or {}
    state = read_json(state_path) or {}

    def log(entry):
        if args.dry:
            print("LOG " + json.dumps(entry, ensure_ascii=False))
            return
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    # Abruffehler stehen im Stand, nicht nur im Log des Laufs: die Logs sind
    # ohne Anmeldung nicht lesbar, die Testseite zeigt den Stand. Wie
    # hydro_error in level.json steht das Feld nur, solange es zutrifft.
    def note(key, err):
        if err:
            state[key] = {"time": iso(now), "error": err[:300]}
            print(err, file=sys.stderr)
        else:
            state.pop(key, None)

    try:
        fc = get_json(FORECAST)
    except Exception as e:                                  # noqa: BLE001
        # Ohne Prognose kein neuer Text; der alte Stand bleibt stehen
        note("fetch_error", f"Prognose nicht geholt: {e}")
        if not args.dry:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.write("\n")
        return 1
    had = (state.get("fetch_error"), state.get("aq_error"))
    note("fetch_error", None)
    try:
        aq = get_json(AIRQ)
        note("aq_error", None)
    except Exception as e:                                  # noqa: BLE001
        note("aq_error", f"Luftqualitaet nicht geholt, ohne AQI-Override: {e}")
        aq = None

    hours, suns = build_hours(fc, aq), sun_times(fc)
    today = now.astimezone(TZ).date()
    show = [today + timedelta(days=i) for i in range(3)]
    lake = lake_temp(level, now)
    warning = level.get("warning")
    analyses = {d: analyse(d, hours.get(d, []), suns.get(d), now, today, lake, warning)
                for d in show}

    old_days = state.get("days") or {}
    days, changed = {}, False
    for d in show:
        key, a = d.isoformat(), analyses[d]
        e = dict(old_days.get(key) or {})
        if a is not None:
            if e.get("override") != a["override"]:
                e["override"] = a["override"]
                log({"type": "override", "time": iso(now), "day": key, "override": a["override"]})
                changed = True
            if "headline" not in e:
                # Neuer Tag zwischen zwei Fenstern: fester Satz als Platzhalter,
                # das naechste Fenster schreibt ihn neu. Nicht ins Log.
                h, s = fallback(a)
                e.update(headline=h, subline=s, source="placeholder", written=iso(now),
                         slot=None, sig=None, points=a["points"], facts=a["facts"])
                changed = True
        days[key] = e
    if set(days) != set(old_days) or had != (state.get("fetch_error"), state.get("aq_error")):
        changed = True

    # Kam der Key erst nach dem Fenster dazu oder war er unbrauchbar, das
    # Fenster nachholen, sobald er taugt — sonst stuenden bis zum naechsten
    # Fenster die festen Saetze da
    if (api_key().startswith("sk-ant-")
            and (state.get("error") or {}).get("error") in (NOKEY, BADKEY)):
        state.pop("slot", None)
    # Knopf im Workflow ("Assessment sofort neu schreiben"), etwa nach einer
    # Aenderung an assessment_prompt.md: das aktuelle Fenster jetzt, alle Tage
    force = args.force or os.environ.get("ASSESS_FORCE") == "true"
    if force:
        for k in ("slot", "slot_tries", "slot_try_id"):
            state.pop(k, None)

    slot = due_slot(now, suns, today)
    if slot and slot[0] != state.get("slot"):
        sid, name, _, sday = slot
        offsets = (1, 2) if name == "evening" else (0, 1, 2)
        targets = [sday + timedelta(days=i) for i in offsets]
        targets = [d for d in targets if d in show and analyses.get(d)]
        need = targets if force else [
            d for d in targets
            if days[d.isoformat()].get("source") != "model"
            or days[d.isoformat()].get("sig") != analyses[d]["sig"]]
        tries = state.get("slot_tries", 0) if state.get("slot_try_id") == sid else 0
        done, meta = True, {}
        if need:
            texts, meta = write_texts(need, analyses, today, recent_headlines(log_path, now), args.dry)
            if meta.get("error"):
                state["error"] = {"time": iso(now), "slot": sid, "error": meta["error"]}
                log({"type": "error", "time": iso(now), "slot": sid, "error": meta["error"]})
                print(f"Modell: {meta['error']}", file=sys.stderr)
                if not meta.get("final") and tries + 1 < SLOT_TRIES:
                    done = False
                    state["slot_try_id"], state["slot_tries"] = sid, tries + 1
            else:
                state.pop("error", None)
            if done:
                for d in need:
                    h, s, src = texts[d]
                    e = days[d.isoformat()]
                    # Versagt das Modell, bleibt ein frueherer Modelltext stehen —
                    # als Prognose ist er noch brauchbar. Nur ohne ihn der feste Satz.
                    if src == "fallback" and e.get("source") == "model":
                        continue
                    a = analyses[d]
                    # Derselbe feste Satz wie zuvor ist kein neuer Text: nur
                    # nachfuehren, nicht ins Log (laeuft so, solange der Key fehlt)
                    same = (src == "fallback" and e.get("source") == "fallback"
                            and (e.get("headline"), e.get("subline")) == (h, s))
                    if same:
                        e.update(sig=a["sig"], points=a["points"], facts=a["facts"], slot=sid)
                        continue
                    e.update(headline=h, subline=s, source=src, written=iso(now), slot=sid,
                             sig=a["sig"], points=a["points"], facts=a["facts"],
                             model=meta.get("model") if src == "model" else None)
                    log({"type": "text", "time": iso(now), "slot": sid, "day": d.isoformat(),
                         "source": src, "headline": h, "subline": s,
                         "points": a["points"], "facts": a["facts"],
                         "model": e["model"], "request_id": meta.get("request_id"),
                         "tokens": [meta.get("in"), meta.get("out")] if src == "model" else None})
        if done:
            state["slot"], state["slot_done"] = sid, iso(now)
            state.pop("slot_tries", None)
            state.pop("slot_try_id", None)
        if done:
            print(f"Fenster {sid}: {len(need)} von {len(targets)} Tagen neu"
                  + (f", Modell {meta.get('model')} ({meta.get('in')}/{meta.get('out')} Tokens)"
                     if meta.get("model") else ""))
        else:
            print(f"Fenster {sid}: Modell nicht erreichbar, neuer Versuch im naechsten Lauf "
                  f"({tries + 1}/{SLOT_TRIES})")
        changed = True

    for key, e in days.items():
        ov = e.get("override")
        if "headline" not in e:
            print(f"{key}: kein Text (Tag vorbei)")
            continue
        print(f"{key}: [{e['source']}] {e['headline']} — {e['subline']}"
              + (f"  OVERRIDE: {ov['headline']} {ov['subline']}" if ov else ""))

    state["days"] = days
    if changed and not args.dry:
        state["updated"] = iso(now)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Meldet Schluessel, die bald ablaufen, als GitHub-Issue.

GitHub schickt zu jedem neuen Issue eine Mail an den Besitzer — derselbe Weg
wie bei der Lueckenmeldung. Eine Meldung je Schluessel und Ablaufdatum,
VORLAUF_TAGE vorher. Laeuft bei jedem Bot-Lauf, fragt GitHub aber nur, wenn
ein Schluessel wirklich faellig ist.

Die Daten stehen in .github/ablauf.json. Nach dem Erneuern dort das neue
Ablaufdatum eintragen (auf GitHub im Web-Editor), dann kommt die naechste
Meldung von selbst.

Testen:  python scripts/ablauf.py --dry --heute 2027-01-15
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

VORLAUF_TAGE = 7
LISTE = os.path.join(os.path.dirname(__file__), "..", ".github", "ablauf.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry", action="store_true", help="nur anzeigen, kein Issue anlegen")
    ap.add_argument("--heute", help="Datum vortaeuschen, JJJJ-MM-TT")
    args = ap.parse_args(argv)

    heute = (date.fromisoformat(args.heute) if args.heute
             else datetime.now(ZoneInfo("Europe/Zurich")).date())
    with open(LISTE, encoding="utf-8") as f:
        schluessel = json.load(f)

    faellig = []
    for k in schluessel:
        ab = date.fromisoformat(k["expires"])
        rest = (ab - heute).days
        if rest <= VORLAUF_TAGE:
            faellig.append((k, ab, rest))
    if not faellig:
        return 0

    vorhanden = set()
    if not args.dry:
        vorhanden = set(subprocess.run(
            ["gh", "issue", "list", "--state", "all", "--limit", "200",
             "--json", "title", "--jq", ".[].title"],
            capture_output=True, text=True, check=True).stdout.splitlines())

    for k, ab, rest in faellig:
        titel = f"Schlüssel läuft ab: {k['name']} am {ab:%d.%m.%Y}"
        if titel in vorhanden:
            continue
        if rest < 0:
            tage = "1 Tag" if rest == -1 else f"{-rest} Tagen"
            satz = f"**{k['name']}** ist seit {tage} abgelaufen, seit dem {ab:%d.%m.%Y}."
        else:
            wann = f"in **{rest} Tagen**" if rest > 1 else "**morgen**" if rest == 1 else "**heute**"
            satz = f"**{k['name']}** läuft {wann} ab, am {ab:%d.%m.%Y}."
        text = (f"{satz}\n\n"
                f"Gebraucht für: {k['used_for']}\n\n"
                f"**Erneuern:** {k['renew']}\n\n"
                f"Danach in `.github/ablauf.json` das neue Ablaufdatum eintragen, "
                f"dann kommt die nächste Erinnerung von selbst.\n\n"
                f"_Gemeldet von scripts/ablauf.py, {VORLAUF_TAGE} Tage vorher._\n")
        if args.dry:
            print(f"--- würde anlegen: {titel}\n{text}")
            continue
        subprocess.run(["gh", "issue", "create", "--title", titel, "--body", text], check=True)
        print(f"Issue angelegt: {titel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Haengt eine Benotung von der Testseite an data/assessment_grades.csv an.

Aufgerufen vom Workflow grade.yml. Die Eingaben kommen als
Umgebungsvariablen, nie direkt im Shell-Befehl — so kann eine Notiz mit
Anfuehrungszeichen oder $(...) nichts ausfuehren. Die Datei ist
append-only wie hydro.csv: nie kuerzen.

Zwei Noten, weil sie Verschiedenes pruefen: "accurate" (stimmt's?) den
Code, der die Lage einordnet, "liked" (gefaellt's?) die Formulierung des
Modells. "version" ist der Zeitstempel des benoteten Textes, wie er in
assessment.json als "written" und im Log als "time" steht.
"""
import csv
import os
import re
import sys
from datetime import datetime, timezone

CSV = os.path.join(os.path.dirname(__file__), "..", "data", "assessment_grades.csv")
HEAD = ["graded_at", "day", "version", "seen_at", "accurate", "liked",
        "dock", "depth_cm", "note"]
NOTES = ("", "yes", "partly", "no")


def main() -> int:
    def env(k):
        return " ".join((os.environ.get(k.upper()) or "").split())

    row = {k: env(k) for k in HEAD[1:]}
    row["graded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row["note"] = row["note"][:500]

    checks = [
        (re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["day"]), "day: JJJJ-MM-TT"),
        (row["accurate"] in NOTES, "accurate: yes, partly oder no"),
        (row["liked"] in NOTES, "liked: yes, partly oder no"),
        (row["dock"] in ("", "d1", "d2", "d3"), "dock: d1, d2 oder d3"),
        (not row["seen_at"] or re.fullmatch(r"\d{2}:\d{2}", row["seen_at"]), "seen_at: HH:MM"),
        (not row["depth_cm"] or re.fullmatch(r"\d{1,3}", row["depth_cm"]), "depth_cm: ganze cm"),
        (not row["version"] or re.fullmatch(r"[0-9T:.+\-Z]{10,40}", row["version"]),
         "version: Zeitstempel"),
        (row["accurate"] or row["liked"] or row["note"] or row["depth_cm"], "leere Benotung"),
    ]
    for ok, why in checks:
        if not ok:
            print(f"Abgelehnt — {why}", file=sys.stderr)
            return 1

    neu = not os.path.exists(CSV) or os.path.getsize(CSV) == 0
    with open(CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEAD, lineterminator="\n")
        if neu:
            w.writeheader()
        w.writerow(row)
    print(f"Note fuer {row['day']}: stimmt={row['accurate'] or '-'} "
          f"gefaellt={row['liked'] or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

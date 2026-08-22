# Pumpfoil Guru

Ampel für drei Docks am Schiffenensee. Statische Seite, läuft auf GitHub Pages,
kein Server, keine Kosten.

## Aufsetzen

1. Repo anlegen, diese Dateien reinkopieren, pushen.
2. **Settings → Pages → Source: Deploy from a branch**, Branch `main`, Ordner `/ (root)`.
3. **Settings → Actions → General → Workflow permissions**: „Read and write permissions" anhaken.
   Ohne das kann der Bot `data/level.json` nicht zurückschreiben.
4. **Actions → Pegel aktualisieren → Run workflow**, einmal von Hand, zum Testen.

Danach läuft es stündlich von selbst.

## Docks kalibrieren

Jedes Dock hat vier Schwellen, alle in m ü. M.:

| Feld | Bedeutung |
|---|---|
| `stopLow` | darunter rot |
| `goLow` | ab hier grün |
| `goHigh` | bis hier grün |
| `stopHigh` | darüber rot |

Dazwischen orange. `calibrated: false` heisst: geraten, die Seite schreibt
„ungeeicht" auf die Karte.

**Referenzwert ist das Tagesminimum der Groupe-E-Prognose**, nicht der
Mittelwert und nicht das Maximum. Beim Notieren immer dieselbe Grösse nehmen,
sonst sind die Zahlen untereinander nicht vergleichbar.

Auf der Seite unten „Stege kalibrieren" öffnen, Regler schieben bis die Ampeln
stimmen, „Config kopieren" drücken, den Block in `index.html` bei `CONFIG.docks`
einsetzen, pushen.

### Stand

| Dock | rot unter | orange | grün ab | Grundlage |
|---|---|---|---|---|
| Dock 1 (Clubhouse) | 531.09 | 531.09–531.34 | 531.35 | 22.08.2026, unsicher (s.u.) |
| Dock 2 (Main Rowing) | 530.10 | 530.10–530.30 | 530.31 | 22.08.2026, Groupe-E-Min |
| Dock 3 (Rowing South) | 528.00 | – | 528.00 | Schätzung, ungemessen |
| Dock 4 (Floating) | – | – | – | Schwimmsteg, keine Pegelgrenzen |

Standorte (Klick auf eine Dock-Karte öffnet Google Maps):

| Dock | Koordinaten |
|---|---|
| Dock 2 (Main Rowing) | 46.847258, 7.141409 |
| Dock 3 (Rowing South) | 46.846452, 7.141960 |
| Dock 4 (Floating) | 46.847556, 7.142008 |

Für Dock 1 liegt noch keine Koordinate vor — die Karte ist dort nicht verlinkt.
Die Kurzlinks stehen in `CONFIG.docks` als `map:`, die Koordinaten als Kommentar
daneben, falls ein Google-Kurzlink je ungültig wird.

Dock 1 braucht am meisten Wasser, Dock 3 am wenigsten.

**Dock 4 ist ein Schwimmsteg und hat gar keine Pegelgrenzen** (`floating:true`).
Er hebt und senkt sich mit dem Wasser, der Pegel ist ihm egal. Deshalb steht er
nicht an der Messlatte, hat keine Kalibrierregler und seine Ampel hängt nur an
`dock4_installed`: eingebaut → grün („schwimmt mit — Pegel egal"), ausgebaut →
rot („Out of order.").

Er trägt zusätzlich `summary:false` und taucht deshalb **nicht im Satz zuoberst**
auf — Karte und Ampel unten zeigt er normal.

**Kein einziges Dock ist bisher echt geeicht.** Auch die 530.10 bei Dock 2 ist
keine Messung, sondern die Groupe-E-Min-Angabe des Tages. Der See stand beim
Befahren irgendwo im Band, also eher höher als das Minimum — die wahre Grenze
liegt damit tendenziell **über** den eingetragenen Werten, die Ampel ist also
eher zu optimistisch. Alle drei tragen deshalb `calibrated: false`.

Solange konsequent gegen das Tagesminimum geeicht *und* ausgewertet wird, ist das
System immerhin in sich stimmig — der Fehler kürzt sich weitgehend weg, solange
du ungefähr zur selben Tageszeit unterwegs bist. Verlässlich wird es erst mit dem
Messband.

**Dock 1 ist bewusst breit orange.** Die Angabe lautet „20 cm weniger als am
22.08.2026 um 10:00 Uhr, dann geht es nicht mehr". Groupe E gibt für diesen Tag
aber nur das Band 531.29–531.55 an — wo der See um 10:00 tatsächlich stand, ist
unbekannt. Die Grenze liegt also irgendwo zwischen 531.09 und 531.35. Solange
das so ist, zeigt die Seite in diesem Bereich orange und nie grün.

Das löst sich mit einer einzigen Messung an einem Tag mit **schmalem Band**:
Grenze ansteuern, Uhrzeit und Band notieren.

**Obergrenzen gibt es keine.** Dock 2 und Dock 3 haben keine, Dock 1 vielleicht,
aber die wurde nie erreicht. Der See wird ohnehin bei **532.00 (Stauziel)**
gedeckelt — höher darf Groupe E nicht einstauen. Die Felder stehen deshalb auf
532.50 und lösen nie aus.

## Sensor anschliessen

Sobald die eigene Messung steht, schreibt der ESP32 per GitHub Contents API
einen `measured`-Block in dieselbe Datei:

```json
{
  "forecast": [ ... ],
  "measured": {
    "level": 531.42,
    "water_temp": 19.3,
    "time": "2026-08-22T14:35:00+02:00"
  }
}
```

Sobald `measured` vorhanden ist, blendet die Seite das Prognoseband als
Hauptwert aus und zeigt die Messung — das Band bleibt nur noch als Ausblick
für die nächsten Tage stehen. Der Scraper überschreibt `measured` nicht.

Pegel in m ü. M. = Höhe der Sensormembran über Meer + gemessene Wassersäule.
Die Membranhöhe einmal einmessen, indem die Messung mit der Groupe-E-Prognose
zu einer ruhigen Nachtstunde verglichen wird.

## Manuelle Schalter

In `data/level.json`:

```json
"manual": { "rowing_competition": false, "dock4_installed": false }
```

| Schalter | Wirkung |
|---|---|
| `rowing_competition: true` | Dock 2 und Dock 3 rot, „Ruderwettkampf — gesperrt." Dock 1 und 4 unberührt |
| `dock4_installed: false` | Dock 4 rot, „Out of order." Normalzustand |

Umlegen direkt auf GitHub: Datei öffnen, Stift, Wert auf `true`, committen.
Nach ein bis zwei Minuten steht es auf der Seite. Der stündliche Job
überschreibt den Block nie.

## Messarchiv

Der stündliche Job schreibt eine Zeile nach `data/hydro.csv` — append-only,
nie gekürzt:

```
timestamp,gE_min,gE_max,h2119,q2119,h2215,q2215
```

- `gE_min` / `gE_max` — Groupe-E-Band des Tages
- `h2119` / `q2119` — BAFU Sarine – Fribourg, oberhalb des Sees: **gemessener
  Pegel** in m ü. M. und Zufluss
- `h2215` / `q2215` — BAFU Saane – Laupen, unterhalb der Staumauer

Alles in einer Zeile, damit sich später ohne Zusammenführen ausrechnen lässt,
wie `h2119` mit dem Groupe-E-Band zusammenhängt. Wenn dieser Zusammenhang stabil
ist, gibt es einen öffentlichen, viertelstündlich gemessenen Pegel — und die
eigene Drucksonde erübrigt sich weitgehend.

Aus `q2215` rechnet der Job Perzentile und legt sie als `hydro.stats` in
`level.json`. Die Seite macht daraus ein Wort: `9 m³/s · wenig`. Solange weniger
als 300 Werte da sind (gut zwei Wochen), steht nur die Zahl — die Einordnung
schaltet sich von selbst frei.

## Datenquellen

- Seepegel-Prognose: Groupe E. Min/Max-Band, keine Messung.
- Abfluss unten: BAFU, Saane – Laupen (2215), via api.existenz.ch.
- Pegel und Zufluss oben: BAFU, Sarine – Fribourg (2119), via api.existenz.ch.
- **Wassertemperatur gibt es bei BAFU nicht** — weder 2215 noch 2119 liefern
  `temperature`. Kommt erst mit dem eigenen Sensor.
  BAFU muss genannt und verlinkt werden.
- Wetter: Open-Meteo.

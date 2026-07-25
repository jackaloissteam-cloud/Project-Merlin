---
title: Aircraft Tracking Center V3.3b
emoji: ✈️
colorFrom: blue
colorTo: blue
sdk: gradio
sdk_version: 6.5.1
app_file: app.py
pinned: false
python_version: 3.12.12
---

# Aircraft Tracking Center V3.3b

Mobile Gradio-App für Hugging Face ZeroGPU.

## Voreinstellungen

- **OO-NZW**: feste ICAO24 `44bb57`
- **CZ468**: sucht beim Flughafen Frankfurt nach den ADS-B-Callsigns `CSN468` oder `CZ468`. Nach dem Fund wird die aktuelle ICAO24 automatisch gespeichert und weltweit weiterverfolgt.
- **Benutzerdefiniert**: Eingabe einer sechsstelligen ICAO24 oder eines ADS-B-Callsigns.

## OpenSky

Die App funktioniert anonym. Für bessere Limits können in **Settings → Variables and secrets** optional diese Secrets gesetzt werden:

- `OPENSKY_CLIENT_ID`
- `OPENSKY_CLIENT_SECRET`

## Bedienung

1. Flugzeug auswählen.
2. **Ziel übernehmen** antippen.
3. Bei einem Callsign sucht die App zunächst im Raum Frankfurt.
4. Sobald das Flugzeug gefunden wurde, zeigt die App die automatisch erkannte ICAO24 an und verfolgt diese weiter.
5. **Callsign bei Frankfurt neu suchen** entfernt die gespeicherte ICAO24 und startet eine neue Suche.

Hinweis: IATA-Flugnummer und ADS-B-Callsign können voneinander abweichen. Für CZ468 wird deshalb auch `CSN468` berücksichtigt.

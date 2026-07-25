from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import gradio as gr
import httpx
import pandas as pd
import plotly.graph_objects as go

try:
    import spaces
except ImportError:
    class _SpacesFallback:
        @staticmethod
        def GPU(*args, **kwargs):
            def decorate(func):
                return func
            if args and callable(args[0]) and len(args) == 1:
                return args[0]
            return decorate
    spaces = _SpacesFallback()

ROOT = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "tracker.db"))
BEEP_PATH = ROOT / "alarm.wav"
POLL_INTERVAL = max(15, min(600, int(os.getenv("POLL_INTERVAL_SECONDS", "60"))))
TAKEOFF_SPEED_KNOTS = float(os.getenv("TAKEOFF_SPEED_KNOTS", "40"))
GROUND_ALT_M = float(os.getenv("GROUND_ALT_M", "100"))

OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID", "").strip()
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()
OPENSKY_URL = "https://opensky-network.org/api/states/all"
ADSBLOL_URL = "https://api.adsb.lol/v2/hex/{icao24}"
HTTP_TIMEOUT = httpx.Timeout(25.0, connect=12.0)
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

# Frankfurt search box. Once a callsign is found, its ICAO24 is locked and tracked globally.
FRA_BBOX = {"lamin": 48.5, "lamax": 51.5, "lomin": 6.5, "lomax": 10.5}
PRESETS = {
    "✈️ D-AIXA – Testflug": {
        "label": "D-AIXA", "mode": "icao24", "query": "3c6701", "aliases": "3c6701,DLH712"
    },
}

lock = threading.RLock()
token_lock = threading.Lock()
_token: Optional[str] = None
_token_expiry: Optional[datetime] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                label TEXT NOT NULL, mode TEXT NOT NULL, query TEXT NOT NULL,
                aliases TEXT NOT NULL, locked_icao24 TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK(id=1),
                icao24 TEXT, tail TEXT, aircraft_type TEXT, callsign TEXT,
                latitude REAL, longitude REAL, baro_altitude REAL, geo_altitude REAL,
                velocity REAL, true_track REAL, vertical_rate REAL, on_ground INTEGER,
                origin_country TEXT, squawk TEXT, has_signal INTEGER, in_flight INTEGER,
                last_seen TEXT, last_poll TEXT, last_position TEXT, auth_mode TEXT, message TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL, timestamp TEXT NOT NULL,
                latitude REAL, longitude REAL, altitude REAL, velocity REAL, note TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
                altitude REAL, true_track REAL, velocity REAL
            );
            """
        )
        if not conn.execute("SELECT id FROM settings WHERE id=1").fetchone():
            conn.execute(
                "INSERT INTO settings VALUES(1,?,?,?,?,?,?)",
                ("D-AIXA", "icao24", "3c6701", "3c6701,DLH712", "3c6701", now_iso()),
            )
        if not conn.execute("SELECT id FROM state WHERE id=1").fetchone():
            conn.execute(
                """INSERT INTO state
                (id,icao24,tail,aircraft_type,has_signal,in_flight,last_poll,auth_mode,message)
                VALUES(1,?,?,?,?,?,?,?,?)""",
                ("3c6701", "D-AIXA", "Aircraft", 0, 0, now_iso(), "anonymous", "Noch keine Abfrage"),
            )
        # Testbetrieb: vorhandene Datenbank ebenfalls fest auf D-AIXA umstellen.
        conn.execute(
            "UPDATE settings SET label=?,mode=?,query=?,aliases=?,locked_icao24=?,updated_at=? WHERE id=1",
            ("D-AIXA", "icao24", "3C6701", "3c6701,DLH712", "3c6701", now_iso()),
        )
        conn.execute(
            "UPDATE state SET icao24=?,tail=? WHERE id=1",
            ("3c6701", "D-AIXA"),
        )


def create_beep() -> None:
    if BEEP_PATH.exists():
        return
    sample_rate, duration, frequency, amplitude = 44100, 0.55, 880.0, 14000
    frames = bytearray()
    for i in range(int(sample_rate * duration)):
        fade = min(1.0, i / 2200, (sample_rate * duration - i) / 2200)
        value = int(amplitude * fade * math.sin(2 * math.pi * frequency * i / sample_rate))
        frames += value.to_bytes(2, "little", signed=True)
    with wave.open(str(BEEP_PATH), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate); wav.writeframes(frames)


def load_settings() -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id=1").fetchone()
    return dict(row)


def load_state() -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM state WHERE id=1").fetchone()
    return dict(row) if row else {}


def save_state(state: dict) -> None:
    fields = ["icao24", "tail", "aircraft_type", "callsign", "latitude", "longitude",
              "baro_altitude", "geo_altitude", "velocity", "true_track", "vertical_rate",
              "on_ground", "origin_country", "squawk", "has_signal", "in_flight",
              "last_seen", "last_poll", "last_position", "auth_mode", "message"]
    values = [state.get(f) for f in fields]
    with db() as conn:
        conn.execute(f"UPDATE state SET {','.join(f'{f}=?' for f in fields)} WHERE id=1", values)


def normalize(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def looks_like_icao24(value: str) -> bool:
    value = normalize(value).lower()
    return len(value) == 6 and all(ch in "0123456789abcdef" for ch in value)


def set_target(label: str, mode: str, query: str, aliases: str) -> None:
    query = normalize(query)
    locked = query.lower() if mode == "icao24" else None
    with lock, db() as conn:
        conn.execute(
            "UPDATE settings SET label=?,mode=?,query=?,aliases=?,locked_icao24=?,updated_at=? WHERE id=1",
            (label, mode, query, aliases, locked, now_iso()),
        )
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM trail")
        conn.execute(
            """UPDATE state SET icao24=?,tail=?,callsign=NULL,latitude=NULL,longitude=NULL,
            baro_altitude=NULL,geo_altitude=NULL,velocity=NULL,true_track=NULL,vertical_rate=NULL,
            on_ground=NULL,origin_country=NULL,squawk=NULL,has_signal=0,in_flight=0,last_seen=NULL,
            last_poll=?,last_position=NULL,auth_mode='anonymous',message=? WHERE id=1""",
            (locked, label, now_iso(), f"Ziel auf {label} umgestellt"),
        )


def add_event(event_type: str, state: dict, note: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO events(type,timestamp,latitude,longitude,altitude,velocity,note) VALUES(?,?,?,?,?,?,?)",
            (event_type, now_iso(), state.get("latitude"), state.get("longitude"),
             state.get("baro_altitude"), state.get("velocity"), note),
        )
        return int(cur.lastrowid)


def add_trail(state: dict) -> None:
    if state.get("latitude") is None or state.get("longitude") is None:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO trail(timestamp,latitude,longitude,altitude,true_track,velocity) VALUES(?,?,?,?,?,?)",
            (state.get("last_poll") or now_iso(), state["latitude"], state["longitude"],
             state.get("baro_altitude"), state.get("true_track"), state.get("velocity")),
        )
        conn.execute("DELETE FROM trail WHERE id NOT IN (SELECT id FROM trail ORDER BY id DESC LIMIT 1000)")


def get_token(force: bool = False) -> Optional[str]:
    global _token, _token_expiry
    if not OPENSKY_CLIENT_ID or not OPENSKY_CLIENT_SECRET:
        return None
    with token_lock:
        now = datetime.now(timezone.utc)
        if not force and _token and _token_expiry and now < _token_expiry:
            return _token
        response = httpx.post(TOKEN_URL, data={
            "grant_type": "client_credentials", "client_id": OPENSKY_CLIENT_ID,
            "client_secret": OPENSKY_CLIENT_SECRET,
        }, timeout=25)
        response.raise_for_status()
        payload = response.json()
        _token = payload["access_token"]
        _token_expiry = now + timedelta(seconds=max(60, int(payload.get("expires_in", 1800)) - 30))
        return _token


def adsblol_request(icao24: str) -> list[dict]:
    """Primäre Datenquelle: ADSB.lol, ADS-B-Exchange-v2-kompatibles Format."""
    headers = {"User-Agent": "Warnherr-Aircraft-Tracker-Gradio/3.3"}
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            response = httpx.get(
                ADSBLOL_URL.format(icao24=icao24.lower()),
                headers=headers,
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.json().get("ac") or []
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
    if last_error:
        raise last_error
    return []


def adsblol_to_state(ac: dict) -> dict:
    altitude = ac.get("alt_baro")
    on_ground = altitude == "ground"
    if isinstance(altitude, (int, float)):
        baro_altitude = float(altitude) / 3.28084
    else:
        baro_altitude = None
    geom = ac.get("alt_geom")
    geo_altitude = float(geom) / 3.28084 if isinstance(geom, (int, float)) else None
    gs = ac.get("gs")
    velocity = float(gs) / 1.94384 if isinstance(gs, (int, float)) else None
    vr = ac.get("baro_rate") if ac.get("baro_rate") is not None else ac.get("geom_rate")
    vertical_rate = float(vr) / 196.8504 if isinstance(vr, (int, float)) else None
    return {
        "icao24": (ac.get("hex") or "").lower() or None,
        "callsign": (ac.get("flight") or "").strip() or None,
        "origin_country": None,
        "longitude": ac.get("lon"), "latitude": ac.get("lat"),
        "baro_altitude": baro_altitude, "geo_altitude": geo_altitude,
        "on_ground": on_ground, "velocity": velocity,
        "true_track": ac.get("track"), "vertical_rate": vertical_rate,
        "squawk": ac.get("squawk"),
        "registration": ac.get("r"), "aircraft_type": ac.get("t"),
    }


def opensky_request(params: dict) -> tuple[list, str]:
    headers = {"User-Agent": "Warnherr-Aircraft-Tracker-Gradio/3.3"}
    auth_mode = "anonymous"
    token = get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        auth_mode = "oauth2"
    response = httpx.get(OPENSKY_URL, params=params, headers=headers, timeout=35)
    if response.status_code == 401 and auth_mode == "oauth2":
        token = get_token(True)
        headers["Authorization"] = f"Bearer {token}"
        response = httpx.get(OPENSKY_URL, params=params, headers=headers, timeout=35)
    response.raise_for_status()
    return response.json().get("states") or [], auth_mode


def row_to_state(row: list) -> dict:
    return {
        "icao24": row[0], "callsign": (row[1] or "").strip() or None,
        "origin_country": row[2], "longitude": row[5], "latitude": row[6],
        "baro_altitude": row[7], "on_ground": row[8], "velocity": row[9],
        "true_track": row[10], "vertical_rate": row[11], "geo_altitude": row[13],
        "squawk": row[14],
    }


def fetch_aircraft() -> tuple[Optional[dict], str, str]:
    """ADSB.lol zuerst, OpenSky nur als Reserve."""
    settings = load_settings()
    label = settings["label"]
    locked = (settings.get("locked_icao24") or "").lower()

    adsblol_error = None
    if locked:
        try:
            aircraft = adsblol_request(locked)
            if aircraft:
                raw = adsblol_to_state(aircraft[0])
                return raw, "ADSB.lol", "Live-Daten über ADSB.lol empfangen"
        except Exception as exc:
            adsblol_error = type(exc).__name__

    # Reservequelle OpenSky
    try:
        if locked:
            rows, auth = opensky_request({"icao24": locked})
            if rows:
                return row_to_state(rows[0]), f"OpenSky ({auth})", "Fallback-Live-Daten über OpenSky empfangen"
            msg = f"Aktuell kein ADS-B-Signal für {label} (ICAO24 {locked})"
            if adsblol_error:
                msg += f" · ADSB.lol: {adsblol_error}"
            return None, "ADSB.lol → OpenSky", msg

        aliases = {normalize(x) for x in settings.get("aliases", "").split(",") if normalize(x)}
        aliases.add(normalize(settings.get("query", "")))
        rows, auth = opensky_request(FRA_BBOX)
        match = next((row for row in rows if normalize((row[1] or "").strip()) in aliases), None)
        if not match:
            shown = "/".join(sorted(a for a in aliases if a)) or settings.get("query", "")
            return None, f"OpenSky ({auth})", f"{shown} im Suchgebiet Frankfurt noch nicht gefunden"
        raw = row_to_state(match)
        with db() as conn:
            conn.execute("UPDATE settings SET locked_icao24=?,updated_at=? WHERE id=1", (raw["icao24"], now_iso()))
        add_event("target_locked", raw, f"{label} gefunden – ICAO24 {raw['icao24']} gespeichert")
        return raw, f"OpenSky ({auth})", f"Callsign gefunden; ICAO24 {raw['icao24']} gespeichert"
    except httpx.HTTPStatusError as exc:
        return None, "ADSB.lol → OpenSky", f"Beide Quellen ohne Live-Daten · OpenSky HTTP {exc.response.status_code}"
    except Exception as exc:
        suffix = f" · ADSB.lol: {adsblol_error}" if adsblol_error else ""
        return None, "ADSB.lol → OpenSky", f"Datenquellen derzeit nicht erreichbar: {type(exc).__name__}{suffix}"


def derive_in_flight(raw: dict) -> bool:
    if raw.get("on_ground") is True:
        return False
    if raw.get("on_ground") is False:
        return True
    speed = (raw.get("velocity") or 0) * 1.94384
    altitude = raw.get("baro_altitude")
    return bool(speed > TAKEOFF_SPEED_KNOTS and (altitude is None or altitude > GROUND_ALT_M))


def poll_once() -> dict:
    with lock:
        previous = load_state()
        settings = load_settings()
        raw, auth_mode, message = fetch_aircraft()
        timestamp = now_iso()
        if raw is None:
            if previous.get("has_signal"):
                add_event("signal_lost", previous, "Signal verloren")
            previous.update({"tail": settings["label"], "has_signal": 0, "last_poll": timestamp,
                             "auth_mode": auth_mode, "message": message})
            save_state(previous)
            return previous

        flying = derive_in_flight(raw)
        lat, lon = raw.get("latitude"), raw.get("longitude")
        position = previous.get("last_position")
        if lat is not None and lon is not None:
            position = f"{lat:.4f}, {lon:.4f}"
        state = {
            "icao24": raw.get("icao24"), "tail": settings["label"], "aircraft_type": raw.get("aircraft_type") or "Aircraft",
            "callsign": raw.get("callsign"), "latitude": lat, "longitude": lon,
            "baro_altitude": raw.get("baro_altitude"), "geo_altitude": raw.get("geo_altitude"),
            "velocity": raw.get("velocity"), "true_track": raw.get("true_track"),
            "vertical_rate": raw.get("vertical_rate"),
            "on_ground": int(bool(raw.get("on_ground"))) if raw.get("on_ground") is not None else None,
            "origin_country": raw.get("origin_country"), "squawk": raw.get("squawk"),
            "has_signal": 1, "in_flight": int(flying), "last_seen": timestamp,
            "last_poll": timestamp, "last_position": position, "auth_mode": auth_mode, "message": message,
        }
        if not previous.get("has_signal"):
            add_event("signal_available", state, "Signal verfügbar")
        if not previous.get("in_flight") and flying:
            add_event("takeoff", state, f"{settings['label']} ist in der Luft")
        if previous.get("in_flight") and not flying:
            add_event("landing", state, f"{settings['label']} ist gelandet")
        add_trail(state)
        save_state(state)
        return state


def latest_event_id() -> int:
    with db() as conn:
        return int(conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])


def format_time(value: Optional[str]) -> str:
    if not value:
        return "–"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return value


def fmt(value, factor=1.0, unit="", digits=0) -> str:
    return "–" if value is None else f"{float(value) * factor:.{digits}f} {unit}".strip()


def build_map() -> go.Figure:
    settings = load_settings()
    with db() as conn:
        rows = list(reversed(conn.execute("SELECT * FROM trail ORDER BY id DESC LIMIT 300").fetchall()))
    fig = go.Figure()
    if rows:
        lats, lons = [r["latitude"] for r in rows], [r["longitude"] for r in rows]
        fig.add_trace(go.Scattermap(lat=lats, lon=lons, mode="lines+markers", marker={"size": 7},
                                    line={"width": 3}, name="Flugspur",
                                    text=[format_time(r["timestamp"]) for r in rows],
                                    hovertemplate="%{text}<br>%{lat:.4f}, %{lon:.4f}<extra></extra>"))
        center, zoom = {"lat": lats[-1], "lon": lons[-1]}, 7
    else:
        center, zoom = {"lat": 50.0379, "lon": 8.5622}, 6
    fig.update_layout(map={"style": "open-street-map", "center": center, "zoom": zoom}, height=430,
                      margin={"l": 0, "r": 0, "t": 35, "b": 0},
                      title=f"{settings['label']} – Position und Flugspur", showlegend=False)
    return fig


def build_events() -> pd.DataFrame:
    labels = {"takeoff": "🛫 Start", "landing": "🛬 Landung", "signal_lost": "📡 Signal verloren",
              "signal_available": "✅ Signal verfügbar", "target_locked": "🎯 Ziel gefunden", "test": "🔔 Test"}
    with db() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50").fetchall()
    data = [{"Zeit": format_time(r["timestamp"]), "Ereignis": labels.get(r["type"], r["type"]),
             "Hinweis": r["note"], "Höhe": fmt(r["altitude"], 3.28084, "ft"),
             "Tempo": fmt(r["velocity"], 1.94384, "kt")} for r in rows]
    return pd.DataFrame(data, columns=["Zeit", "Ereignis", "Hinweis", "Höhe", "Tempo"])


def target_summary() -> str:
    s = load_settings()
    lock_text = f" · gesperrte ICAO24: `{s['locked_icao24']}`" if s.get("locked_icao24") else " · wartet auf Erkennung bei Frankfurt"
    return f"**Aktuelles Ziel:** {s['label']} · Modus: {s['mode']}{lock_text}"


def dashboard(last_seen_event: int, force_poll: bool = False):
    state = poll_once() if force_poll else load_state()
    settings = load_settings()
    status = "🟢 IN DER LUFT" if state.get("in_flight") else ("🟡 AM BODEN" if state.get("has_signal") else "⚫ KEIN SIGNAL")
    details = f"""
### {status}

**Flugzeug:** {settings['label']} · **ICAO24:** `{state.get('icao24') or settings.get('locked_icao24') or 'wird gesucht'}` · **Callsign:** {state.get('callsign') or '–'}  
**Signal:** {'✅ vorhanden' if state.get('has_signal') else '❌ aktuell nicht vorhanden'} · **Datenquelle:** {state.get('auth_mode') or '–'}  
**Höhe:** {fmt(state.get('baro_altitude'), 3.28084, 'ft')} · **Geschwindigkeit:** {fmt(state.get('velocity'), 1.94384, 'kt')}  
**Kurs:** {fmt(state.get('true_track'), 1, '°')} · **Steigen/Sinken:** {fmt(state.get('vertical_rate'), 196.8504, 'ft/min')}  
**Position:** {state.get('last_position') or '–'}  
**Letzte Abfrage:** {format_time(state.get('last_poll'))}  

_{state.get('message') or ''}_
"""
    event_id = latest_event_id()
    return target_summary(), details, build_map(), build_events(), event_id


def first_load():
    return dashboard(latest_event_id(), False)


def timer_refresh(last_seen_event: int):
    return dashboard(last_seen_event or 0, False)


def manual_refresh(last_seen_event: int):
    return dashboard(last_seen_event or 0, True)


def apply_target(preset: str, last_seen_event: int):
    config = PRESETS[preset]
    set_target(config["label"], config["mode"], config["query"], config["aliases"])
    return dashboard(0, True)


def rescan_callsign(last_seen_event: int):
    with db() as conn:
        settings = load_settings()
        if settings["mode"] != "callsign":
            gr.Info("Das aktuelle Ziel verwendet bereits eine feste ICAO24.")
        else:
            conn.execute("UPDATE settings SET locked_icao24=NULL,updated_at=? WHERE id=1", (now_iso(),))
            conn.execute("DELETE FROM trail")
    return dashboard(last_seen_event or 0, True)


def test_alarm():
    add_event("test", load_state(), "Tonalarm manuell getestet")
    return str(BEEP_PATH)


def clear_events():
    with db() as conn:
        conn.execute("DELETE FROM events")
    return build_events(), 0


init_db()
create_beep()

@spaces.GPU(duration=1)
def zerogpu_startup_probe() -> str:
    return "ZeroGPU ready"


def polling_loop() -> None:
    while True:
        try:
            poll_once()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


threading.Thread(target=polling_loop, name="aircraft-poller", daemon=True).start()

CSS = """
.gradio-container {max-width: 1100px !important;}
#title {text-align:center;}
footer {display:none !important;}
"""

with gr.Blocks(title="D-AIXA Aircraft Tracking Test V3.3") as demo:
    gr.Markdown(f"# ✈️ D-AIXA Tracking-Test V3.3\nADSB.lol → OpenSky-Fallback · ICAO24 `3C6701` · Abfrage alle {POLL_INTERVAL} Sekunden", elem_id="title")
    last_event = gr.State(0)

    with gr.Group():
        gr.Markdown("## Testflugzeug")
        preset = gr.Dropdown(
            list(PRESETS), value="✈️ D-AIXA – Testflug",
            label="Fest eingestelltes Ziel", interactive=False
        )
        apply_btn = gr.Button("🎯 D-AIXA neu laden", variant="primary")
        target_md = gr.Markdown()

    with gr.Row():
        refresh_btn = gr.Button("🔄 Jetzt prüfen", variant="primary")
        alarm_btn = gr.Button("🔔 Ton aktivieren / testen")
        clear_btn = gr.Button("🧹 Ereignisse löschen")

    status_md = gr.Markdown()
    map_plot = gr.Plot()
    gr.Markdown("## Ereignisprotokoll")
    event_table = gr.Dataframe(interactive=False, wrap=True)
    timer = gr.Timer(value=max(15, min(POLL_INTERVAL, 60)), active=True)

    outputs = [target_md, status_md, map_plot, event_table, last_event]
    demo.load(first_load, outputs=outputs)
    timer.tick(timer_refresh, inputs=[last_event], outputs=outputs)
    refresh_btn.click(manual_refresh, inputs=[last_event], outputs=outputs)
    apply_btn.click(apply_target, inputs=[preset, last_event], outputs=outputs)
    alarm_btn.click(
        fn=None,
        js="""() => {
            const AudioCtx = window.AudioContext || window.webkitAudioContext;
            const ctx = new AudioCtx();
            const gain = ctx.createGain();
            gain.gain.setValueAtTime(0.0001, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.45, ctx.currentTime + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.85);
            gain.connect(ctx.destination);
            [880, 660, 880].forEach((freq, index) => {
                const osc = ctx.createOscillator();
                osc.type = 'sine';
                osc.frequency.value = freq;
                osc.connect(gain);
                const start = ctx.currentTime + index * 0.22;
                osc.start(start);
                osc.stop(start + 0.18);
            });
            setTimeout(() => ctx.close(), 1200);
            return [];
        }"""
    )
    clear_btn.click(clear_events, outputs=[event_table, last_event])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(
        server_name="0.0.0.0", server_port=7860, show_error=True, ssr_mode=False, css=CSS
    )

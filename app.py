from __future__ import annotations


import math
import os
import sqlite3
import threading
import time
import wave
import httpx
import spaces
import gradio as gr
import plotly.graph_objects as go
import pandas as pd

from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent

gr.set_static_paths(paths=[ROOT])

DB_PATH = Path(os.getenv("DB_PATH", ROOT / "tracker.db"))
BEEP_PATH = ROOT / "alarm.wav"

POLL_INTERVAL = max(
    15,
    min(600, int(os.getenv("POLL_INTERVAL_SECONDS", "60")))
)

HERO_IMAGE_URL = os.getenv("HERO_IMAGE_URL", "/gradio_api/file=hero.png").strip()
EMBEDDED_HERO_IMAGE = ""

TAKEOFF_SPEED_KNOTS = float(
    os.getenv("TAKEOFF_SPEED_KNOTS", "40")
)

GROUND_ALT_M = float(
    os.getenv("GROUND_ALT_M", "100")
)

APP_VERSION = "PRESENTATION EDITION V2.1"

OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID", "").strip()
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()
OPENSKY_URL = "https://opensky-network.org/api/states/all"
ADSBLOL_URL = "https://api.adsb.lol/v2/hex/{icao24}"
HTTP_TIMEOUT = httpx.Timeout(25.0, connect=12.0)
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

# Frankfurt search box. Once a callsign is found, its ICAO24 is locked and tracked globally.
FRA_BBOX = {"lamin": 48.5, "lamax": 51.5, "lomin": 6.5, "lomax": 10.5}
PRESETS = {
    "🛩️ Frances Dell – OO-NZW": {
        "label": "OO-NZW", "mode": "icao24", "query": "44bb57", "aliases": "44bb57,OO-NZW"
    },
    "✈️ D-AIXA – Testflug": {
        "label": "D-AIXA", "mode": "icao24", "query": "3c6701", "aliases": "3c6701,DLH712"
    },
}

AIRCRAFT_PROFILE = {
    "name": "FRANCES DELL",
    "manufacturer": "North American Aviation",
    "type": "P-51D Mustang",
    "registration": "OO-NZW",
    "icao24": "44BB57",
    "year": "1944",
    "category": "Historic Warbird",
    "engine": "Packard-built Rolls-Royce Merlin V-1650",
    "mission": "Living history · Airshow operations · Heritage flight",
    "legacy": (
        "Frances Dell verbindet die Geschichte der P-51 Mustang mit dem heutigen Flugbetrieb. "
        "Project Merlin präsentiert sie nicht als Datensatz, sondern als lebendiges Luftfahrtdenkmal."
    ),
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
                ("OO-NZW", "icao24", "44bb57", "44bb57,OO-NZW", "44bb57", now_iso()),
            )
        if not conn.execute("SELECT id FROM state WHERE id=1").fetchone():
            conn.execute(
                """INSERT INTO state
                (id,icao24,tail,aircraft_type,has_signal,in_flight,last_poll,auth_mode,message)
                VALUES(1,?,?,?,?,?,?,?,?)""",
                ("44bb57", "OO-NZW", "P-51D Mustang", 0, 0, now_iso(), "anonymous", "Noch keine Abfrage"),
            )
        # Project Merlin startet standardmäßig mit Frances Dell.
        conn.execute(
            "UPDATE settings SET label=?,mode=?,query=?,aliases=?,locked_icao24=?,updated_at=? WHERE id=1",
            ("OO-NZW", "icao24", "44BB57", "44bb57,OO-NZW", "44bb57", now_iso()),
        )
        conn.execute(
            "UPDATE state SET icao24=?,tail=?,aircraft_type=? WHERE id=1",
            ("44bb57", "OO-NZW", "P-51D Mustang"),
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
            "icao24": raw.get("icao24"), "tail": settings["label"], "aircraft_type": "Aircraft",
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
    fig.update_traces(line={"width": 4, "color": "#d8a95b"}, marker={"size": 8, "color": "#f4d49a"})
    fig.update_layout(
        map={"style": "carto-darkmatter", "center": center, "zoom": zoom},
        height=680, margin={"l": 0, "r": 0, "t": 54, "b": 0},
        title={"text": f"{settings['label']} · LIVE POSITION & FLIGHT TRAIL", "x": .02},
        showlegend=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#f4f1e8", "family": "Arial"},
    )
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




def build_prologue() -> str:
    return """
    <div class="merlin-prologue" aria-hidden="true">
      <div class="hangar-light"></div>
      <div class="hangar-door door-left"></div>
      <div class="hangar-door door-right"></div>
      <div class="prologue-aircraft">
        <div class="propeller"><i></i><i></i></div>
        <div class="aircraft-name">FRANCES DELL</div>
      </div>
      <div class="prologue-copy">
        <small>PROJECT MERLIN</small>
        <h1>Welcome aboard.</h1>
        <p>Preparing Flight Operations...</p>
      </div>
    </div>
    """


def build_hero(state: dict, settings: dict) -> str:
    if state.get("in_flight"):
        mode_class = "airborne"
        eyebrow = "LIVE FLIGHT OPERATIONS"
        status = "IN DER LUFT"
        statement = "Frances Dell is flying."
    elif state.get("has_signal"):
        mode_class = "hangar"
        eyebrow = "AIRCRAFT STATUS"
        status = "AM BODEN"
        statement = "Frances Dell is resting peacefully in the hangar."
    else:
        mode_class = "hangar"
        eyebrow = "HANGAR STATUS"
        status = "RESTING IN HANGAR"
        statement = "Frances Dell is resting peacefully in the hangar."

    hero_image = HERO_IMAGE_URL
    last_poll = format_time(state.get("last_poll"))
    callsign = state.get("callsign") or "–"
    icao24 = state.get("icao24") or settings.get("locked_icao24") or "–"

    return f"""
<div style="background:red;color:white;font-size:48px;padding:40px;z-index:99999;position:relative;">
TEST HERO WIRD ANGEZEIGT
</div>

<section class="merlin-hero {mode_class}">
      <img class="merlin-hero__image" src="{hero_image}" alt="Frances Dell P-51D Mustang" loading="eager" decoding="async">
      <div class="merlin-hero__veil"></div>
      <div class="merlin-hero__content">
        <div class="merlin-brand">FRANCES DELL <span>·</span> WHEN HISTORY COMES ALIVE</div>
        <div class="merlin-version">{APP_VERSION}</div>
        <div class="merlin-eyebrow"><span class="status-dot"></span>{eyebrow}</div>
        <h1>FRANCES DELL</h1>
        <p class="merlin-subtitle">North American P-51D Mustang · Built 1944</p>
        <p class="merlin-statement">{statement}</p>
        <div class="merlin-scrollcue"><span></span> ENTER FLIGHT EXPERIENCE</div>
        <div class="merlin-statusbar">
          <div><small>STATUS</small><strong>{status}</strong></div>
          <div><small>CALLSIGN</small><strong>{callsign}</strong></div>
          <div><small>ICAO24</small><strong>{icao24.upper()}</strong></div>
          <div><small>LETZTE ABFRAGE</small><strong>{last_poll}</strong></div>
        </div>
      </div>
    </section>
    """

def build_aircraft_passport() -> str:
    profile = AIRCRAFT_PROFILE
    return f"""
    <section class="merlin-passport">
      <div class="merlin-passport__intro">
        <div class="merlin-kicker">AIRCRAFT PASSPORT</div>
        <h2>{profile['name']}</h2>
        <p>{profile['legacy']}</p>
        <div class="merlin-passport__mission">{profile['mission']}</div>
      </div>
      <div class="merlin-passport__grid">
        <article><small>REGISTRATION</small><strong>{profile['registration']}</strong></article>
        <article><small>ICAO24</small><strong>{profile['icao24']}</strong></article>
        <article><small>AIRCRAFT</small><strong>{profile['type']}</strong></article>
        <article><small>MANUFACTURER</small><strong>{profile['manufacturer']}</strong></article>
        <article><small>YEAR</small><strong>{profile['year']}</strong></article>
        <article><small>CLASS</small><strong>{profile['category']}</strong></article>
        <article class="wide"><small>POWERPLANT</small><strong>{profile['engine']}</strong></article>
      </div>
    </section>
    """



def build_mission_center(state: dict, settings: dict) -> str:
    if state.get("in_flight"):
        mode, status, status_de = "airborne", "AIRBORNE", "Frances Dell ist in der Luft"
    elif state.get("has_signal"):
        mode, status, status_de = "ground", "ON GROUND", "Frances Dell befindet sich am Boden"
    else:
        mode, status, status_de = "ground", "RESTING IN HANGAR", "Waiting for the next mission"

    altitude = fmt(state.get("baro_altitude"), 3.28084, "ft")
    speed = fmt(state.get("velocity"), 1.94384, "kt")
    heading = fmt(state.get("true_track"), 1, "°")
    vertical = fmt(state.get("vertical_rate"), 196.8504, "ft/min")
    position = state.get("last_position") or "–"
    callsign = state.get("callsign") or "–"
    icao24 = (state.get("icao24") or settings.get("locked_icao24") or "–").upper()
    last_poll = format_time(state.get("last_poll"))
    message = state.get("message") or ""

    return f"""
    <section class="mission-center {mode}">
      <div class="mission-center__header">
        <div><div class="merlin-kicker">LIVE MISSION CENTER</div><h2>Flight Operations</h2><p>{status_de}</p></div>
        <div class="mission-badge"><span></span>{status}</div>
      </div>
      <div class="mission-grid">
        <article class="mission-card primary"><small>ALTITUDE</small><strong>{altitude}</strong><em>BAROMETRIC</em></article>
        <article class="mission-card"><small>GROUND SPEED</small><strong>{speed}</strong><em>LIVE DATA</em></article>
        <article class="mission-card"><small>HEADING</small><strong>{heading}</strong><em>TRUE TRACK</em></article>
        <article class="mission-card"><small>VERTICAL SPEED</small><strong>{vertical}</strong><em>CLIMB / DESCENT</em></article>
      </div>
      <div class="mission-meta">
        <div><small>CALLSIGN</small><strong>{callsign}</strong></div><div><small>ICAO24</small><strong>{icao24}</strong></div>
        <div><small>LAST POSITION</small><strong>{position}</strong></div><div><small>LAST UPDATE</small><strong>{last_poll}</strong></div>
      </div>
      <div class="mission-message">{message}</div>
    </section>
    """


def build_timeline() -> str:
    labels = {"takeoff":"TAKEOFF","landing":"LANDING","signal_lost":"SIGNAL LOST","signal_available":"SIGNAL ACQUIRED","target_locked":"TARGET LOCKED","test":"SYSTEM TEST"}
    with db() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 8").fetchall()
    if not rows:
        return '<section class="mission-timeline"><div class="merlin-kicker">MISSION TIMELINE</div><div class="timeline-empty">Noch keine Flugereignisse gespeichert.</div></section>'
    items = []
    for row in rows:
        items.append(f'<article class="timeline-item"><div class="timeline-marker"></div><div><small>{format_time(row["timestamp"])}</small><strong>{labels.get(row["type"], row["type"].upper())}</strong><p>{row["note"]}</p></div></article>')
    return f'<section class="mission-timeline"><div class="merlin-kicker">MISSION TIMELINE</div><div class="timeline-list">{"".join(items)}</div></section>'


def build_footer() -> str:
    return f"""
    <footer class="merlin-footer">
      <div>
        <span class="merlin-footer__mark">M</span>
        <div><strong>PROJECT MERLIN</strong><small>FRANCES DELL · OO-NZW</small></div>
      </div>
      <p>Created with admiration for aviation.</p>
      <small>DRUDE FLIGHT OPERATIONS · {APP_VERSION}</small>
    </footer>
    """

def target_summary() -> str:
    s = load_settings()
    lock_text = f" · gesperrte ICAO24: `{s['locked_icao24']}`" if s.get("locked_icao24") else " · wartet auf Erkennung bei Frankfurt"
    return f"**Aktuelles Ziel:** {s['label']} · Modus: {s['mode']}{lock_text}"


def dashboard(last_seen_event: int, force_poll: bool = False):
    state = poll_once() if force_poll else load_state()
    settings = load_settings()
    event_id = latest_event_id()
    return (
        build_hero(state, settings), build_aircraft_passport(), build_mission_center(state, settings),
        target_summary(), build_map(), build_timeline(), build_events(), build_footer(), event_id,
    )


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


threading.Thread(target=polling_loop, name="adsb-poller", daemon=True).start()

CSS = """
:root{
  --bg:#070b10;
  --panel:#0e151d;
  --panel-2:#121b25;
  --line:rgba(255,255,255,.12);
  --text:#f4f1e8;
  --muted:#a9b1ba;
  --amber:#d8a95b;
  --green:#61d98a;
}
html{scroll-behavior:smooth}
body,.gradio-container{
  margin:0!important;
  background:#070b10!important;
  color:var(--text)!important;
}
.gradio-container{
  max-width:none!important;
  width:100%!important;
  padding:0!important;
}
footer{display:none!important}
.main,.wrap,.contain{max-width:none!important;width:100%!important}

/* Prologue */
.merlin-prologue{
  position:fixed;inset:0;z-index:9999;overflow:hidden;
  background:#030507;
  animation:prologueExit 5.2s cubic-bezier(.7,0,.2,1) forwards;
  pointer-events:auto;
}
.merlin-prologue::before{
  content:"";position:absolute;inset:0;
  background:
    radial-gradient(ellipse at 50% 82%,rgba(216,169,91,.12),transparent 42%),
    linear-gradient(180deg,#020304 0%,#06090c 70%,#0a0d10 100%);
}
.hangar-light{
  position:absolute;left:50%;top:8%;width:48vw;height:92vh;
  transform:translateX(-50%);
  clip-path:polygon(42% 0,58% 0,100% 100%,0 100%);
  background:linear-gradient(180deg,rgba(255,239,193,.18),rgba(216,169,91,.04) 58%,transparent);
  opacity:0;animation:hangarLight 4.8s ease forwards;
}
.hangar-door{
  position:absolute;top:0;width:51%;height:100%;
  background:repeating-linear-gradient(90deg,#0b0f13 0 3px,#11171d 3px 8px,#080c10 8px 11px);
  box-shadow:inset 0 0 80px rgba(0,0,0,.85);
  animation-duration:4.6s;animation-timing-function:cubic-bezier(.65,0,.2,1);animation-fill-mode:forwards;
}
.door-left{left:0;border-right:1px solid rgba(216,169,91,.18);animation-name:doorLeftOpen}
.door-right{right:0;border-left:1px solid rgba(216,169,91,.18);animation-name:doorRightOpen}
.prologue-copy{
  position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;color:var(--text);
  animation:copyFade 4.2s ease forwards;
}
.prologue-copy small{font-size:.65rem;letter-spacing:.34em;color:#c89f60;margin-bottom:20px}
.prologue-copy h1{
  margin:0!important;font:400 clamp(2.4rem,6vw,5.4rem)/1.05 Georgia,serif!important;
  letter-spacing:-.035em!important;color:#f5f0e6!important
}
.prologue-copy p{margin:18px 0 0;color:#9fa8b0;letter-spacing:.12em;font-size:.78rem}
.prologue-aircraft{
  position:absolute;left:50%;bottom:12%;transform:translateX(-50%);
  width:min(760px,84vw);height:96px;opacity:0;animation:aircraftReveal 4.5s ease forwards;
}
.prologue-aircraft::before{
  content:"";position:absolute;left:14%;right:10%;top:43%;height:18px;
  border-radius:80% 36% 40% 80%;background:linear-gradient(180deg,#353d44,#11171c 60%,#050708);
  box-shadow:0 10px 30px rgba(0,0,0,.7)
}
.prologue-aircraft::after{
  content:"";position:absolute;left:31%;top:31%;width:47%;height:9px;
  border-radius:60%;background:#171d22;transform:skewX(-18deg)
}
.aircraft-name{position:absolute;left:44%;top:24%;font:700 .56rem/1 Arial;letter-spacing:.18em;color:rgba(216,169,91,.72)}
.propeller{
  position:absolute;left:9.5%;top:2%;width:78px;height:78px;
  border:1px solid rgba(216,169,91,.25);border-radius:50%;
  animation:propellerSpin 1.2s linear 2.1s infinite
}
.propeller i{
  position:absolute;left:37px;top:4px;width:4px;height:70px;border-radius:50%;
  background:linear-gradient(transparent,rgba(216,169,91,.55),transparent)
}
.propeller i+i{transform:rotate(90deg)}
@keyframes doorLeftOpen{0%,39%{transform:none}88%,100%{transform:translateX(-96%)}}
@keyframes doorRightOpen{0%,39%{transform:none}88%,100%{transform:translateX(96%)}}
@keyframes hangarLight{0%,34%{opacity:0}55%{opacity:.55}100%{opacity:1}}
@keyframes aircraftReveal{0%,47%{opacity:0;transform:translate(-50%,18px)}72%,100%{opacity:.78;transform:translate(-50%,0)}}
@keyframes propellerSpin{to{transform:rotate(360deg)}}
@keyframes copyFade{0%{opacity:0;transform:translateY(12px)}18%,48%{opacity:1;transform:none}74%,100%{opacity:0;transform:translateY(-8px)}}
@keyframes prologueExit{0%,84%{opacity:1;visibility:visible}98%{opacity:0}100%{opacity:0;visibility:hidden;pointer-events:none}}

/* Full-screen hero */
.merlin-hero{
  position:relative;
  min-height:100svh;
  margin:0;
  overflow:hidden;
  background:#0a1016;
}
.merlin-hero__image{
  position:absolute;
  inset:0;
  z-index:0;
  width:100%;
  height:100%;
  display:block;
  object-fit:cover;
  object-position:center center;
  opacity:1!important;
  visibility:visible!important;
}
.merlin-hero::before{
  content:"";position:absolute;inset:0;z-index:1;pointer-events:none;
  background:linear-gradient(90deg,rgba(2,5,8,.78) 0%,rgba(2,5,8,.48) 38%,rgba(2,5,8,.05) 72%,rgba(2,5,8,.18) 100%);
}
.merlin-hero::after{
  content:"P-51D";position:absolute;z-index:1;right:3%;bottom:-7%;
  font:900 12rem/1 Arial,sans-serif;letter-spacing:-.08em;color:rgba(255,255,255,.025);
}
.merlin-hero__veil{
  position:absolute;inset:0;z-index:1;pointer-events:none;
  background:linear-gradient(180deg,rgba(0,0,0,.02),rgba(0,0,0,.10) 52%,rgba(3,6,9,.76) 100%);
}
.merlin-hero__content{
  position:relative;z-index:2;min-height:100svh;
  padding:clamp(34px,6vw,74px) clamp(28px,8vw,120px) clamp(30px,5vw,62px);
  display:flex;flex-direction:column;justify-content:flex-end;
}
.merlin-brand{
  position:absolute;top:34px;left:clamp(28px,8vw,120px);
  font-size:.72rem;letter-spacing:.22em;color:rgba(255,255,255,.62)
}
.merlin-brand span{color:var(--amber);margin:0 .55rem}
.merlin-version{
  position:absolute;top:34px;right:clamp(28px,8vw,120px);
  font-size:.58rem;letter-spacing:.18em;color:rgba(255,255,255,.42)
}
.merlin-eyebrow{
  display:flex;align-items:center;gap:.7rem;
  font-size:.78rem;letter-spacing:.24em;font-weight:700;color:var(--amber)
}
.status-dot{
  width:9px;height:9px;border-radius:50%;background:#8b9299;
  box-shadow:0 0 0 6px rgba(139,146,153,.1)
}
.airborne .status-dot{background:var(--green);box-shadow:0 0 0 6px rgba(97,217,138,.12),0 0 18px rgba(97,217,138,.8)}
.hangar .status-dot{background:var(--amber);box-shadow:0 0 0 6px rgba(216,169,91,.12),0 0 18px rgba(216,169,91,.55)}
.merlin-hero h1{
  max-width:1050px;margin:.35rem 0 0!important;
  font-size:clamp(4.7rem,11vw,10.8rem)!important;line-height:.88!important;
  letter-spacing:-.055em!important;font-weight:800!important;color:#f5f0e6!important;
  text-shadow:0 10px 40px rgba(0,0,0,.45)
}
.merlin-subtitle{
  margin:.8rem 0 0;font-size:clamp(.72rem,1.15vw,1rem);
  letter-spacing:.28em;text-transform:uppercase;color:rgba(255,255,255,.72)
}
.merlin-statement{
  margin:1.8rem 0 2.2rem;max-width:760px;
  font:italic clamp(1.15rem,2.1vw,1.8rem)/1.55 Georgia,serif;color:#eee8dc
}
.merlin-scrollcue{
  display:flex;align-items:center;gap:.75rem;margin:0 0 14px;
  font-size:.56rem;letter-spacing:.18em;color:rgba(255,255,255,.48)
}
.merlin-scrollcue span{display:block;width:34px;height:1px;background:linear-gradient(90deg,var(--amber),transparent)}
.merlin-statusbar{
  display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
  max-width:1120px;border-top:1px solid var(--line);
  background:rgba(4,8,12,.58);backdrop-filter:blur(14px)
}
.merlin-statusbar>div{padding:18px 22px;border-right:1px solid var(--line)}
.merlin-statusbar>div:last-child{border-right:0}
.merlin-statusbar small,.mission-card small,.mission-meta small,.merlin-passport article small{
  display:block;font-size:.62rem;letter-spacing:.18em;color:#8f9aa5
}
.merlin-statusbar strong{font-size:.92rem;color:#fff}

/* Content */
.content-shell{
  width:min(1320px,calc(100% - 48px));
  margin:0 auto;
}
.merlin-kicker{font-size:.68rem;letter-spacing:.24em;font-weight:800;color:var(--amber)}
.mission-center,.merlin-passport,.mission-timeline{
  margin:32px auto 0;padding:30px;border:1px solid var(--line);border-radius:24px;
  background:linear-gradient(145deg,rgba(18,25,33,.96),rgba(7,11,15,.98));
  box-shadow:0 22px 60px rgba(0,0,0,.28)
}
.mission-center__header{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;margin-bottom:24px}
.mission-center__header h2,.merlin-passport h2{
  margin:.45rem 0 .35rem!important;font-size:clamp(2rem,4vw,3.25rem)!important;
  letter-spacing:-.045em!important;color:#f5f0e6!important
}
.mission-center__header p,.merlin-passport__intro p{margin:0;color:var(--muted);line-height:1.7}
.mission-badge{
  display:flex;align-items:center;gap:.65rem;padding:12px 16px;
  border:1px solid var(--line);border-radius:999px;
  font-size:.7rem;font-weight:800;letter-spacing:.14em;color:#fff
}
.mission-badge span{width:9px;height:9px;border-radius:50%;background:#7f8890}
.mission-center.airborne .mission-badge span{background:var(--green);box-shadow:0 0 18px rgba(97,217,138,.8)}
.mission-center.ground .mission-badge span{background:var(--amber);box-shadow:0 0 18px rgba(216,169,91,.7)}
.mission-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.mission-card{
  min-height:142px;padding:20px;border:1px solid rgba(255,255,255,.09);border-radius:18px;
  background:linear-gradient(145deg,rgba(255,255,255,.065),rgba(255,255,255,.018));
  display:flex;flex-direction:column;justify-content:space-between
}
.mission-card.primary{background:linear-gradient(145deg,rgba(216,169,91,.2),rgba(255,255,255,.025));border-color:rgba(216,169,91,.35)}
.mission-card strong{font-size:clamp(1.65rem,3vw,2.45rem);line-height:1;color:#fff}
.mission-card em{font-style:normal;font-size:.58rem;letter-spacing:.14em;color:#78838e}
.mission-meta{
  display:grid;grid-template-columns:1fr 1fr 1.5fr 1.4fr;
  margin-top:14px;border:1px solid var(--line);border-radius:16px;overflow:hidden
}
.mission-meta>div{padding:15px 16px;border-right:1px solid var(--line)}
.mission-meta>div:last-child{border-right:0}
.mission-meta strong{display:block;margin-top:6px;font-size:.82rem;color:#fff;overflow-wrap:anywhere}
.mission-message{margin-top:14px;padding:13px 16px;border-left:2px solid var(--amber);background:rgba(216,169,91,.07);color:#c8cfd5;font-size:.86rem}

.merlin-passport{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1.45fr);gap:24px}
.merlin-passport__intro{padding:8px 18px 8px 8px;border-right:1px solid var(--line)}
.merlin-passport__mission{margin-top:20px;padding-top:16px;border-top:1px solid var(--line);font-size:.72rem;line-height:1.6;letter-spacing:.1em;text-transform:uppercase;color:#e7d4b3}
.merlin-passport__grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.merlin-passport article{
  min-height:90px;padding:17px 18px;border:1px solid rgba(255,255,255,.08);border-radius:16px;
  background:linear-gradient(145deg,rgba(255,255,255,.055),rgba(255,255,255,.018));
  display:flex;flex-direction:column;justify-content:space-between
}
.merlin-passport article.wide{grid-column:1/-1}
.merlin-passport article strong{font-size:1rem;line-height:1.25;color:#fff}

.merlin-section-title{
  width:min(1320px,calc(100% - 48px));margin:48px auto 8px!important;
  font-size:.72rem!important;letter-spacing:.23em!important;color:var(--amber)!important
}
.block.gradio-plot{
  width:min(1320px,calc(100% - 48px))!important;
  margin:0 auto!important;padding:12px!important;min-height:720px!important;
  border:1px solid var(--line)!important;border-radius:24px!important;
  background:#070b10!important
}
.gradio-plot,.plot-container,.js-plotly-plot,.js-plotly-plot .plotly,
.js-plotly-plot .svg-container,.js-plotly-plot .main-svg{
  min-height:680px!important;background:#070b10!important;background-color:#070b10!important
}
.mission-timeline{margin-bottom:26px}
.timeline-list{margin-top:18px}
.timeline-item{position:relative;display:grid;grid-template-columns:18px 1fr;gap:14px;padding:0 0 22px}
.timeline-item:not(:last-child)::before{content:"";position:absolute;left:5px;top:12px;bottom:0;width:1px;background:linear-gradient(var(--amber),rgba(255,255,255,.08))}
.timeline-marker{z-index:1;width:11px;height:11px;margin-top:4px;border:2px solid var(--amber);border-radius:50%;background:#091018}
.timeline-item small{display:block;font-size:.62rem;color:#7e8994}
.timeline-item strong{display:block;margin:.25rem 0;color:#fff;font-size:.86rem;letter-spacing:.08em}
.timeline-item p{margin:0;color:var(--muted);font-size:.86rem}
.timeline-empty{margin-top:16px;color:var(--muted)}

.merlin-footer{
  width:min(1320px,calc(100% - 48px));margin:34px auto 12px;padding:24px 4px 10px;
  border-top:1px solid var(--line);display:grid;grid-template-columns:1fr auto;align-items:center;gap:16px;color:var(--muted)
}
.merlin-footer>div{display:flex;align-items:center;gap:12px}
.merlin-footer__mark{display:grid;place-items:center;width:38px;height:38px;border:1px solid rgba(216,169,91,.45);border-radius:50%;font:700 1rem Georgia;color:var(--amber)}
.merlin-footer strong,.merlin-footer small{display:block}
.merlin-footer strong{font-size:.72rem;letter-spacing:.16em;color:#f5f0e6}
.merlin-footer small{margin-top:3px;font-size:.56rem;letter-spacing:.14em}
.merlin-footer p{margin:0;font:italic .9rem Georgia;color:#c9c1b2}
.merlin-footer>small{grid-column:1/-1;text-align:right;opacity:.55}

.merlin-ops-accordion{
  width:min(1320px,calc(100% - 48px))!important;
  margin:36px auto 30px!important;
}
.gr-group,.block,.form{border-color:var(--line)!important}
button.primary{background:linear-gradient(135deg,#b88135,#e0b76f)!important;color:#111!important;border:0!important;font-weight:700!important}

@media(max-width:760px){
  .merlin-prologue .prologue-copy{padding:24px}
  .prologue-aircraft{bottom:15%;height:74px}
  .propeller{width:58px;height:58px}
  .propeller i{left:27px;height:52px}
  .merlin-hero__content{padding:36px 22px 24px}
  .merlin-brand{left:22px;top:24px;font-size:.58rem}
  .merlin-version{top:48px;left:22px;right:auto}
  .merlin-hero h1{font-size:clamp(3.6rem,18vw,5.2rem)!important}
  .merlin-statusbar{grid-template-columns:1fr 1fr}
  .merlin-statusbar>div{border-bottom:1px solid var(--line)}
  .mission-grid{grid-template-columns:1fr 1fr}
  .mission-meta{grid-template-columns:1fr 1fr}
  .mission-center{padding:20px}
  .mission-center__header{flex-direction:column}
  .merlin-passport{grid-template-columns:1fr;padding:20px}
  .merlin-passport__intro{padding:4px 4px 20px;border-right:0;border-bottom:1px solid var(--line)}
  .merlin-passport__grid{grid-template-columns:1fr 1fr}
  .merlin-passport article{min-height:82px;padding:14px}
  .content-shell,.merlin-section-title,.block.gradio-plot,.merlin-footer,.merlin-ops-accordion{
    width:calc(100% - 22px)!important
  }
  .block.gradio-plot{min-height:520px!important}
  .gradio-plot,.plot-container,.js-plotly-plot{min-height:490px!important}
  .merlin-footer{grid-template-columns:1fr}
  .merlin-footer p{padding-left:50px}
  .merlin-footer>small{text-align:left;padding-left:50px}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}
}
"""

# Prime the visible components immediately, then refresh them on load/timer.
_initial_state = load_state()
_initial_settings = load_settings()

with gr.Blocks(title="Project Merlin – Frances Dell") as demo:
    gr.HTML(build_prologue())

    hero_html = gr.HTML(
        value=build_hero(_initial_state, _initial_settings),
        elem_id="merlin-hero-component",
    )
    last_event = gr.State(latest_event_id())

    mission_html = gr.HTML(
        value=build_mission_center(_initial_state, _initial_settings),
        elem_classes=["content-shell"],
    )

    gr.Markdown("### NAVIGATION TABLE", elem_classes=["merlin-section-title"])
    map_plot = gr.Plot(value=build_map())

    passport_html = gr.HTML(
        value=build_aircraft_passport(),
        elem_classes=["content-shell"],
    )
    timeline_html = gr.HTML(
        value=build_timeline(),
        elem_classes=["content-shell"],
    )
    footer_html = gr.HTML(value=build_footer())

    with gr.Accordion(
        "Operations Center · Test & Technik",
        open=False,
        elem_classes=["merlin-ops-accordion"],
    ):
        with gr.Row():
            refresh_btn = gr.Button("Live-Status aktualisieren", variant="primary")
            alarm_btn = gr.Button("Tonalarm testen")
            clear_btn = gr.Button("Ereignisse löschen")
        preset = gr.Dropdown(
            list(PRESETS),
            value="🛩️ Frances Dell – OO-NZW",
            label="Flugzeug",
            interactive=True,
        )
        apply_btn = gr.Button("Ausgewähltes Flugzeug laden")
        target_md = gr.Markdown(value=target_summary())
        with gr.Accordion("Technisches Ereignisprotokoll", open=False):
            event_table = gr.Dataframe(
                value=build_events(),
                interactive=False,
                wrap=True,
            )

    timer = gr.Timer(value=max(15, min(POLL_INTERVAL, 60)), active=True)

    outputs = [
        hero_html,
        passport_html,
        mission_html,
        target_md,
        map_plot,
        timeline_html,
        event_table,
        footer_html,
        last_event,
    ]

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
          gain.gain.setValueAtTime(.0001, ctx.currentTime);
          gain.gain.exponentialRampToValueAtTime(.45, ctx.currentTime + .02);
          gain.gain.exponentialRampToValueAtTime(.0001, ctx.currentTime + .85);
          gain.connect(ctx.destination);
          [880, 660, 880].forEach((freq, index) => {
            const osc = ctx.createOscillator();
            osc.type = "sine";
            osc.frequency.value = freq;
            osc.connect(gain);
            const start = ctx.currentTime + index * .22;
            osc.start(start);
            osc.stop(start + .18);
          });
          setTimeout(() => ctx.close(), 1200);
          return [];
        }""",
    )
    clear_btn.click(clear_events, outputs=[event_table, last_event])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(
        server_name="0.0.0.0",
        server_port=7860,
        show_error=True,
        ssr_mode=False,
        css=CSS,
        allowed_paths=[str(ROOT)],
    )

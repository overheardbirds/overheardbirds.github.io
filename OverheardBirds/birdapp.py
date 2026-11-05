#!/usr/bin/env python3
"""
Overheard Birds - a local website + database for your Haikubox.

Double-click "Open Overheard Birds.bat" (one folder up). This program:
  1. Pulls your detection history straight from the Haikubox API
     (no CSV exports needed) into a SQLite database: data/birds.db
  2. Looks up photos + a short description for every species (iNaturalist)
  3. Serves the website at http://localhost:8765 and opens your browser
  4. Re-syncs automatically every hour while the window stays open

Only uses the Python standard library - nothing to pip install.
"""

import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
WEB_DIR = APP_DIR / "web"
IMPORT_DIR = APP_DIR / "imports"
CONFIG_FILE = APP_DIR / "config.json"
DB_FILE = DATA_DIR / "birds.db"
if not DB_FILE.exists() and (DATA_DIR / "yard_birds.db").exists():
    (DATA_DIR / "yard_birds.db").rename(DB_FILE)  # keep data from the earlier version

DEFAULT_CONFIG = {
    "serial": "",
    "years_of_history": 3,
    "port": 8765,
    "auto_sync_minutes": 60,
    "open_browser": True,
    "phone_access": True,
    "latitude": None,
    "longitude": None,
    "nearby_radius_km": 25,
}

HAIKUBOX_API = os.environ.get("HAIKUBOX_API", "https://api.haikubox.com/haikubox")
INAT_API = os.environ.get("INAT_API", "https://api.inaturalist.org/v1")
USER_AGENT = "OverheardBirds/2.0 (personal Haikubox dashboard)"

DB_LOCK = threading.Lock()


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"config.json could not be read ({e}); using defaults")
    else:
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return cfg


CONFIG = load_config()
# Overrides used by the GitHub version (set as repository secrets)
for _env, _key, _conv in (("HAIKUBOX_SERIAL", "serial", str), ("YB_LATITUDE", "latitude", float),
                          ("YB_LONGITUDE", "longitude", float), ("YB_YEARS", "years_of_history", int)):
    if os.environ.get(_env, "").strip():
        try:
            CONFIG[_key] = _conv(os.environ[_env].strip())
        except ValueError:
            pass


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_counts (
    day TEXT NOT NULL,
    species TEXT NOT NULL,
    count INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'api',
    PRIMARY KEY (day, species)
);
CREATE TABLE IF NOT EXISTS days_fetched (
    day TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    species_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS detections (
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    hour INTEGER NOT NULL,
    species TEXT NOT NULL,
    scientific TEXT,
    score REAL,
    source TEXT NOT NULL DEFAULT 'api',
    PRIMARY KEY (ts, species)
);
CREATE TABLE IF NOT EXISTS species (
    name TEXT PRIMARY KEY,
    scientific TEXT,
    taxon_id INTEGER,
    photo TEXT,
    photos TEXT,
    summary TEXT,
    wiki_url TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    info_fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS reviews (
    species TEXT NOT NULL,
    ts TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (species, ts)
);
CREATE TABLE IF NOT EXISTS nearby (
    scientific TEXT PRIMARY KEY,
    taxon_id INTEGER,
    name TEXT,
    count_all INTEGER NOT NULL DEFAULT 0,
    count_month INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_daily_species ON daily_counts(species);
CREATE INDEX IF NOT EXISTS idx_det_day ON detections(day);
"""


def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        # upgrade older databases in place
        for table, col, typ in [("detections", "wav", "TEXT"), ("detections", "conf", "TEXT"),
                                ("detections", "raw", "TEXT"), ("species", "verdict", "TEXT")]:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def meta_get(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key, value):
    with DB_LOCK, db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))


def ensure_species(conn, name, scientific=None):
    conn.execute("INSERT OR IGNORE INTO species(name) VALUES(?)", (name,))
    if scientific:
        conn.execute(
            "UPDATE species SET scientific=? WHERE name=? AND (scientific IS NULL OR scientific='')",
            (scientific, name),
        )


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def get_json(url, timeout=25, retries=2):
    """Return (status_code, parsed_json_or_None)."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, (json.loads(body) if body.strip() else None)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 422):
                return e.code, None
            last_err = e
        except Exception as e:  # network hiccup - retry
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise ConnectionError(f"{url} -> {last_err}")


def save_debug_sample(name, payload):
    """Keep one raw sample of each API response for troubleshooting."""
    try:
        f = DATA_DIR / "api_samples.json"
        samples = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        text = json.dumps(payload)[:4000]
        samples[name] = {"saved": datetime.now().isoformat(timespec="seconds"), "sample": text}
        f.write_text(json.dumps(samples, indent=2), encoding="utf-8")
    except Exception:
        pass


def find_list(payload, keys=("detections", "data", "counts", "birds", "results", "items", "species")):
    """API responses may be a bare list or a dict wrapping a list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in keys:
            if isinstance(payload.get(k), list):
                return payload[k]
        for v in payload.values():
            if isinstance(v, list):
                return v
        # dict of {bird: count}
        if payload and all(isinstance(v, (int, float)) for v in payload.values()):
            return [{"bird": k, "count": v} for k, v in payload.items()]
    return []


def pick(d, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


# --------------------------------------------------------------------------
# Time zone handling
# --------------------------------------------------------------------------
def get_tz():
    tzname = meta_get("box_tz")
    if tzname:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tzname)
        except Exception:
            pass  # Windows without tzdata - fall back to this PC's local time
    return None


def to_local(dt_str, tz):
    s = str(dt_str).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # trim fractional seconds beyond 6 digits
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz) if tz else dt.astimezone()


def local_today():
    tz = get_tz()
    return (datetime.now(tz) if tz else datetime.now()).date()


# --------------------------------------------------------------------------
# Sync engine
# --------------------------------------------------------------------------
class SyncState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.phase = "idle"
        self.message = ""
        self.done = 0
        self.total = 0
        self.error = None
        self.finished_at = None

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def as_dict(self):
        with self.lock:
            return {
                "running": self.running, "phase": self.phase, "message": self.message,
                "done": self.done, "total": self.total, "error": self.error,
                "finishedAt": self.finished_at,
            }


SYNC = SyncState()


def start_sync(full=False):
    with SYNC.lock:
        if SYNC.running:
            return False
        SYNC.running = True
        SYNC.error = None
    threading.Thread(target=run_sync, args=(full,), daemon=True).start()
    return True


def run_sync(full=False):
    serial = (CONFIG.get("serial") or "").strip()
    try:
        if not serial:
            raise RuntimeError("No Haikubox serial set. Put it in config.json (or the HAIKUBOX_SERIAL secret on GitHub).")
        SYNC.update(phase="box", message="Connecting to your Haikubox...", done=0, total=0)
        status, box = get_json(f"{HAIKUBOX_API}/{serial}")
        if status != 200 or not box:
            raise RuntimeError(f"Haikubox didn't recognise serial {serial} (HTTP {status}). Check config.json.")
        save_debug_sample("box", box)
        if isinstance(box, dict):
            meta_set("box_name", pick(box, "haikuboxName", "name", default="My Haikubox"))
            tz = pick(box, "tz", "timezone", "timeZone")
            if tz:
                meta_set("box_tz", tz)
            lat = pick(box, "lat", "latitude")
            lng = pick(box, "lon", "lng", "longitude")
            loc = box.get("location") if isinstance(box.get("location"), dict) else {}
            lat = lat if lat is not None else pick(loc, "lat", "latitude")
            lng = lng if lng is not None else pick(loc, "lon", "lng", "longitude")
            if lat is not None and lng is not None:
                meta_set("box_lat", lat)
                meta_set("box_lng", lng)
        log(f"Connected to {meta_get('box_name')} ({serial})")

        sync_recent_detections(serial)
        sync_daily_history(serial, full)
        import_csv_files()
        backfill_daily_from_detections()
        sync_species_info()
        sync_nearby()

        meta_set("last_sync", datetime.now().isoformat(timespec="seconds"))
        meta_set("last_sync_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        SYNC.update(phase="done", message="Up to date", finished_at=datetime.now().isoformat(timespec="seconds"))
        log("Sync complete.")
    except Exception as e:
        traceback.print_exc()
        SYNC.update(phase="error", error=str(e), message="Sync failed")
        log(f"SYNC ERROR: {e}")
    finally:
        SYNC.update(running=False)


def sync_recent_detections(serial):
    """Individual detections with exact times (powers 'last heard' + time-of-day charts)."""
    SYNC.update(phase="recent", message="Getting the latest detections...")
    last = meta_get("last_detection_sync")
    hours = 24
    if last:
        try:
            gap = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
            hours = int(min(max(gap + 2, 24), 168))
        except Exception:
            pass
    else:
        hours = 168

    payload = None
    for h in sorted({hours, 72, 24}, reverse=True):
        status, payload = get_json(f"{HAIKUBOX_API}/{serial}/detections?hours={h}")
        if status == 200 and payload is not None:
            break
    if payload is None:
        log("Recent detections endpoint returned nothing")
        return
    save_debug_sample("detections", payload)
    rows = find_list(payload)
    tz = get_tz()
    added = 0
    with DB_LOCK, db() as conn:
        for e in rows:
            if not isinstance(e, dict):
                continue
            name = str(pick(e, "cn", "bird", "commonName", "common_name", "species", default="")).strip()
            ts = pick(e, "dt", "timestamp", "time", "detectedAt")
            if not name or not ts:
                continue
            try:
                local = to_local(ts, tz)
            except Exception:
                continue
            sci = pick(e, "sn", "scientificName", "scientific_name")
            conf = pick(e, "cl", "confidenceLevel", "confidence_level", "level", "conf", "confidence", "score")
            score = None
            try:
                score = float(conf) if conf is not None else None
            except (TypeError, ValueError):
                score = None
            wav = pick(e, "wav", "wavUrl", "audio", "audioUrl", "recording", "url")
            ts_local = local.isoformat(timespec="seconds")
            cur = conn.execute(
                "INSERT OR IGNORE INTO detections(ts,day,hour,species,scientific,score,wav,conf,raw) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ts_local, local.date().isoformat(), local.hour, name, sci, score, wav,
                 None if conf is None else str(conf), json.dumps(e)[:2000]),
            )
            if not cur.rowcount and wav:
                conn.execute("UPDATE detections SET wav=? WHERE ts=? AND species=? AND wav IS NULL",
                             (wav, ts_local, name))
            added += cur.rowcount
            ensure_species(conn, name, sci)
    meta_set("last_detection_sync", datetime.now().isoformat(timespec="seconds"))
    log(f"Recent detections: {len(rows)} received, {added} new")


def parse_daily(payload):
    out = {}
    for e in find_list(payload):
        if not isinstance(e, dict):
            continue
        name = str(pick(e, "bird", "cn", "species", "commonName", "name", default="")).strip()
        cnt = pick(e, "count", "total", "detections", "n", default=1)
        try:
            cnt = int(cnt)
        except (TypeError, ValueError):
            cnt = 1
        if name and cnt > 0:
            out[name] = out.get(name, 0) + cnt
    return out


def fetch_day(serial, d):
    status, payload = get_json(f"{HAIKUBOX_API}/{serial}/daily-count?date={d.isoformat()}")
    return d, status, payload


def sync_daily_history(serial, full=False):
    """Day-by-day species counts going back N years. Only fetches days we don't already have."""
    if meta_get("daily_endpoint") == "missing" and not full:
        log("daily-count endpoint unavailable earlier; skipping history backfill")
        return
    today = local_today()
    years = int(CONFIG.get("years_of_history", 3))
    earliest = today - timedelta(days=365 * years)
    hist_start = meta_get("history_start")
    if hist_start and not full:
        earliest = max(earliest, date.fromisoformat(hist_start))

    with db() as conn:
        done = {r["day"]: r["fetched_at"] for r in conn.execute("SELECT day, fetched_at FROM days_fetched")}

    def needs(d):
        if d >= today - timedelta(days=1):
            return True  # today/yesterday are still filling in
        f = done.get(d.isoformat())
        if not f:
            return True
        return f[:10] <= d.isoformat()  # fetched on the same day = incomplete

    days = []
    d = today
    while d >= earliest:
        if needs(d):
            days.append(d)
        d -= timedelta(days=1)
    if not days:
        return

    SYNC.update(phase="history", message="Downloading your detection history...", done=0, total=len(days))
    log(f"History: {len(days)} day(s) to fetch")

    batch_size = 40
    seen_data = False
    empty_run = 0
    oldest_with_data = None
    fetched_n = 0
    sample_saved = False
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i in range(0, len(days), batch_size):
            batch = days[i:i + batch_size]
            results = list(pool.map(lambda x: fetch_day(serial, x), batch))
            results.sort(key=lambda r: r[0], reverse=True)

            # Endpoint missing entirely?
            if i == 0 and all(s == 404 for _, s, _ in results):
                meta_set("daily_endpoint", "missing")
                log("daily-count endpoint not available - history will build up from detections instead")
                return

            now = datetime.now().isoformat(timespec="seconds")
            with DB_LOCK, db() as conn:
                for d, status, payload in results:
                    if status != 200:
                        continue
                    if not sample_saved and payload:
                        save_debug_sample("daily-count", payload)
                        sample_saved = True
                    counts = parse_daily(payload)
                    conn.execute("DELETE FROM daily_counts WHERE day=? AND source='api'", (d.isoformat(),))
                    for name, c in counts.items():
                        conn.execute(
                            "INSERT OR REPLACE INTO daily_counts(day,species,count,source) VALUES(?,?,?, 'api')",
                            (d.isoformat(), name, c),
                        )
                        ensure_species(conn, name)
                    conn.execute(
                        "INSERT OR REPLACE INTO days_fetched(day,fetched_at,species_count) VALUES(?,?,?)",
                        (d.isoformat(), now, len(counts)),
                    )
                    if counts:
                        seen_data = True
                        empty_run = 0
                        oldest_with_data = d
                    else:
                        empty_run += 1
            fetched_n += len(batch)
            SYNC.update(done=fetched_n, message=f"Downloading history... back to {batch[-1].strftime('%b %d, %Y')}")

            # Stop once we've gone well past the day the box was installed
            if seen_data and empty_run >= 60:
                meta_set("history_start", (oldest_with_data or batch[-1]).isoformat())
                log(f"History begins around {oldest_with_data}; stopping backfill")
                break
            if not seen_data and fetched_n >= 120 and not hist_start:
                log("No history found in the last 120 days from daily-count")
                break
    SYNC.update(done=SYNC.total)


def backfill_daily_from_detections():
    """For any day we only know from individual detections (or CSV), derive daily counts."""
    with DB_LOCK, db() as conn:
        api_days = {r["day"] for r in conn.execute("SELECT DISTINCT day FROM daily_counts WHERE source='api'")}
        rows = conn.execute("SELECT day, species, COUNT(*) c FROM detections GROUP BY day, species").fetchall()
        for r in rows:
            if r["day"] in api_days:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO daily_counts(day,species,count,source) VALUES(?,?,?, 'detections')",
                (r["day"], r["species"], r["c"]),
            )


def import_csv_files():
    """Optional: drop Haikubox CSV exports into the 'imports' folder for older history."""
    files = sorted(IMPORT_DIR.glob("*.csv"))
    if not files:
        return
    imported = set(json.loads(meta_get("imported_csv", "[]")))
    for f in files:
        key = f"{f.name}:{f.stat().st_size}"
        if key in imported:
            continue
        SYNC.update(phase="csv", message=f"Importing {f.name}...")
        n = 0
        try:
            with open(f, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}

                def col(*cands):
                    for cand in cands:
                        for lc, orig in cols.items():
                            if cand in lc:
                                return orig
                    return None

                c_name = col("common", "species", "bird", "name")
                c_sci = col("scientific", "latin")
                c_dt = col("timestamp", "detected", "datetime", "date")
                c_time = col("time") if c_dt and "date" in c_dt.lower() and "time" not in c_dt.lower() else None
                c_score = col("score", "confidence")
                with DB_LOCK, db() as conn:
                    for row in reader:
                        name = (row.get(c_name) or "").strip() if c_name else ""
                        raw = (row.get(c_dt) or "").strip() if c_dt else ""
                        if c_time:
                            raw = f"{raw} {(row.get(c_time) or '').strip()}".strip()
                        if not name or not raw:
                            continue
                        dt = None
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                                    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%Y-%m-%d", "%m/%d/%Y"):
                            try:
                                dt = datetime.strptime(raw, fmt)
                                break
                            except ValueError:
                                continue
                        if dt is None:
                            try:
                                dt = to_local(raw, get_tz()).replace(tzinfo=None)
                            except Exception:
                                continue
                        score = None
                        if c_score:
                            try:
                                score = float(row.get(c_score))
                            except (TypeError, ValueError):
                                pass
                        sci = (row.get(c_sci) or "").strip() if c_sci else None
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO detections(ts,day,hour,species,scientific,score,source) "
                            "VALUES(?,?,?,?,?,?,'csv')",
                            (dt.isoformat(timespec="seconds"), dt.date().isoformat(), dt.hour, name, sci, score),
                        )
                        n += cur.rowcount
                        ensure_species(conn, name, sci)
            imported.add(key)
            meta_set("imported_csv", json.dumps(sorted(imported)))
            log(f"Imported {n} detections from {f.name}")
        except Exception as e:
            log(f"Could not import {f.name}: {e}")


def lookup_inat(name, scientific):
    queries = [q for q in (scientific, name) if q]
    for q in queries:
        url = f"{INAT_API}/taxa?" + urllib.parse.urlencode(
            {"q": q, "taxon_id": 3, "per_page": 5, "is_active": "true"}
        )
        status, data = get_json(url, timeout=20, retries=1)
        results = (data or {}).get("results", []) if status == 200 else []
        if not results:
            time.sleep(1)
            continue
        best = None
        for r in results:
            if (r.get("preferred_common_name") or "").lower() == name.lower() or (r.get("name") or "").lower() == (scientific or "").lower():
                best = r
                break
        best = best or results[0]
        time.sleep(1)
        status, detail = get_json(f"{INAT_API}/taxa/{best['id']}", timeout=20, retries=1)
        taxon = ((detail or {}).get("results") or [best])[0]
        photos = []
        for tp in taxon.get("taxon_photos", [])[:6]:
            p = tp.get("photo", {})
            med = p.get("medium_url") or p.get("url")
            if med:
                photos.append({
                    "url": med,
                    "large": p.get("large_url") or med.replace("medium", "large"),
                    "credit": p.get("attribution", ""),
                })
        if not photos and best.get("default_photo"):
            p = best["default_photo"]
            med = p.get("medium_url") or p.get("url")
            photos.append({"url": med, "large": med.replace("medium", "large"), "credit": p.get("attribution", "")})
        summary = re.sub(r"<[^>]+>", "", taxon.get("wikipedia_summary") or "").strip()
        return {
            "taxon_id": taxon.get("id"),
            "scientific": taxon.get("name"),
            "photos": photos,
            "summary": summary,
            "wiki_url": taxon.get("wikipedia_url"),
        }
    return None


def sync_species_info():
    with db() as conn:
        todo = conn.execute(
            "SELECT name, scientific FROM species WHERE info_fetched_at IS NULL OR "
            "(photo IS NULL AND info_fetched_at < ?)",
            ((datetime.now() - timedelta(days=7)).isoformat(),),
        ).fetchall()
    if not todo:
        return
    SYNC.update(phase="photos", message="Finding bird photos...", done=0, total=len(todo))
    for i, r in enumerate(todo):
        SYNC.update(done=i, message=f"Finding photos: {r['name']}")
        try:
            info = lookup_inat(r["name"], r["scientific"])
        except Exception as e:
            log(f"Photo lookup failed for {r['name']}: {e}")
            info = None
        with DB_LOCK, db() as conn:
            if info:
                conn.execute(
                    "UPDATE species SET taxon_id=?, scientific=COALESCE(NULLIF(scientific,''), ?), photo=?, photos=?, "
                    "summary=?, wiki_url=?, info_fetched_at=? WHERE name=?",
                    (info["taxon_id"], info["scientific"], info["photos"][0]["url"] if info["photos"] else None,
                     json.dumps(info["photos"]), info["summary"], info["wiki_url"],
                     datetime.now().isoformat(timespec="seconds"), r["name"]),
                )
            else:
                conn.execute("UPDATE species SET info_fetched_at=? WHERE name=?",
                             (datetime.now().isoformat(timespec="seconds"), r["name"]))
    SYNC.update(done=len(todo))


def get_location():
    lat, lng = CONFIG.get("latitude"), CONFIG.get("longitude")
    if lat in (None, "") or lng in (None, ""):
        lat, lng = meta_get("box_lat"), meta_get("box_lng")
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def sync_nearby():
    """How often each bird is reported by people within N km (iNaturalist research-grade sightings).
    A bird nobody has ever photographed near you is worth double-checking."""
    loc = get_location()
    if not loc:
        return
    last = meta_get("nearby_synced")
    key = f"{loc[0]:.3f},{loc[1]:.3f},{CONFIG.get('nearby_radius_km', 25)},{date.today().month}"
    if last == key:
        return
    SYNC.update(phase="nearby", message="Checking which birds are reported near you...")
    radius = int(CONFIG.get("nearby_radius_km", 25))
    found = {}
    for which, extra in (("count_all", {}), ("count_month", {"month": date.today().month})):
        page = 1
        while page <= 4:
            params = {"lat": loc[0], "lng": loc[1], "radius": radius, "taxon_id": 3,
                      "quality_grade": "research", "per_page": 500, "page": page, **extra}
            status, data = get_json(f"{INAT_API}/observations/species_counts?" + urllib.parse.urlencode(params),
                                    timeout=30, retries=1)
            results = (data or {}).get("results", []) if status == 200 else []
            for r in results:
                t = r.get("taxon") or {}
                sci = t.get("name")
                if not sci:
                    continue
                row = found.setdefault(sci, {"taxon_id": t.get("id"), "name": t.get("preferred_common_name"),
                                             "count_all": 0, "count_month": 0})
                row[which] = int(r.get("count") or 0)
            if len(results) < 500:
                break
            page += 1
            time.sleep(1)
        time.sleep(1)
    if not found:
        log("Nearby sightings: nothing returned (check latitude/longitude in config.json)")
        return
    with DB_LOCK, db() as conn:
        conn.execute("DELETE FROM nearby")
        for sci, r in found.items():
            conn.execute("INSERT INTO nearby(scientific,taxon_id,name,count_all,count_month) VALUES(?,?,?,?,?)",
                         (sci, r["taxon_id"], r["name"], r["count_all"], r["count_month"]))
    meta_set("nearby_synced", key)
    log(f"Nearby sightings: {len(found)} bird species reported within {radius} km")


# --------------------------------------------------------------------------
# Data for the website
# --------------------------------------------------------------------------
def build_payload():
    with db() as conn:
        species = []
        for r in conn.execute("SELECT * FROM species ORDER BY name"):
            species.append({
                "name": r["name"],
                "sci": r["scientific"] or "",
                "photo": r["photo"],
                "photos": json.loads(r["photos"]) if r["photos"] else [],
                "summary": r["summary"] or "",
                "wiki": r["wiki_url"],
                "hidden": bool(r["hidden"]),
                "verdict": r["verdict"],
                "taxon_id": r["taxon_id"],
            })
        daily = {}
        for r in conn.execute("SELECT species, day, count FROM daily_counts ORDER BY day"):
            daily.setdefault(r["species"], []).append([r["day"], r["count"]])
        hourly = {}
        for r in conn.execute("SELECT species, hour, COUNT(*) c FROM detections GROUP BY species, hour"):
            hourly.setdefault(r["species"], [0] * 24)[r["hour"]] = r["c"]
        last_heard = {r["species"]: r["t"] for r in conn.execute(
            "SELECT species, MAX(ts) t FROM detections GROUP BY species")}
        cutoff = (datetime.now() - timedelta(hours=72)).isoformat()
        recent = [[r["ts"], r["species"]] for r in conn.execute(
            "SELECT ts, species FROM detections WHERE ts >= ? ORDER BY ts DESC LIMIT 5000", (cutoff,))]
        # recordings (newest 15 per species) + any per-clip reviews
        reviews = {(r["species"], r["ts"]): r["verdict"] for r in conn.execute("SELECT * FROM reviews")}
        recordings = {}
        for r in conn.execute(
                "SELECT species, ts, wav, conf, score FROM ("
                " SELECT *, ROW_NUMBER() OVER (PARTITION BY species ORDER BY ts DESC) rn FROM detections"
                " WHERE wav IS NOT NULL) WHERE rn <= 15"):
            recordings.setdefault(r["species"], []).append(
                {"ts": r["ts"], "wav": r["wav"], "conf": r["conf"], "verdict": reviews.get((r["species"], r["ts"]))})
        conf_stats = {}
        for r in conn.execute("SELECT species, conf, COUNT(*) c FROM detections WHERE conf IS NOT NULL "
                              "GROUP BY species, conf"):
            conf_stats.setdefault(r["species"], {})[r["conf"]] = r["c"]
        review_counts = {}
        for (sp, _), v in reviews.items():
            review_counts.setdefault(sp, {"correct": 0, "wrong": 0})[v] = review_counts.get(sp, {}).get(v, 0) + 1
        nearby = {r["scientific"].lower(): {"all": r["count_all"], "month": r["count_month"], "id": r["taxon_id"]}
                  for r in conn.execute("SELECT * FROM nearby")}
        nearby_ids = {v["id"]: v for v in nearby.values() if v["id"]}
        totals = conn.execute(
            "SELECT (SELECT COUNT(*) FROM detections) det, (SELECT COUNT(*) FROM days_fetched) days, "
            "(SELECT MIN(day) FROM daily_counts) first_day").fetchone()
    return {
        "box": {"name": meta_get("box_name", "My Haikubox"), "serial": CONFIG["serial"], "tz": meta_get("box_tz")},
        "today": local_today().isoformat(),
        "lastSync": meta_get("last_sync_utc") or meta_get("last_sync"),
        "historyNote": "missing" if meta_get("daily_endpoint") == "missing" else "",
        "firstDay": totals["first_day"],
        "species": species,
        "daily": daily,
        "hourly": hourly,
        "lastHeard": last_heard,
        "recent": recent,
        "recordings": recordings,
        "confStats": conf_stats,
        "reviewCounts": review_counts,
        "nearby": {s["name"]: (nearby_ids.get(s["taxon_id"]) or nearby.get((s["sci"] or "").lower()))
                   for s in species} if nearby else None,
        "nearbyRadius": CONFIG.get("nearby_radius_km", 25),
        "phone": phone_urls(),
        "sync": SYNC.as_dict(),
    }


def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "common_name", "scientific_name", "detections"])
    with db() as conn:
        for r in conn.execute(
            "SELECT d.day, d.species, s.scientific, d.count FROM daily_counts d "
            "LEFT JOIN species s ON s.name=d.species ORDER BY d.day, d.species"):
            w.writerow([r[0], r[1], r[2] or "", r[3]])
    return out.getvalue().encode("utf-8")


# --------------------------------------------------------------------------
# Phone access (same Wi-Fi, or anywhere via Tailscale)
# --------------------------------------------------------------------------
import socket
import ipaddress

SERVER_PORT = None


def local_addresses():
    """Return {'wifi': [...], 'tailscale': [...]} IPv4 addresses of this computer."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    out = {"wifi": [], "tailscale": []}
    for ip in sorted(ips):
        a = ipaddress.ip_address(ip)
        if a.is_loopback or a.is_link_local:
            continue
        if a in ipaddress.ip_network("100.64.0.0/10"):
            out["tailscale"].append(ip)
        elif a.is_private:
            out["wifi"].append(ip)
    return out


def phone_urls():
    if not CONFIG.get("phone_access", True) or not SERVER_PORT:
        return {}
    a = local_addresses()
    return {k: [f"http://{ip}:{SERVER_PORT}/" for ip in v] for k, v in a.items()}


STATIC_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json",
                ".js": "text/javascript", ".css": "text/css", ".ico": "image/x-icon"}


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        extra = extra or {}
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self.send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            static = (WEB_DIR / path.lstrip("/")).resolve()
            if (path.count("/") == 1 and static.suffix in STATIC_TYPES and static.parent == WEB_DIR.resolve()
                    and static.is_file()):
                return self.send(200, static.read_bytes(), STATIC_TYPES[static.suffix],
                                 {"Cache-Control": "max-age=86400"})
            if path == "/api/ping":
                return self.send(200, {"app": "overheardbirds"})
            if path == "/api/data":
                return self.send(200, build_payload())
            if path == "/api/sync-status":
                return self.send(200, SYNC.as_dict())
            if path == "/export.csv":
                return self.send(200, export_csv(), "text/csv",
                                 {"Content-Disposition": f'attachment; filename="overheard_birds_{date.today()}.csv"'})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self.send(500, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        try:
            if path == "/api/sync":
                started = start_sync(full=bool(body.get("full")))
                return self.send(200, {"started": started, "sync": SYNC.as_dict()})
            if path == "/api/review":
                name, ts, verdict = body.get("name"), body.get("ts"), body.get("verdict")
                with DB_LOCK, db() as conn:
                    if verdict in ("correct", "wrong"):
                        conn.execute("INSERT OR REPLACE INTO reviews(species,ts,verdict,reviewed_at) VALUES(?,?,?,?)",
                                     (name, ts, verdict, datetime.now().isoformat(timespec="seconds")))
                    else:
                        conn.execute("DELETE FROM reviews WHERE species=? AND ts=?", (name, ts))
                return self.send(200, {"ok": True})
            if path == "/api/verdict":
                name, verdict = body.get("name"), body.get("verdict")
                with DB_LOCK, db() as conn:
                    conn.execute("UPDATE species SET verdict=?, hidden=? WHERE name=?",
                                 (verdict if verdict in ("correct", "wrong") else None,
                                  1 if verdict == "wrong" else 0, name))
                return self.send(200, {"ok": True})
            if path == "/api/hide":
                with DB_LOCK, db() as conn:
                    conn.execute("UPDATE species SET hidden=? WHERE name=?",
                                 (1 if body.get("hidden") else 0, body.get("name")))
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self.send(500, {"error": str(e)})


def already_running(port):
    try:
        status, data = get_json(f"http://127.0.0.1:{port}/api/ping", timeout=2, retries=0)
        return status == 200 and (data or {}).get("app") == "overheardbirds"
    except Exception:
        return False


def auto_sync_loop():
    minutes = max(15, int(CONFIG.get("auto_sync_minutes", 60)))
    while True:
        time.sleep(minutes * 60)
        start_sync()


def build_site(out_dir):
    """GitHub version: sync, then write a static website (index.html + data.json) to out_dir."""
    import shutil
    init_db()
    SYNC.update(running=True)
    run_sync()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for f in WEB_DIR.iterdir():
        if f.is_file() and f.name != "index.html":
            shutil.copy2(f, out / f.name)
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("</head>", "<script>window.YB_STATIC = true;</script>\n</head>", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    payload = build_payload()
    payload["static"] = True
    payload["phone"] = {}
    payload["box"].pop("serial", None)
    if payload["sync"].get("error"):
        err = payload["sync"]["error"]
        payload["sync"]["error"] = ("couldn't reach the Haikubox" if "haikubox" in err.lower()
                                    else "a data service didn't respond")
    (out / "data.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    (out / ".nojekyll").write_text("")
    log(f"Built site in {out} ({len(payload['species'])} species)")
    if SYNC.error:
        log(f"Note: this sync had a problem: {SYNC.error}")


def main():
    if "--build" in sys.argv:
        i = sys.argv.index("--build")
        build_site(sys.argv[i + 1] if len(sys.argv) > i + 1 else "site")
        return
    port = int(CONFIG.get("port", 8765))
    if already_running(port):
        log("Overheard Birds is already running - opening it in your browser.")
        webbrowser.open(f"http://localhost:{port}/")
        return

    init_db()
    server = None
    for p in range(port, port + 10):
        try:
            host = "0.0.0.0" if CONFIG.get("phone_access", True) else "127.0.0.1"
            server = ThreadingHTTPServer((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if not server:
        log("Could not open a local port. Is something else using 8765-8774?")
        sys.exit(1)

    global SERVER_PORT
    SERVER_PORT = port
    url = f"http://localhost:{port}/"
    print("=" * 60)
    print("  Overheard Birds is running")
    print(f"  Open: {url}")
    urls = phone_urls()
    for u in urls.get("wifi", []):
        print(f"  On your phone (same Wi-Fi): {u}")
    for u in urls.get("tailscale", []):
        print(f"  On your phone (anywhere, via Tailscale): {u}")
    print("  Leave this window open while you use the site.")
    print("  Close this window to stop.")
    print("=" * 60, flush=True)

    last = meta_get("last_sync")
    stale = True
    if last:
        try:
            stale = datetime.now() - datetime.fromisoformat(last) > timedelta(minutes=20)
        except Exception:
            pass
    if stale or "--sync" in sys.argv:
        start_sync(full="--full" in sys.argv)
    threading.Thread(target=auto_sync_loop, daemon=True).start()

    if CONFIG.get("open_browser", True) and "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

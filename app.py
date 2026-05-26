#!/usr/bin/env python3
"""Salon SMS Marketing Dashboard — scores clients for SMS targeting."""

import os
import json
import secrets
import psycopg2
import psycopg2.extras
import threading
import uuid
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import (Flask, jsonify, request, send_from_directory,
                   session as flask_session, redirect, make_response)
import requests
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter
import time

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

@app.errorhandler(Exception)
def json_error(e):
    from werkzeug.exceptions import HTTPException
    code = e.code if isinstance(e, HTTPException) else 500
    app.logger.exception(e)
    return jsonify(error=str(e)), code

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
import calendar

# ── Activity logging ──────────────────────────────────────────
def _get_db():
    url = os.environ.get('DATABASE_URL', '')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return psycopg2.connect(url)

def _db_setup():
    import sys
    try:
        con = _get_db()
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id            SERIAL PRIMARY KEY,
                ts            TEXT    NOT NULL,
                event_type    TEXT    NOT NULL,
                salon         TEXT,
                username      TEXT,
                question      TEXT,
                format        TEXT,
                is_followup   INTEGER,
                result_count  INTEGER,
                result_title  TEXT,
                result_summary TEXT,
                response_ms   INTEGER,
                input_tokens  INTEGER,
                output_tokens INTEGER,
                error         TEXT
            )
        """)
        cur.execute("ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS username TEXT")
        con.commit()
        cur.execute("SELECT COUNT(*) FROM activity_log")
        count = cur.fetchone()[0]
        cur.close()
        con.close()
        print(f"[startup] activity_log ready — {count} rows persisted in DB", flush=True)
    except Exception as e:
        print(f"[startup] DB setup FAILED: {e}", file=sys.stderr, flush=True)
        app.logger.warning("DB setup failed: %s", e)

_db_setup()

def _salon_label(ctx=None):
    ac  = (ctx or {}).get('loaded_account_code', '')
    tn  = (ctx or {}).get('loaded_tenant_name', '')
    sn  = (ctx or {}).get('loaded_salon_name', '')
    tid = (ctx or {}).get('loaded_tenant_id', '')
    if ac or tn:
        return (' – '.join(p for p in [ac, tn] if p))[:100]
    return (sn or tid or '')[:100]


def _get_ctx(tenant_id, server="BETA"):
    """Return the stored data context for a tenant, or None if not loaded."""
    if tenant_id:
        return _tenant_store.get(f"{server}|{tenant_id}".upper())
    return None


def _log(event_type, **kwargs):
    import sys
    try:
        from datetime import timezone
        if 'username' not in kwargs:
            kwargs['username'] = flask_session.get('username', '')
        con = _get_db()
        cur = con.cursor()
        cols = ['ts', 'event_type'] + list(kwargs.keys())
        vals = [datetime.now(timezone.utc).isoformat(timespec='seconds'), event_type] + list(kwargs.values())
        cur.execute(
            f"INSERT INTO activity_log ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})",
            vals,
        )
        con.commit()
        cur.close()
        con.close()
    except Exception as e:
        print(f"[_log] WRITE FAILED ({event_type}): {e}", file=sys.stderr, flush=True)
        app.logger.warning("Activity log write failed: %s", e)


def _saloniq_login(account_code, username, password):
    """Validate credentials via the SalonIQ Aria LogOn API.
    Returns (tenant_id, server) on success, or (None, None) on failure.
    GRT001 routes to the BETA (greathairhub) server; all others go to LIVE (apihub).
    """
    server = 'BETA' if account_code.strip().upper() == 'GRT001' else 'LIVE'
    srv    = SERVERS[server]
    today  = date.today()
    fmt    = srv['date_fmt']
    sd     = today.replace(day=1).strftime(fmt)
    ed     = today.replace(day=calendar.monthrange(today.year, today.month)[1]).strftime(fmt)
    params = {
        'TokenID':    srv['token'],
        'ReportName': 'XXX_Export_Admin_Aria_LogOn',
        'TenantID':   '',
        'Salonid':    '',
        'UserID':     '',
        'startdate':  sd,
        'enddate':    ed,
        'data1':      account_code,
        'data2':      username,
        'data3':      password,
        'data4':      '',
    }
    try:
        print(f"[login] calling {srv['base']} account={account_code!r} user={username!r} server={server}", flush=True)
        resp = requests.post(srv['base'], params=params, timeout=15)
        print(f"[login] HTTP {resp.status_code} — body: {resp.text[:500]!r}", flush=True)
        resp.raise_for_status()
        data = resp.json()
        print(f"[login] parsed JSON: {str(data)[:300]}", flush=True)
        # Response format: {"Data": {"Array": [{"TenantId": "..."}]}, "Status": "Success"}
        arr = (data.get('Data') or {}).get('Array') or []
        if arr and isinstance(arr, list):
            tenant_id = (arr[0].get('TenantId') or arr[0].get('TenantID') or '').strip()
            print(f"[login] tenant_id found: {tenant_id!r}", flush=True)
            if tenant_id:
                return tenant_id, server
        print(f"[login] no tenant_id in response — login failed", flush=True)
        return None, None
    except Exception as e:
        print(f"[login] EXCEPTION: {e}", flush=True)
        app.logger.warning("SalonIQ login API error: %s", e)
        return None, None

def _session_role():
    """Return the role stored in the Flask session, or None."""
    return flask_session.get('role')

def _login_redirect():
    """Redirect to /login, preserving the current URL as the 'next' param."""
    next_url = request.full_path if request.query_string else request.path
    return redirect(f'/login?next={next_url}')

def require_auth(f):
    """Protect a route — accepts both admin and user sessions."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _session_role():
            return f(*args, **kwargs)
        return _login_redirect()
    return decorated

def require_admin(f):
    """Restrict a route to admin sessions only — redirects others to /."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _session_role() == 'admin':
            return f(*args, **kwargs)
        return redirect('/')
    return decorated

def require_auth_or_redirect(f):
    """Protect a route — redirects to /login when unauthenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _session_role():
            return f(*args, **kwargs)
        return _login_redirect()
    return decorated


API_COMMON = dict(Salonid="", UserID="", data1="", data2="", data3="", data4="")

SERVERS = {
    "BETA": {
        "base":           "https://greathairhub.saloniq.co.uk/api/GetAPIReport",
        "token":          "ACD7636F-D6D5-45AB-92FC-785D4904ADA5",
        "default_tenant": "1E7D7624-FEB7-4950-A6BE-5FBB1498EE39",
        "date_fmt":       "%d/%m/%Y",
    },
    "LIVE": {
        "base":           "https://apihub.saloniq.co.uk/api/GetAPIReport",
        "token":          "517a41d9-48e3-4af7-ae6c-0e30688f9325",
        "default_tenant": "1E7D7624-FEB7-4950-A6BE-5FBB1498EE39",
        "date_fmt":       "%m/%d/%Y",
    },
}

_cache, _cache_ts = {}, {}
CACHE_TTL = 3600
_jobs = {}          # job_id -> {status, data, error}
_tenant_store = {}  # f"{server}|{tenant_id}" → per-tenant data context


NOCACHE_REPORTS = {"XXX_Export_Admin_TUBR_Bookings"}

def fetch(report_name, sd="", ed="", tenant_id=None, server="BETA", method="POST"):
    srv = SERVERS.get(server, SERVERS["BETA"])
    tid = tenant_id or srv["default_tenant"]
    key = f"{server}|{report_name}|{sd}|{ed}|{tid}"
    now = time.time()
    if report_name not in NOCACHE_REPORTS:
        if key in _cache and now - _cache_ts.get(key, 0) < CACHE_TTL:
            app.logger.info("CACHE HIT  %s [%s→%s]", report_name, sd, ed)
            return _cache[key]
    app.logger.info("FETCH START %s [%s→%s] tenant=%s server=%s", report_name, sd, ed, tid, server)
    t0 = time.time()
    params = {**API_COMMON, "TokenID": srv["token"], "TenantID": tid.upper(),
              "ReportName": report_name, "startdate": sd, "enddate": ed}
    if method == "GET":
        # Build query string manually — requests encodes / as %2F which breaks the API
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        r  = requests.get(f"{srv['base']}?{qs}", timeout=180)
    else:
        r = requests.post(srv["base"], params=params, headers={"Content-Length": "0"}, timeout=180)
    r.raise_for_status()
    payload = r.json()
    result  = (payload.get("Data") or {}).get("Array") or []
    app.logger.info("FETCH DONE  %s [%s→%s] rows=%d elapsed=%.1fs",
                    report_name, sd, ed, len(result), time.time() - t0)
    if report_name not in NOCACHE_REPORTS:
        _cache[key], _cache_ts[key] = result, now
    return result


def parse_dt(s):
    if not s:
        return None
    for fmt in ["%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"]:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SKIP_KEYWORDS = ("NO SHOW", "DEPOSIT", "CONSULTATION", "PATCH TEST")


def build_sms(cid, name, status, top_cats, pref_tm, days_since, overdue, avg_gap):
    first    = name.split()[0] if name else "there"
    stylist  = pref_tm if pref_tm and pref_tm not in ("?", "") else None
    with_who = f" with {stylist}" if stylist else ""

    cat = (top_cats[0] if top_cats else "").upper()
    is_colour    = any(x in cat for x in ("COLOUR", "COLOR", "COLOURING", "TINT", "FOIL", "HIGHLIGHT", "BALAYAGE", "OMBRE"))
    is_extension = "EXTENSION" in cat
    is_cut       = any(x in cat for x in ("CUT", "TRIM", "FINISH", "BLOWDRY", "BLOW DRY"))

    v = hash(cid) % 2

    if status == "active":
        opts = [
            f"Hi {first}! Lovely seeing you recently 😊 Why not prebook your next appointment{with_who} before the diary fills up? Give us a call!",
            f"Hi {first}, thanks for your recent visit! Lock in your next appointment{with_who} – call us or book online 📅",
        ]
    elif status == "due":
        if is_colour:
            opts = [
                f"Hi {first}! Your colour will be ready for a refresh soon 🎨 {stylist or 'We'} {'has' if stylist else 'have'} availability – shall we get you booked in?",
                f"Hi {first}, time to freshen up your colour? Book{with_who} and keep those tones looking gorgeous 💇‍♀️ Give us a call!",
            ]
        elif is_extension:
            opts = [
                f"Hi {first}! Your extensions will be due for a maintenance appointment soon 💕 Book{with_who} to keep them looking their best.",
                f"Hi {first}, time to check in on your extensions! Call us to book your next maintenance{with_who} 🌟",
            ]
        else:
            opts = [
                f"Hi {first}! It's nearly time for your next visit 😊 Give us a call to book in{with_who} – we'd love to see you!",
                f"Hi {first}, your hair is probably ready for some TLC! Book{with_who} – we have great availability 💕",
            ]
    elif status == "lapsing":
        gap_note = f" – it's been {days_since} days!" if days_since else ""
        if is_colour:
            opts = [
                f"Hi {first}, we've missed you{gap_note} Your colour could really do with some love 🎨 Come and see {stylist or 'us'} – call to book.",
                f"Hi {first}! It's been a while – time to bring your colour back to life? Book{with_who} today 💇‍♀️",
            ]
        else:
            opts = [
                f"Hi {first}, we miss you{gap_note} It would be lovely to have you back{with_who} – give us a call to book 😊",
                f"Hi {first}! It's been too long 💕 {stylist or 'The team'} would love to see you – shall we get you booked in?",
            ]
    else:
        opts = [
            f"Hi {first}, it's been a while and we'd love to welcome you back! {stylist or 'The team'} has availability – give us a call 🌟",
            f"Hi {first}! We've really missed you 💕 It would be wonderful to see you again{with_who} – call us to rebook anytime.",
        ]

    msg = opts[v % len(opts)]
    return msg[:157] + "…" if len(msg) > 160 else msg


def time_label(h):
    if h < 12:
        return "Morning"
    if h < 14:
        return "Lunchtime"
    if h < 17:
        return "Afternoon"
    return "Evening"


def build_data(tenant_id=None, server="BETA", step_fn=None):
    # All data is kept in local variables and written to _tenant_store at the end.
    # No module-level globals are used, so concurrent requests for different tenants
    # cannot overwrite each other.
    all_scored = []; all_clients = []; total_clients = 0
    utilisation = []; retail_summary = {}; team_kpis = {}; load_timings = {}
    loaded_tenant_name = ""; loaded_account_code = ""
    load_timings['t_start'] = time.time()
    service_monthly = {}; service_weekly = {}; service_daily = {}
    service_cat_monthly = {}; service_salon_monthly = {}; booking_stats = {}
    loaded_salon_ids = []; loaded_salon_name = ""
    loaded_server    = server
    loaded_tenant_id = tenant_id or SERVERS.get(server, SERVERS["BETA"])["default_tenant"]

    def step(msg):
        if step_fn:
            step_fn(msg)

    today = date.today()

    step("Fetching client records")
    clients_raw = fetch("XXX_Export_Admin_TUBR_Clients", "01/01/2026", "01/01/2026", tenant_id=tenant_id, server=server)

    step("Fetching services & team")
    svcs_raw    = fetch("XXX_Export_Admin_TUBR_services", "01/01/2026", "01/01/2026", tenant_id=tenant_id, server=server)
    # Use a wide date range so team members who have left are still included —
    # bookings go back 2 years and their TeamMemberId must resolve to a name.
    _team_sd = (today - timedelta(days=730)).strftime(SERVERS.get(server, SERVERS["BETA"])["date_fmt"])
    _team_ed = today.strftime(SERVERS.get(server, SERVERS["BETA"])["date_fmt"])
    team_raw    = fetch("XXX_Export_Admin_TUBR_TeamMembers", _team_sd, _team_ed, tenant_id=tenant_id, server=server)
    try:
        salons_raw = fetch("XXX_Export_Admin_BenchMarks_SalonList", "01/01/2026", "01/01/2026", tenant_id=tenant_id, server=server)

    except Exception as e:
        app.logger.warning("SalonList fetch failed (salon names will be blank): %s", e)
        salons_raw = []

    total_clients = len(clients_raw)

    svc_map  = {s["ServiceId"]: s for s in svcs_raw}
    team_map = {
        t["TeamMemberId"]: (t.get("NickName") or t.get("FirstName") or "Unknown")
        for t in team_raw if t.get("TeamMemberId")
    }
    cli_map  = {c["ClientId"].lower(): c for c in clients_raw if c.get("ClientId")}
    salon_map = {
        str(s.get("SalonId") or s.get("Salonid") or s.get("salonid") or s.get("ID") or ""):
        (s.get("SalonName") or s.get("Name") or s.get("name") or "")
        for s in salons_raw
    }
    loaded_salon_ids = [sid for sid in salon_map if sid]
    salon_name_map = {sid: name for sid, name in salon_map.items() if sid}
    # Use the SalonList name if available; the /api/data route will override with
    # the tenant name from the UI dropdown if this remains blank
    if salon_name_map and not loaded_salon_name:
        loaded_salon_name = next(iter(salon_name_map.values()), "")

    _PERIOD_LABELS = {0: "Weekly", 1: "4-Weekly", 2: "Monthly", 3: "Daily"}
    for t in team_raw:
        tid = t.get("TeamMemberId")
        if not tid:
            continue
        name = team_map.get(tid, "Unknown")
        team_kpis[name] = {
            "period":           _PERIOD_LABELS.get(int(t.get("PeriodRange") or 0), "Unknown"),
            "kpi_retail":       float(t.get("KPIRetail")         or 0),
            "kpi_care_factor":  float(t.get("KPICareFactor")      or 0),
            "kpi_rebookings":   float(t.get("KPIReBookings")      or 0),
            "kpi_client_count": float(t.get("KPIClientCount")     or 0),
            "kpi_req_count":    float(t.get("KPIRequestCount")    or 0),
            "kpi_avg_services": float(t.get("KPIAverageServices") or 0),
            "kpi_utilization":  float(t.get("KPIUtilization")     or 0),
        }
    del svcs_raw, team_raw, clients_raw, salons_raw  # free raw API data now maps are built

    step("Fetching utilisation data")
    try:
        date_fmt = SERVERS.get(server, SERVERS["BETA"])["date_fmt"]
        util_sd  = (today - timedelta(days=182)).strftime(date_fmt)   # 6 months back
        util_ed  = (today + timedelta(days=91)).strftime(date_fmt)    # 3 months forward
        raw_util = fetch("XXX_Export_Admin_TUBR_Utilisation", util_sd, util_ed,
                         tenant_id=tenant_id, server=server)
        # Resolve TeamMemberId → name; drop rows for unrecognised IDs (admin/reception staff)
        resolved = []
        for row in raw_util:
            name = team_map.get(row.get("TeamMemberId", ""))
            if not name:
                continue   # skip staff not in the stylist/team list
            row["StylistName"] = name
            sid = row.get("SalonId", "")
            row["SalonName"] = salon_map.get(sid, sid)  # fallback to ID if name unknown
            resolved.append(row)
        utilisation = resolved
        salon_count   = len({r["SalonName"] for r in utilisation})
        stylist_count = len({r["StylistName"] for r in utilisation})
        app.logger.info("Utilisation: %d rows, %d salons, %d stylists",
                        len(utilisation), salon_count, stylist_count)
        step(f"Utilisation loaded ({len(utilisation)} rows · {salon_count} salon(s) · {stylist_count} stylist(s))")
    except Exception as e:
        app.logger.warning("Utilisation fetch failed: %s", e)
        step(f"Utilisation unavailable: {e}")
        utilisation = []

    step("Fetching client tags")
    tags_by_client = defaultdict(list)
    try:
        tags_raw = fetch("XXX_Export_Admin_TUBR_Tags", "01/01/2026", "01/01/2026", tenant_id=tenant_id, server=server)
        for t in tags_raw:
            cid = (t.get("ClientId") or "").lower()
            tag = t.get("Tag") or ""
            if cid and tag:
                tags_by_client[cid].append(tag)
    except Exception as e:
        print(f"TAGS fetch failed: {e}", flush=True)

    # Fetch each booking chunk and process it immediately — never hold more than
    # one chunk in memory at a time
    date_fmt = SERVERS.get(server, SERVERS["BETA"])["date_fmt"]
    bounds = [
        today - timedelta(days=730),
        today - timedelta(days=547),
        today - timedelta(days=365),
        today - timedelta(days=182),
        today + timedelta(days=365),
    ]
    booking_ranges = [
        (bounds[i].strftime(date_fmt), bounds[i + 1].strftime(date_fmt))
        for i in range(4)
    ]

    by_client = defaultdict(list)
    future_bookings = defaultdict(list)  # cid -> [{dt, svc, cat}]

    def _fetch_chunk(args):
        sd, ed = args
        try:
            return fetch("XXX_Export_Admin_TUBR_Bookings", sd, ed,
                         tenant_id=tenant_id, server=server)
        except Exception as e:
            app.logger.error("CHUNK FAILED [%s→%s]: %s", sd, ed, e)
            raise RuntimeError(f"Booking chunk {sd}→{ed} failed: {e}") from e

    step("Fetching booking history")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_chunk, r): r for r in booking_ranges}
        for future in as_completed(futures):
            chunk = future.result()
            for b in chunk:
                cid = (b.get("ClientId") or "").lower()
                dt  = parse_dt(b.get("Start"))
                if not cid or not dt:
                    continue
                svc      = svc_map.get(b.get("ServiceId"), {})
                svc_name = svc.get("Description", "")
                bk_status = int(b.get("Status") or 0)   # 0=booked,1=arrived,2=paid,3=no-show
                bk_source = int(b.get("Source") or 5)   # 1=online,5=in-salon
                bk_id = str(b.get("BookingId") or "")
                if dt.date() > today:
                    if not any(k in svc_name.upper() for k in SKIP_KEYWORDS):
                        future_bookings[cid].append({
                            "dt":     dt,
                            "svc":    svc_name,
                            "cat":    svc.get("Categoty", "").replace("HAIR - ", ""),
                            "source": bk_source,
                            "bid":    bk_id,
                        })
                    continue
                if any(k in svc_name.upper() for k in SKIP_KEYWORDS):
                    continue
                by_client[cid].append({
                    "dt":     dt,
                    "price":  float(b.get("TotalSalesPrice") or 0),
                    "tm":     b.get("TeamMemberId", ""),
                    "cat":    svc.get("Categoty", "").replace("HAIR - ", ""),
                    "svc":    svc_name,
                    "sid":    str(b.get("Salonid") or b.get("SalonId") or b.get("salonid") or ""),
                    "dept":   (svc.get("Department") or "").lower(),
                    "status": bk_status,
                    "source": bk_source,
                    "bid":    bk_id,
                })
            del chunk  # discard as soon as processed

    # Aggregate service revenue by month and week; no-shows excluded from revenue/visits
    _STATUS_LABELS = {0: "Booked", 1: "Arrived", 2: "Paid", 3: "No-show"}
    _SOURCE_LABELS = {1: "Online", 5: "In-salon"}
    _zero = lambda: {"revenue": 0.0, "visits": 0, "no_shows": 0, "no_show_value": 0.0, "online": 0, "clients": set()}
    _svc_agg      = defaultdict(_zero)
    _wk_agg       = defaultdict(_zero)
    _status_counts = defaultdict(int)
    _source_counts = defaultdict(int)
    _seen_mk  = set()   # (bid, mk)  — dedup visits/no-shows per month
    _seen_wk  = set()   # (bid, wk)  — dedup visits/no-shows per week
    _seen_dk  = set()   # (bid, day) — dedup visits/no-shows per day
    _seen_omk = set()   # (bid, mk)  — dedup online count per month
    _seen_owk = set()   # (bid, wk)  — dedup online count per week
    _seen_odk = set()   # (bid, day) — dedup online count per day
    _seen_cmk  = set()   # (bid, cat, mk)    — dedup category visits per month
    _seen_smk  = set()   # (bid, salon, mk)  — dedup salon visits per month
    _day_agg   = defaultdict(_zero)
    _cat_agg   = defaultdict(lambda: defaultdict(lambda: {"revenue": 0.0, "visits": 0}))
    _salon_agg = defaultdict(lambda: defaultdict(lambda: {"revenue": 0.0, "visits": 0,
                                                           "no_shows": 0, "no_show_value": 0.0}))
    for cid, bkgs in by_client.items():
        for b in bkgs:
            mk     = b["dt"].strftime("%b %Y")
            bdate  = b["dt"].date()
            monday = bdate - timedelta(days=bdate.weekday())
            wk     = monday.isoformat()
            st     = b.get("status", 0)
            src    = b.get("source", 5)
            cat    = b.get("cat", "")
            sid    = b.get("sid", "")
            salon  = salon_name_map.get(sid, sid) or sid   # resolve to name; fall back to raw ID
            bid    = b.get("bid") or f"{cid}_{bdate.isoformat()}"
            _status_counts[st]  += 1
            _source_counts[src] += 1
            day = bdate.isoformat()
            if st == 2:  # paid — revenue sums all service lines; visits deduplicated by booking ID
                _svc_agg[mk]["revenue"]  += b["price"]
                _wk_agg[wk]["revenue"]   += b["price"]
                _day_agg[day]["revenue"] += b["price"]
                if (bid, mk) not in _seen_mk:
                    _seen_mk.add((bid, mk))
                    _svc_agg[mk]["visits"]  += 1
                    _svc_agg[mk]["clients"].add(cid)
                if (bid, wk) not in _seen_wk:
                    _seen_wk.add((bid, wk))
                    _wk_agg[wk]["visits"]  += 1
                    _wk_agg[wk]["clients"].add(cid)
                if (bid, day) not in _seen_dk:
                    _seen_dk.add((bid, day))
                    _day_agg[day]["visits"] += 1
                    _day_agg[day]["clients"].add(cid)
                if cat:  # category × month breakdown
                    _cat_agg[cat][mk]["revenue"] += b["price"]
                    if (bid, cat, mk) not in _seen_cmk:
                        _seen_cmk.add((bid, cat, mk))
                        _cat_agg[cat][mk]["visits"] += 1
                if salon:  # salon × month breakdown
                    _salon_agg[salon][mk]["revenue"] += b["price"]
                    if (bid, salon, mk) not in _seen_smk:
                        _seen_smk.add((bid, salon, mk))
                        _salon_agg[salon][mk]["visits"] += 1
            elif st == 3:  # no-show — value sums all service lines; count deduplicated by booking ID
                _svc_agg[mk]["no_show_value"]  += b["price"]
                _wk_agg[wk]["no_show_value"]   += b["price"]
                _day_agg[day]["no_show_value"] += b["price"]
                if (bid, mk) not in _seen_mk:
                    _seen_mk.add((bid, mk))
                    _svc_agg[mk]["no_shows"] += 1
                if (bid, wk) not in _seen_wk:
                    _seen_wk.add((bid, wk))
                    _wk_agg[wk]["no_shows"]  += 1
                if (bid, day) not in _seen_dk:
                    _seen_dk.add((bid, day))
                    _day_agg[day]["no_shows"] += 1
                if salon:  # salon no-show tracking
                    _salon_agg[salon][mk]["no_show_value"] += b["price"]
                    if (bid, salon, mk) not in _seen_smk:
                        _seen_smk.add((bid, salon, mk))
                        _salon_agg[salon][mk]["no_shows"] += 1
            # status 0 (booked) and 1 (arrived) excluded from all counts
            if src == 1:
                if (bid, mk) not in _seen_omk:
                    _seen_omk.add((bid, mk))
                    _svc_agg[mk]["online"] += 1
                if (bid, wk) not in _seen_owk:
                    _seen_owk.add((bid, wk))
                    _wk_agg[wk]["online"]  += 1
                if (bid, day) not in _seen_odk:
                    _seen_odk.add((bid, day))
                    _day_agg[day]["online"] += 1

    def _agg_to_dict(d):
        return {"revenue": round(d["revenue"]), "visits": d["visits"],
                "no_shows": d["no_shows"], "no_show_value": round(d["no_show_value"]),
                "online": d["online"], "clients": len(d["clients"])}

    service_monthly = {mk: _agg_to_dict(d) for mk, d in _svc_agg.items()}
    service_cat_monthly = {
        cat: {mk: {"revenue": round(d["revenue"]), "visits": d["visits"]}
              for mk, d in months.items()}
        for cat, months in _cat_agg.items()
    }
    service_weekly  = {wk: _agg_to_dict(d) for wk, d in _wk_agg.items()}
    service_daily   = {day: _agg_to_dict(d) for day, d in _day_agg.items()}
    service_salon_monthly = {
        salon: {mk: {"revenue": round(d["revenue"]), "visits": d["visits"],
                     "no_shows": d["no_shows"], "no_show_value": round(d["no_show_value"])}
                for mk, d in months.items()}
        for salon, months in _salon_agg.items()
    }
    booking_stats = {
        "status": {_STATUS_LABELS.get(k, f"Status{k}"): v
                   for k, v in sorted(_status_counts.items())},
        "source": {_SOURCE_LABELS.get(k, f"Source{k}"): v
                   for k, v in sorted(_source_counts.items())},
    }

    step("Fetching gift cards")
    giftcard_by_client = defaultdict(list)
    try:
        gc_sd   = (today - timedelta(days=730)).strftime(date_fmt)
        gc_ed   = today.strftime(date_fmt)
        gc_rows = fetch("XXX_Export_Admin_TUBR_GiftCards", gc_sd, gc_ed, tenant_id=tenant_id, server=server)
        for gc in gc_rows:
            cid = (gc.get("ClientId") or gc.get("ClientID") or gc.get("clientid") or "").lower()
            if not cid:
                continue
            dt     = parse_dt(gc.get("TransactionDate") or "")
            amount = float(gc.get("Value") or 0)
            giftcard_by_client[cid].append({"dt": dt, "amount": amount})
    except Exception as e:
        print(f"GIFTCARDS fetch failed: {e}", flush=True)

    step("Fetching promotions")
    promo_by_client = defaultdict(list)
    try:
        pr_rows = fetch("XXX_Export_Admin_TUBR_Promotions", gc_sd, gc_ed, tenant_id=tenant_id, server=server)
        for pr in pr_rows:
            cid = (pr.get("ClientId") or "").lower()
            if not cid:
                continue
            dt   = parse_dt(pr.get("TransactionDate") or "")
            name = (pr.get("Description") or "").strip()
            code = (pr.get("PromotionCode") or "").strip()
            promo_by_client[cid].append({"dt": dt, "name": name, "code": code})
    except Exception as e:
        print(f"PROMOTIONS fetch failed: {e}", flush=True)

    step("Fetching retail sales")
    retail_by_client = defaultdict(list)
    try:
        products_raw = fetch("XXX_Export_Admin_TUBR_Products", "01/01/2026", "01/01/2026", tenant_id=tenant_id, server=server)
        product_map  = {p["ProductId"].lower(): p for p in products_raw if p.get("ProductId")}
        del products_raw
        retail_raw = fetch("XXX_Export_Admin_TUBR_RetailSales", gc_sd, gc_ed, tenant_id=tenant_id, server=server)
        for r in retail_raw:
            cid = (r.get("PayingClientId") or "").lower()
            if not cid:
                continue
            pid     = (r.get("ProductId") or "").lower()
            product = product_map.get(pid, {})
            retail_by_client[cid].append({
                "name":  product.get("Description", ""),
                "brand": product.get("Supplier", ""),
                "line":  product.get("SupplierLine", ""),
                "price": float(r.get("TotalSalesPrice") or 0),
                "qty":   int(float(r.get("Qty") or 1)),
                "dt":    parse_dt(r.get("TransactionDate") or ""),
                "sid":   str(r.get("Salonid") or r.get("SalonId") or r.get("salonid") or ""),
            })
        del retail_raw
    except Exception as e:
        print(f"RETAIL fetch failed: {e}", flush=True)

    # Aggregate retail transactions into summary tables while retail_by_client is in scope
    prod_agg          = defaultdict(lambda: {"clients": set(), "units": 0, "revenue": 0.0})
    brand_agg         = defaultdict(lambda: {"clients": set(), "units": 0, "revenue": 0.0})
    line_agg          = defaultdict(lambda: {"clients": set(), "units": 0, "revenue": 0.0})
    monthly_agg       = defaultdict(lambda: {"units": 0, "revenue": 0.0})
    monthly_prod_agg  = defaultdict(lambda: defaultdict(lambda: {"units": 0, "revenue": 0.0}))
    monthly_brand_agg = defaultdict(lambda: defaultdict(lambda: {"units": 0, "revenue": 0.0}))
    salon_monthly_agg = defaultdict(lambda: defaultdict(lambda: {"units": 0, "revenue": 0.0}))

    for cid, items in retail_by_client.items():
        for r in items:
            name   = (r.get("name")  or "").strip()
            brand  = (r.get("brand") or "").strip()
            line   = (r.get("line")  or "").strip()
            qty    = int(r.get("qty") or 1)
            price  = float(r.get("price") or 0) * qty
            dt     = r.get("dt")
            mk     = dt.strftime("%b %Y") if dt else None
            sid    = (r.get("sid") or "").strip()
            salon  = salon_name_map.get(sid, "") if sid else ""

            if name:
                prod_agg[name]["clients"].add(cid)
                prod_agg[name]["units"]   += qty
                prod_agg[name]["revenue"] += price
                if mk:
                    monthly_prod_agg[mk][name]["units"]   += qty
                    monthly_prod_agg[mk][name]["revenue"] += price
            if brand:
                brand_agg[brand]["clients"].add(cid)
                brand_agg[brand]["units"]   += qty
                brand_agg[brand]["revenue"] += price
                if mk:
                    monthly_brand_agg[mk][brand]["units"]   += qty
                    monthly_brand_agg[mk][brand]["revenue"] += price
            if line:
                line_agg[line]["clients"].add(cid)
                line_agg[line]["units"]   += qty
                line_agg[line]["revenue"] += price
            if mk:
                monthly_agg[mk]["units"]   += qty
                monthly_agg[mk]["revenue"] += price
            if mk and salon:
                salon_monthly_agg[salon][mk]["units"]   += qty
                salon_monthly_agg[salon][mk]["revenue"] += price

    retail_summary = {
        "products": {k: {"clients": len(v["clients"]), "units": v["units"],
                         "revenue": round(v["revenue"])}
                     for k, v in prod_agg.items()},
        "brands":   {k: {"clients": len(v["clients"]), "units": v["units"],
                         "revenue": round(v["revenue"])}
                     for k, v in brand_agg.items()},
        "lines":    {k: {"clients": len(v["clients"]), "units": v["units"],
                         "revenue": round(v["revenue"])}
                     for k, v in line_agg.items()},
        "monthly":  {k: {"units": v["units"], "revenue": round(v["revenue"])}
                     for k, v in monthly_agg.items()},
        # month → top products/brands (for "top products in May" type questions)
        "monthly_products": {
            mk: {prod: {"units": d["units"], "revenue": round(d["revenue"])}
                 for prod, d in prods.items()}
            for mk, prods in monthly_prod_agg.items()
        },
        "monthly_brands": {
            mk: {brand: {"units": d["units"], "revenue": round(d["revenue"])}
                 for brand, d in brands.items()}
            for mk, brands in monthly_brand_agg.items()
        },
        "salon_monthly": {
            salon: {mk: {"units": d["units"], "revenue": round(d["revenue"])}
                    for mk, d in months.items()}
            for salon, months in salon_monthly_agg.items()
        },
    }

    load_timings['t_fetch_done'] = time.time()
    step("Building client profiles")
    rows = []
    for cid, bkgs in by_client.items():
        cli = cli_map.get(cid)
        if not cli:
            continue
        fb          = future_bookings.get(cid, [])
        has_future  = bool(fb)
        future_svcs = [b["svc"] for b in sorted(fb, key=lambda x: x["dt"]) if b["svc"]]
        future_cats = list({b["cat"] for b in fb if b["cat"]})
        next_booking = min(fb, key=lambda x: x["dt"])["dt"].strftime("%-d %b %Y") if fb else None

        bkgs.sort(key=lambda x: x["dt"])
        actual_visits = [b for b in bkgs if b.get("status") == 2]
        if not actual_visits:
            continue  # client has only no-shows — skip
        last_dt, first_dt = actual_visits[-1]["dt"], actual_visits[0]["dt"]
        days_since = (today - last_dt.date()).days

        visit_dates = sorted(set(b["dt"].date() for b in actual_visits))
        n = len(visit_dates)

        avg_gap = (visit_dates[-1] - visit_dates[0]).days / (n - 1) if n > 1 else None
        overdue = (days_since - avg_gap) if avg_gap else None

        total_spend = sum(b["price"] for b in actual_visits)
        avg_spend   = total_spend / n if n else 0

        pref_day  = DAYS[Counter(b["dt"].weekday() for b in actual_visits).most_common(1)[0][0]]
        pref_time = time_label(Counter(b["dt"].hour for b in actual_visits).most_common(1)[0][0])

        tm_cnt     = Counter(b["tm"] for b in actual_visits if b["tm"])
        pref_tm    = team_map.get(tm_cnt.most_common(1)[0][0], "?") if tm_cnt else "?"
        n_stylists = len(tm_cnt)

        salon_cnt  = Counter(b["sid"] for b in actual_visits if b["sid"])
        pref_salon = salon_map.get(salon_cnt.most_common(1)[0][0], "") if salon_cnt else ""

        top_cats        = [c for c, _ in Counter(b["cat"] for b in actual_visits if b["cat"]).most_common(2)]
        all_cats        = list(dict.fromkeys(b["cat"] for b in actual_visits if b["cat"]))
        departments     = list(dict.fromkeys(b["dept"] for b in actual_visits if b["dept"]))
        top_svcs        = [s for s, _ in Counter(b["svc"] for b in actual_visits if b["svc"]).most_common(5)]
        no_shows        = int(cli.get("NoShows") or 0)
        booking_noshows = len({b.get("bid") or b["dt"].date().isoformat()
                               for b in bkgs if b.get("status") == 3})
        online_bookings = len({b.get("bid") or b["dt"].date().isoformat()
                               for b in bkgs if b.get("source") == 1})
        future_online   = sum(1 for b in fb   if b.get("source") == 1)

        gc_list        = giftcard_by_client.get(cid, [])
        giftcard_count = len(gc_list)
        giftcard_total = round(sum(g["amount"] for g in gc_list))
        gc_dated       = sorted([g for g in gc_list if g["dt"]], key=lambda x: x["dt"], reverse=True)
        last_giftcard  = gc_dated[0]["dt"].strftime("%-d %b %Y") if gc_dated else None
        giftcard_dates = list(dict.fromkeys(g["dt"].strftime("%-d %b %Y") for g in gc_dated))

        pr_list      = promo_by_client.get(cid, [])
        promo_count  = len(pr_list)
        promo_names  = list(dict.fromkeys(p["name"] for p in pr_list if p["name"]))
        promo_codes  = list(dict.fromkeys(p["code"] for p in pr_list if p["code"]))
        pr_dated     = sorted([p for p in pr_list if p["dt"]], key=lambda x: x["dt"], reverse=True)
        last_promo   = pr_dated[0]["dt"].strftime("%-d %b %Y") if pr_dated else None
        promo_dates  = list(dict.fromkeys(p["dt"].strftime("%-d %b %Y") for p in pr_dated))

        tags      = tags_by_client.get(cid, [])
        tag_count = len(tags)

        rt_list        = retail_by_client.get(cid, [])
        retail_count   = len(rt_list)
        retail_total   = round(sum(r["price"] for r in rt_list))
        retail_products = list(dict.fromkeys(r["name"]  for r in rt_list if r["name"]))
        retail_brands   = list(dict.fromkeys(r["brand"] for r in rt_list if r["brand"]))
        retail_lines    = list(dict.fromkeys(r["line"]  for r in rt_list if r["line"]))
        rt_dated        = sorted([r for r in rt_list if r["dt"]], key=lambda x: x["dt"], reverse=True)
        retail_dates    = list(dict.fromkeys(r["dt"].strftime("%-d %b %Y") for r in rt_dated))

        if days_since <= 30:
            r_score = 10
        elif days_since <= 90:
            r_score = 40
        elif days_since <= 180:
            r_score = 30
        elif days_since <= 365:
            r_score = 15
        else:
            r_score = 5

        if avg_gap and overdue and overdue > 0:
            o_score = min(overdue / avg_gap * 20, 20)
        else:
            o_score = 0

        years   = max((today - first_dt.date()).days / 365.25, 0.08)
        f_score = min(n / years * 3, 20)
        m_score = min(avg_spend / 5, 20)
        penalty = min(no_shows * 3, 15)

        total_score = r_score + o_score + f_score + m_score - penalty

        if days_since <= 60:
            status, scls = "Active", "active"
        elif days_since <= 120:
            status, scls = "Due Soon", "due"
        elif days_since <= 365:
            status, scls = "Lapsing", "lapsing"
        else:
            status, scls = "Lapsed", "lapsed"

        full_name = f"{cli.get('Firstname','').strip()} {cli.get('Lastname','').strip()}".strip()
        sms_msg   = build_sms(cid, full_name, status, top_cats, pref_tm,
                              days_since,
                              round(overdue) if overdue and overdue > 0 else None,
                              round(avg_gap) if avg_gap else None)

        rows.append(dict(
            id=cid,
            name=full_name,
            score=round(total_score, 1),
            status=status,
            scls=scls,
            days_since=days_since,
            last_visit=last_dt.strftime("%-d %b %Y"),
            n_visits=n,
            total_spend=round(total_spend),
            avg_spend=round(avg_spend),
            avg_gap=round(avg_gap) if avg_gap else None,
            overdue=round(overdue) if overdue and overdue > 0 else None,
            pref_day=pref_day,
            pref_time=pref_time,
            pref_tm=pref_tm,
            pref_salon=pref_salon,
            top_cats=top_cats,
            all_cats=all_cats,
            departments=departments,
            top_svcs=top_svcs,
            has_future_booking=has_future,
            future_svcs=future_svcs,
            future_cats=future_cats,
            next_booking=next_booking,
            no_shows=no_shows,
            booking_noshows=booking_noshows,
            online_bookings=online_bookings,
            future_online=future_online,
            n_stylists=n_stylists,
            giftcard_count=giftcard_count,
            giftcard_total=giftcard_total,
            last_giftcard=last_giftcard,
            giftcard_dates=giftcard_dates,
            promo_count=promo_count,
            promo_names=promo_names,
            promo_codes=promo_codes,
            last_promo=last_promo,
            promo_dates=promo_dates,
            tags=tags,
            tag_count=tag_count,
            sms_optout=str(cli.get("IsSmsOptOut", "False")) == "True",
            email_optout=str(cli.get("IsEmailOptOut", "False")) == "True",
            salonspy_optin=str(cli.get("IsSalonSpyOptIn", "False")) == "True",
            points_enabled=str(cli.get("IsPointsEnabled", "False")) == "True",
            retail_count=retail_count,
            retail_total=retail_total,
            retail_products=retail_products,
            retail_brands=retail_brands,
            retail_lines=retail_lines,
            retail_dates=retail_dates,
            mobile=cli.get("MobilePhoneNumber", ""),
            email=cli.get("emailaddress", ""),
            gender=cli.get("Gender", ""),
            birth_month=cli.get("Birthmonth", ""),
            birth_day=cli.get("BirthDay", ""),
            points=int(cli.get("PointsBalance") or 0),
            account_balance=round(float(cli.get("AccountBalance") or 0), 2),
            age_group=cli.get("AgeGroup", ""),
            occupation=cli.get("Occupation", ""),
            how_heard=cli.get("HowHeard", ""),
            sr=round(r_score, 1),
            so=round(o_score, 1),
            sf=round(f_score, 1),
            sm=round(m_score, 1),
            sp=-penalty,
            score_pct=min(round(total_score), 100),
            sms=sms_msg,
        ))

    rows.sort(key=lambda x: x["score"], reverse=True)
    all_scored = [r for r in rows if not r["has_future_booking"]]

    # Add clients with no past visits (but possibly future bookings) to all_clients
    visited_ids = {c["id"] for c in rows}
    no_history = []
    for cid, cli in cli_map.items():
        if cid in visited_ids:
            continue
        full_name = f"{cli.get('Firstname','').strip()} {cli.get('Lastname','').strip()}".strip()
        fb          = future_bookings.get(cid, [])
        has_future  = bool(fb)
        future_svcs = [b["svc"] for b in sorted(fb, key=lambda x: x["dt"]) if b["svc"]]
        future_cats = list({b["cat"] for b in fb if b["cat"]})
        next_booking = min(fb, key=lambda x: x["dt"])["dt"].strftime("%-d %b %Y") if fb else None
        no_history.append(dict(
            id=cid, name=full_name, score=0, status="No Visit (2yrs)", scls="never",
            days_since=None, last_visit=None, n_visits=0, total_spend=0, avg_spend=0,
            avg_gap=None, overdue=None, pref_day=None, pref_time=None,
            pref_tm=None, pref_salon=None, top_cats=[], all_cats=[], departments=[], top_svcs=[],
            has_future_booking=has_future, future_svcs=future_svcs,
            future_cats=future_cats, next_booking=next_booking,
            no_shows=int(cli.get("NoShows") or 0), n_stylists=0,
            giftcard_count=len(giftcard_by_client.get(cid, [])),
            giftcard_total=round(sum(g["amount"] for g in giftcard_by_client.get(cid, []))),
            last_giftcard=max((g for g in giftcard_by_client.get(cid, []) if g["dt"]), key=lambda x: x["dt"], default={"dt": None})["dt"].strftime("%-d %b %Y") if any(g["dt"] for g in giftcard_by_client.get(cid, [])) else None,
            giftcard_dates=list(dict.fromkeys(g["dt"].strftime("%-d %b %Y") for g in sorted((g for g in giftcard_by_client.get(cid, []) if g["dt"]), key=lambda x: x["dt"], reverse=True))),
            promo_count=len(promo_by_client.get(cid, [])),
            promo_names=list(dict.fromkeys(p["name"] for p in promo_by_client.get(cid, []) if p["name"])),
            promo_codes=list(dict.fromkeys(p["code"] for p in promo_by_client.get(cid, []) if p["code"])),
            last_promo=max((p for p in promo_by_client.get(cid, []) if p["dt"]), key=lambda x: x["dt"], default={"dt": None})["dt"].strftime("%-d %b %Y") if any(p["dt"] for p in promo_by_client.get(cid, [])) else None,
            promo_dates=list(dict.fromkeys(p["dt"].strftime("%-d %b %Y") for p in sorted((p for p in promo_by_client.get(cid, []) if p["dt"]), key=lambda x: x["dt"], reverse=True))),
            tags=tags_by_client.get(cid, []),
            tag_count=len(tags_by_client.get(cid, [])),
            sms_optout=str(cli.get("IsSmsOptOut", "False")) == "True",
            email_optout=str(cli.get("IsEmailOptOut", "False")) == "True",
            salonspy_optin=str(cli.get("IsSalonSpyOptIn", "False")) == "True",
            points_enabled=str(cli.get("IsPointsEnabled", "False")) == "True",
            retail_count=len(retail_by_client.get(cid, [])),
            retail_total=round(sum(r["price"] for r in retail_by_client.get(cid, []))),
            retail_products=list(dict.fromkeys(r["name"]  for r in retail_by_client.get(cid, []) if r["name"])),
            retail_brands=list(dict.fromkeys(r["brand"] for r in retail_by_client.get(cid, []) if r["brand"])),
            retail_lines=list(dict.fromkeys(r["line"]  for r in retail_by_client.get(cid, []) if r["line"])),
            retail_dates=list(dict.fromkeys(r["dt"].strftime("%-d %b %Y") for r in sorted((r for r in retail_by_client.get(cid, []) if r["dt"]), key=lambda x: x["dt"], reverse=True))),
            mobile=cli.get("MobilePhoneNumber", ""), email=cli.get("emailaddress", ""),
            gender=cli.get("Gender", ""), birth_month=cli.get("Birthmonth", ""),
            birth_day=cli.get("BirthDay", ""), points=int(cli.get("PointsBalance") or 0),
            account_balance=round(float(cli.get("AccountBalance") or 0), 2),
            age_group=cli.get("AgeGroup", ""), occupation=cli.get("Occupation", ""),
            how_heard=cli.get("HowHeard", ""), sr=0, so=0, sf=0, sm=0, sp=0, score_pct=0, sms="",
        ))
    all_clients = rows + no_history

    load_timings['t_build_done'] = time.time()
    ctx_key = f"{loaded_server}|{loaded_tenant_id}".upper()
    _tenant_store[ctx_key] = {
        'all_clients':           all_clients,
        'all_scored':            all_scored,
        'total_clients':         total_clients,
        'utilisation':           utilisation,
        'retail_summary':        retail_summary,
        'salon_map':             salon_name_map,
        'service_monthly':       service_monthly,
        'service_weekly':        service_weekly,
        'service_daily':         service_daily,
        'service_cat_monthly':   service_cat_monthly,
        'service_salon_monthly': service_salon_monthly,
        'booking_stats':         booking_stats,
        'team_kpis':             team_kpis,
        'load_timings':          load_timings,
        'loaded_salon_name':     loaded_salon_name,
        'loaded_tenant_id':      loaded_tenant_id,
        'loaded_server':         loaded_server,
        'loaded_tenant_name':    loaded_tenant_name,
        'loaded_account_code':   loaded_account_code,
        'loaded_salon_ids':      loaded_salon_ids,
    }
    top = rows[:500]
    for i, c in enumerate(top, 1):
        c["rank"] = i
    return top


@app.route("/")
@require_auth
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route("/api/tenants")
@require_auth
def tenants():
    server = request.args.get("server", "BETA")
    rows   = fetch("XXX_Export_Admin_BenchMarks_TenantList", "01/01/2026", "01/01/2026", server=server)
    result = []
    for r in rows:
        tid  = (r.get("TenantID") or r.get("TenantId") or r.get("tenantid")
                or r.get("ID") or r.get("id") or "")
        name = (r.get("TenantName") or r.get("Name") or r.get("SalonName")
                or r.get("name") or "")
        code = (r.get("AccountCode") or r.get("Account") or r.get("Code")
                or r.get("code") or "")
        if tid:
            result.append({"id": str(tid), "name": name, "code": code})
    result.sort(key=lambda x: x["code"])
    return jsonify(result)


def _build_response(tenant_id, server, set_step=None):
    """Build the full API response dict — called from background thread."""
    import traceback
    try:
        clients  = build_data(tenant_id, server, step_fn=set_step)
        stylists = sorted(set(c["pref_tm"] for c in clients))
        resolved_tid = (tenant_id or SERVERS.get(server, SERVERS["BETA"])["default_tenant"]).upper()
        ctx_key = f"{server}|{resolved_tid}".upper()
        stored = _tenant_store.get(ctx_key, {})
        all_scored = stored.get('all_scored', [])
        total_clients = stored.get('total_clients', 0)
        result   = dict(
            clients=clients,
            stylists=stylists,
            n_active  =sum(1 for c in all_scored if c["scls"] == "active"),
            n_due     =sum(1 for c in all_scored if c["scls"] == "due"),
            n_lapsing =sum(1 for c in all_scored if c["scls"] == "lapsing"),
            n_lapsed  =sum(1 for c in all_scored if c["scls"] == "lapsed"),
            n_total=total_clients,
            generated=datetime.now().strftime("%-d %b %Y at %H:%M"),
        )
        return {"status": "done", "data": result}
    except Exception as e:
        app.logger.error("build_data failed: %s\n%s", e, traceback.format_exc())
        return {"status": "error", "error": str(e)}


@app.route("/api/data")
@require_auth
def data():
    """Start a background job and return its ID immediately."""
    tenant_id    = request.args.get("tenant_id") or None
    server       = request.args.get("server", "BETA")
    tenant_name  = request.args.get("tenant_name", "").strip()
    account_code = request.args.get("account_code", "").strip()
    job_id      = str(uuid.uuid4())
    _jobs[job_id] = {"status": "loading", "step": ""}
    _current_user = flask_session.get('username', '')

    def worker():
        def set_step(msg):
            if isinstance(_jobs.get(job_id), dict) and _jobs[job_id].get("status") == "loading":
                _jobs[job_id]["step"] = msg
        result = _build_response(tenant_id, server, set_step=set_step)
        resolved_tid = (tenant_id or SERVERS.get(server, SERVERS["BETA"])["default_tenant"]).upper()
        ctx_key = f"{server}|{resolved_tid}".upper()
        stored_ctx = _tenant_store.get(ctx_key, {})
        if tenant_name and not stored_ctx.get('loaded_salon_name'):
            stored_ctx['loaded_salon_name'] = tenant_name
        if tenant_name:
            stored_ctx['loaded_tenant_name'] = tenant_name
        if account_code:
            stored_ctx['loaded_account_code'] = account_code
        load_timings = stored_ctx.get('load_timings', {})
        if result.get("status") == "done" and load_timings.get('t_start'):
            fetch_ms = round((load_timings['t_fetch_done'] - load_timings['t_start']) * 1000)
            build_ms = round((load_timings['t_build_done'] - load_timings['t_fetch_done']) * 1000)
            total_ms = round((load_timings['t_build_done'] - load_timings['t_start']) * 1000)
            _log('data_loaded',
                 salon=_salon_label(stored_ctx),
                 username=_current_user,
                 result_count=stored_ctx.get('total_clients', 0),
                 result_title=f"API fetch: {fetch_ms/1000:.1f}s | Profile build: {build_ms/1000:.1f}s",
                 response_ms=total_ms,
            )
        _jobs[job_id] = result

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id})




@app.route("/api/job/<job_id>")
@require_auth
def job_status(job_id):
    """Poll this until status is 'done' or 'error'."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] == "done":
        _jobs.pop(job_id, None)   # clean up after delivery
        return jsonify(job["data"])
    if job["status"] == "error":
        _jobs.pop(job_id, None)
        return jsonify({"error": job["error"]}), 500
    return jsonify({"status": "loading", "step": job.get("step", "")})





@app.route("/api/debug/utilisation")
@require_auth
def debug_utilisation():
    """Try TenantID-only and SalonId combinations to find what returns rows."""
    server    = request.args.get("server", "BETA")
    tenant_id = request.args.get("tenant_id") or SERVERS.get(server, SERVERS["BETA"])["default_tenant"]
    ctx = _get_ctx(tenant_id, server) or {}
    known_salon_ids = ctx.get('loaded_salon_ids', [])
    utilisation     = ctx.get('utilisation', [])
    today_d   = date.today()
    srv       = SERVERS.get(server, SERVERS["BETA"])
    sd        = (today_d - timedelta(days=182)).strftime("%m/%d/%Y")
    ed        = (today_d + timedelta(days=91)).strftime("%m/%d/%Y")

    results = {
        "_query_info": {
            "server":          server,
            "tenant_id":       tenant_id,
            "known_salon_ids": known_salon_ids,
        },
        "cached_global": {"row_count": len(utilisation), "sample": utilisation[:2]},
    }

    # Test with no SalonId (current approach)
    for label, tsd, ted in [("empty_dates", "", ""), ("mmddyyyy_range", sd, ed)]:
        try:
            rows = fetch("XXX_Export_Admin_TUBR_Utilisation", tsd, ted,
                         tenant_id=tenant_id, server=server)
            results[f"no_salonid__{label}"] = {"row_count": len(rows), "sample": rows[:1]}
        except Exception as e:
            results[f"no_salonid__{label}"] = {"error": str(e)}

    # Test passing each known SalonId explicitly in the Salonid parameter
    for salon_id in (known_salon_ids or ["0f3bc7d0-38d4-10be-2960-1c6d71b3dc8a"])[:5]:
        for tsd, ted, dlabel in [("", "", "empty"), (sd, ed, "range")]:
            key = f"salonid_{salon_id[:8]}__{dlabel}"
            try:
                params = {
                    **API_COMMON,
                    "Salonid":    salon_id,
                    "TokenID":    srv["token"],
                    "TenantID":   tenant_id.upper(),
                    "ReportName": "XXX_Export_Admin_TUBR_Utilisation",
                    "startdate":  tsd,
                    "enddate":    ted,
                }
                r    = requests.post(srv["base"], params=params,
                                     headers={"Content-Length": "0"}, timeout=30)
                r.raise_for_status()
                rows = (r.json().get("Data") or {}).get("Array") or []
                results[key] = {"salon_id": salon_id, "row_count": len(rows), "sample": rows[:1]}
            except Exception as e:
                results[key] = {"salon_id": salon_id, "error": str(e)}

    return jsonify(results)


@app.route("/api/refresh", methods=["POST"])
@require_auth
def refresh():
    server    = request.args.get("server", "BETA")
    tenant_id = request.args.get("tenant_id") or None
    prefix    = f"{server}|"
    suffix    = f"|{tenant_id}" if tenant_id else None
    to_delete = [k for k in list(_cache.keys())
                 if k.startswith(prefix) and (suffix is None or k.endswith(suffix))]
    for k in to_delete:
        _cache.pop(k, None)
        _cache_ts.pop(k, None)
    return jsonify(ok=True)


@app.route("/api/search")
@require_auth
def search_clients():
    q         = request.args.get("q", "").lower().strip()
    tenant_id = request.args.get("tenant_id") or None
    server    = request.args.get("server", "BETA")
    if len(q) < 2:
        return jsonify([])
    ctx    = _get_ctx(tenant_id, server)
    if tenant_id and not ctx:
        return jsonify([])
    scored = (ctx or {}).get('all_scored', [])
    results = [c for c in scored if q in c["name"].lower()]
    return jsonify(results[:20])


@app.route("/api/query")
@require_auth
def query_clients():
    q         = request.args.get("q", "").strip()
    tenant_id = request.args.get("tenant_id") or None
    server    = request.args.get("server", "BETA")
    ctx       = _get_ctx(tenant_id, server)
    if tenant_id and not ctx:
        return jsonify({"error": "Salon data not found — please reload the salon data."}), 400
    clients   = (ctx or {}).get('all_clients', [])
    if not q:
        return jsonify({"error": "No query provided"}), 400
    if not clients:
        return jsonify({"error": "No data loaded — load a salon first"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured on this server"}), 500

    t0 = time.time()
    schema = """
NOTE: Booking history, gift cards, and promotions cover the last 2 years only. Client records, tags, and opt-out status are current. Mention this limitation in your description when relevant.

Fields available on each client record:
- name (string): full name
- scls (string): "active" <60 days, "due" 60-120 days, "lapsing" 120-365 days, "lapsed" >365 days, "never" no visit recorded in the last 2 years
- last_visit (string or null): date of last visit e.g. "5 Jan 2026", "31 Dec 2025"
- days_since (int): days since last visit
- n_visits (int): visits in the last 2 years
- total_spend (int £): total spend
- avg_spend (int £): average spend per visit
- avg_gap (int or null): average days between visits
- overdue (int or null): days past their usual visit interval
- pref_day (string): Mon/Tue/Wed/Thu/Fri/Sat/Sun
- pref_time (string): Morning/Lunchtime/Afternoon/Evening
- pref_tm (string): preferred stylist name
- top_cats (array of strings): top 2 service categories e.g. ["Colour","Cut & Finish"]
- all_cats (array of strings): every unique service category the client has had
- departments (array of strings): service departments visited e.g. ["hair"], ["beauty"], ["hair","beauty"]. Values are lowercase "hair" or "beauty".
- top_svcs (array of strings): individual service names e.g. ["Full Head Colour","Ladies Cut & Blow Dry","Balayage"]
- has_future_booking (bool): true if client has an upcoming appointment
- future_svcs (array of strings): service names booked for future appointments
- future_cats (array of strings): service categories for future appointments
- next_booking (string or null): date of their next appointment e.g. "5 May 2026"
- no_shows (int): number of recorded no-shows
- n_stylists (int): number of distinct stylists visited
- pref_salon (string): name of the salon they visit most
- mobile (string): mobile phone number
- email (string): email address
- gender (string): client gender
- birth_month (string): birth month as a number string e.g. "1"=January, "6"=June, "12"=December
- birth_day (string): birth day as a number string e.g. "1", "15", "31"
- points (int): loyalty points balance
- account_balance (float £): account balance e.g. 25.00 (can be negative if in debit)
- age_group (string): age group
- occupation (string): occupation
- how_heard (string): how they heard about the salon
- sms_optout (bool): true if client has opted out of SMS marketing
- email_optout (bool): true if client has opted out of email marketing
- salonspy_optin (bool): true if client has opted in to Salon Spy
- points_enabled (bool): true if loyalty points are enabled for this client
- retail_count (int): number of retail items purchased (0 if none)
- retail_total (int £): total retail spend
- retail_products (array of strings): product names purchased e.g. ["Shampoo X", "Conditioner Y"]
- retail_brands (array of strings): brand/supplier names purchased e.g. ["Kerastase", "L'Oreal"]
- retail_lines (array of strings): brand lines purchased e.g. ["TREATMENTS", "COLOUR", "SHAMPOO"]
- retail_dates (array of strings): ALL retail purchase dates e.g. ["5 Jan 2026","3 Dec 2025"]. Use this to find purchases in a specific month or period.
- score (float 0-100): SMS targeting score
- giftcard_count (int): number of gift cards purchased (0 if none)
- giftcard_total (int £): total value of gift cards purchased
- last_giftcard (string or null): date of most recent gift card purchase e.g. "5 Jan 2026"
- giftcard_dates (array of strings): ALL gift card purchase dates e.g. ["28 Apr 2026","15 Dec 2025"]. Use this to find purchases in a specific month or period.
- promo_count (int): number of promotions used (0 if none)
- promo_names (array of strings): names of promotions used e.g. ["20% off colour", "Refer a Friend"]
- promo_codes (array of strings): promotion codes used e.g. ["REFER2024", "SUMMER20"]
- last_promo (string or null): date of most recent promotion use e.g. "5 Jan 2026"
- promo_dates (array of strings): ALL promotion use dates e.g. ["5 Jan 2026","3 Nov 2025"]. Use this to find promotions used in a specific month or period.
- tags (array of strings): tags applied to the client e.g. ["New", "VIP", "Colour Client"]
- tag_count (int): number of tags applied (0 if none)
"""

    prompt = f"""You are a filter assistant for a hair salon CRM.
Convert the natural language query into JSON filter criteria for the client database.

{schema}

Query: "{q}"

Return ONLY a JSON object — no markdown, no explanation — in this exact structure:
{{
  "filters": [
    {{"field": "fieldname", "op": "operator", "value": <value>}}
  ],
  "logic": "AND",
  "description": "Plain English explanation of the segment"
}}

Supported operators: eq, ne, gt, gte, lt, lte, in (value is a list), contains (array field has an item containing the string as a substring), contains_exact (array field has an item that exactly equals the string — use for codes and tags), not_contains (array field does NOT contain string), every_contains (ALL items in array contain string — use for "only" queries), exists (value true=not null, false=null)

Examples:
"last visit in January 2026" → [{{"field":"last_visit","op":"contains","value":"Jan 2026"}}]
"visited only once" → [{{"field":"n_visits","op":"eq","value":1}}]
"loyal regulars" → [{{"field":"n_visits","op":"gte","value":10}}]
"high value lapsing" → logic AND, [{{"field":"scls","op":"eq","value":"lapsing"}},{{"field":"avg_spend","op":"gte","value":60}}]
"colour clients overdue" → logic AND, [{{"field":"top_cats","op":"contains","value":"Colour"}},{{"field":"overdue","op":"exists","value":true}}]
"clients that have had a beauty service" → [{{"field":"departments","op":"contains","value":"beauty"}}]
"clients that have had a hair service" → [{{"field":"departments","op":"contains","value":"hair"}}]
"clients who visit both hair and beauty" → logic AND, [{{"field":"departments","op":"contains","value":"hair"}},{{"field":"departments","op":"contains","value":"beauty"}}]
"clients that have had a beauty service but not a hair service" → logic AND, [{{"field":"departments","op":"contains","value":"beauty"}},{{"field":"departments","op":"not_contains","value":"hair"}}]
"clients that have had a hair service but not a beauty service" → logic AND, [{{"field":"departments","op":"contains","value":"hair"}},{{"field":"departments","op":"not_contains","value":"beauty"}}]
"only ever seen one stylist" → [{{"field":"n_stylists","op":"eq","value":1}}]
"no-show history" → [{{"field":"no_shows","op":"gte","value":1}}]
"clients with a future booking" → [{{"field":"has_future_booking","op":"eq","value":true}}]
"clients with no future booking" → [{{"field":"has_future_booking","op":"eq","value":false}}]
IMPORTANT: when the query mentions a specific future service or treatment, ALWAYS use future_svcs (not has_future_booking):
"clients booked for a blow dry" → [{{"field":"future_svcs","op":"contains","value":"blow dry"}}]
"clients that have a future booking for a blow dry" → [{{"field":"future_svcs","op":"contains","value":"blow dry"}}]
"clients that have a future booking for a blow dry only" → [{{"field":"future_svcs","op":"every_contains","value":"blow dry"}}]
"clients with a colour appointment coming up" → [{{"field":"future_svcs","op":"contains","value":"colour"}}]
"clients booked in for a cut" → [{{"field":"future_svcs","op":"contains","value":"cut"}}]
"future colour appointments" → [{{"field":"future_cats","op":"contains","value":"Colour"}}]
"clients who bought a gift card" → [{{"field":"giftcard_count","op":"gte","value":1}}]
"gift card purchases in March 2026" → [{{"field":"giftcard_dates","op":"contains","value":"Mar 2026"}}]
"gift card purchases in December 2025" → [{{"field":"giftcard_dates","op":"contains","value":"Dec 2025"}}]
"high value gift card buyers" → [{{"field":"giftcard_total","op":"gte","value":100}}]
"clients who have opted out of SMS" → [{{"field":"sms_optout","op":"eq","value":true}}]
"clients who have not opted out of SMS" → [{{"field":"sms_optout","op":"eq","value":false}}]
"clients who have opted out of email" → [{{"field":"email_optout","op":"eq","value":true}}]
"clients opted in to Salon Spy" → [{{"field":"salonspy_optin","op":"eq","value":true}}]
"clients not opted in to Salon Spy" → [{{"field":"salonspy_optin","op":"eq","value":false}}]
"clients with points enabled" → [{{"field":"points_enabled","op":"eq","value":true}}]
"clients without points enabled" → [{{"field":"points_enabled","op":"eq","value":false}}]
"clients who bought retail" → [{{"field":"retail_count","op":"gte","value":1}}]
"clients who spent over £50 on retail" → [{{"field":"retail_total","op":"gte","value":50}}]
"clients who bought Kerastase" → [{{"field":"retail_brands","op":"contains","value":"Kerastase"}}]
"clients who bought a treatment product" → [{{"field":"retail_lines","op":"contains","value":"TREATMENT"}}]
"clients who bought shampoo X" → [{{"field":"retail_products","op":"contains","value":"shampoo x"}}]
"clients who bought retail in December 2025" → [{{"field":"retail_dates","op":"contains","value":"Dec 2025"}}]
"clients who bought Kerastase in 2026" → logic AND, [{{"field":"retail_brands","op":"contains","value":"Kerastase"}},{{"field":"retail_dates","op":"contains","value":"2026"}}]
"clients with a balance greater than 100" → [{{"field":"account_balance","op":"gt","value":100}}]
"clients with a negative balance" → [{{"field":"account_balance","op":"lt","value":0}}]
"clients tagged with New" → [{{"field":"tags","op":"contains_exact","value":"New"}}]
"clients with any tag" → [{{"field":"tag_count","op":"gte","value":1}}]
"VIP clients" → [{{"field":"tags","op":"contains_exact","value":"VIP"}}]
"clients who used a promotion" → [{{"field":"promo_count","op":"gte","value":1}}]
Promotion field rules:
- promo_codes = short alphanumeric identifiers e.g. "SAF30", "SUMMER20" → use contains_exact
- promo_names = human-readable descriptions e.g. "BIRTHDAY £15", "Refer a Friend" → use contains
- If the query mentions a descriptive name (has spaces, £, %, words), use promo_names with contains
- If the query mentions a short code (no spaces, alphanumeric), use promo_codes with contains_exact
- If ambiguous, search both with OR logic
"clients who used promotion code SAF30" → [{{"field":"promo_codes","op":"contains_exact","value":"SAF30"}}]
"clients who used promotion code SUMMER20" → [{{"field":"promo_codes","op":"contains_exact","value":"SUMMER20"}}]
"clients who used the refer a friend promotion" → [{{"field":"promo_names","op":"contains","value":"refer a friend"}}]
"clients who used the promotion BIRTHDAY £15" → [{{"field":"promo_names","op":"contains","value":"BIRTHDAY £15"}}]
"clients who used the 20% off promotion" → [{{"field":"promo_names","op":"contains","value":"20% off"}}]
"clients who used promotion SAF30" → logic OR, [{{"field":"promo_codes","op":"contains_exact","value":"SAF30"}},{{"field":"promo_names","op":"contains","value":"SAF30"}}]
IMPORTANT: always use contains_exact (not contains) for promo_codes — these are exact identifiers, not free text.
"promotion uses in January 2026" → [{{"field":"promo_dates","op":"contains","value":"Jan 2026"}}]
"""

    try:
        import anthropic as _anthropic
        ai  = _anthropic.Anthropic(api_key=api_key)
        msg = ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        criteria = json.loads(raw.strip())

    except Exception as e:
        _log('query',
             salon=_salon_label(),
             question=q[:500],
             response_ms=round((time.time() - t0) * 1000),
             error=str(e)[:500],
        )
        return jsonify({"error": f"Could not interpret query: {e}"}), 400

    filters     = criteria.get("filters", [])
    logic       = criteria.get("logic", "AND").upper()
    description = criteria.get("description", q)

    def matches(client, f):
        field, op, val = f.get("field"), f.get("op"), f.get("value")
        cv = client.get(field)
        # normalise strings for case-insensitive comparison; handle bool specially
        if isinstance(cv, bool) or isinstance(val, bool):
            if op == "eq": return bool(cv) == bool(val)
            if op == "ne": return bool(cv) != bool(val)
        cv_cmp  = cv.lower()  if isinstance(cv, str)  else cv
        val_cmp = val.lower() if isinstance(val, str) else val
        if op == "eq":       return cv_cmp == val_cmp
        if op == "ne":       return cv_cmp != val_cmp
        if op == "gt":       return cv is not None and cv > val
        if op == "gte":      return cv is not None and cv >= val
        if op == "lt":       return cv is not None and cv < val
        if op == "lte":      return cv is not None and cv <= val
        if op == "in":
            val_list = [v.lower() if isinstance(v, str) else v for v in val]
            return cv_cmp in val_list
        if op == "contains":
            if isinstance(cv, list):
                return any(val_cmp in c.lower() for c in cv)
            if isinstance(cv, str):
                return val_cmp in cv_cmp
        if op == "contains_exact":
            if isinstance(cv, list):
                return any(val_cmp == c.strip().lower() for c in cv)
            if isinstance(cv, str):
                return val_cmp == cv_cmp.strip()
        if op == "not_contains":
            if isinstance(cv, list):
                return not any(val_cmp in c.lower() for c in cv)
            if isinstance(cv, str):
                return val_cmp not in cv_cmp
        if op == "every_contains":
            if isinstance(cv, list) and cv:
                return all(val_cmp in c.lower() for c in cv)
            return False
        if op == "exists":   return (cv is not None) == val
        return False

    results = [
        c for c in clients
        if (any if logic == "OR" else all)(matches(c, f) for f in filters)
    ] if filters else []

    _log('query',
         salon=_salon_label(),
         question=q[:500],
         result_count=len(results),
         result_title=description[:200],
         response_ms=round((time.time() - t0) * 1000),
    )
    return jsonify({"clients": results, "total": len(results),
                    "description": description, "criteria": criteria})


def _month_key(date_str):
    parts = date_str.strip().split()
    return f"{parts[1]} {parts[2]}" if len(parts) >= 3 else None


def _sort_months(keys):
    def parse(mk):
        try:
            return datetime.strptime(mk, "%b %Y")
        except ValueError:
            return datetime.min
    return sorted(keys, key=parse, reverse=True)


def build_analysis_context(question="", ctx=None):
    # Unpack per-tenant context
    all_clients          = (ctx or {}).get('all_clients', [])
    all_scored           = (ctx or {}).get('all_scored', [])
    booking_stats        = (ctx or {}).get('booking_stats', {})
    service_daily        = (ctx or {}).get('service_daily', {})
    service_weekly       = (ctx or {}).get('service_weekly', {})
    service_monthly      = (ctx or {}).get('service_monthly', {})
    service_salon_monthly= (ctx or {}).get('service_salon_monthly', {})
    service_cat_monthly  = (ctx or {}).get('service_cat_monthly', {})
    retail_summary       = (ctx or {}).get('retail_summary', {})
    utilisation          = (ctx or {}).get('utilisation', [])
    team_kpis            = (ctx or {}).get('team_kpis', {})
    loaded_salon_name    = (ctx or {}).get('loaded_salon_name', '')
    loaded_tenant_name   = (ctx or {}).get('loaded_tenant_name', '')
    loaded_account_code  = (ctx or {}).get('loaded_account_code', '')
    loaded_tenant_id     = (ctx or {}).get('loaded_tenant_id', '')
    loaded_server        = (ctx or {}).get('loaded_server', 'BETA')
    salon_map            = (ctx or {}).get('salon_map', {})

    if not all_clients:
        return "No data loaded."

    today = date.today()
    status_counts = Counter(c.get("scls", "") for c in all_clients)

    stylist_data = defaultdict(lambda: {"clients": 0, "revenue": 0, "visits": 0})
    for c in all_clients:
        tm = c.get("pref_tm", "?")
        stylist_data[tm]["clients"] += 1
        stylist_data[tm]["revenue"] += c.get("total_spend", 0)
        stylist_data[tm]["visits"]  += c.get("n_visits", 0)

    all_cats = []
    for c in all_clients:
        all_cats.extend(c.get("top_cats", []))

    # Monthly retail: distribute each client's total spend evenly across their purchase months
    monthly_retail = defaultdict(lambda: {"clients": 0, "spend": 0.0})
    for c in all_clients:
        months = list({_month_key(d) for d in (c.get("retail_dates") or []) if _month_key(d)})
        if not months:
            continue
        share = c.get("retail_total", 0) / len(months)
        for mk in months:
            monthly_retail[mk]["clients"] += 1
            monthly_retail[mk]["spend"]   += share

    monthly_gc = defaultdict(int)
    for c in all_clients:
        for mk in {_month_key(d) for d in (c.get("giftcard_dates") or []) if _month_key(d)}:
            monthly_gc[mk] += 1

    monthly_promo = defaultdict(int)
    for c in all_clients:
        for mk in {_month_key(d) for d in (c.get("promo_dates") or []) if _month_key(d)}:
            monthly_promo[mk] += 1

    # Segment deep-dives (aggregated, no individual rows)
    segments = {}
    for seg in ("active", "due", "lapsing", "lapsed", "never"):
        grp = [c for c in all_clients if c.get("scls") == seg]
        if grp:
            segments[seg] = {
                "count":        len(grp),
                "total_revenue": sum(c.get("total_spend", 0) for c in grp),
                "avg_spend":    round(sum(c.get("avg_spend", 0) for c in grp) / len(grp)),
                "avg_visits":   round(sum(c.get("n_visits", 0) for c in grp) / len(grp), 1),
                "retail_buyers": sum(1 for c in grp if c.get("retail_count", 0) > 0),
            }

    # Specific client lookup — include full record if the question names someone
    named_clients = []
    if question:
        q_lower = question.lower()
        q_words = set(q_lower.split())
        for c in all_clients:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            first = name.split()[0].lower()
            # Only match on first name if it's at least 4 chars (avoids "is", "new", etc.)
            if name.lower() in q_lower or (len(first) >= 4 and first in q_words):
                named_clients.append(c)

    this_monday  = today - timedelta(days=today.weekday())
    last_monday  = this_monday - timedelta(days=7)
    last_sunday  = this_monday - timedelta(days=1)

    all_salon_names = sorted(set(salon_map.values())) if salon_map else []
    tenant_label = (' – '.join(p for p in [loaded_account_code, loaded_tenant_name] if p)) or loaded_salon_name or loaded_tenant_id or 'Unknown'
    if len(all_salon_names) > 1:
        salons_str = ', '.join(all_salon_names)
        scope_note = f"DATA SCOPE: This data covers ALL {len(all_salon_names)} salons in this tenant: {salons_str}. Client totals and revenue figures are combined across all locations."
    else:
        scope_note = f"DATA SCOPE: Single salon — {all_salon_names[0] if all_salon_names else tenant_label}."

    lines = [
        f"SALON / TENANT: {tenant_label} (server: {loaded_server}) — data as of {today.strftime('%-d %b %Y')}",
        scope_note,
        f"DATE CONTEXT: Today={today.strftime('%-d %b %Y')} | "
        f"This week=w/c {this_monday.strftime('%-d %b %Y')} | "
        f"Last week={last_monday.strftime('%-d %b %Y')}–{last_sunday.strftime('%-d %b %Y')}",
        f"IMPORTANT: When answering questions, make clear whether figures are for a specific salon or the whole group. "
        f"Use the PrefSalon field on client records to attribute clients to their home salon.",
        f"Total clients: {len(all_clients)} | Active scoring pool: {len(all_scored)}",
        f"Total 2yr service revenue: £{sum(c.get('total_spend',0) for c in all_clients):,.0f}",
        f"Total 2yr retail spend:    £{sum(c.get('retail_total',0) for c in all_clients):,.0f}",
        f"Total 2yr gift card spend: £{sum(c.get('giftcard_total',0) for c in all_clients):,.0f}",
        "",
        "CLIENT STATUS BREAKDOWN:",
    ]
    for seg, d in segments.items():
        lines.append(
            f"  {seg.title()} — {d['count']} clients | "
            f"£{d['total_revenue']:,.0f} total revenue | "
            f"£{d['avg_spend']} avg spend/visit | "
            f"{d['avg_visits']} avg visits | "
            f"{d['retail_buyers']} retail buyers"
        )

    lines += ["", "STYLISTS (by revenue):"]
    for nm, d in sorted(stylist_data.items(), key=lambda x: -x[1]["revenue"])[:15]:
        avg = d["revenue"] / d["clients"] if d["clients"] else 0
        lines.append(f"  {nm}: {d['clients']} clients, {d['visits']} visits, "
                     f"£{d['revenue']:,.0f} revenue, £{avg:.0f} avg/client")

    if team_kpis:
        lines += ["", "TEAM MEMBER KPI TARGETS:",
                  "Name,Period,RetailTarget£,CareFactorTarget%,ReBookingsTarget%,"
                  "ClientCountTarget,RequestCountTarget,AvgServicesTarget,UtilizationTarget%"]
        for name, k in sorted(team_kpis.items()):
            lines.append(
                f"{name},{k['period']},£{k['kpi_retail']:.0f},{k['kpi_care_factor']:.1f}%,"
                f"{k['kpi_rebookings']:.1f}%,{k['kpi_client_count']:.0f},"
                f"{k['kpi_req_count']:.0f},{k['kpi_avg_services']:.1f},{k['kpi_utilization']:.1f}%"
            )

    lines += ["", "TOP SERVICE CATEGORIES:"]
    for cat, cnt in Counter(all_cats).most_common(15):
        lines.append(f"  {cat}: {cnt} clients")

    if booking_stats:
        st = booking_stats.get("status", {})
        src = booking_stats.get("source", {})
        tot = sum(st.values()) or 1
        lines += ["", "BOOKING STATUS BREAKDOWN (all past bookings):"]
        for label, cnt in st.items():
            lines.append(f"  {label}: {cnt:,} ({cnt/tot*100:.1f}%)")
        if src:
            tot_src = sum(src.values()) or 1
            lines += ["", "BOOKING SOURCE (how clients book):"]
            for label, cnt in src.items():
                lines.append(f"  {label}: {cnt:,} ({cnt/tot_src*100:.1f}%)")

    if service_daily:
        cutoff = (today - timedelta(days=90)).isoformat()
        recent_days = sorted(d for d in service_daily if d >= cutoff)
        lines += ["", "DAILY SERVICE DATA (last 90 days — use for specific dates, 'yesterday', 'today', daily questions):",
                  "Date,ServiceRevenue£,Visits,NoShows,NoShowValue£,OnlineBookings,UniqueClients"]
        for day in recent_days:
            d = service_daily[day]
            day_label = date.fromisoformat(day).strftime("%a %-d %b %Y")
            lines.append(f"{day_label},£{d['revenue']:,},{d['visits']},"
                         f"{d.get('no_shows',0)},£{d.get('no_show_value',0):,},{d.get('online',0)},{d['clients']}")

    if service_weekly:
        recent_wks = sorted(service_weekly.keys(), reverse=True)[:52]
        lines += ["", "WEEKLY SERVICE DATA (most recent 52 weeks — use this for 'last week', 'this week', weekly questions):",
                  "WeekCommencing,ServiceRevenue£,Visits,NoShows,NoShowValue£,OnlineBookings,UniqueClients"]
        for wk in sorted(recent_wks):
            w = service_weekly[wk]
            wk_label = date.fromisoformat(wk).strftime("%-d %b %Y")
            lines.append(f"w/c {wk_label},£{w['revenue']:,},{w['visits']},"
                         f"{w.get('no_shows',0)},£{w.get('no_show_value',0):,},{w.get('online',0)},{w['clients']}")

    if service_monthly:
        lines += ["", "MONTHLY SERVICE REVENUE & VISITS:",
                  "Month,ServiceRevenue£,Visits,NoShows,NoShowValue£,OnlineBookings,UniqueClients"]
        for mk in _sort_months(service_monthly)[:24]:
            m = service_monthly[mk]
            lines.append(f"{mk},£{m['revenue']:,},{m['visits']},"
                         f"{m.get('no_shows',0)},£{m.get('no_show_value',0):,},{m.get('online',0)},{m['clients']}")

    if service_salon_monthly:
        salon_totals = {s: sum(d["revenue"] for d in months.values())
                        for s, months in service_salon_monthly.items()}
        lines += ["", "MONTHLY REVENUE & VISITS BY SALON:",
                  "Month,Salon,Revenue£,Visits,NoShows,NoShowValue£"]
        for mk in _sort_months(service_monthly)[:24]:
            for salon in sorted(salon_totals, key=lambda s: -salon_totals[s]):
                d = service_salon_monthly.get(salon, {}).get(mk)
                if d and d["revenue"] > 0:
                    lines.append(f"{mk},{salon},£{d['revenue']:,},{d['visits']},"
                                 f"{d['no_shows']},£{d['no_show_value']:,}")

    if service_cat_monthly:
        # Rank categories by total 2yr revenue; include top 15
        cat_totals = {cat: sum(d["revenue"] for d in months.values())
                      for cat, months in service_cat_monthly.items()}
        top_cats_ranked = sorted(cat_totals, key=lambda c: -cat_totals[c])[:15]
        lines += ["", "MONTHLY REVENUE & VISITS BY SERVICE CATEGORY (paid bookings only):",
                  "Month,Category,Revenue£,Visits"]
        for mk in _sort_months(service_monthly)[:24]:
            for cat in top_cats_ranked:
                d = service_cat_monthly.get(cat, {}).get(mk)
                if d and d["revenue"] > 0:
                    lines.append(f"{mk},{cat},£{d['revenue']:,},{d['visits']}")

    # Retail product aggregates — use transaction-level data if available,
    # fall back to counting product names across client records
    if retail_summary.get("products"):
        rs = retail_summary
        lines += ["", "TOP RETAIL PRODUCTS (transaction data — units sold + revenue):",
                  "Product,ClientsBuying,UnitsSold,Revenue£"]
        for name, d in sorted(rs["products"].items(), key=lambda x: -x[1]["revenue"])[:30]:
            lines.append(f"{name},{d['clients']},{d['units']},£{d['revenue']}")

        lines += ["", "TOP RETAIL BRANDS (by revenue):",
                  "Brand,ClientsBuying,UnitsSold,Revenue£"]
        for brand, d in sorted(rs["brands"].items(), key=lambda x: -x[1]["revenue"])[:15]:
            lines.append(f"{brand},{d['clients']},{d['units']},£{d['revenue']}")

        lines += ["", "TOP RETAIL LINES (by revenue):",
                  "Line,ClientsBuying,UnitsSold,Revenue£"]
        for line, d in sorted(rs["lines"].items(), key=lambda x: -x[1]["revenue"])[:15]:
            lines.append(f"{line},{d['clients']},{d['units']},£{d['revenue']}")

        if rs.get("monthly"):
            lines += ["", "MONTHLY RETAIL SALES (from transaction data):",
                      "Month,UnitsSold,Revenue£"]
            for mk in _sort_months(rs["monthly"])[:24]:
                m = rs["monthly"][mk]
                lines.append(f"{mk},{m['units']},£{m['revenue']}")

        if rs.get("monthly_products"):
            lines += ["", "TOP RETAIL PRODUCTS BY MONTH (top 20 per month by revenue):",
                      "Month,Product,UnitsSold,Revenue£"]
            for mk in _sort_months(rs["monthly_products"])[:24]:
                prods = rs["monthly_products"][mk]
                for prod, d in sorted(prods.items(), key=lambda x: -x[1]["revenue"])[:20]:
                    lines.append(f"{mk},{prod},{d['units']},£{d['revenue']}")

        if rs.get("monthly_brands"):
            lines += ["", "TOP RETAIL BRANDS BY MONTH (top 10 per month by revenue):",
                      "Month,Brand,UnitsSold,Revenue£"]
            for mk in _sort_months(rs["monthly_brands"])[:24]:
                brands = rs["monthly_brands"][mk]
                for brand, d in sorted(brands.items(), key=lambda x: -x[1]["revenue"])[:10]:
                    lines.append(f"{mk},{brand},{d['units']},£{d['revenue']}")

        if rs.get("salon_monthly"):
            salon_totals = {s: sum(d["revenue"] for d in months.values())
                            for s, months in rs["salon_monthly"].items()}
            lines += ["", "MONTHLY RETAIL SALES BY SALON:",
                      "Month,Salon,UnitsSold,Revenue£"]
            for mk in _sort_months(retail_summary.get("monthly", {}))[:24]:
                for salon in sorted(salon_totals, key=lambda s: -salon_totals[s]):
                    d = rs["salon_monthly"].get(salon, {}).get(mk)
                    if d and d["revenue"] > 0:
                        lines.append(f"{mk},{salon},{d['units']},£{d['revenue']}")
    else:
        # Fallback: aggregate product/brand names from client records (buyer counts only)
        prod_counts  = Counter()
        brand_counts = Counter()
        line_counts  = Counter()
        for c in all_clients:
            for p in (c.get("retail_products") or []):
                if p: prod_counts[p] += 1
            for b in (c.get("retail_brands") or []):
                if b: brand_counts[b] += 1
            for l in (c.get("retail_lines") or []):
                if l: line_counts[l] += 1
        if prod_counts:
            lines += ["", "TOP RETAIL PRODUCTS (client buyer counts — reload data for full revenue figures):",
                      "Product,ClientsBuying"]
            for name, cnt in prod_counts.most_common(30):
                lines.append(f"{name},{cnt}")
        if brand_counts:
            lines += ["", "TOP RETAIL BRANDS (client buyer counts):", "Brand,ClientsBuying"]
            for brand, cnt in brand_counts.most_common(15):
                lines.append(f"{brand},{cnt}")
        if monthly_retail:
            lines += ["", "MONTHLY RETAIL SPEND (estimated):", "Month,BuyerCount,EstRevenue£"]
            for mk in _sort_months(monthly_retail)[:24]:
                m = monthly_retail[mk]
                lines.append(f"{mk},{m['clients']},~£{m['spend']:,.0f}")

    if monthly_gc:
        lines += ["", "MONTHLY GIFT CARD BUYERS:"]
        for mk in _sort_months(monthly_gc)[:24]:
            lines.append(f"  {mk}: {monthly_gc[mk]} clients")

    if monthly_promo:
        lines += ["", "MONTHLY PROMOTION USES:"]
        for mk in _sort_months(monthly_promo)[:24]:
            lines.append(f"  {mk}: {monthly_promo[mk]} clients")

    lines += ["", "SPEND DISTRIBUTION (avg spend per visit):"]
    for lo, hi, label in [(0,50,"£0-50"),(50,100,"£50-100"),(100,200,"£100-200"),
                           (200,500,"£200-500"),(500,1000,"£500-1000"),(1000,9e9,"£1000+")]:
        cnt = sum(1 for c in all_clients if lo <= c.get("avg_spend", 0) < hi)
        lines.append(f"  {label}: {cnt} clients")

    # Top 100 clients by total spend (compact rows, no arrays)
    top100 = sorted(all_clients, key=lambda x: x.get("total_spend", 0), reverse=True)[:100]
    lines += ["", "TOP 100 CLIENTS BY TOTAL SPEND:",
              "Name,Status,DaysSince,Visits,ServiceRevenue,AvgSpend,RetailTotal,GiftcardTotal,Stylist,PrefSalon,Services"]
    for c in top100:
        cats = "|".join(c.get("top_cats", []))
        lines.append(
            f"{c['name']},{c.get('scls','')},{c.get('days_since','')},{c.get('n_visits',0)},"
            f"£{c.get('total_spend',0)},£{c.get('avg_spend',0)},"
            f"£{c.get('retail_total',0)},£{c.get('giftcard_total',0)},"
            f"{c.get('pref_tm','')},{c.get('pref_salon','')},{cats}"
        )

    # Named client spotlight
    if named_clients:
        lines += ["", "NAMED CLIENT RECORDS (full detail):"]
        for c in named_clients[:5]:
            lines += [
                f"  Name: {c['name']}",
                f"  Status: {c.get('status','')} | Score: {c.get('score','')} | Days since visit: {c.get('days_since','')}",
                f"  Visits: {c.get('n_visits',0)} | Last visit: {c.get('last_visit','')} | Avg gap: {c.get('avg_gap','')}d",
                f"  Service revenue: £{c.get('total_spend',0)} | Avg spend: £{c.get('avg_spend',0)} | Overdue: {c.get('overdue','')}d",
                f"  Retail: {c.get('retail_count',0)} purchases, £{c.get('retail_total',0)} total",
                f"  Gift cards: {c.get('giftcard_count',0)}, £{c.get('giftcard_total',0)} total",
                f"  Promotions: {c.get('promo_count',0)} — {', '.join(c.get('promo_names') or [])}",
                f"  Preferred stylist: {c.get('pref_tm','')} | Day: {c.get('pref_day','')} {c.get('pref_time','')}",
                f"  Services: {', '.join(c.get('top_cats',[]))}",
                f"  No-shows: {c.get('no_shows',0)} | Points: {c.get('points',0)} | Balance: £{c.get('account_balance',0)}",
                f"  SMS opt-out: {c.get('sms_optout',False)} | Email opt-out: {c.get('email_optout',False)}",
                "",
            ]

    # Utilisation data — aggregated from daily rows (ClientMinutes / AvailablTime)
    if utilisation:
        zero = lambda: {"client_mins": 0, "avail_mins": 0, "days": 0,
                        "future_days": 0, "future_client_mins": 0, "future_avail_mins": 0}
        salon_util   = defaultdict(zero)
        stylist_util = defaultdict(zero)
        # keyed (salon, stylist) so we can show stylist-within-salon
        salon_stylist_util = defaultdict(zero)
        monthly_util = defaultdict(lambda: {"client_mins": 0, "avail_mins": 0})

        for row in utilisation:
            stylist    = row.get("StylistName", "Unknown")
            salon      = row.get("SalonName") or row.get("SalonId", "Unknown salon")
            cm         = int(row.get("ClientMinutes", 0) or 0)
            am         = int(row.get("AvailablTime",  0) or 0)
            if am <= 0:
                continue
            dt = None
            for dfmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
                try:
                    dt = datetime.strptime(row.get("Date", ""), dfmt)
                    break
                except ValueError:
                    pass
            if not dt:
                continue

            is_future = dt.date() > today
            if is_future:
                for d in (salon_util[salon], stylist_util[stylist],
                          salon_stylist_util[(salon, stylist)]):
                    d["future_days"]        += 1
                    d["future_client_mins"] += cm
                    d["future_avail_mins"]  += am
            else:
                mk = dt.strftime("%b %Y")
                for d in (salon_util[salon], stylist_util[stylist],
                          salon_stylist_util[(salon, stylist)]):
                    d["client_mins"] += cm
                    d["avail_mins"]  += am
                    d["days"]        += 1
                monthly_util[mk]["client_mins"] += cm
                monthly_util[mk]["avail_mins"]  += am

        def util_pct(d):
            return round(d["client_mins"] / d["avail_mins"] * 100, 1) if d["avail_mins"] else 0

        # Per-salon summary
        lines += ["", "UTILISATION BY SALON (past 6 months):",
                  "Salon,WorkingDays,ClientMins,AvailMins,Utilisation%"]
        for salon, d in sorted(salon_util.items(), key=lambda x: -util_pct(x[1])):
            lines.append(f"{salon},{d['days']},{d['client_mins']},{d['avail_mins']},{util_pct(d)}%")

        # Per-stylist within each salon
        lines += ["", "UTILISATION BY STYLIST PER SALON (past 6 months):",
                  "Salon,Stylist,WorkingDays,ClientMins,AvailMins,Utilisation%"]
        for (salon, stylist), d in sorted(salon_stylist_util.items(),
                                          key=lambda x: (-util_pct(x[1]), x[0][0])):
            if d["avail_mins"] == 0:
                continue
            lines.append(f"{salon},{stylist},{d['days']},{d['client_mins']},{d['avail_mins']},{util_pct(d)}%")

        if monthly_util:
            lines += ["", "MONTHLY OVERALL UTILISATION (past 6 months):",
                      "Month,ClientMins,AvailMins,Utilisation%"]
            for mk in _sort_months(monthly_util):
                m = monthly_util[mk]
                if m["avail_mins"] == 0:
                    continue
                lines.append(f"{mk},{m['client_mins']},{m['avail_mins']},{util_pct(m)}%")

        # Forward diary — per salon
        future_salons = {s: d for s, d in salon_util.items() if d["future_avail_mins"] > 0}
        if future_salons:
            lines += ["", "FORWARD DIARY BY SALON — next 3 months:",
                      "Salon,FutureDays,BookedMins,AvailMins,BookedPct%,AvailabilityPct%"]
            for salon, d in sorted(future_salons.items(),
                                   key=lambda x: x[1]["future_client_mins"] / max(x[1]["future_avail_mins"], 1)):
                booked = round(d["future_client_mins"] / d["future_avail_mins"] * 100, 1)
                avail  = round(100 - booked, 1)
                lines.append(f"{salon},{d['future_days']},{d['future_client_mins']},"
                             f"{d['future_avail_mins']},{booked}%,{avail}%")

        # Forward diary — per stylist within salon
        future_ss = {k: d for k, d in salon_stylist_util.items() if d["future_avail_mins"] > 0}
        if future_ss:
            lines += ["", "FORWARD DIARY BY STYLIST PER SALON — next 3 months:",
                      "Salon,Stylist,FutureDays,BookedMins,AvailMins,BookedPct%,AvailabilityPct%"]
            for (salon, stylist), d in sorted(future_ss.items(),
                                              key=lambda x: x[1]["future_client_mins"] / max(x[1]["future_avail_mins"], 1)):
                booked = round(d["future_client_mins"] / d["future_avail_mins"] * 100, 1)
                avail  = round(100 - booked, 1)
                lines.append(f"{salon},{stylist},{d['future_days']},{d['future_client_mins']},"
                             f"{d['future_avail_mins']},{booked}%,{avail}%")

    return "\n".join(lines)


@app.route("/api/analyse", methods=["POST"])
@require_auth
def analyse():
    """Start a background analysis job; returns job_id immediately."""
    try:
        import anthropic as _anthropic

        body            = request.get_json(silent=True) or {}
        question        = (body.get("question") or "").strip()
        fmt             = body.get("format") or "dashboard"
        previous_result = body.get("previous_result")  # optional JSON of prior analysis
        tenant_id       = body.get("tenant_id") or None
        server          = body.get("server") or "BETA"
        ctx             = _get_ctx(tenant_id, server)
        if not question:
            return jsonify(error="No question provided"), 400
        if tenant_id and not ctx:
            return jsonify(error="Salon data not found — please reload the salon data."), 400
        if not ctx:
            return jsonify(error="No data loaded — load a salon first."), 400

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return jsonify(error="ANTHROPIC_API_KEY is not configured on this server"), 500

        job_id = str(uuid.uuid4())
        _jobs[job_id] = {"status": "loading", "step": "Thinking…"}
        _current_user = flask_session.get('username', '')

        def worker():
            t0 = time.time()
            try:
                fmt_instructions = {
                    "dashboard": (
                        'Return JSON: {"title":"...","format":"dashboard","summary":"1-2 sentence summary",'
                        '"kpis":[{"label":"...","value":"...","trend":"up|down|neutral","detail":"..."}],'
                        '"sections":[{"title":"...","insight":"...","items":[{"label":"...","value":"..."}]}]}'
                        " Include 3-6 KPIs and 1-3 relevant sections."
                    ),
                    "list": (
                        'Return JSON: {"title":"...","format":"list","summary":"...",'
                        '"columns":["Col1","Col2"],"rows":[["val1","val2"],...]} '
                        "Include all relevant rows sorted meaningfully."
                    ),
                    "report": (
                        'Return JSON: {"title":"...","format":"report","summary":"executive summary",'
                        '"sections":[{"heading":"...","body":"detailed paragraph"}],"conclusion":"..."}'
                        " Include 3-5 well-developed sections."
                    ),
                    "chart": (
                        'Return JSON: {"title":"...","format":"chart","summary":"1-2 sentence insight",'
                        '"charts":[{"type":"bar|line|pie|doughnut|horizontalBar","title":"...","insight":"optional 1-line note",'
                        '"labels":["label1","label2",...],"datasets":[{"label":"series name","data":[1,2,3]}]}]}'
                        " Include 1-4 charts that best visualise the data. "
                        "Use bar for comparisons, horizontalBar for rankings, line for trends over time, "
                        "pie/doughnut for proportions. Keep labels short. Numbers only in data arrays (no £ symbols)."
                    ),
                }
                system = (
                    "You are an expert salon business analyst with access to live UK hair salon data. "
                    "Analyse the data carefully and answer the user's question accurately. "
                    "All monetary values are in British Pounds (£). "
                    "Return ONLY valid JSON — no markdown, no code blocks, no extra text. "
                    + fmt_instructions.get(fmt, fmt_instructions["dashboard"])
                )
                _jobs[job_id]["step"] = "Building data context…"
                context  = build_analysis_context(question, ctx=ctx)
                prev_block = ""
                if previous_result:
                    prev_block = (
                        "\n\nPREVIOUS ANALYSIS (the result you already showed the user — "
                        "use this as context for the follow-up question):\n"
                        + json.dumps(previous_result, ensure_ascii=False)
                    )
                user_msg = (
                    f"SALON DATA:\n{context}"
                    f"{prev_block}"
                    f"\n\nQUESTION: {question}\n\nOutput format: {fmt}"
                )
                app.logger.info("ANALYSE question=%r fmt=%s context_chars=%d followup=%s",
                                question, fmt, len(context), bool(previous_result))

                _jobs[job_id]["step"] = "Asking Claude…"
                ai  = _anthropic.Anthropic(api_key=api_key)
                msg = ai.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=8192,
                    system=system,
                    messages=[{"role": "user", "content": user_msg}],
                )
                text = msg.content[0].text.strip()
                app.logger.info("ANALYSE done chars=%d stop=%s in=%d out=%d",
                                len(text), msg.stop_reason,
                                msg.usage.input_tokens, msg.usage.output_tokens)

                if text.startswith("```"):
                    text = text.split("```", 1)[1]
                    if text.startswith("json"):
                        text = text[4:]
                    text = text.rsplit("```", 1)[0]

                text = text.strip()
                try:
                    result = json.loads(text)
                except json.JSONDecodeError as je:
                    app.logger.warning("JSON parse failed (%s) — attempting repair. Raw text[:500]=%r",
                                       je, text[:500])
                    from json_repair import repair_json
                    result = json.loads(repair_json(text))
                result["_usage"] = {
                    "input_tokens":  msg.usage.input_tokens,
                    "output_tokens": msg.usage.output_tokens,
                }
                _log('analyse',
                     salon=_salon_label(ctx),
                     username=_current_user,
                     question=question[:500],
                     format=fmt,
                     is_followup=1 if previous_result else 0,
                     result_title=(result.get('title') or '')[:200],
                     result_summary=(result.get('summary') or '')[:500],
                     response_ms=round((time.time() - t0) * 1000),
                     input_tokens=msg.usage.input_tokens,
                     output_tokens=msg.usage.output_tokens,
                )
                _jobs[job_id] = {"status": "done", "data": result}
            except Exception as e:
                app.logger.exception("ANALYSE worker error: %s", e)
                _log('analyse',
                     salon=_salon_label(ctx),
                     username=_current_user,
                     question=question[:500],
                     format=fmt,
                     is_followup=1 if previous_result else 0,
                     response_ms=round((time.time() - t0) * 1000),
                     error=str(e)[:500],
                )
                _jobs[job_id] = {"status": "error", "error": str(e)}

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"job_id": job_id})

    except Exception as e:
        app.logger.exception("ANALYSE error: %s", e)
        return jsonify(error=str(e)), 500


@app.route("/api/log", methods=["POST"])
@require_auth
def client_log():
    """Receives lightweight frontend events (page loads, UI interactions)."""
    body       = request.get_json(silent=True) or {}
    event_type = (body.get("event_type") or "client_event")[:50]
    code       = (body.get("account_code") or "").strip()
    detail     = (body.get("detail") or "").strip()
    salon      = (f"{code} – {detail}" if (code and detail) else code or detail or _salon_label())[:100]
    _log(event_type,
         salon=salon,
         question=detail[:500],
    )
    return jsonify(ok=True)


_LOGIN_PAGE = """<!DOCTYPE html>
<html>
<head>
<title>SalonIQ Aria — Sign in</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#F0F4F8;font-family:system-ui,-apple-system,sans-serif;
      display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:#fff;border-radius:16px;padding:40px;width:380px;
       box-shadow:0 4px 32px rgba(0,0,0,0.10);border:1px solid #D8E2EC}}
h1{{color:#1C2B3A;font-size:1.5rem;margin-bottom:4px}}
.sub{{color:#64748B;font-size:.875rem;margin-bottom:28px}}
label{{display:block;font-size:.75rem;font-weight:600;color:#64748B;
       text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}}
input{{width:100%;padding:10px 14px;border:1.5px solid #D8E2EC;border-radius:8px;
       font-size:.95rem;color:#1C2B3A;background:#fff;outline:none}}
input:focus{{border-color:#C9A84C}}
.field{{margin-bottom:18px}}
.btn{{width:100%;padding:12px;background:#C9A84C;color:#fff;border:none;
      border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer;margin-top:4px}}
.btn:hover{{background:#A8883A}}
.btn:disabled{{opacity:0.6;cursor:not-allowed}}
.err{{background:#FEF2F2;border:1px solid #FECACA;color:#DC2626;
      padding:10px 14px;border-radius:8px;font-size:.875rem;margin-bottom:20px}}
.divider{{border:none;border-top:1px solid #E2E8F0;margin:20px 0}}
</style>
</head>
<body><div class="card">
  <h1>SalonIQ <span style="color:#C9A84C">Aria</span></h1>
  <p class="sub">Sign in to continue</p>
  {error_html}
  <form method="POST" action="/login" id="loginForm">
    <input type="hidden" name="next" value="{next_url}">
    <div class="field"><label>Account Code</label>
      <input type="text" name="account_code" id="account_code" autofocus autocomplete="organization"
             placeholder=""></div>
    <hr class="divider">
    <div class="field"><label>Username</label>
      <input type="text" name="username" autocomplete="username"></div>
    <div class="field"><label>Password</label>
      <div style="position:relative">
        <input type="password" name="password" id="passwordInput" autocomplete="current-password" style="width:100%;padding-right:44px;box-sizing:border-box">
        <button type="button" id="togglePwd" onclick="var i=document.getElementById('passwordInput');var b=document.getElementById('togglePwd');i.type=i.type==='password'?'text':'password';b.textContent=i.type==='password'?'👁':'🙈';" style="position:absolute;right:10px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;font-size:16px;padding:0;line-height:1">👁</button>
      </div></div>
    <button type="submit" class="btn" id="submitBtn">Sign in →</button>
  </form>
  <script>
    document.getElementById('loginForm').addEventListener('submit', function(){{
      document.getElementById('submitBtn').disabled = true;
      document.getElementById('submitBtn').textContent = 'Signing in…';
    }});
  </script>
</div></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.args.get('next') or request.form.get('next') or '/'
    if request.method == "POST":
        account_code = request.form.get('account_code', '').strip().upper()
        username     = request.form.get('username', '').strip()
        password     = request.form.get('password', '').strip()
        if not account_code or not username or not password:
            return _login_page(next_url, '<div class="err">Please fill in all fields.</div>'), 401

        # ── Env-var fallback (used while SalonIQ LogOn endpoint is confirmed) ──
        _fb_user   = os.environ.get('ADMIN_USER', '').strip()
        _fb_pass   = os.environ.get('ADMIN_PASS', '').strip()
        _fb_tenant = os.environ.get('ADMIN_TENANT', '').strip()
        _fb_server = os.environ.get('ADMIN_SERVER', 'BETA').strip().upper()
        if _fb_user and _fb_pass and _fb_tenant:
            if username == _fb_user and password == _fb_pass:
                role = 'admin' if username.lower() == 'admin' else 'user'
                flask_session.permanent       = True
                flask_session['role']         = role
                flask_session['username']     = username
                flask_session['tenant_id']    = _fb_tenant
                flask_session['server']       = _fb_server
                flask_session['account_code'] = account_code
                return redirect(next_url)

        tenant_id, server = _saloniq_login(account_code, username, password)
        if tenant_id:
            role = 'admin' if username.lower() == 'admin' else 'user'
            flask_session.permanent       = True
            flask_session['role']         = role
            flask_session['username']     = username
            flask_session['tenant_id']    = tenant_id
            flask_session['server']       = server
            flask_session['account_code'] = account_code
            return redirect(next_url)
        return _login_page(next_url, '<div class="err">Invalid account code, username, or password.</div>'), 401
    return _login_page(next_url, '')


def _login_page(next_url, error_html):
    return _LOGIN_PAGE.format(next_url=next_url, error_html=error_html)


@app.route("/logout")
def logout():
    flask_session.clear()
    return redirect('/login')


@app.route("/api/mode")
@require_auth
def api_mode():
    return jsonify(
        mode         = _session_role(),
        tenant_id    = flask_session.get('tenant_id', ''),
        server       = flask_session.get('server', 'BETA'),
        account_code = flask_session.get('account_code', ''),
    )


@app.route("/admin/logs")
@require_admin
def admin_logs():
    page     = max(1, int(request.args.get('page', 1)))
    per_page = 100
    offset   = (page - 1) * per_page
    con = _get_db()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT %s OFFSET %s", (per_page, offset))
    rows = cur.fetchall()
    cur.execute("""
        SELECT
            COUNT(*)                                                        AS total,
            COUNT(CASE WHEN event_type='analyse' THEN 1 END)                AS analyses,
            COUNT(CASE WHEN event_type='query'   THEN 1 END)                AS queries,
            COUNT(CASE WHEN event_type='session' THEN 1 END)                AS sessions,
            ROUND(AVG(CASE WHEN response_ms IS NOT NULL AND error IS NULL
                           THEN response_ms END))                           AS avg_ms,
            SUM(COALESCE(input_tokens,  0))                                 AS total_in,
            SUM(COALESCE(output_tokens, 0))                                 AS total_out,
            COUNT(CASE WHEN error IS NOT NULL THEN 1 END)                   AS errors
        FROM activity_log
    """)
    totals = cur.fetchone()
    cur.close()
    con.close()

    # Cost estimate: Sonnet 4.6 — $3/MTok in, $15/MTok out
    total_in  = totals['total_in']  or 0
    total_out = totals['total_out'] or 0
    avg_ms    = totals['avg_ms']    or 0
    cost_usd  = (total_in * 3 + total_out * 15) / 1_000_000

    def badge(event_type):
        colours = {'analyse': '#0EA5E9', 'query': '#8B5CF6', 'session': '#10B981'}
        bg = colours.get(event_type, '#94A3B8')
        return f'<span style="background:{bg};color:#fff;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">{event_type}</span>'

    def ms_cell(ms):
        if ms is None: return '<td style="color:#94A3B8">—</td>'
        s = ms / 1000
        colour = '#10B981' if s < 5 else ('#F59E0B' if s < 15 else '#EF4444')
        return f'<td style="color:{colour};font-weight:600">{s:.1f}s</td>'

    def esc(v):
        if v is None: return ''
        return str(v).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

    def fmt_ts(ts):
        if not ts: return '—'
        try:
            from datetime import timezone
            dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            diff = now - dt
            if diff.total_seconds() < 60:
                return 'just now'
            if diff.total_seconds() < 3600:
                return f"{int(diff.total_seconds() // 60)}m ago"
            if dt.date() == now.date():
                return f"Today {dt.strftime('%H:%M')}"
            if (now.date() - dt.date()).days == 1:
                return f"Yesterday {dt.strftime('%H:%M')}"
            return dt.strftime('%-d %b %Y %H:%M')
        except Exception:
            return str(ts)[:16]

    rows_html = ''
    for r in rows:
        tok = f"{r['input_tokens'] or 0:,} / {r['output_tokens'] or 0:,}" if (r['input_tokens'] or r['output_tokens']) else '—'
        err_cell = f'<td style="color:#EF4444;font-size:11px">{esc(r["error"])}</td>' if r['error'] else '<td style="color:#94A3B8">—</td>'
        followup = '↩ follow-up' if r['is_followup'] else ''
        rows_html += f"""<tr style="border-bottom:1px solid #F1F5F9">
            <td style="white-space:nowrap;color:#64748B;font-size:12px" title="{esc(r['ts'])}">{fmt_ts(r['ts'])}</td>
            <td>{badge(r['event_type'])}</td>
            <td style="font-size:12px;color:#475569">{esc(r['salon'])}</td>
            <td style="font-size:12px;color:#475569;font-weight:600">{esc(r.get('username') or '—')}</td>
            <td style="max-width:320px;font-size:13px" title="{esc(r['question'])}">{esc((r['question'] or '')[:80])}{'…' if len(r['question'] or '')>80 else ''}</td>
            <td style="font-size:12px;color:#64748B">{esc(r['format'] or '')} <span style="color:#94A3B8;font-size:11px">{followup}</span></td>
            {ms_cell(r['response_ms'])}
            <td style="font-size:12px;color:#64748B">{tok}</td>
            <td style="max-width:260px;font-size:12px;color:#334155" title="{esc(r['result_title'])}">{esc((r['result_title'] or '')[:60])}{'…' if len(r['result_title'] or '')>60 else ''}</td>
            {err_cell}
        </tr>"""

    total_rows  = totals['total'] or 0
    total_pages = max(1, -(-total_rows // per_page))  # ceiling division
    pgn_parts = []
    if page > 1:
        pgn_parts.append(f'<a href="/admin/logs?page={page-1}">← Prev</a>')
    for p in range(max(1, page-2), min(total_pages+1, page+3)):
        if p == page:
            pgn_parts.append(f'<span class="cur">{p}</span>')
        else:
            pgn_parts.append(f'<a href="/admin/logs?page={p}">{p}</a>')
    if page < total_pages:
        pgn_parts.append(f'<a href="/admin/logs?page={page+1}">Next →</a>')
    pagination_html = f'<div class="pgn"><span style="font-size:13px;color:#64748B;margin-right:8px">Showing {offset+1}–{min(offset+per_page, total_rows)} of {total_rows:,}</span>{"".join(pgn_parts)}</div>' if total_pages > 1 else ''

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Activity Log — SalonIQ AI</title>
<meta http-equiv="refresh" content="30">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;background:#F8FAFC;color:#1E293B}}
  .hdr{{background:#0F1923;color:#fff;padding:18px 32px;display:flex;align-items:center;gap:24px;justify-content:space-between}}
  .hdr h1{{margin:0;font-size:20px;font-weight:700}}
  .hdr span{{font-size:13px;opacity:.7}}
  .btn-back{{background:#C9A84C;color:#0F1923;border:none;border-radius:8px;padding:8px 18px;font-size:13px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}}
  .stat-row{{display:flex;gap:16px;padding:20px 32px;flex-wrap:wrap}}
  .stat{{background:#fff;border:1px solid #E2E8F0;border-radius:12px;padding:14px 22px;min-width:120px}}
  .stat .val{{font-size:28px;font-weight:700;color:#1C2B3A}}
  .stat .lbl{{font-size:12px;color:#64748B;margin-top:2px}}
  .tbl-wrap{{padding:0 32px 40px}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #E2E8F0}}
  th{{background:#F1F5F9;padding:10px 12px;text-align:left;font-size:12px;font-weight:600;color:#475569;white-space:nowrap}}
  td{{padding:9px 12px;vertical-align:top}}
  tr:hover td{{background:#FAFBFF}}
  .refresh{{font-size:12px;opacity:.6}}
  .pgn{{display:flex;align-items:center;gap:10px;padding:16px 32px 32px;justify-content:flex-end}}
  .pgn a{{background:#fff;border:1px solid #E2E8F0;border-radius:8px;padding:7px 16px;font-size:13px;font-weight:600;color:#1C2B3A;text-decoration:none}}
  .pgn a:hover{{background:#F1F5F9}}
  .pgn .cur{{background:#0F1923;color:#fff;border-radius:8px;padding:7px 16px;font-size:13px;font-weight:600}}
</style></head><body>
<div class="hdr">
  <div style="display:flex;align-items:center;gap:24px">
    <h1>Activity Log</h1>
    <span>SalonIQ Aria &nbsp;·&nbsp; page {page} &nbsp;·&nbsp; auto-refreshes every 30s</span>
  </div>
  <a href="javascript:history.back()" class="btn-back">← Back to Aria</a>
</div>
<div class="stat-row">
  <div class="stat"><div class="val">{totals['total']}</div><div class="lbl">Total events</div></div>
  <div class="stat"><div class="val">{totals['sessions']}</div><div class="lbl">Sessions</div></div>
  <div class="stat"><div class="val">{totals['analyses']}</div><div class="lbl">AI analyses</div></div>
  <div class="stat"><div class="val">{totals['queries']}</div><div class="lbl">Client queries</div></div>
  <div class="stat"><div class="val">{int(avg_ms) // 1000}.{(int(avg_ms) % 1000) // 100}s</div><div class="lbl">Avg response</div></div>
  <div class="stat"><div class="val">${cost_usd:.2f}</div><div class="lbl">Est. API cost (USD)</div></div>
  <div class="stat"><div class="val" style="color:{'#EF4444' if totals['errors'] else '#10B981'}">{totals['errors']}</div><div class="lbl">Errors</div></div>
</div>
<div class="tbl-wrap">
<table>
<thead><tr>
  <th>Time (UTC)</th><th>Type</th><th>Salon</th><th>User</th><th>Question</th>
  <th>Format</th><th>Response</th><th>Tokens in/out</th><th>Result title</th><th>Error</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table></div>
{pagination_html}
</body></html>"""
    return html


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Salon SMS Dashboard on http://127.0.0.1:{port}")
    app.run(debug=False, port=port, host="0.0.0.0")

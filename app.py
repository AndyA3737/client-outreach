#!/usr/bin/env python3
"""Salon SMS Marketing Dashboard — scores clients for SMS targeting."""

import os
import base64
import json
import threading
import uuid
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request, Response, send_from_directory
import requests
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter
import time

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_USER = os.environ.get('DASHBOARD_USER', 'admin').strip()
DASHBOARD_PASS = os.environ.get('DASHBOARD_PASS', 'changeme').strip()


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Try Flask's built-in parser first, then fall back to manual header parse
        auth = request.authorization
        if auth:
            if auth.username == DASHBOARD_USER and auth.password == DASHBOARD_PASS:
                return f(*args, **kwargs)
        else:
            raw = request.headers.get('Authorization') or request.environ.get('HTTP_AUTHORIZATION', '')
            if raw.startswith('Basic '):
                try:
                    creds = base64.b64decode(raw[6:]).decode('utf-8')
                    user, pwd = creds.split(':', 1)
                    if user == DASHBOARD_USER and pwd == DASHBOARD_PASS:
                        return f(*args, **kwargs)
                except Exception:
                    pass
        return Response(
            'Authentication required.',
            401,
            {'WWW-Authenticate': 'Basic realm="SalonIQ SMS Dashboard"'},
        )
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
_all_scored = []
_total_clients = 0
_jobs = {}  # job_id -> {status, data, error}


NOCACHE_REPORTS = {"XXX_Export_Admin_TUBR_Bookings"}

def fetch(report_name, sd="", ed="", tenant_id=None, server="BETA"):
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
    params = {**API_COMMON, "TokenID": srv["token"], "TenantID": tid,
              "ReportName": report_name, "startdate": sd, "enddate": ed}
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


def build_data(tenant_id=None, server="BETA"):
    today = date.today()

    clients_raw = fetch("XXX_Export_Admin_TUBR_Clients", "01/01/2026", "01/01/2026", tenant_id=tenant_id, server=server)
    svcs_raw    = fetch("XXX_Export_Admin_TUBR_services", "01/01/2026", "01/01/2026", tenant_id=tenant_id, server=server)
    team_raw    = fetch("XXX_Export_Admin_TUBR_TeamMembers", "01/01/2026", "01/01/2026", tenant_id=tenant_id, server=server)
    try:
        salons_raw = fetch("Export_Admin_BenchMarks_SalonList", "01/01/2026", "01/01/2026", tenant_id=tenant_id, server=server)
        app.logger.info("SalonList rows=%d tenant=%s sample=%s",
                        len(salons_raw), tenant_id,
                        list(salons_raw[0].keys()) if salons_raw else "EMPTY")
    except Exception as e:
        app.logger.warning("SalonList fetch failed (salon names will be blank): %s", e)
        salons_raw = []

    global _total_clients
    _total_clients = len(clients_raw)

    svc_map  = {s["ServiceId"]: s for s in svcs_raw}
    team_map = {t["TeamMemberId"]: (t.get("NickName") or t["FirstName"]) for t in team_raw}
    cli_map  = {c["ClientId"]: c for c in clients_raw}
    salon_map = {
        str(s.get("SalonId") or s.get("Salonid") or s.get("salonid") or s.get("ID") or ""):
        (s.get("SalonName") or s.get("Name") or s.get("name") or "")
        for s in salons_raw
    }
    del svcs_raw, team_raw, clients_raw, salons_raw  # free raw API data now maps are built

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
    has_future_booking = set()

    def _fetch_chunk(args):
        sd, ed = args
        try:
            return fetch("XXX_Export_Admin_TUBR_Bookings", sd, ed,
                         tenant_id=tenant_id, server=server)
        except Exception as e:
            app.logger.error("CHUNK FAILED [%s→%s]: %s", sd, ed, e)
            raise RuntimeError(f"Booking chunk {sd}→{ed} failed: {e}") from e

    # Fetch all chunks in parallel (total time = slowest chunk, not sum of all)
    # Process each chunk as it completes and discard immediately to keep memory low
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_chunk, r): r for r in booking_ranges}
        for future in as_completed(futures):
            chunk = future.result()
            for b in chunk:
                cid = b.get("ClientId")
                dt  = parse_dt(b.get("Start"))
                if not cid or not dt:
                    continue
                if dt.date() > today:
                    has_future_booking.add(cid)
                    continue
                svc      = svc_map.get(b.get("ServiceId"), {})
                svc_name = svc.get("Description", "")
                if any(k in svc_name.upper() for k in SKIP_KEYWORDS):
                    continue
                by_client[cid].append({
                    "dt":    dt,
                    "price": float(b.get("TotalSalesPrice") or 0),
                    "tm":    b.get("TeamMemberId", ""),
                    "cat":   svc.get("Categoty", "").replace("HAIR - ", ""),
                    "svc":   svc_name,
                    "sid":   str(b.get("Salonid") or b.get("SalonId") or b.get("salonid") or ""),
                })
            del chunk  # discard as soon as processed

    rows = []
    for cid, bkgs in by_client.items():
        cli = cli_map.get(cid)
        if not cli:
            continue
        if cid in has_future_booking:
            continue

        bkgs.sort(key=lambda x: x["dt"])
        last_dt, first_dt = bkgs[-1]["dt"], bkgs[0]["dt"]
        days_since = (today - last_dt.date()).days

        visit_dates = sorted(set(b["dt"].date() for b in bkgs))
        n = len(visit_dates)

        avg_gap = (visit_dates[-1] - visit_dates[0]).days / (n - 1) if n > 1 else None
        overdue = (days_since - avg_gap) if avg_gap else None

        total_spend = sum(b["price"] for b in bkgs)
        avg_spend   = total_spend / n if n else 0

        pref_day  = DAYS[Counter(b["dt"].weekday() for b in bkgs).most_common(1)[0][0]]
        pref_time = time_label(Counter(b["dt"].hour for b in bkgs).most_common(1)[0][0])

        tm_cnt     = Counter(b["tm"] for b in bkgs if b["tm"])
        pref_tm    = team_map.get(tm_cnt.most_common(1)[0][0], "?") if tm_cnt else "?"
        n_stylists = len(tm_cnt)

        salon_cnt  = Counter(b["sid"] for b in bkgs if b["sid"])
        pref_salon = salon_map.get(salon_cnt.most_common(1)[0][0], "") if salon_cnt else ""

        top_cats  = [c for c, _ in Counter(b["cat"] for b in bkgs if b["cat"]).most_common(2)]
        no_shows  = int(cli.get("NoShows") or 0)

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
            no_shows=no_shows,
            n_stylists=n_stylists,
            sr=round(r_score, 1),
            so=round(o_score, 1),
            sf=round(f_score, 1),
            sm=round(m_score, 1),
            sp=-penalty,
            score_pct=min(round(total_score), 100),
            sms=sms_msg,
        ))

    rows.sort(key=lambda x: x["score"], reverse=True)
    global _all_scored
    _all_scored = rows
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


def _build_response(tenant_id, server):
    """Build the full API response dict — called from background thread."""
    import traceback
    try:
        clients  = build_data(tenant_id, server)
        stylists = sorted(set(c["pref_tm"] for c in clients))
        result   = dict(
            clients=clients,
            stylists=stylists,
            n_active  =sum(1 for c in _all_scored if c["scls"] == "active"),
            n_due     =sum(1 for c in _all_scored if c["scls"] == "due"),
            n_lapsing =sum(1 for c in _all_scored if c["scls"] == "lapsing"),
            n_lapsed  =sum(1 for c in _all_scored if c["scls"] == "lapsed"),
            n_total=_total_clients,
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
    tenant_id = request.args.get("tenant_id") or None
    server    = request.args.get("server", "BETA")
    job_id    = str(uuid.uuid4())
    _jobs[job_id] = {"status": "loading"}

    def worker():
        _jobs[job_id] = _build_response(tenant_id, server)

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
    return jsonify({"status": "loading"})


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
    q = request.args.get("q", "").lower().strip()
    if len(q) < 2:
        return jsonify([])
    results = [c for c in _all_scored if q in c["name"].lower()]
    return jsonify(results[:20])


@app.route("/api/query")
@require_auth
def query_clients():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "No query provided"}), 400
    if not _all_scored:
        return jsonify({"error": "No data loaded — load a salon first"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured on this server"}), 500

    schema = """
Fields available on each client record:
- name (string): full name
- scls (string): "active" <60 days, "due" 60-120 days, "lapsing" 120-365 days, "lapsed" >365 days
- days_since (int): days since last visit
- n_visits (int): visits in the last 2 years
- total_spend (int £): total spend
- avg_spend (int £): average spend per visit
- avg_gap (int or null): average days between visits
- overdue (int or null): days past their usual visit interval
- pref_day (string): Mon/Tue/Wed/Thu/Fri/Sat/Sun
- pref_time (string): Morning/Lunchtime/Afternoon/Evening
- pref_tm (string): preferred stylist name
- top_cats (array of strings): service categories e.g. ["Colour","Cut & Finish"]
- no_shows (int): number of recorded no-shows
- n_stylists (int): number of distinct stylists visited
- score (float 0-100): SMS targeting score
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

Supported operators: eq, ne, gt, gte, lt, lte, in (value is a list), contains (array field contains string), exists (value true=not null, false=null)

Examples:
"visited only once" → [{{"field":"n_visits","op":"eq","value":1}}]
"loyal regulars" → [{{"field":"n_visits","op":"gte","value":10}}]
"high value lapsing" → logic AND, [{{"field":"scls","op":"eq","value":"lapsing"}},{{"field":"avg_spend","op":"gte","value":60}}]
"colour clients overdue" → logic AND, [{{"field":"top_cats","op":"contains","value":"Colour"}},{{"field":"overdue","op":"exists","value":true}}]
"only ever seen one stylist" → [{{"field":"n_stylists","op":"eq","value":1}}]
"no-show history" → [{{"field":"no_shows","op":"gte","value":1}}]
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
        return jsonify({"error": f"Could not interpret query: {e}"}), 400

    filters     = criteria.get("filters", [])
    logic       = criteria.get("logic", "AND").upper()
    description = criteria.get("description", q)

    def matches(client, f):
        field, op, val = f.get("field"), f.get("op"), f.get("value")
        cv = client.get(field)
        if op == "eq":       return cv == val
        if op == "ne":       return cv != val
        if op == "gt":       return cv is not None and cv > val
        if op == "gte":      return cv is not None and cv >= val
        if op == "lt":       return cv is not None and cv < val
        if op == "lte":      return cv is not None and cv <= val
        if op == "in":       return cv in val
        if op == "contains":
            if isinstance(cv, list):
                return any(val.lower() in c.lower() for c in cv)
        if op == "exists":   return (cv is not None) == val
        return False

    results = [
        c for c in _all_scored
        if (any if logic == "OR" else all)(matches(c, f) for f in filters)
    ] if filters else []

    return jsonify({"clients": results[:500], "total": len(results),
                    "description": description, "criteria": criteria})


if __name__ == "__main__":
    print("Starting Salon SMS Dashboard on http://127.0.0.1:5000")
    app.run(debug=False, port=5000)

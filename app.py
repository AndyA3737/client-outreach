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

@app.errorhandler(Exception)
def json_error(e):
    from werkzeug.exceptions import HTTPException
    code = e.code if isinstance(e, HTTPException) else 500
    app.logger.exception(e)
    return jsonify(error=str(e)), code

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
_all_clients = []   # every client including those with no visits
_total_clients = 0
_jobs = {}  # job_id -> {status, data, error}


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

    global _total_clients
    _total_clients = len(clients_raw)

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
    del svcs_raw, team_raw, clients_raw, salons_raw  # free raw API data now maps are built

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
                if dt.date() > today:
                    if not any(k in svc_name.upper() for k in SKIP_KEYWORDS):
                        future_bookings[cid].append({
                            "dt":  dt,
                            "svc": svc_name,
                            "cat": svc.get("Categoty", "").replace("HAIR - ", ""),
                        })
                    continue
                if any(k in svc_name.upper() for k in SKIP_KEYWORDS):
                    continue
                by_client[cid].append({
                    "dt":    dt,
                    "price": float(b.get("TotalSalesPrice") or 0),
                    "tm":    b.get("TeamMemberId", ""),
                    "cat":   svc.get("Categoty", "").replace("HAIR - ", ""),
                    "svc":   svc_name,
                    "sid":   str(b.get("Salonid") or b.get("SalonId") or b.get("salonid") or ""),
                    "dept":  (svc.get("Department") or "").lower(),
                })
            del chunk  # discard as soon as processed

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
                "price": float(r.get("UnitSalesPrice") or 0),
                "qty":   int(float(r.get("Qty") or 1)),
                "dt":    parse_dt(r.get("TransactionDate") or ""),
            })
        del retail_raw
    except Exception as e:
        print(f"RETAIL fetch failed: {e}", flush=True)

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

        top_cats    = [c for c, _ in Counter(b["cat"] for b in bkgs if b["cat"]).most_common(2)]
        all_cats    = list(dict.fromkeys(b["cat"] for b in bkgs if b["cat"]))
        departments = list(dict.fromkeys(b["dept"] for b in bkgs if b["dept"]))
        top_svcs    = [s for s, _ in Counter(b["svc"] for b in bkgs if b["svc"]).most_common(5)]
        no_shows  = int(cli.get("NoShows") or 0)

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
    global _all_scored, _all_clients
    _all_scored = [r for r in rows if not r["has_future_booking"]]

    # Add clients with no past visits (but possibly future bookings) to _all_clients
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
    _all_clients = rows + no_history

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
    _jobs[job_id] = {"status": "loading", "step": ""}

    def worker():
        def set_step(msg):
            if isinstance(_jobs.get(job_id), dict) and _jobs[job_id].get("status") == "loading":
                _jobs[job_id]["step"] = msg
        _jobs[job_id] = _build_response(tenant_id, server, set_step=set_step)

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
    if not _all_clients:
        return jsonify({"error": "No data loaded — load a salon first"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured on this server"}), 500

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
        c for c in _all_clients
        if (any if logic == "OR" else all)(matches(c, f) for f in filters)
    ] if filters else []

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


def build_analysis_context(question=""):
    if not _all_clients:
        return "No data loaded."

    today = date.today()
    status_counts = Counter(c.get("scls", "") for c in _all_clients)

    stylist_data = defaultdict(lambda: {"clients": 0, "revenue": 0, "visits": 0})
    for c in _all_clients:
        tm = c.get("pref_tm", "?")
        stylist_data[tm]["clients"] += 1
        stylist_data[tm]["revenue"] += c.get("total_spend", 0)
        stylist_data[tm]["visits"]  += c.get("n_visits", 0)

    all_cats = []
    for c in _all_clients:
        all_cats.extend(c.get("top_cats", []))

    # Monthly retail: distribute each client's total spend evenly across their purchase months
    monthly_retail = defaultdict(lambda: {"clients": 0, "spend": 0.0})
    for c in _all_clients:
        months = list({_month_key(d) for d in (c.get("retail_dates") or []) if _month_key(d)})
        if not months:
            continue
        share = c.get("retail_total", 0) / len(months)
        for mk in months:
            monthly_retail[mk]["clients"] += 1
            monthly_retail[mk]["spend"]   += share

    monthly_gc = defaultdict(int)
    for c in _all_clients:
        for mk in {_month_key(d) for d in (c.get("giftcard_dates") or []) if _month_key(d)}:
            monthly_gc[mk] += 1

    monthly_promo = defaultdict(int)
    for c in _all_clients:
        for mk in {_month_key(d) for d in (c.get("promo_dates") or []) if _month_key(d)}:
            monthly_promo[mk] += 1

    # Segment deep-dives (aggregated, no individual rows)
    segments = {}
    for seg in ("active", "due", "lapsing", "lapsed", "never"):
        grp = [c for c in _all_clients if c.get("scls") == seg]
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
        for c in _all_clients:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            first = name.split()[0].lower()
            # Only match on first name if it's at least 4 chars (avoids "is", "new", etc.)
            if name.lower() in q_lower or (len(first) >= 4 and first in q_words):
                named_clients.append(c)

    lines = [
        f"SALON DATA — {today.strftime('%-d %b %Y')}",
        f"Total clients: {len(_all_clients)} | Active scoring pool: {len(_all_scored)}",
        f"Total 2yr service revenue: £{sum(c.get('total_spend',0) for c in _all_clients):,.0f}",
        f"Total 2yr retail spend:    £{sum(c.get('retail_total',0) for c in _all_clients):,.0f}",
        f"Total 2yr gift card spend: £{sum(c.get('giftcard_total',0) for c in _all_clients):,.0f}",
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

    lines += ["", "TOP SERVICE CATEGORIES:"]
    for cat, cnt in Counter(all_cats).most_common(15):
        lines.append(f"  {cat}: {cnt} clients")

    if monthly_retail:
        lines += ["", "MONTHLY RETAIL SPEND (estimated — total distributed evenly across client purchase months):"]
        for mk in _sort_months(monthly_retail)[:24]:
            m = monthly_retail[mk]
            lines.append(f"  {mk}: {m['clients']} buyers, ~£{m['spend']:,.0f}")

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
        cnt = sum(1 for c in _all_clients if lo <= c.get("avg_spend", 0) < hi)
        lines.append(f"  {label}: {cnt} clients")

    # Top 100 clients by total spend (compact rows, no arrays)
    top100 = sorted(_all_clients, key=lambda x: x.get("total_spend", 0), reverse=True)[:100]
    lines += ["", "TOP 100 CLIENTS BY TOTAL SPEND:",
              "Name,Status,DaysSince,Visits,ServiceRevenue,AvgSpend,RetailTotal,GiftcardTotal,Stylist,Services"]
    for c in top100:
        cats = "|".join(c.get("top_cats", []))
        lines.append(
            f"{c['name']},{c.get('scls','')},{c.get('days_since','')},{c.get('n_visits',0)},"
            f"£{c.get('total_spend',0)},£{c.get('avg_spend',0)},"
            f"£{c.get('retail_total',0)},£{c.get('giftcard_total',0)},"
            f"{c.get('pref_tm','')},{cats}"
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

        if not question:
            return jsonify(error="No question provided"), 400
        if not _all_clients:
            return jsonify(error="No data loaded — load a salon first."), 400

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return jsonify(error="ANTHROPIC_API_KEY is not configured on this server"), 500

        job_id = str(uuid.uuid4())
        _jobs[job_id] = {"status": "loading", "step": "Thinking…"}

        def worker():
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
                }
                system = (
                    "You are an expert salon business analyst with access to live UK hair salon data. "
                    "Analyse the data carefully and answer the user's question accurately. "
                    "All monetary values are in British Pounds (£). "
                    "Return ONLY valid JSON — no markdown, no code blocks, no extra text. "
                    + fmt_instructions.get(fmt, fmt_instructions["dashboard"])
                )
                _jobs[job_id]["step"] = "Building data context…"
                context  = build_analysis_context(question)
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
                app.logger.info("ANALYSE done chars=%d stop=%s", len(text), msg.stop_reason)

                if text.startswith("```"):
                    text = text.split("```", 1)[1]
                    if text.startswith("json"):
                        text = text[4:]
                    text = text.rsplit("```", 1)[0]

                _jobs[job_id] = {"status": "done", "data": json.loads(text.strip())}
            except Exception as e:
                app.logger.exception("ANALYSE worker error: %s", e)
                _jobs[job_id] = {"status": "error", "error": str(e)}

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"job_id": job_id})

    except Exception as e:
        app.logger.exception("ANALYSE error: %s", e)
        return jsonify(error=str(e)), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Salon SMS Dashboard on http://127.0.0.1:{port}")
    app.run(debug=False, port=port, host="0.0.0.0")

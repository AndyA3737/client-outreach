#!/usr/bin/env python3
"""Salon SMS Marketing Dashboard — scores clients for SMS targeting."""

import os
import base64
from functools import wraps
from flask import Flask, render_template_string, jsonify, request, Response
import requests
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter
import time

app = Flask(__name__)

DASHBOARD_USER = os.environ.get('DASHBOARD_USER', 'admin')
DASHBOARD_PASS = os.environ.get('DASHBOARD_PASS', 'changeme')


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Basic '):
            try:
                creds = base64.b64decode(auth[6:]).decode('utf-8')
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

API_BASE = "https://greathairhub.saloniq.co.uk/api/GetAPIReport"
API_DEFAULTS = dict(
    TokenID="ACD7636F-D6D5-45AB-92FC-785D4904ADA5",
    TenantID="1E7D7624-FEB7-4950-A6BE-5FBB1498EE39",
    Salonid="", UserID="", data1="", data2="", data3="", data4=""
)

_cache, _cache_ts = {}, {}
CACHE_TTL = 3600  # 1 hour
_all_scored = []  # full scored list, used by client search


def fetch(report_name, sd="", ed=""):
    key = f"{report_name}|{sd}|{ed}"
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < CACHE_TTL:
        return _cache[key]
    params = {**API_DEFAULTS, "ReportName": report_name, "startdate": sd, "enddate": ed}
    r = requests.post(API_BASE, params=params, headers={"Content-Length": "0"}, timeout=60)
    r.raise_for_status()
    result = r.json()["Data"]["Array"]
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

    # Pick a variant deterministically so it doesn't change on every page load
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

    else:  # lapsed
        opts = [
            f"Hi {first}, it's been a while and we'd love to welcome you back! {stylist or 'The team'} has availability – give us a call 🌟",
            f"Hi {first}! We've really missed you 💕 It would be wonderful to see you again{with_who} – call us to rebook anytime.",
        ]

    msg = opts[v % len(opts)]
    # Hard-trim to 160 chars if somehow over (rare)
    return msg[:157] + "…" if len(msg) > 160 else msg


def time_label(h):
    if h < 12:
        return "Morning"
    if h < 14:
        return "Lunchtime"
    if h < 17:
        return "Afternoon"
    return "Evening"


def build_data():
    today = date.today()
    sd = (today - timedelta(days=730)).strftime("%m/%d/%Y")
    ed = (today + timedelta(days=365)).strftime("%m/%d/%Y")

    clients_raw = fetch("XXX_Export_Admin_TUBR_Clients")
    svcs_raw    = fetch("XXX_Export_Admin_TUBR_services")
    team_raw    = fetch("XXX_Export_Admin_TUBR_TeamMembers")
    bkgs_raw    = fetch("XXX_Export_Admin_TUBR_Bookings", sd, ed)

    svc_map  = {s["ServiceId"]: s for s in svcs_raw}
    team_map = {t["TeamMemberId"]: (t.get("NickName") or t["FirstName"]) for t in team_raw}
    cli_map  = {c["ClientId"]: c for c in clients_raw}

    by_client = defaultdict(list)
    has_future_booking = set()

    for b in bkgs_raw:
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
        })

    rows = []
    for cid, bkgs in by_client.items():
        cli = cli_map.get(cid)
        if not cli:
            continue
        if cid in has_future_booking:
            continue  # already booked in — no SMS needed

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

        tm_cnt    = Counter(b["tm"] for b in bkgs if b["tm"])
        pref_tm   = team_map.get(tm_cnt.most_common(1)[0][0], "?") if tm_cnt else "?"

        top_cats  = [c for c, _ in Counter(b["cat"] for b in bkgs if b["cat"]).most_common(2)]
        no_shows  = int(cli.get("NoShows") or 0)

        # ── Scoring ─────────────────────────────────────────────────────────
        # Recency (max 40): sweet spot is 30–180 days — recently visited but overdue
        if days_since <= 30:
            r_score = 10   # just visited — low priority for SMS
        elif days_since <= 90:
            r_score = 40
        elif days_since <= 180:
            r_score = 30
        elif days_since <= 365:
            r_score = 15
        else:
            r_score = 5

        # Overdue bonus (max 20): extra weight if past their personal visit rhythm
        if avg_gap and overdue and overdue > 0:
            o_score = min(overdue / avg_gap * 20, 20)
        else:
            o_score = 0

        # Frequency (max 20): visits per year × 3, capped at 20
        years   = max((today - first_dt.date()).days / 365.25, 0.08)
        f_score = min(n / years * 3, 20)

        # Monetary (max 20): £100 avg spend per visit = 20 pts
        m_score = min(avg_spend / 5, 20)

        # No-show penalty (max −15)
        penalty = min(no_shows * 3, 15)

        total_score = r_score + o_score + f_score + m_score - penalty
        # ────────────────────────────────────────────────────────────────────

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
            top_cats=top_cats,
            no_shows=no_shows,
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
    clients   = build_data()
    n_active  = sum(1 for c in clients if c["scls"] == "active")
    n_due     = sum(1 for c in clients if c["scls"] == "due")
    n_lapsing = sum(1 for c in clients if c["scls"] == "lapsing")
    n_lapsed  = sum(1 for c in clients if c["scls"] == "lapsed")
    stylists  = sorted(set(c["pref_tm"] for c in clients))
    return render_template_string(
        TEMPLATE,
        clients=clients,
        stylists=stylists,
        n_active=n_active, n_due=n_due, n_lapsing=n_lapsing, n_lapsed=n_lapsed,
        generated=datetime.now().strftime("%-d %b %Y at %H:%M"),
    )


@app.route("/api/refresh", methods=["POST"])
@require_auth
def refresh():
    _cache.clear()
    _cache_ts.clear()
    return jsonify(ok=True)


@app.route("/api/search")
@require_auth
def search_clients():
    q = request.args.get("q", "").lower().strip()
    if len(q) < 2:
        return jsonify([])
    results = [c for c in _all_scored if q in c["name"].lower()]
    return jsonify(results[:20])


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SMS Targeting · SalonIQ</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<style>
  :root {
    --navy:   #1C2B3A;
    --teal:   #00B5A4;
    --teal-d: #008F81;
    --bg:     #F2F5F8;
    --card:   #FFFFFF;
    --border: #E2E8EF;
    --text:   #1C2B3A;
    --muted:  #64748B;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter', system-ui, sans-serif; }

  /* Badges */
  .badge-active  { background:#D1FAF5; color:#007A6D; }
  .badge-due     { background:#FEF9C3; color:#854D0E; }
  .badge-lapsing { background:#FFEDD5; color:#9A3412; }
  .badge-lapsed  { background:#FEE2E2; color:#991B1B; }

  /* Table */
  table { border-collapse:collapse; width:100%; }
  thead th { position:sticky; top:0; z-index:10; background:#F8FAFC;
             border-bottom:2px solid var(--border);
             cursor:pointer; user-select:none; white-space:nowrap; color:var(--muted); font-weight:600; }
  thead th:hover { background:#EEF2F7; }
  tbody tr { border-bottom:1px solid var(--border); }
  tbody tr:hover td { background:#F0FAF9; }

  /* Score bar */
  .score-bar  { width:56px; height:5px; background:#E2E8EF; border-radius:3px; overflow:hidden; display:inline-block; vertical-align:middle; }
  .score-fill { height:100%; border-radius:3px; background:linear-gradient(90deg, var(--teal), #00D4C2); }

  /* Tooltips */
  .tip-wrap { position:relative; display:inline-block; }
  .tip-box  { display:none; position:absolute; bottom:calc(100% + 8px); left:50%;
              transform:translateX(-50%); background:var(--navy); color:#E2E8F0;
              padding:10px 14px; border-radius:10px; white-space:nowrap; z-index:50;
              font-size:12px; box-shadow:0 8px 30px rgba(0,0,0,0.2); }
  .tip-box::after { content:''; position:absolute; top:100%; left:50%; transform:translateX(-50%);
                    border:6px solid transparent; border-top-color:var(--navy); }
  .tip-wrap:hover .tip-box { display:block; }

  /* Inputs */
  input, select { background:#fff; border:1px solid var(--border); color:var(--text);
                  border-radius:8px; padding:7px 12px; font-size:14px; font-family:inherit; }
  input:focus, select:focus { outline:2px solid var(--teal); outline-offset:0; border-color:var(--teal); }
  input::placeholder { color:#94A3B8; }

  /* Scrollbar */
  ::-webkit-scrollbar { width:6px; height:6px; }
  ::-webkit-scrollbar-track { background:#F2F5F8; }
  ::-webkit-scrollbar-thumb { background:#CBD5E1; border-radius:3px; }

  .sort-asc::after  { content:" ▲"; font-size:9px; color:var(--teal); }
  .sort-desc::after { content:" ▼"; font-size:9px; color:var(--teal); }

  .btn-teal { background:var(--teal); color:#fff; border:none; border-radius:8px;
              padding:8px 18px; font-size:14px; font-weight:600; cursor:pointer; transition:background 0.15s; }
  .btn-teal:hover { background:var(--teal-d); }
  .btn-teal:disabled { opacity:0.6; cursor:not-allowed; }

  .btn-copy { background:#F1F5F9; color:var(--muted); border:1px solid var(--border);
              border-radius:6px; padding:4px 10px; font-size:12px; cursor:pointer; transition:all 0.15s; white-space:nowrap; }
  .btn-copy:hover { background:var(--teal); color:#fff; border-color:var(--teal); }
  .btn-copy.copied { background:#10B981; color:#fff; border-color:#10B981; }

  .stat-card { background:#fff; border-radius:16px; padding:20px 24px;
               box-shadow:0 1px 4px rgba(0,0,0,0.06), 0 4px 16px rgba(0,0,0,0.04); border:1px solid var(--border); }

  .lookup-card { background:#fff; border:1px solid var(--border); border-radius:16px; padding:24px;
                 margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,0.06); }
  .mini-stat { background:#F8FAFC; border:1px solid var(--border); border-radius:10px; padding:12px; }
</style>
</head>
<body>

<!-- ── Header ──────────────────────────────────────────────── -->
<header style="background:var(--navy);" class="px-6 py-4 flex items-center justify-between shadow-lg">
  <div class="flex items-center gap-4">
    <img src="https://www.saloniq.com/img/logo.svg" alt="SalonIQ" class="h-8">
    <div class="w-px h-8 bg-white/20"></div>
    <div>
      <div class="text-white font-semibold text-base leading-tight">SMS Targeting Dashboard</div>
      <div class="text-white/50 text-xs">Top 500 clients · Generated {{ generated }}</div>
    </div>
  </div>
  <button onclick="refreshData()" id="refreshBtn" class="btn-teal text-sm">
    ↻ Refresh Data
  </button>
</header>

<!-- ── Stat Cards ───────────────────────────────────────────── -->
<div class="grid grid-cols-2 md:grid-cols-4 gap-4 p-6 pb-4">
  <div class="stat-card">
    <div class="text-xs font-semibold uppercase tracking-widest mb-2" style="color:var(--muted)">In Top 500</div>
    <div class="text-4xl font-extrabold" style="color:var(--navy)">500</div>
    <div class="text-xs mt-1" style="color:var(--muted)">ranked by SMS score</div>
  </div>
  <div class="stat-card" style="border-top:3px solid var(--teal)">
    <div class="text-xs font-semibold uppercase tracking-widest mb-2" style="color:var(--teal-d)">Active</div>
    <div class="text-4xl font-extrabold" style="color:var(--teal)">{{ n_active }}</div>
    <div class="text-xs mt-1" style="color:var(--muted)">visited in last 60 days</div>
  </div>
  <div class="stat-card" style="border-top:3px solid #F59E0B">
    <div class="text-xs font-semibold uppercase tracking-widest mb-2 text-amber-600">Due / Lapsing</div>
    <div class="text-4xl font-extrabold text-amber-500">{{ n_due + n_lapsing }}</div>
    <div class="text-xs mt-1" style="color:var(--muted)">prime SMS targets</div>
  </div>
  <div class="stat-card" style="border-top:3px solid #EF4444">
    <div class="text-xs font-semibold uppercase tracking-widest mb-2 text-red-500">Lapsed</div>
    <div class="text-4xl font-extrabold text-red-500">{{ n_lapsed }}</div>
    <div class="text-xs mt-1" style="color:var(--muted)">not seen in over a year</div>
  </div>
</div>

<!-- ── Scoring Explanation ──────────────────────────────────── -->
<div class="px-6 pb-4">
  <details class="rounded-2xl border overflow-hidden shadow-sm" style="border-color:var(--border); background:#fff">
    <summary class="flex items-center justify-between px-6 py-4 cursor-pointer select-none font-semibold"
             style="color:var(--navy); list-style:none;">
      <div class="flex items-center gap-2">
        <span style="color:var(--teal)">&#9432;</span>
        How the SMS score is calculated
      </div>
      <span class="text-sm font-normal" style="color:var(--muted)">Click to expand ▾</span>
    </summary>
    <div class="px-6 pb-6 pt-2" style="border-top:1px solid var(--border)">
      <p class="text-sm mb-5" style="color:var(--muted)">
        Every client in your database is scored out of <strong style="color:var(--navy)">100 points</strong> across four signals,
        with a penalty for no-shows. The top 500 by score are shown below.
        Clients who already have a <strong style="color:var(--navy)">future booking</strong> are automatically excluded — no SMS needed.
        Clients who visited very recently score lower on purpose — they don't need a nudge yet.
      </p>
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-5">

        <div class="rounded-xl p-4" style="background:#F0FBF9; border:1px solid #C8EEEB">
          <div class="flex items-center gap-2 mb-2">
            <div class="w-8 h-8 rounded-lg flex items-center justify-center text-white text-sm font-bold"
                 style="background:var(--teal)">R</div>
            <div class="font-bold" style="color:var(--navy)">Recency <span class="font-normal text-xs" style="color:var(--muted)">max 40 pts</span></div>
          </div>
          <p class="text-xs mb-3" style="color:#475569">How long ago the client last visited. The sweet spot is 30–180 days — recent enough to remember you, overdue enough to need a nudge.</p>
          <table class="w-full text-xs" style="border-collapse:collapse">
            <tr style="border-bottom:1px solid #C8EEEB"><td class="py-1" style="color:#475569">Just visited (≤30 days)</td><td class="py-1 text-right font-semibold" style="color:var(--teal)">10 pts</td></tr>
            <tr style="border-bottom:1px solid #C8EEEB"><td class="py-1" style="color:#475569">30–90 days</td><td class="py-1 text-right font-semibold" style="color:var(--teal)">40 pts</td></tr>
            <tr style="border-bottom:1px solid #C8EEEB"><td class="py-1" style="color:#475569">90–180 days</td><td class="py-1 text-right font-semibold" style="color:var(--teal)">30 pts</td></tr>
            <tr style="border-bottom:1px solid #C8EEEB"><td class="py-1" style="color:#475569">180 days – 1 year</td><td class="py-1 text-right font-semibold" style="color:var(--teal)">15 pts</td></tr>
            <tr><td class="py-1" style="color:#475569">Over 1 year</td><td class="py-1 text-right font-semibold" style="color:var(--teal)">5 pts</td></tr>
          </table>
        </div>

        <div class="rounded-xl p-4" style="background:#FFFBEB; border:1px solid #FDE68A">
          <div class="flex items-center gap-2 mb-2">
            <div class="w-8 h-8 rounded-lg flex items-center justify-center text-white text-sm font-bold bg-amber-400">O</div>
            <div class="font-bold" style="color:var(--navy)">Overdue Bonus <span class="font-normal text-xs" style="color:var(--muted)">max 20 pts</span></div>
          </div>
          <p class="text-xs mb-3" style="color:#475569">
            Based on the client's <em>personal</em> visit rhythm. If their average gap between visits is 6 weeks
            and they're now 9 weeks overdue, they score higher than someone who is simply 9 weeks since their only visit.
          </p>
          <p class="text-xs p-2 rounded-lg" style="background:#FEF3C7; color:#92400E">
            <strong>Formula:</strong> (days overdue ÷ average gap) × 20, capped at 20 pts.<br>
            Only applies when a client has 2+ visits to establish a rhythm.
          </p>
        </div>

        <div class="rounded-xl p-4" style="background:#EFF6FF; border:1px solid #BFDBFE">
          <div class="flex items-center gap-2 mb-2">
            <div class="w-8 h-8 rounded-lg flex items-center justify-center text-white text-sm font-bold bg-blue-500">F</div>
            <div class="font-bold" style="color:var(--navy)">Frequency <span class="font-normal text-xs" style="color:var(--muted)">max 20 pts</span></div>
          </div>
          <p class="text-xs mb-3" style="color:#475569">
            Visits per year, rewarding loyal regulars over one-time visitors.
            A client visiting 6+ times a year scores the full 20 pts.
          </p>
          <p class="text-xs p-2 rounded-lg" style="background:#DBEAFE; color:#1E40AF">
            <strong>Formula:</strong> (visits ÷ years as client) × 3, capped at 20 pts.<br>
            Example: 8 visits over 2 years = 4/yr × 3 = 12 pts.
          </p>
        </div>

        <div class="rounded-xl p-4" style="background:#F5F3FF; border:1px solid #DDD6FE">
          <div class="flex items-center gap-2 mb-2">
            <div class="w-8 h-8 rounded-lg flex items-center justify-center text-white text-sm font-bold bg-violet-500">M</div>
            <div class="font-bold" style="color:var(--navy)">Spend Value <span class="font-normal text-xs" style="color:var(--muted)">max 20 pts</span></div>
          </div>
          <p class="text-xs mb-3" style="color:#475569">
            Average spend per visit. Higher-value clients are prioritised because winning them back
            has a greater revenue impact.
          </p>
          <p class="text-xs p-2 rounded-lg" style="background:#EDE9FE; color:#4C1D95">
            <strong>Formula:</strong> avg spend ÷ 5, capped at 20 pts.<br>
            Example: £80 avg spend = 16 pts. £100+ = full 20 pts.
          </p>
        </div>

      </div>

      <div class="rounded-xl p-4 flex gap-4 items-start" style="background:#FFF1F2; border:1px solid #FECDD3">
        <div class="w-8 h-8 rounded-lg flex items-center justify-center text-white text-sm font-bold bg-red-400 flex-shrink-0">!</div>
        <div>
          <div class="font-bold mb-1" style="color:#991B1B">No-show Penalty <span class="font-normal text-xs text-red-400">up to −15 pts</span></div>
          <p class="text-xs" style="color:#7F1D1D">
            Each recorded no-show deducts 3 points from the total score (capped at −15).
            This reduces the priority of clients who are less likely to honour an SMS-prompted booking,
            saving you from wasted appointment slots. The ⚠ icon next to a client's name shows their no-show count.
          </p>
        </div>
      </div>

    </div>
  </details>
</div>

<!-- ── Filters ──────────────────────────────────────────────── -->
<div class="px-6 pb-3 flex flex-wrap gap-3 items-center">
  <input type="search" id="searchInput" placeholder="🔍  Search client name…"
         oninput="applyFilters()" class="w-56">
  <select id="statusFilter" onchange="applyFilters()">
    <option value="">All Statuses</option>
    <option value="active">Active</option>
    <option value="due">Due Soon</option>
    <option value="lapsing">Lapsing</option>
    <option value="lapsed">Lapsed</option>
  </select>
  <select id="stylistFilter" onchange="applyFilters()">
    <option value="">All Stylists</option>
    {% for s in stylists %}
    <option value="{{ s }}">{{ s }}</option>
    {% endfor %}
  </select>
  <select id="dayFilter" onchange="applyFilters()">
    <option value="">Any Day</option>
    <option>Mon</option><option>Tue</option><option>Wed</option>
    <option>Thu</option><option>Fri</option><option>Sat</option><option>Sun</option>
  </select>
  <span id="rowCount" class="text-sm ml-auto font-medium" style="color:var(--muted)"></span>
</div>

<!-- ── Table ────────────────────────────────────────────────── -->
<div class="px-6 pb-10 overflow-x-auto">
<div class="rounded-2xl border overflow-hidden shadow-sm" style="border-color:var(--border); background:#fff">
<table id="mainTable">
<thead>
  <tr class="text-xs uppercase tracking-wider">
    <th class="text-left py-3 px-3" onclick="sortBy(0)" id="th0">#</th>
    <th class="text-left py-3 px-3" onclick="sortBy(1)" id="th1">Client</th>
    <th class="text-left py-3 px-3" onclick="sortBy(2)" id="th2">Score</th>
    <th class="text-left py-3 px-3">Status</th>
    <th class="text-left py-3 px-3" onclick="sortBy(4)" id="th4">Last Visit</th>
    <th class="text-left py-3 px-3" onclick="sortBy(5)" id="th5">Days Since</th>
    <th class="text-left py-3 px-3 tip-wrap">Overdue
      <span class="tip-box">Days past their usual visit rhythm.<br>Blank = not yet overdue or only 1 visit.</span>
    </th>
    <th class="text-left py-3 px-3" onclick="sortBy(7)" id="th7">Visits</th>
    <th class="text-left py-3 px-3" onclick="sortBy(8)" id="th8">Avg Spend</th>
    <th class="text-left py-3 px-3" onclick="sortBy(9)" id="th9">Total Spend</th>
    <th class="text-left py-3 px-3">Stylist</th>
    <th class="text-left py-3 px-3">Best Contact</th>
    <th class="text-left py-3 px-3">Services</th>
    <th class="text-left py-3 px-3">Suggested SMS</th>
  </tr>
</thead>
<tbody id="tableBody">
{% for c in clients %}
<tr data-status="{{ c.scls }}"
    data-stylist="{{ c.pref_tm }}"
    data-day="{{ c.pref_day }}"
    data-name="{{ c.name | lower }}">
  <td class="py-3 px-3 font-mono text-xs" style="color:var(--muted)">{{ c.rank }}</td>

  <td class="py-3 px-3">
    <span class="font-semibold" style="color:var(--navy)">{{ c.name }}</span>
    {% if c.no_shows > 0 %}
    <span class="ml-1.5 text-xs text-red-400 tip-wrap">
      ⚠ {{ c.no_shows }}
      <span class="tip-box">{{ c.no_shows }} recorded no-show(s) — score penalised</span>
    </span>
    {% endif %}
  </td>

  <td class="py-3 px-3">
    <div class="tip-wrap">
      <div class="flex items-center gap-2">
        <span class="font-bold w-10" style="color:var(--navy)">{{ c.score }}</span>
        <div class="score-bar"><div class="score-fill" style="width:{{ c.score_pct }}%"></div></div>
      </div>
      <div class="tip-box">
        <div class="font-semibold mb-2 text-center text-white">Score Breakdown</div>
        <div class="space-y-1 text-white/80">
          <div class="flex justify-between gap-6"><span>Recency</span><span style="color:var(--teal)">+{{ c.sr }}</span></div>
          <div class="flex justify-between gap-6"><span>Overdue bonus</span><span style="color:var(--teal)">+{{ c.so }}</span></div>
          <div class="flex justify-between gap-6"><span>Frequency</span><span style="color:var(--teal)">+{{ c.sf }}</span></div>
          <div class="flex justify-between gap-6"><span>Spend</span><span style="color:var(--teal)">+{{ c.sm }}</span></div>
          {% if c.sp < 0 %}
          <div class="flex justify-between gap-6"><span>No-show penalty</span><span class="text-red-400">{{ c.sp }}</span></div>
          {% endif %}
          <div class="border-t border-white/20 pt-1 flex justify-between gap-6 font-bold text-white">
            <span>Total</span><span>{{ c.score }}</span>
          </div>
        </div>
      </div>
    </div>
  </td>

  <td class="py-3 px-3">
    <span class="px-2.5 py-0.5 rounded-full text-xs font-semibold badge-{{ c.scls }}">{{ c.status }}</span>
  </td>

  <td class="py-3 px-3 text-sm" style="color:var(--text)" data-val="{{ c.days_since }}">{{ c.last_visit }}</td>
  <td class="py-3 px-3 text-sm" style="color:var(--muted)" data-val="{{ c.days_since }}">{{ c.days_since }}d</td>

  <td class="py-3 px-3">
    {% if c.overdue %}
      <span class="text-amber-500 font-semibold text-sm">{{ c.overdue }}d late</span>
      {% if c.avg_gap %}<div class="text-xs" style="color:var(--muted)">avg gap {{ c.avg_gap }}d</div>{% endif %}
    {% elif c.avg_gap %}
      <span class="text-xs" style="color:var(--muted)">gap {{ c.avg_gap }}d</span>
    {% else %}
      <span style="color:#CBD5E1">—</span>
    {% endif %}
  </td>

  <td class="py-3 px-3 text-sm text-center" style="color:var(--text)" data-val="{{ c.n_visits }}">{{ c.n_visits }}</td>
  <td class="py-3 px-3 text-sm" style="color:var(--text)" data-val="{{ c.avg_spend }}">£{{ c.avg_spend }}</td>
  <td class="py-3 px-3 text-sm" style="color:var(--text)" data-val="{{ c.total_spend }}">£{{ c.total_spend }}</td>
  <td class="py-3 px-3 text-sm" style="color:var(--text)">{{ c.pref_tm }}</td>

  <td class="py-3 px-3">
    <div class="text-sm font-medium" style="color:var(--navy)">{{ c.pref_day }}</div>
    <div class="text-xs" style="color:var(--muted)">{{ c.pref_time }}</div>
  </td>

  <td class="py-3 px-3">
    {% for cat in c.top_cats %}
    <span class="inline-block text-xs px-2 py-0.5 rounded-md mr-1 mb-0.5 whitespace-nowrap font-medium"
          style="background:#EEF9F8; color:var(--teal-d); border:1px solid #C8EEEB">{{ cat }}</span>
    {% endfor %}
  </td>

  <td class="py-3 px-3 min-w-[340px] max-w-[420px]">
    <div class="flex items-start gap-2">
      <span class="text-sm leading-relaxed flex-1 sms-text" style="color:var(--text)">{{ c.sms }}</span>
      <button onclick="copySMS(this)" class="btn-copy flex-shrink-0 mt-0.5">Copy</button>
    </div>
    <div class="text-xs mt-1" style="color:#94A3B8">{{ c.sms | length }} chars</div>
  </td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
</div>

<!-- ── Client Lookup ────────────────────────────────────────── -->
<div class="px-6 pb-16">
  <div class="pt-2 pb-6 mb-6" style="border-top:2px solid var(--border)">
    <h2 class="text-xl font-bold mb-1" style="color:var(--navy)">Client Lookup</h2>
    <p class="text-sm" style="color:var(--muted)">Search any client by name — regardless of their rank in the top 100.</p>
  </div>
  <input type="search" id="lookupInput" placeholder="🔍  Type a client name…"
         oninput="onLookupInput()" class="w-72 mb-6">
  <div id="lookupResults"></div>
</div>

<script>
// ── Filter ────────────────────────────────────────────────────
function applyFilters() {
  const search  = document.getElementById('searchInput').value.toLowerCase();
  const status  = document.getElementById('statusFilter').value;
  const stylist = document.getElementById('stylistFilter').value;
  const day     = document.getElementById('dayFilter').value;
  let visible   = 0;
  document.querySelectorAll('#tableBody tr').forEach(row => {
    const ok = (!search  || row.dataset.name.includes(search))
            && (!status  || row.dataset.status  === status)
            && (!stylist || row.dataset.stylist  === stylist)
            && (!day     || row.dataset.day      === day);
    row.style.display = ok ? '' : 'none';
    if (ok) visible++;
  });
  document.getElementById('rowCount').textContent = `Showing ${visible} of 500 clients`;
}

// ── Sort ──────────────────────────────────────────────────────
let _sortCol = -1, _sortAsc = false;
function sortBy(col) {
  _sortAsc = (_sortCol === col) ? !_sortAsc : true;
  _sortCol = col;
  document.querySelectorAll('thead th[id]').forEach(th => th.className = '');
  const th = document.getElementById('th' + col);
  if (th) th.className = _sortAsc ? 'sort-asc' : 'sort-desc';
  const tbody = document.getElementById('tableBody');
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {
    const ac = a.cells[col], bc = b.cells[col];
    const av = ac.dataset.val ?? ac.textContent.trim();
    const bv = bc.dataset.val ?? bc.textContent.trim();
    const an = parseFloat(av), bn = parseFloat(bv);
    let cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv);
    return _sortAsc ? cmp : -cmp;
  });
  rows.forEach(r => tbody.appendChild(r));
  applyFilters();
}

// ── Refresh ───────────────────────────────────────────────────
async function refreshData() {
  const btn = document.getElementById('refreshBtn');
  btn.textContent = '⏳ Refreshing…';
  btn.disabled = true;
  await fetch('/api/refresh', { method: 'POST' });
  location.reload();
}

// ── Client Lookup ─────────────────────────────────────────────
let lookupTimer = null;
function onLookupInput() {
  clearTimeout(lookupTimer);
  lookupTimer = setTimeout(runLookup, 300);
}

async function runLookup() {
  const q = document.getElementById('lookupInput').value.trim();
  const box = document.getElementById('lookupResults');
  if (q.length < 2) { box.innerHTML = ''; return; }
  box.innerHTML = '<p style="color:#64748B" class="text-sm p-2">Searching…</p>';
  const res  = await fetch('/api/search?q=' + encodeURIComponent(q));
  const data = await res.json();
  if (!data.length) {
    box.innerHTML = '<p style="color:#64748B" class="text-sm p-2">No clients found matching "' + q + '".</p>';
    return;
  }
  box.innerHTML = data.map(c => {
    const bCls = {active:'badge-active',due:'badge-due',lapsing:'badge-lapsing',lapsed:'badge-lapsed'}[c.scls]||'';
    const rankBadge = c.rank
      ? `<span style="background:#EEF9F8;color:#007A6D;border:1px solid #C8EEEB" class="text-xs px-2 py-0.5 rounded-full font-bold">#${c.rank} in Top 100</span>`
      : `<span style="background:#F1F5F9;color:#64748B;border:1px solid #E2E8EF" class="text-xs px-2 py-0.5 rounded-full">Not in Top 100</span>`;
    const overdueTxt = c.overdue
      ? `<span class="text-amber-500 font-semibold">${c.overdue}d overdue</span>`
      : `<span style="color:#64748B">Not overdue</span>`;
    const cats = (c.top_cats||[]).map(cat =>
      `<span style="background:#EEF9F8;color:#007A6D;border:1px solid #C8EEEB" class="text-xs px-2 py-0.5 rounded-md font-medium">${cat}</span>`
    ).join(' ');
    return `
    <div class="lookup-card">
      <div class="flex flex-wrap items-start justify-between gap-3 mb-5">
        <div>
          <div class="flex items-center gap-2 flex-wrap mb-1">
            <h3 class="text-xl font-bold" style="color:#1C2B3A">${c.name}</h3>
            ${rankBadge}
            <span class="px-2.5 py-0.5 rounded-full text-xs font-semibold ${bCls}">${c.status}</span>
            ${c.no_shows>0?`<span class="text-red-400 text-xs">⚠ ${c.no_shows} no-show(s)</span>`:''}
          </div>
          <div class="text-sm" style="color:#64748B">Last visit: <strong style="color:#1C2B3A">${c.last_visit}</strong> &nbsp;·&nbsp; ${c.days_since} days ago</div>
        </div>
        <div class="text-right">
          <div class="text-4xl font-extrabold" style="color:#00B5A4">${c.score}</div>
          <div class="text-xs font-medium" style="color:#64748B">SMS score</div>
        </div>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <div class="mini-stat"><div class="text-xs font-semibold mb-1" style="color:#64748B">Visits (2yr)</div><div class="font-bold" style="color:#1C2B3A">${c.n_visits}</div></div>
        <div class="mini-stat"><div class="text-xs font-semibold mb-1" style="color:#64748B">Avg Spend</div><div class="font-bold" style="color:#1C2B3A">£${c.avg_spend}</div></div>
        <div class="mini-stat"><div class="text-xs font-semibold mb-1" style="color:#64748B">Total Spend</div><div class="font-bold" style="color:#1C2B3A">£${c.total_spend}</div></div>
        <div class="mini-stat"><div class="text-xs font-semibold mb-1" style="color:#64748B">Visit Gap</div><div class="font-bold" style="color:#1C2B3A">${c.avg_gap?c.avg_gap+'d':'—'}</div></div>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-3 gap-3 mb-4 text-sm">
        <div><span style="color:#64748B">Preferred stylist: </span><span class="font-semibold" style="color:#1C2B3A">${c.pref_tm}</span></div>
        <div><span style="color:#64748B">Best day: </span><span class="font-semibold" style="color:#1C2B3A">${c.pref_day} ${c.pref_time}</span></div>
        <div><span style="color:#64748B">Overdue: </span>${overdueTxt}</div>
      </div>
      ${cats?`<div class="flex flex-wrap gap-1 mb-4">${cats}</div>`:''}
      <div class="rounded-xl p-4 mb-4" style="background:#F8FAFC;border:1px solid #E2E8EF">
        <div class="text-xs font-semibold uppercase tracking-widest mb-2" style="color:#64748B">Suggested SMS</div>
        <div class="flex items-start gap-3">
          <p class="text-sm leading-relaxed flex-1" style="color:#1C2B3A" id="sms-lookup-${c.id}">${c.sms}</p>
          <button onclick="copyLookupSMS('${c.id}')" class="btn-copy flex-shrink-0">Copy</button>
        </div>
        <div class="text-xs mt-1" style="color:#94A3B8">${c.sms.length} chars</div>
      </div>
      <div class="grid grid-cols-5 gap-2 text-xs text-center">
        <div class="mini-stat"><div style="color:#64748B" class="mb-1">Recency</div><div class="font-bold" style="color:#00B5A4">+${c.sr}</div></div>
        <div class="mini-stat"><div style="color:#64748B" class="mb-1">Overdue</div><div class="font-bold" style="color:#00B5A4">+${c.so}</div></div>
        <div class="mini-stat"><div style="color:#64748B" class="mb-1">Frequency</div><div class="font-bold" style="color:#00B5A4">+${c.sf}</div></div>
        <div class="mini-stat"><div style="color:#64748B" class="mb-1">Spend</div><div class="font-bold" style="color:#00B5A4">+${c.sm}</div></div>
        <div class="mini-stat"><div style="color:#64748B" class="mb-1">Penalty</div><div class="font-bold ${c.sp<0?'text-red-400':''}">${c.sp}</div></div>
      </div>
    </div>`;
  }).join('');
}

function copyLookupSMS(id) {
  const el  = document.getElementById('sms-lookup-' + id);
  const btn = el.parentElement.querySelector('button');
  navigator.clipboard.writeText(el.textContent.trim()).then(() => {
    const orig = btn.textContent;
    btn.classList.add('copied'); btn.textContent = '✓ Copied';
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = orig; }, 2000);
  });
}

// ── Copy SMS ──────────────────────────────────────────────────
function copySMS(btn) {
  const text = btn.closest('td').querySelector('.sms-text').textContent.trim();
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.classList.add('copied'); btn.textContent = '✓ Copied';
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = orig; }, 2000);
  });
}

// Init
applyFilters();
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("Starting Salon SMS Dashboard on http://127.0.0.1:5000")
    print("First load fetches ~2 years of bookings — may take 10–20 seconds.")
    app.run(debug=False, port=5000)

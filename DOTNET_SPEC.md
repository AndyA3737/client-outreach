# SalonIQ Aria — .NET Core Technical Specification
**Version:** 1.0  
**Reference implementation:** Python/Flask at tag `v1.0-reference`  
**Target stack:** ASP.NET Core 8, Razor Pages, Alpine.js, Hangfire, Redis, PostgreSQL  
**Deployment:** Railway (initial), Azure (future)

---

## 1. Overview

SalonIQ Aria is a web application that connects to the SalonIQ API, processes salon business data, and provides AI-powered analysis and client marketing tools. It has three core features:

1. **Data Analysis** — natural language questions answered by Claude AI against loaded salon data
2. **Client Selections** — natural language segment builder that filters the full client database
3. **SMS & Email Blast** — send personalised messages to selected clients via SalonIQ messaging APIs

---

## 2. Technology Stack

| Concern | Technology |
|---|---|
| Web framework | ASP.NET Core 8, Razor Pages |
| Client interactivity | Alpine.js 3.x (CDN), Chart.js 4.x (CDN) |
| Background jobs | Hangfire 1.8.x with Redis storage |
| Distributed cache | Redis (StackExchange.Redis) |
| Database | PostgreSQL (Npgsql + Dapper for raw SQL; no EF Core) |
| AI | Anthropic .NET SDK (`Anthropic.SDK`) |
| HTTP client | `HttpClient` via `IHttpClientFactory` |
| Deployment | Railway (Dockerfile or nixpacks) |

**NuGet packages required:**
```
Hangfire.Core
Hangfire.AspNetCore
Hangfire.Redis.StackExchange
StackExchange.Redis
Npgsql
Dapper
Anthropic.SDK
Newtonsoft.Json (for JSON repair/parsing)
```

---

## 3. Project Structure

```
SalonIQAria/
├── SalonIQAria.csproj
├── Program.cs
├── appsettings.json
├── Dockerfile
├── Models/
│   ├── ClientRecord.cs
│   ├── TenantContext.cs
│   ├── JobResult.cs
│   ├── BrandSettings.cs
│   └── HistoryItem.cs
├── Services/
│   ├── SalonIQService.cs        # SalonIQ API HTTP client
│   ├── DataBuildService.cs      # Equivalent of build_data()
│   ├── AnalysisService.cs       # Claude AI analysis jobs
│   ├── TenantStore.cs           # Redis-backed tenant data store
│   ├── JobStore.cs              # Redis-backed job status store
│   ├── BrandService.cs          # Brand settings CRUD
│   ├── HistoryService.cs        # History CRUD
│   ├── ActivityLogService.cs    # Activity logging
│   ├── SmsService.cs            # SMS blast
│   └── EmailService.cs          # Email blast
├── Pages/
│   ├── Index.cshtml             # Main app shell (redirects to login if unauth)
│   ├── Login.cshtml
│   ├── Login.cshtml.cs
│   ├── Logout.cshtml.cs
│   └── Admin/
│       └── Logs.cshtml
├── Api/
│   ├── TenantsController.cs
│   ├── DataController.cs        # /api/data, /api/job/{id}, /api/refresh
│   ├── AnalyseController.cs     # /api/analyse
│   ├── QueryController.cs       # /api/query
│   ├── SearchController.cs      # /api/search
│   ├── HistoryController.cs     # /api/history
│   ├── BrandController.cs       # /api/brand
│   ├── EmailController.cs       # /api/email/preview, /api/email/send
│   ├── SmsController.cs         # /api/sms/send, /api/sms/test
│   ├── ModeController.cs        # /api/mode
│   └── LogController.cs         # /api/log
└── wwwroot/
    ├── js/
    │   └── aria.js              # All Alpine.js components and page logic
    ├── css/
    │   └── aria.css             # All styles (port from index.html <style> block)
    └── favicon.svg
```

---

## 4. Configuration

### appsettings.json structure
```json
{
  "ConnectionStrings": {
    "Postgres": "",
    "Redis": ""
  },
  "SalonIQ": {
    "SmsToken": "79B57270-8300-40F8-82FE-FFE47EE62A44",
    "EmailToken": "1166554",
    "EmailHtmlBaseUrl": "",
    "SmsPathBeta": "/Wella/SendSMS",
    "SmsPathLive": "/Wella/SendSMS",
    "SmsPathDemo": "/Wella/SendSMS"
  },
  "Anthropic": {
    "ApiKey": ""
  },
  "Auth": {
    "AdminUser": "",
    "AdminPass": "",
    "AdminTenant": "",
    "AdminServer": "BETA"
  }
}
```

All values overridden by Railway environment variables. Map directly:
- `DATABASE_URL` → `ConnectionStrings:Postgres`
- `REDIS_URL` → `ConnectionStrings:Redis`
- `ANTHROPIC_API_KEY` → `Anthropic:ApiKey`
- `SMS_TOKEN` → `SalonIQ:SmsToken`
- `EMAIL_TOKEN` → `SalonIQ:EmailToken`
- `ADMIN_USER` / `ADMIN_PASS` / `ADMIN_TENANT` / `ADMIN_SERVER` → `Auth:*`

### Server definitions (hardcoded in `SalonIQService.cs`)
```csharp
public static readonly Dictionary<string, ServerConfig> Servers = new()
{
    ["BETA"] = new ServerConfig {
        Base           = "https://greathairhub.saloniq.co.uk/api/GetAPIReport",
        SmsBase        = "https://greathairhub.saloniq.co.uk",
        EmailBase      = "https://greathairhub.saloniq.co.uk/api/SendEmail",
        HtmlEmailBase  = "https://greathairhub.saloniq.co.uk/api/SendHTMLEmail",
        Token          = "ACD7636F-D6D5-45AB-92FC-785D4904ADA5",
        DefaultTenant  = "1E7D7624-FEB7-4950-A6BE-5FBB1498EE39",
        DateFormat     = "dd/MM/yyyy",   // ← CRITICAL: BETA uses DD/MM/YYYY
    },
    ["LIVE"] = new ServerConfig {
        Base           = "https://apihub.saloniq.co.uk/api/GetAPIReport",
        SmsBase        = "https://apihub.saloniq.co.uk",
        EmailBase      = "https://apihub.saloniq.co.uk/api/SendEmail",
        HtmlEmailBase  = "https://apihub.saloniq.co.uk/api/SendHTMLEmail",
        Token          = "517a41d9-48e3-4af7-ae6c-0e30688f9325",
        DefaultTenant  = "1E7D7624-FEB7-4950-A6BE-5FBB1498EE39",
        DateFormat     = "MM/dd/yyyy",   // ← CRITICAL: LIVE uses MM/DD/YYYY
    },
    ["DEMO"] = new ServerConfig {
        Base           = "https://demohub.saloniq.co.uk/api/GETAPIReport",
        SmsBase        = "https://demohub.saloniq.co.uk",
        EmailBase      = "https://demohub.saloniq.co.uk/api/SendEmail",
        HtmlEmailBase  = "https://demohub.saloniq.co.uk/api/SendHTMLEmail",
        Token          = "ACD7636F-D6D5-45AB-92FC-785D4904ADA5",
        DefaultTenant  = "1E7D7624-FEB7-4950-A6BE-5FBB1498EE39",
        DateFormat     = "dd/MM/yyyy",
    },
};
```

---

## 5. Authentication

### Session-based cookie auth
Use `AddCookieAuthentication` with a 7-day sliding expiration. Store in session:
- `username` (string)
- `role` — `"admin"` or `"user"`
- `tenant_id` (string GUID)
- `server` — `"BETA"`, `"LIVE"`, or `"DEMO"`
- `account_code` (string, e.g. `"GRT001"`)

### Login flow (`POST /login`)
1. Extract `account_code`, `username`, `password` from form
2. **Environment variable fallback first:** if `ADMIN_USER` + `ADMIN_PASS` + `ADMIN_TENANT` are set and credentials match → assign `role = username == "admin" ? "admin" : "user"`, use `ADMIN_TENANT` and `ADMIN_SERVER`
3. **SalonIQ LogOn API:** POST to `{server.Base}` (replace `GetAPIReport` with `LogOn`) with params:
   ```
   TokenID      = server.Token
   ReportName   = "XXX_Export_Admin_Aria_LogOn"
   TenantID     = ""
   data1        = account_code
   data2        = username
   data3        = password
   startdate    = first day of current month (server date format)
   enddate      = last day of current month (server date format)
   ```
   Parse response: `data.Data.Array[0].TenantId`
4. Server routing by account code:
   - `GRT001` → BETA
   - `DEM001` → DEMO
   - Everything else → LIVE
5. On success: set auth cookie, redirect to `/`
6. On failure: re-render login page with error message

### Roles
- `admin` — sees Logs button, can access `/admin/logs`
- `user` — same as admin minus the logs button

---

## 6. Database

Use Dapper for all database operations (no EF Core). Run `CREATE TABLE IF NOT EXISTS` on startup.

### activity_log
```sql
CREATE TABLE IF NOT EXISTS activity_log (
    id            SERIAL PRIMARY KEY,
    ts            TEXT NOT NULL,
    event_type    TEXT NOT NULL,
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
);
```

### history
```sql
CREATE TABLE IF NOT EXISTS history (
    id             SERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_code   TEXT NOT NULL,
    type           TEXT NOT NULL,         -- 'analysis' or 'selection'
    username       TEXT,
    question       TEXT NOT NULL,
    format         TEXT,
    result_json    JSONB,
    result_title   TEXT,
    result_summary TEXT,
    result_count   INTEGER,
    is_followup    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS history_account_ts ON history(account_code, ts DESC);
```

### brand_settings
```sql
CREATE TABLE IF NOT EXISTS brand_settings (
    account_code  TEXT PRIMARY KEY,
    logo_url      TEXT,
    primary_color TEXT DEFAULT '#3A7A50',
    font_pair     TEXT DEFAULT 'premium',
    salon_name    TEXT,
    salon_phone   TEXT,
    salon_address TEXT,
    booking_url   TEXT,
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);
```

### On startup: auto-purge history older than 90 days
```sql
DELETE FROM history WHERE ts < NOW() - INTERVAL '90 days';
```

---

## 7. Redis Cache Structure

All keys are prefixed with `aria:`.

| Key | Type | TTL | Content |
|---|---|---|---|
| `aria:tenant:{SERVER}\|{TENANT_ID}` | JSON string | 4 hours | Full `TenantContext` (serialised) |
| `aria:job:{job_id}` | JSON string | 2 hours | `JobResult` object |
| `aria:apicache:{server}\|{report}\|{sd}\|{ed}\|{tid}` | JSON string | 1 hour | Raw SalonIQ API response array |

### TenantContext model
```csharp
public class TenantContext {
    public List<ClientRecord> AllClients { get; set; }
    public List<ClientRecord> AllScored { get; set; }   // clients without future bookings
    public int TotalClients { get; set; }
    public Dictionary<string, MonthlyStat> ServiceMonthly { get; set; }
    public Dictionary<string, MonthlyStat> ServiceWeekly { get; set; }
    public Dictionary<string, MonthlyStat> ServiceDaily { get; set; }
    public Dictionary<string, Dictionary<string, CategoryStat>> ServiceCatMonthly { get; set; }
    public Dictionary<string, Dictionary<string, SalonMonthlyStat>> ServiceSalonMonthly { get; set; }
    public Dictionary<string, SalonKpi> SalonKpis { get; set; }
    public Dictionary<string, string> SalonMap { get; set; }
    public RetailSummary RetailSummary { get; set; }
    public BookingStats BookingStats { get; set; }
    public Dictionary<string, Dictionary<string, TeamStat>> TeamRebookMonthly { get; set; }
    public Dictionary<string, Dictionary<string, TeamStat>> TeamDayStats { get; set; }
    public Dictionary<string, Dictionary<string, TeamStat>> TeamWeekStats { get; set; }
    public Dictionary<string, TeamKpi> TeamKpis { get; set; }
    public string LoadedSalonName { get; set; }
    public string LoadedTenantId { get; set; }
    public string LoadedServer { get; set; }
    public string LoadedTenantName { get; set; }
    public string LoadedAccountCode { get; set; }
    public List<string> LoadedSalonIds { get; set; }
    public DateTime LoadedAt { get; set; }
}
```

---

## 8. SalonIQ API Integration (`SalonIQService.cs`)

### Base fetch method
```
POST {server.Base}
params:
  TokenID     = server.Token
  TenantID    = tenantId.ToUpper()
  ReportName  = reportName
  startdate   = sd
  enddate     = ed
  Salonid     = ""
  UserID      = ""
  data1..4    = ""
```

Response shape: `{ "Data": { "Array": [...] }, "Status": "Success" }`  
Extract: `response.Data.Array` as `List<Dictionary<string, object>>`

**Cache all responses** using the Redis API cache key (1 hour TTL).  
**Do NOT cache** these reports: `XXX_Export_Admin_Aria_Bookings`, `XXX_Export_Admin_TUBR_Bookings`

### Reports used and their fixed dates

| Report | startdate | enddate | Notes |
|---|---|---|---|
| `XXX_Export_Admin_TUBR_Clients` | 01/01/2026 | 01/01/2026 | Fixed date, returns all clients |
| `XXX_Export_Admin_TUBR_services` | 01/01/2026 | 01/01/2026 | Service catalogue |
| `XXX_Export_Admin_Aria_TeamMembers` | today-730d | today | Wide range to include past staff |
| `XXX_Export_Admin_Aria_SalonList` | 01/01/2026 | 01/01/2026 | Salon list + KPIs |
| `XXX_Export_Admin_TUBR_Utilisation` | today-182d | today+91d | 6mo back, 3mo forward |
| `XXX_Export_Admin_TUBR_Tags` | 01/01/2026 | 01/01/2026 | Client tags |
| `XXX_Export_Admin_Aria_Bookings` | (4 chunks, see below) | — | Never cached |
| `XXX_Export_Admin_TUBR_GiftCards` | today-730d | today | — |
| `XXX_Export_Admin_TUBR_Promotions` | today-730d | today | — |
| `XXX_Export_Admin_TUBR_Products` | 01/01/2026 | 01/01/2026 | Product catalogue |
| `XXX_Export_Admin_TUBR_RetailSales` | today-730d | today | — |
| `XXX_Export_Admin_BenchMarks_TenantList` | 01/01/2026 | 01/01/2026 | Tenant list for admin |

### Booking date chunks (fetch in parallel, 4 tasks)
```
Chunk 1: today-730d → today-547d
Chunk 2: today-547d → today-365d
Chunk 3: today-365d → today-182d
Chunk 4: today-182d → today+365d   (includes future bookings)
```
For each chunk: try `XXX_Export_Admin_Aria_Bookings` first. If empty result, fall back to `XXX_Export_Admin_TUBR_Bookings`.

### Booking fields (key names in the response dict)
```
BookingId         → string (booking unique ID, use for deduplication)
ClientId          → string (GUID, lowercase for matching)
Start             → datetime string "M/d/yyyy h:mm:ss tt" or "M/d/yyyy H:mm:ss"
ServiceId         → string (lookup in svc_map)
TeamMemberId      → string (lookup in team_map)
TotalSalesPrice   → decimal
Status            → int  0=booked, 1=arrived, 2=paid, 3=no-show
Source            → int  1=online, 5=in-salon
Salonid           → string
HasBeenRebooked   → "True"/"False" string
RequestTeamMember → "True"/"False" string
```

### Datetime parsing for bookings
Try both formats in order:
1. `"M/d/yyyy h:mm:ss tt"` (12-hour with AM/PM)
2. `"M/d/yyyy H:mm:ss"` (24-hour)

---

## 9. Data Processing Pipeline (`DataBuildService.cs`)

This is the most complex part. Implement as a Hangfire background job.

### Step sequence
1. Fetch clients, services, team members, salons (parallel where possible)
2. Fetch utilisation data
3. Fetch client tags
4. Fetch booking chunks in parallel (4 tasks, `Task.WhenAll`)
5. Aggregate booking stats (revenue, visits, no-shows by month/week/day/salon/category/team)
6. Fetch gift cards, promotions, retail
7. Build client profiles (score each client)
8. Store result in Redis as `TenantContext`

### Client scoring algorithm

For each client with at least one paid visit (Status == 2):

```
days_since   = (today - last_paid_visit.Date).Days
first_dt     = earliest paid visit datetime
n            = count of unique visit dates (paid only)
avg_gap      = n > 1 ? (last_visit - first_visit).Days / (n - 1) : null
overdue      = avg_gap.HasValue ? days_since - avg_gap.Value : null
total_spend  = sum of TotalSalesPrice for paid visits
avg_spend    = total_spend / n
years        = max((today - first_dt.Date).TotalDays / 365.25, 0.08)
no_shows     = int(client["NoShows"])  // from client record, not bookings

// Recency score (max 40)
r_score = days_since <= 30  ? 10
        : days_since <= 90  ? 40
        : days_since <= 180 ? 30
        : days_since <= 365 ? 15
        : 5

// Overdue bonus (max 20)
o_score = (overdue.HasValue && overdue > 0 && avg_gap.HasValue)
          ? Math.Min(overdue.Value / avg_gap.Value * 20, 20)
          : 0

// Frequency score (max 20)
f_score = Math.Min(n / years * 3, 20)

// Spend score (max 20)
m_score = Math.Min(avg_spend / 5, 20)

// No-show penalty (max -15)
penalty = Math.Min(no_shows * 3, 15)

total_score = r_score + o_score + f_score + m_score - penalty

// Status classification
status = days_since <= 60  ? "Active"   (scls: "active")
       : days_since <= 120 ? "Due Soon" (scls: "due")
       : days_since <= 365 ? "Lapsing"  (scls: "lapsing")
       : "Lapsed" (scls: "lapsed")
```

Clients with a future booking are excluded from `AllScored` (top 500) but included in `AllClients`.

### Monthly aggregation deduplication
When aggregating visits and no-shows by month/week/day, **deduplicate by BookingId** within each time bucket. Use `HashSet<(string bookingId, string bucket)>` patterns to avoid counting multi-service bookings multiple times.

### Preferred stylist / salon / day / time
```
pref_tm    = team_map[most frequent TeamMemberId across paid visits]
pref_salon = salon_map[most frequent Salonid across paid visits]
pref_day   = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][most frequent DayOfWeek]
pref_time  = hour < 12 ? "Morning" : hour < 14 ? "Lunchtime" : hour < 17 ? "Afternoon" : "Evening"
```

### Top service categories
```
top_cats = top 2 service categories by count of paid visits
           (use svc_map[ServiceId].Categoty — note the typo in the API field name)
           strip "HAIR - " prefix from category names
```

### SMS message generation
Pre-generate an SMS message for each client. Rules:
- Use client's first name, preferred stylist, status (active/due/lapsing/lapsed)
- Top service category determines tone (colour client gets colour-specific message)
- Use a hash of the client ID for A/B variant selection (`hash(clientId) % 2`)
- Cap at 160 characters (truncate with "…" if needed)
- See Python `build_sms()` for exact message templates

---

## 10. API Endpoints

All controllers return JSON. Auth required on all except `/login`, `/logout`, `/favicon.*`.

### GET `/api/tenants?server={server}`
Returns list of tenants from `XXX_Export_Admin_BenchMarks_TenantList`.
```json
[{ "id": "...", "name": "...", "code": "..." }]
```
Sort by `code`.

### GET `/api/data?server=&tenant_id=&tenant_name=&account_code=&force=0`
Starts a background job to load salon data. Returns immediately.
```json
{ "job_id": "...", "cached": true|false }
```
- If data already in Redis and not expired and `force != "1"`: return existing job immediately with `cached: true`
- Otherwise: enqueue `DataBuildJob` via Hangfire, return new `job_id`

### GET `/api/job/{jobId}`
Polls job status.
```json
// Loading:
{ "status": "loading", "step": "Fetching booking history..." }
// Done:
{ "status": "done", ...result fields... }
// Error:
{ "status": "error", "error": "..." }
```

### GET `/api/refresh?server=&tenant_id=`
Invalidates the Redis tenant cache. Returns `{"ok": true}`.

### GET `/api/mode`
Returns current session state.
```json
{
  "mode": "admin" | "user",
  "tenant_id": "...",
  "server": "BETA",
  "account_code": "GRT001"
}
```
Returns 401 if not authenticated.

### GET `/api/search?q=&server=&tenant_id=`
Searches all clients by name (case-insensitive, partial match). Returns up to 10 results. Searches `AllClients` in TenantContext.

### POST `/api/analyse`
Starts a background AI analysis job.
```json
// Request:
{
  "question": "...",
  "format": "dashboard|list|report|chart",
  "previous_result": null | {...},
  "tenant_id": "...",
  "server": "BETA"
}
// Response:
{ "job_id": "..." }
```
The job calls Claude via Anthropic SDK. See Section 12 for prompt construction.

### GET `/api/query?q=&server=&tenant_id=`
Interprets natural language into filter criteria via Claude Haiku, applies filters to `AllClients`, returns matched clients.
```json
{
  "clients": [...],
  "total": 287,
  "description": "Clients who...",
  "criteria": { "filters": [...], "logic": "AND" }
}
```

### GET/POST `/api/history?account_code=&type=`
GET: Returns last 100 history items for account within 90 days.  
DELETE `/api/history/{id}?account_code=`: Deletes a specific record.

### GET/POST `/api/brand`
GET: Returns brand settings for current session's account_code.  
POST: Saves brand settings (body is JSON with all brand fields).

### POST `/api/email/preview`
Generates and returns HTML email from template + brand + content (no sending).
```json
// Request:
{
  "template_id": "minimal|hero|brand_block|announcement",
  "headline": "...",
  "body": "...",
  "cta_text": "...",
  "cta_url": "...",
  "image_url": "...",
  "recipient": { ...client fields... } | null
}
// Response:
{ "html": "<html>...</html>" }
```

### POST `/api/email/send`
Starts background email send job.
```json
// Request:
{
  "clients": [...],      // full client objects for merge field resolution
  "template_id": "...",
  "subject": "...",
  "headline": "...",
  "body": "...",
  "cta_text": "...",
  "cta_url": "...",
  "image_url": "...",
  "tenant_id": "...",
  "server": "BETA"
}
// Response:
{ "job_id": "...", "total": 212 }
```

### POST `/api/sms/send`
Starts background SMS send job.
```json
// Request:
{ "messages": [{"client_id":"...","name":"...","mobile":"...","message":"..."}], "tenant_id":"...", "server":"BETA" }
// Response:
{ "job_id": "...", "total": 212 }
```
The Salonid for SMS is taken from `TenantContext.LoadedSalonIds[0]`.  
SMS endpoint: `{server.SmsBase}{smsPath}` (configurable per server via env vars)  
Method: **POST** (query string params: `TokenID`, `ClientId` (uppercase), `Salonid`, `Message`)

### POST `/api/sms/test`
Fires a single SMS and returns full diagnostic info (for debugging). Admin only.

### POST `/api/log`
Lightweight event logging from frontend.

---

## 11. Claude AI Integration

### Model usage
- **Data Analysis:** `claude-sonnet-4-6` (or latest Sonnet)
- **Client Query interpretation:** `claude-haiku-4-5-20251001` (or latest Haiku)

### Analysis prompt construction
The system prompt includes:
1. A description of the AI's role as a salon business analyst
2. Important notes about VAT-inclusive pricing, rebooking rate data, request rate definitions
3. A redirect instruction: when asked to list specific clients, direct to Client Selections feature

The user message includes a structured data summary built from `TenantContext`:
- Salon summary (name, total clients, active/lapsed counts)
- Monthly service revenue table (last 24 months): `Month,Revenue,Visits,NoShows,OnlineBookings,UniqueClients,NewClients,RequestClients,RequestRate%`
- Monthly revenue by salon (last 24 months)
- Monthly revenue by service category (top 15 categories, last 24 months)
- Top retail products, brands, monthly retail
- Team rebooking rates by month (per stylist)
- Team KPI targets
- Booking stats (by status, source, hour of day, day of week)
- Top 100 clients by spend (name, status, days_since, avg_spend, total_spend, visits, top_cats)

Then the format instruction (dashboard/list/report/chart) and the user's question.

### Format instructions (append to user message)
Each format expects a specific JSON structure:
- **dashboard:** `{"title","format":"dashboard","summary","kpis":[{"label","value","trend","detail"}],"sections":[{"title","insight","items":[{"label","value"}]}]}`
- **list:** `{"title","format":"list","summary","columns":[],"rows":[[]]}`
- **report:** `{"title","format":"report","summary","sections":[{"heading","body"}],"conclusion"}`
- **chart:** `{"title","format":"chart","summary","charts":[{"type","title","insight","labels":[],"datasets":[{"label","data":[]}]}]}`

Parse Claude's response as JSON. Use a JSON repair library if initial parse fails. Always use `max_tokens: 4096`.

### Query interpretation (Haiku)
Send the natural language query with the full schema description and operator examples. Claude returns:
```json
{
  "filters": [{"field": "...", "op": "eq|ne|gt|gte|lt|lte|in|contains|contains_exact|not_contains|every_contains|exists", "value": ...}],
  "logic": "AND|OR",
  "description": "Plain English description of the segment"
}
```
Apply filters in C# against the in-memory `AllClients` list. Match Python filter logic exactly (case-insensitive string comparison, array `contains` checks substring of each element, `exists` checks null/not-null).

---

## 12. Email Templates

Port the Python `_build_email_html()` function directly. Four templates: `minimal`, `hero`, `brand_block`, `announcement`.

### Font pairs
```csharp
var FontPairs = new Dictionary<string, FontPair> {
    ["premium"]  = new("https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600;700&display=swap",
                        "'Cormorant Garamond', Georgia, 'Times New Roman', serif",
                        "'Helvetica Neue', Helvetica, Arial, sans-serif", "700", "30px", "15px"),
    ["modern"]   = new("https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&display=swap",
                        "'Poppins', system-ui, sans-serif", "'Poppins', system-ui, sans-serif", "600", "26px", "14px"),
    ["friendly"] = new("https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700&display=swap",
                        "'Nunito', system-ui, sans-serif", "'Nunito', system-ui, sans-serif", "700", "26px", "15px"),
    ["classic"]  = new("https://fonts.googleapis.com/css2?family=Merriweather:wght@400;700&display=swap",
                        "'Merriweather', Georgia, serif", "Georgia, 'Times New Roman', serif", "700", "26px", "15px"),
};
```

### Merge field resolution (with fallbacks)
```csharp
public static string ResolveMerge(string text, ClientRecord client) {
    if (client == null || text == null) return text ?? "";
    var first = (client.Name ?? "").Split(' ').FirstOrDefault() ?? "there";
    return text
        .Replace("{first_name}",  string.IsNullOrEmpty(first)               ? "there"        : first)
        .Replace("{full_name}",   string.IsNullOrEmpty(client.Name)          ? "valued client" : client.Name)
        .Replace("{stylist}",     string.IsNullOrEmpty(client.PrefTm)        ? "our team"     : client.PrefTm)
        .Replace("{salon}",       string.IsNullOrEmpty(client.PrefSalon)     ? "the salon"    : client.PrefSalon)
        .Replace("{last_visit}",  string.IsNullOrEmpty(client.LastVisit)     ? "your last visit" : client.LastVisit)
        .Replace("{days_since}",  client.DaysSince.HasValue                  ? $"{client.DaysSince} days" : "a while")
        .Replace("{overdue}",     client.Overdue.HasValue && client.Overdue > 0 ? $"{client.Overdue} days overdue" : "overdue")
        .Replace("{avg_spend}",   client.AvgSpend.HasValue                   ? client.AvgSpend.ToString() : "");
}
```

### Apple Mail / Outlook Mac background fix
All templates must have:
```html
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
...
<body bgcolor="#ffffff" style="margin:0;padding:0;background-color:#ffffff;">
<table ... bgcolor="#ffffff" style="background:#ffffff;...">
```

### SendHTMLEmail API call
```
POST {server.HtmlEmailBase}
Content-Type: application/json
Body: { "TokenID": "1166554", "Email": "...", "Subject": "...", "bdy": "<html>...</html>" }
```
Check response: status 200 AND `"success"` in response body (case-insensitive).

---

## 13. Frontend Architecture

### Layout (`_Layout.cshtml`)
The main app shell is a single-page-like layout with:
- A fixed 260px left sidebar
- A right column with a 44px slim topbar and scrollable content area
- A sticky follow-up bar at the bottom (analysis page only)

All interactivity is handled by **Alpine.js** components defined in `aria.js`.

### Pages
| Razor Page | Path | Notes |
|---|---|---|
| Login | `/login` | Shows before auth |
| Main Shell | `/` | Requires auth, contains all tabs |
| Admin Logs | `/admin/logs` | Requires admin role |

The main shell is a single Razor Page that renders the sidebar, topbar, and page divs. Tab switching is handled client-side by Alpine.js (showing/hiding divs, not navigating).

### Alpine.js global state
```javascript
// Global variables (not Alpine, just JS module-level)
let selectedServer     = 'BETA';
let selectedTenantId   = null;
let selectedTenantName = null;
let selectedAccountCode = '';
let _selectionResults  = [];
let _selectedIds       = new Set();
let _brandSettings     = null;
let _emailTemplate     = 'minimal';
let _locationsData     = null;
let _historyCache      = {};
```

### Chart.js palette
```javascript
const _chartPalette = [
    '#1A2332','#3A7A50','#C0392B','#5DBF85','#F59E0B',
    '#2D6A4F','#4A9EBF','#F97316','#8B5CF6','#A0AEBC',
    '#06B6D4','#84CC16','#A855F7','#243040','#B0C8B8',
];
```

---

## 14. CSS Design Tokens

Port the CSS variables directly from the Python version:
```css
:root {
  --navy:    #1A2332;
  --green:   #3A7A50;
  --green-d: #2D6A4F;
  --bg:      #ECEEF2;
  --card:    #FFFFFF;
  --border:  #E2E6EC;
  --text:    #3A4A5A;
  --muted:   #A0AEBC;
  --heading: #1A2332;
  --risk:    #C0392B;
}
body { font-family: 'Helvetica Neue', Helvetica, Arial, system-ui, sans-serif; font-size: 13px; }
h1, h2, h3, h4 { font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 700; }
```
Load from Google Fonts: `Cormorant+Garamond:wght@400;600;700|Poppins:wght@400;500;600|Nunito:wght@400;600;700|Merriweather:wght@400;700`

---

## 15. Tenant Selector

Before data loads, show a full-screen selector with:
- Three server toggle buttons: BETA (navy when active) | DEMO (amber #D97706) | LIVE (green)
- Dropdown populated from `/api/tenants?server={server}`
- "Load Data →" button that calls `/api/data` and polls `/api/job/{id}`

Loading overlay shows progress steps matching the `DataBuildService` step messages.

---

## 16. Client Selections — Filter Logic

Port exactly from Python. Supported operators:
```
eq, ne, gt, gte, lt, lte     — numeric/string equality and comparison
in                            — value is in a list
contains                      — array field has element containing substring (case-insensitive)
                               OR string field contains substring
contains_exact               — array element exactly equals value (case-insensitive)
not_contains                 — array/string does NOT contain value
every_contains               — ALL elements in array contain value
exists                       — value: true = not null, false = null
```

Boolean fields (`sms_optout`, `email_optout`, `salonspy_optin`, `has_future_booking`, `points_enabled`): compare as `bool == bool`.

SMS eligibility check (exclude from blast):
```csharp
bool IsSmsEligible(ClientRecord c) =>
    !string.IsNullOrWhiteSpace(c.Mobile) &&
    !c.Mobile.Trim().Equals("REFUSED", StringComparison.OrdinalIgnoreCase) &&
    !c.SmsOptOut;
```

---

## 17. Activity Logging

Log to `activity_log` after every significant event:

| event_type | When | Key fields |
|---|---|---|
| `analyse` | AI analysis completes | question, format, is_followup, result_title, result_summary, response_ms, input_tokens, output_tokens |
| `query` | Client selection query | question, result_count, result_title, response_ms, input_tokens, output_tokens |
| `sms_blast` | SMS send completes | question (summary string), result_count (sent), error (if failures) |
| `email_blast` | Email send completes | question (summary string), result_count (sent), error |
| `session` | Data loads successfully | question (generated timestamp) |
| `salon_selected` | User selects a tenant | question (salon name) |
| `history_delete` | History item deleted | question (type + original question) |

---

## 18. Admin Logs Page

Accessible at `/admin/logs?page=N` (admin role only).
- Shows last 100 events per page from `activity_log`, newest first
- Summary stats: total events, sessions, analyses, queries, avg response time, estimated API cost
- API cost formula: `(input_tokens * 3 + output_tokens * 15) / 1_000_000` USD
- Auto-refreshes every 30 seconds

---

## 19. Deployment (Railway)

### Dockerfile
```dockerfile
FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS base
WORKDIR /app
EXPOSE 8080

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY . .
RUN dotnet publish -c Release -o /app/publish

FROM base AS final
WORKDIR /app
COPY --from=build /app/publish .
ENTRYPOINT ["dotnet", "SalonIQAria.dll"]
```

### railway.toml
```toml
[build]
builder = "DOCKERFILE"

[deploy]
startCommand = "dotnet SalonIQAria.dll"
healthcheckPath = "/health"
```

### Required Railway services
1. **Web service** — the .NET app
2. **PostgreSQL** — Railway managed Postgres
3. **Redis** — Railway managed Redis

### Environment variables to set in Railway
```
DATABASE_URL              postgresql://...
REDIS_URL                 redis://...
ANTHROPIC_API_KEY         sk-ant-...
SMS_TOKEN                 79B57270-8300-40F8-82FE-FFE47EE62A44
EMAIL_TOKEN               1166554
EMAIL_HTML_BASE_URL       (leave blank to use per-server defaults)
ADMIN_USER                (optional env-var login fallback)
ADMIN_PASS
ADMIN_TENANT
ADMIN_SERVER              BETA
SECRET_KEY                (random 32-char string for cookie encryption)
ASPNETCORE_URLS           http://+:8080
```

---

## 20. Known Quirks & Edge Cases

These caused bugs in the Python version and must be handled carefully:

1. **SalonIQ date formats differ by server** — BETA uses `dd/MM/yyyy`, LIVE uses `MM/dd/yyyy`. Always use `ServerConfig.DateFormat` when formatting dates for API calls.

2. **Booking deduplication is critical** — multi-service bookings share the same `BookingId`. Always deduplicate visits/no-shows by `(BookingId, bucket)` — never count a booking ID more than once per time bucket.

3. **`TotalSalesPrice` on bookings** — sums all service line items on the same booking. Do NOT sum all rows with the same BookingId for visit count — only count it once.

4. **Team members who have left** — fetch team members over the full 2-year range, not just current staff. Bookings reference TeamMemberId for historical staff.

5. **`Categoty` field** — this is a typo in the SalonIQ API. The service category field is `Categoty`, not `Category`. Strip the `"HAIR - "` prefix from all values.

6. **Clients with no paid visits** — skip entirely from scoring. Only process clients who have at least one booking with `Status == 2`.

7. **Future bookings exclude from top 500** — clients with a future booking are in `AllClients` but excluded from `AllScored` (used for the top 500 ranked list).

8. **SalonIQ SMS API** — use POST (not GET). Parameters go in the query string (not form body). Endpoint: `{smsBase}{smsPath}`.

9. **SalonIQ Email API** — `SendHTMLEmail` endpoint uses POST with JSON body (`Content-Type: application/json`). Field name is `Email` not `EmailAddress`.

10. **ASP.NET Request Validation** — the old `SendEmail` endpoint rejects HTML in the request body. Always use `SendHTMLEmail` for HTML emails.

11. **`pref_salon` and `pref_tm` can be null** — always use null-coalescing fallbacks when building SMS messages or email merge fields.

12. **Redis connection on Railway** — Railway Redis URLs start with `redis://`, not `rediss://`. StackExchange.Redis handles both but check SSL settings.

13. **Hangfire with Redis on Railway** — use `UseRedisStorage(redisUrl)`. Hangfire will automatically create its own Redis keys.

14. **Claude JSON responses** — Claude occasionally wraps JSON in markdown code fences (` ```json ... ``` `). Always strip these before parsing. Use a JSON repair library for malformed responses.

15. **New client count per month** — track `new_clients_by_month` during client profile building: a client is "new" in the month of their first paid visit in the 2-year window. Merge into `ServiceMonthly` as `NewClients` column.

---

## 21. Feature Checklist

Build in this order:

- [ ] Project setup, auth, login page
- [ ] SalonIQ API service + caching
- [ ] Data build pipeline (DataBuildService + Hangfire job)
- [ ] Tenant selector + data loading UI
- [ ] Client Dashboard (top 500 table with scoring)
- [ ] Client Lookup (search by name)
- [ ] Data Analysis page + Claude integration
- [ ] Client Selections page + filter engine
- [ ] SMS Blast (modal, send, eligibility checks)
- [ ] Email Blast (modal, templates, brand settings, send)
- [ ] History (per account, 90-day rolling)
- [ ] Admin Logs page
- [ ] Mobile-responsive layout
- [ ] Sidebar with recent questions/searches
- [ ] Brand settings modal

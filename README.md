# Hundred Trucks — Live FASTag Tracking Dashboard

A real-time truck tracking system for **Mirchi-Lime** built on FASTag toll data. Tracks enroute trucks across India, shows live routes on a map, sends WhatsApp status reports, and lets clients follow their shipment via a public tracking link.

---

## Features

- **Live Route Map** — shows every toll plaza a truck has crossed, with a START marker (origin) and DESTINATION pin
- **Public Tracking Link** — shareable per-truck URL with HMAC token authentication so clients can track without logging in
- **Hourly FASTag Sync** — automatically fetches toll crossing data from Zoho every hour (6 AM – 11 PM IST)
- **WhatsApp Reports** — sends a daily summary + individual tracking links at 11:05 AM and 7:05 PM IST via Meta WhatsApp API and Zoho WA API
- **City + Pincode Labels** — origin and destination show city name and pincode using postalpincode.in / Nominatim geocoding
- **Neon PostgreSQL** — all trucks, crossings, and trip data stored in the cloud

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python · Flask · Gunicorn |
| Scheduler | APScheduler (cron jobs, IST timezone) |
| Database | Neon PostgreSQL (cloud) |
| Frontend | Vanilla JS · Leaflet.js |
| Hosting | Render (free tier, kept alive by UptimeRobot) |
| Data Source | Zoho Creator API (FASTag crossings, enroute trips) |
| Messaging | Meta WhatsApp API · Zoho WhatsApp API |
| Geocoding | postalpincode.in · OpenStreetMap Nominatim |

---

## Project Structure

```
hundred/
├── wsgi.py               # Flask app + APScheduler jobs + all API routes
├── telenity.py           # Telenity SmartTrail API client (auth, entities, consent, location)
├── sim_location.py       # Driver SIM lifecycle: schema, onboarding, consent sweep, poller, mock
├── hundred_trucks.html   # Main dashboard (live map, truck cards)
├── track.html            # Public tracking page (token-authenticated)
├── requirements.txt      # Python dependencies
└── ping_connected.py     # Utility: mark trucks as connected
```

---

## Environment Variables

Set these on Render (or in a `.env` file for local dev):

| Variable | Description |
|---|---|
| `DATABASE_URL` | Neon PostgreSQL connection string |
| `TOKEN_SECRET` | Secret key for HMAC tracking tokens (any random string) |
| `WA_TO` | Default WhatsApp recipient number (e.g. `+919518146736`) |
| `TELENITY_TRAIL_BASIC` | Basic credential for `smarttrail.telenity.com` login. **Unset ⇒ mock mode** |
| `TELENITY_AGW_BASIC` | Basic credential for the consent APIs on `india-agw.telenity.com`. **Unset ⇒ mock mode** |
| `SIM_MOCK` | Set to `1` to force mock SIM locations even when credentials exist |
| `SIM_PING_HOURS` | Cron hours for the SIM poller (default `6-23`) |
| `SIM_PING_MINUTES` | Minutes within those hours (default `0,30` — every 30 min) |
| `SIM_RESEND_COOLDOWN_HOURS` | Consent-resend cooldown (default `24`; Telenity lock numbers re-sent too often) |
| `PUBLIC_DRIVER_PHONE` | `1` (default) shows Call/WhatsApp driver buttons on public track links; `0` hides them |

---

## Scheduled Jobs (IST)

| Time | Job | Description |
|---|---|---|
| 6 AM – 11 PM, :00 | `ping_enroute` | Fetch FASTag crossings for all live trucks (18×/day) |
| 9 AM, 1 PM, 6 PM | `ping_credit` | Sync credit/balance data |
| 6 AM – 11 PM, :00 & :30 | `ping_sims` | Sweep consents, then fetch driver SIM locations (Telenity refresh every 15 min) |
| 11:05 AM & 7:05 PM | `auto_send_report` | WhatsApp summary + per-truck tracking links |

> **Night pause (12 AM – 6 AM):** FASTag pings are skipped overnight since no one monitors at night. The 6 AM ping catches all overnight crossings. This saves ~25% of Neon PostgreSQL compute usage.

> **UptimeRobot** pings the server every 5 minutes to prevent Render from sleeping, including during the overnight no-ping window.

---

## API Routes

| Endpoint | Description |
|---|---|
| `GET /api/hundred-trucks` | All trucks with latest crossing data |
| `GET /api/hundred-truck-detail?vehicle_no=` | Full crossing history for one truck |
| `GET /api/hundred-plazas` | All toll plazas |
| `GET /api/hundred-routes` | All configured routes |
| `GET /api/track?token=` | Public tracking data (token-authenticated) |
| `GET /api/make-token?vehicle_no=` | Generate a tracking token |
| `GET /api/send-report` | Manually trigger WhatsApp report |
| `GET /api/set-sim?vehicle_no=&msisdn=&driver_name=` | Assign a driver SIM to a truck (pings once immediately) |
| `GET /api/remove-sim?vehicle_no=` | Stop SIM tracking for a truck |
| `GET /api/ping-sims[?vehicle_no=]` | Poll SIM locations now — all active SIMs, or one truck |
| `GET /api/sim-latest` | Newest SIM fix per truck (fleet overview) |
| `GET /api/sim-list` | Driver SIM screen data — status, valid till, resend eligibility |
| `GET /api/resend-consent?vehicle_no=` | Re-send the consent SMS (Jio only, 24-hour cooldown) |
| `GET /api/sweep-consents` | Check pending consents and switch tracking on |
| `GET /api/remove-enroute?vehicle_no=` | Remove a truck from enroute list |

---

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env   # fill in your values

# Run
python wsgi.py
```

Open `http://localhost:5000` for the dashboard.

---

## Deployment

Hosted on **Render** with a single Gunicorn worker:

```
gunicorn wsgi:application --workers 1
```

The single worker is intentional — APScheduler uses a file lock (`/tmp/hundred_scheduler.lock`) to ensure only one process runs scheduled jobs.

---

## Developer Setup

For someone picking up this project for the first time.

### 1. Database

Create a free Neon PostgreSQL database at [neon.tech](https://neon.tech). The connection string goes in `DATABASE_URL`. The app expects these tables to already exist:

- `trucks` — vehicle number, route, connected status
- `toll_crossings` — every FASTag crossing per truck
- `enroute_trips` — active long-distance trips with origin/destination
- `credit_trips` — short-distance trips within Tamil Nadu

The schema for each table is defined in the technical documentation PDF.

The two SIM-tracking tables (`truck_sims`, `sim_pings`) are created automatically on
startup by `sim_location.ensure_sim_tables()` — no manual migration needed.

### 1b. Driver SIM tracking (Telenity)

Position now comes from two independent sources, and the maps draw them differently
so they are never confused:

| Source | Table | Line style | Meaning |
|---|---|---|---|
| FASTag toll crossings | `crossings` | **solid** | Confirmed — a crossing proves the truck was there |
| Driver SIM (Telenity) | `sim_pings` | **dotted** | Leading — where the SIM says it has got to, ahead of confirmation |

The SIM leads and the toll confirms, so where a crossing later covers ground the SIM
already reported, the solid line simply paints over the dotted one.

Wherever a truck's newest SIM fix is more recent than its last toll crossing, that fix
becomes the truck's current position (dashboard, journey map, and public track links).

**It is a lifecycle, not a lookup.** A truck is not tracked the moment a number is
entered. `assign_sim()` registers the driver with Telenity, which texts them; the driver
replies `Y` (or calls the IVR and presses 1); `sweep_consents()` sees the approval and
calls Modify to switch tracking on — Telenity never do this for us. Only then does the
Location API return fixes. The **Driver SIMs** screen shows exactly which stage each
truck is at, and crucially whether the driver or we are the ones holding it up.

**Resend is Jio-only and rate-limited.** Telenity lock a number at the operator for 24
hours if the consent SMS is re-sent too often, so `can_resend()` refuses inside
`SIM_RESEND_COOLDOWN_HOURS` and the bulk action skips anything still in cooldown rather
than firing at it. Airtel, Vi and BSNL have no resend endpoint at all — those drivers
use the IVR line.

**Mock mode.** With the two `TELENITY_*_BASIC` variables unset, `sim_location.py`
generates plausible positions by projecting forward from the truck's last two real toll
crossings (~38 km/h along the last heading, seeded per hour so a trail builds smoothly),
and the consent sweep approves immediately. Rows are tagged `sim_pings.source = 'mock'`,
so at go-live clear them with `DELETE FROM sim_pings WHERE source='mock'`.

**Two errors in Telenity's document, both verified against the live API:** the login call
takes `Authorization: Basic <key>`, not the `Token:` header printed in section 4.1; and
the consent token endpoint requires `Content-Type: application/x-www-form-urlencoded`,
not the `application/json` shown in 4.4.1. Our `customer_id` is at `customer.id` in the
login response, which their sample omits.

**Assigning a SIM.** Open a truck in the dashboard and use the `＋ Add driver SIM` chip.
This is a stopgap: once Zoho carries the driver's number, set `SIM_MANUAL_ENTRY = false`
in `hundred_trucks.html` and have `sync_enroute_trips()` insert rows with `source='zoho'`.

**Before going live**, note that India's DoT/TRAI rules require subscriber consent before
a location lookup. `truck_sims.consent_status` exists for this but nothing enforces it yet.

### 2. Zoho API

The app pulls FASTag data from a **Zoho Creator** application owned by Mirchi-Lime. You need:

- The Zoho report URL for FASTag crossings (`ZOHO_FASTAG_URL` used in `ping_enroute`)
- The Zoho WA API URL for sending WhatsApp messages (`ZOHO_WA_URL` used in `auto_send_report`)

Both URLs are already hardcoded in `wsgi.py` — no credentials needed as they use a public key in the URL. Ask the project owner if these URLs need to be updated.

### 3. How the code is organised

Everything lives in `wsgi.py`. The main flows are:

| Flow | Functions involved |
|---|---|
| FASTag sync | `ping_enroute()` → Zoho API → inserts into `toll_crossings` |
| Dashboard load | `/api/hundred-trucks` → reads `trucks` + `toll_crossings` |
| Tracking link | `/api/make-token` → HMAC token → `/api/track?token=` → `track.html` |
| WhatsApp report | `auto_send_report()` → `make_token()` per truck → Zoho WA API |
| Scheduler | `BackgroundScheduler` starts on app launch, guarded by fcntl file lock |

### 4. Common issues

| Problem | Cause | Fix |
|---|---|---|
| Scheduler not firing | Multiple workers running | Always use `--workers 1` with Gunicorn |
| `/api/send-report` crashes | `TOKEN_SECRET` not set | Add it to Render environment variables |
| WhatsApp not sending | `WA_TO` not set | Add recipient number to Render env vars |
| Neon DB sleeping | Free tier pauses after inactivity | UptimeRobot keeps Render alive; Neon wakes on first query |

---

## License

Private — Mirchi-Lime internal use only.

"""
Driver SIM location tracking (Telenity LBS)

Shared by wsgi.py (production) and server.py (local dev).
Runs in mock mode until TELENITY_URL + TELENITY_KEY are set.
"""
import os, json, math, time, random, hashlib, datetime, urllib.request

TELENITY_URL = os.environ.get('TELENITY_URL', '')
TELENITY_KEY = os.environ.get('TELENITY_KEY', '')
SIM_MOCK     = os.environ.get('SIM_MOCK', '')
MOCK_MODE    = bool(SIM_MOCK) or not (TELENITY_URL and TELENITY_KEY)

# Mock movement model — highway average, capped at a long day's drive so a stale
# toll reading can't fling the truck across the country
MOCK_SPEED_KMH  = 38
MOCK_MAX_KM     = 400

def ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

def normalize_msisdn(raw):
    """'+91 98765-43210' -> '919876543210'. Returns None if not a plausible Indian mobile."""
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    if len(digits) == 10:
        digits = '91' + digits
    if len(digits) == 12 and digits.startswith('91'):
        return digits
    return digits if 10 <= len(digits) <= 15 else None

# ── Schema ──

def ensure_sim_tables(con):
    """Create the SIM tables. Takes an open connection, commits, caller closes."""
    cur = con.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS truck_sims (
            id             SERIAL PRIMARY KEY,
            vehicle_no     TEXT NOT NULL,
            msisdn         TEXT NOT NULL,
            driver_name    TEXT,
            source         TEXT DEFAULT 'manual',
            is_active      BOOLEAN DEFAULT TRUE,
            consent_status TEXT DEFAULT 'pending',
            assigned_at    TIMESTAMP DEFAULT NOW(),
            removed_at     TIMESTAMP
        )
    ''')
    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_truck_sims_one_active ON truck_sims (vehicle_no) WHERE is_active')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS sim_pings (
            id         SERIAL PRIMARY KEY,
            vehicle_no TEXT NOT NULL,
            msisdn     TEXT NOT NULL,
            lat        DOUBLE PRECISION NOT NULL,
            lng        DOUBLE PRECISION NOT NULL,
            accuracy_m INTEGER,
            source     TEXT DEFAULT 'mock',
            pinged_at  TIMESTAMP NOT NULL
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sim_pings_vno_time ON sim_pings (vehicle_no, pinged_at)')
    con.commit()

# ── Public seam ──

def fetch_sim_location(msisdn, recent_crossings=None):
    """
    Current handset location for a driver SIM.
    recent_crossings: up to 2 most recent crossings, oldest first,
                      each {'lat','lng','crossed_at'} — mock context only.
    Returns {'lat','lng','accuracy_m','source'} or None.
    """
    if MOCK_MODE:
        return _mock_location(msisdn, recent_crossings or [])
    return _fetch_telenity(msisdn)

def _fetch_telenity(msisdn):
    # TODO: confirm request/response shape against Telenity docs — this is coded
    # against the usual LBS pattern (MSISDN in, cell-tower fix + accuracy out)
    # and is the ONLY function that should need rewriting once they arrive.
    try:
        body = json.dumps({'msisdn': msisdn}).encode()
        req  = urllib.request.Request(TELENITY_URL, data=body, headers={
            'Content-Type':  'application/json',
            'Authorization': f'Bearer {TELENITY_KEY}',
        })
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        node = data.get('result', data) or {}
        lat  = node.get('latitude',  node.get('lat'))
        lng  = node.get('longitude', node.get('lng'))
        if lat is None or lng is None:
            print(f'[telenity] {msisdn} — no fix in response', flush=True)
            return None
        return {
            'lat': float(lat), 'lng': float(lng),
            'accuracy_m': int(node.get('accuracy', node.get('accuracy_m', 0)) or 0),
            'source': 'telenity',
        }
    except Exception as e:
        print(f'[telenity] {msisdn} — ERROR: {e}', flush=True)
        return None

# ── Mock ──

def _to_dt(v):
    if isinstance(v, datetime.datetime):
        return v
    s = str(v or '').strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.datetime.strptime(s[:19], fmt)
        except ValueError:
            pass
    return None

def _project(lat1, lng1, lat2, lng2, dist_km):
    """Continue past point 2 along the heading of leg 1->2 for dist_km."""
    coslat = max(math.cos(math.radians(lat2)), 1e-6)
    dx = (lng2 - lng1) * 111.32 * coslat
    dy = (lat2 - lat1) * 111.32
    norm = math.hypot(dx, dy)
    if norm < 0.001:
        return lat2, lng2
    return (lat2 + (dy / norm) * dist_km / 111.32,
            lng2 + (dx / norm) * dist_km / (111.32 * coslat))

def _mock_location(msisdn, crossings):
    """
    Plausible fix derived from the truck's last toll crossings, seeded per hour so
    repeat calls within an hour agree and successive hours advance smoothly.
    """
    pts = [c for c in crossings if c.get('lat') and c.get('lng')]
    if not pts:
        return None

    now = ist_now()
    rnd = random.Random(int(hashlib.md5(f'{msisdn}{now:%Y%m%d%H}'.encode()).hexdigest()[:12], 16))
    last = pts[-1]

    if len(pts) < 2:
        lat = last['lat'] + rnd.uniform(-0.02, 0.02)
        lng = last['lng'] + rnd.uniform(-0.02, 0.02)
    else:
        prev  = pts[-2]
        since = _to_dt(last.get('crossed_at'))
        hours = max(0.0, (now - since).total_seconds() / 3600) if since else 1.0
        dist  = min(hours * MOCK_SPEED_KMH, MOCK_MAX_KM)
        lat, lng = _project(prev['lat'], prev['lng'], last['lat'], last['lng'], dist)
        lat += rnd.uniform(-0.01, 0.01)
        lng += rnd.uniform(-0.01, 0.01)

    return {'lat': round(lat, 6), 'lng': round(lng, 6),
            'accuracy_m': rnd.randint(300, 1800), 'source': 'mock'}

# ── Poll one SIM and store the result ──

def record_ping(con, vehicle_no, msisdn):
    """Fetch and INSERT one sim_pings row. Returns the ping dict, or None if no fix."""
    cur = con.cursor()
    cur.execute('''SELECT lat, lng, crossed_at FROM crossings
                   WHERE vehicle_no=%s AND lat IS NOT NULL AND lat != 0
                   ORDER BY crossed_at DESC LIMIT 2''', (vehicle_no,))
    recent = [{'lat': r[0], 'lng': r[1], 'crossed_at': r[2]} for r in cur.fetchall()][::-1]

    fix = fetch_sim_location(msisdn, recent)
    if not fix:
        return None
    cur.execute('''INSERT INTO sim_pings (vehicle_no, msisdn, lat, lng, accuracy_m, source, pinged_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                (vehicle_no, msisdn, fix['lat'], fix['lng'], fix['accuracy_m'],
                 fix['source'], ist_now().strftime('%Y-%m-%d %H:%M:%S')))
    con.commit()
    return fix

def active_sims(con, vehicle_no=None):
    """Active SIMs for connected trucks, or for one truck. -> [(vehicle_no, msisdn), ...]"""
    cur = con.cursor()
    if vehicle_no:
        cur.execute('SELECT vehicle_no, msisdn FROM truck_sims WHERE vehicle_no=%s AND is_active', (vehicle_no,))
    else:
        cur.execute('''SELECT s.vehicle_no, s.msisdn FROM truck_sims s
                       JOIN trucks t ON t.vehicle_no = s.vehicle_no AND t.is_connected = 1
                       WHERE s.is_active''')
    return cur.fetchall()

def poll_sims(get_db, vehicle_no=None, delay=2, release=None):
    """
    Ping every active SIM (or just one truck).
    get_db  — the caller's connection factory
    release — how to hand the connection back (server.py pools, wsgi.py closes)
    """
    release = release or (lambda c: c.close())
    con  = get_db()
    sims = active_sims(con, vehicle_no)
    release(con)

    mode = 'MOCK' if MOCK_MODE else 'telenity'
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping SIMs ({mode}): {len(sims)} active...', flush=True)
    done = 0
    for (vno, msisdn) in sims:
        try:
            con = get_db()
            fix = record_ping(con, vno, msisdn)
            release(con)
            if fix:
                done += 1
                print(f'  ✓ {vno} — {fix["lat"]:.4f},{fix["lng"]:.4f} ±{fix["accuracy_m"]}m', flush=True)
            else:
                print(f'  — {vno} — no fix', flush=True)
        except Exception as e:
            print(f'  ✗ {vno} — ERROR: {e}', flush=True)
        if delay and len(sims) > 1:
            time.sleep(delay)
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping SIMs complete — {done}/{len(sims)}.', flush=True)
    return done

# ── Read helpers for the API layer ──

def get_sim(con, vehicle_no):
    cur = con.cursor()
    cur.execute('SELECT msisdn, driver_name FROM truck_sims WHERE vehicle_no=%s AND is_active', (vehicle_no,))
    row = cur.fetchone()
    return {'msisdn': row[0], 'driver_name': row[1]} if row else None

def get_sim_pings(con, vehicle_no, days=10):
    cur = con.cursor()
    cur.execute(f'''SELECT lat, lng, accuracy_m, source, pinged_at FROM sim_pings
                    WHERE vehicle_no=%s AND pinged_at >= NOW() - INTERVAL '{int(days)} days'
                    ORDER BY pinged_at''', (vehicle_no,))
    return [{'lat': r[0], 'lng': r[1], 'accuracy_m': r[2], 'source': r[3],
             'pinged_at': str(r[4])} for r in cur.fetchall()]

def latest_pings(con):
    """Newest ping per truck — one row each, for the fleet overview."""
    cur = con.cursor()
    cur.execute('''SELECT DISTINCT ON (vehicle_no) vehicle_no, lat, lng, accuracy_m, source, pinged_at
                   FROM sim_pings ORDER BY vehicle_no, pinged_at DESC''')
    return [{'vehicle_no': r[0], 'lat': r[1], 'lng': r[2], 'accuracy_m': r[3],
             'source': r[4], 'pinged_at': str(r[5])} for r in cur.fetchall()]

def set_sim(con, vehicle_no, msisdn, driver_name='', source='manual'):
    """Deactivate any current SIM, assign a new one."""
    cur = con.cursor()
    cur.execute('UPDATE truck_sims SET is_active=FALSE, removed_at=NOW() WHERE vehicle_no=%s AND is_active', (vehicle_no,))
    cur.execute('''INSERT INTO truck_sims (vehicle_no, msisdn, driver_name, source, is_active)
                   VALUES (%s,%s,%s,%s,TRUE)''', (vehicle_no, msisdn, driver_name or None, source))
    con.commit()

def remove_sim(con, vehicle_no):
    cur = con.cursor()
    cur.execute('UPDATE truck_sims SET is_active=FALSE, removed_at=NOW() WHERE vehicle_no=%s AND is_active', (vehicle_no,))
    n = cur.rowcount
    con.commit()
    return n

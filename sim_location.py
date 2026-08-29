"""
Driver SIM tracking — our side of it.

Telenity's API lives in telenity.py; this holds the database, the onboarding and
consent lifecycle, the poller, and a mock so the whole feature works without
credentials.

The lifecycle, which is why this is more than a lookup:
    assign_sim()      registers the driver, which is what sends the consent SMS
    sweep_consents()  watches for the reply and switches tracking on
    poll_sims()       stores positions once tracking is live
    release_sim()     stops tracking and frees the licence
"""
import os, json, math, time, random, hashlib, datetime

import telenity

SIM_MOCK  = os.environ.get('SIM_MOCK', '')
MOCK_MODE = bool(SIM_MOCK) or not telenity.CONFIGURED

# Telenity lock a number for 24 hours if the consent SMS is re-sent repeatedly,
# so the resend button has to refuse until this has passed.
RESEND_COOLDOWN_HOURS = int(os.environ.get('SIM_RESEND_COOLDOWN_HOURS', '24'))

# Mock movement model — highway average, capped at a long day's drive so a stale
# toll reading can't fling the truck across the country
MOCK_SPEED_KMH = 38
MOCK_MAX_KM    = 400

def ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

def normalize_msisdn(raw):
    """'+91 98765-43210' -> '919876543210'. None if not a plausible Indian mobile."""
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    if len(digits) == 10:
        digits = '91' + digits
    if len(digits) == 12 and digits.startswith('91'):
        return digits
    return digits if 10 <= len(digits) <= 15 else None

# ── schema ───────────────────────────────────────────────────────────────────

def ensure_sim_tables(con):
    """Create or migrate the SIM tables. Takes an open connection, commits."""
    cur = con.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS truck_sims (
            id             SERIAL PRIMARY KEY,
            vehicle_no     TEXT NOT NULL,
            msisdn         TEXT NOT NULL,
            driver_name    TEXT,
            source         TEXT DEFAULT 'manual',
            is_active      BOOLEAN DEFAULT TRUE,
            consent_status TEXT DEFAULT 'PENDING',
            assigned_at    TIMESTAMP DEFAULT NOW(),
            removed_at     TIMESTAMP
        )
    ''')
    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_truck_sims_one_active ON truck_sims (vehicle_no) WHERE is_active')
    for ddl in [
        "ALTER TABLE truck_sims ADD COLUMN IF NOT EXISTS entity_id BIGINT",
        "ALTER TABLE truck_sims ADD COLUMN IF NOT EXISTS operator TEXT",
        "ALTER TABLE truck_sims ADD COLUMN IF NOT EXISTS tracking_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE truck_sims ADD COLUMN IF NOT EXISTS consent_checked_at TIMESTAMP",
        "ALTER TABLE truck_sims ADD COLUMN IF NOT EXISTS consent_expires_on TEXT",
        "ALTER TABLE truck_sims ADD COLUMN IF NOT EXISTS last_consent_sent_at TIMESTAMP",
        "ALTER TABLE truck_sims ADD COLUMN IF NOT EXISTS last_error TEXT",
    ]:
        cur.execute(ddl)

    cur.execute('''
        CREATE TABLE IF NOT EXISTS sim_pings (
            id         SERIAL PRIMARY KEY,
            vehicle_no TEXT NOT NULL,
            msisdn     TEXT NOT NULL,
            lat        DOUBLE PRECISION NOT NULL,
            lng        DOUBLE PRECISION NOT NULL,
            source     TEXT DEFAULT 'mock',
            pinged_at  TIMESTAMP NOT NULL
        )
    ''')
    for ddl in [
        "ALTER TABLE sim_pings ADD COLUMN IF NOT EXISTS detailed_address TEXT",
        "ALTER TABLE sim_pings ADD COLUMN IF NOT EXISTS result_status INTEGER",
        "ALTER TABLE sim_pings ADD COLUMN IF NOT EXISTS speed_kmh DOUBLE PRECISION",
        "ALTER TABLE sim_pings ADD COLUMN IF NOT EXISTS distance_km DOUBLE PRECISION",
        # accuracy_m never existed in Telenity's API — it was my invention, so it goes
        "ALTER TABLE sim_pings DROP COLUMN IF EXISTS accuracy_m",
    ]:
        cur.execute(ddl)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sim_pings_vno_time ON sim_pings (vehicle_no, pinged_at)')
    con.commit()

# ── state shown on the driver SIM screen ─────────────────────────────────────

STATE_LABELS = {
    'tracking':  'Tracking',
    'enabling':  'Consented — enabling tracking',
    'awaiting':  'Awaiting driver consent',
    'expired':   'Consent expired',
    'hold':      'Licence on hold',
    'problem':   'Needs attention',
}

def sim_state(row):
    """
    One word for what is happening, and crucially who has to act.
    'awaiting' means chase the driver; 'enabling' means we have not caught up yet.
    """
    status = (row.get('consent_status') or '').upper()
    if row.get('tracking_enabled'):
        return 'tracking'
    if status == 'LICENSE_HOLD':
        return 'hold'
    if status in ('EXPIRED', 'DENIED'):
        return 'expired'
    if telenity.consent_allowed(status):
        return 'enabling'
    if status in ('PENDING', '', 'UNKNOWN'):
        return 'awaiting'
    return 'problem'

def can_resend(row, now=None):
    """
    Resend is Jio-only and rate-limited. Returns (allowed, reason).
    Reason is shown in the button's tooltip when it is disabled.
    """
    if sim_state(row) != 'awaiting':
        return False, 'Only for drivers who have not consented yet'
    sent = row.get('last_consent_sent_at')
    if sent:
        # written with ist_now(), so it must be compared with ist_now() — the database
        # server's clock is not necessarily the same zone
        now = now or ist_now()
        if isinstance(sent, str):
            try:
                sent = datetime.datetime.strptime(str(sent)[:19], '%Y-%m-%d %H:%M:%S')
            except ValueError:
                sent = None
        if sent:
            hours = (now - sent).total_seconds() / 3600
            if hours < RESEND_COOLDOWN_HOURS:
                left = RESEND_COOLDOWN_HOURS - hours
                return False, f'Telenity lock numbers that are re-sent too often — available in {left:.0f} h'
    return True, ''

# ── onboarding ───────────────────────────────────────────────────────────────

def assign_sim(con, vehicle_no, msisdn, driver_name='', source='manual'):
    """
    Register a driver against a truck. In live mode this calls Telenity, which
    sends the driver a consent SMS and consumes a licence.
    Returns {'entity_id','tracking','consent'} or raises.
    """
    entity_id, tracking, consent = None, False, 'PENDING'
    if not MOCK_MODE:
        left = telenity.licence().get('left')
        if left is not None and left <= 0:
            raise telenity.TelenityError('No Telenity licences left — release one first')
        # Telenity reject a single-character/punctuation lastName with a generic
        # "Unknown Error : BadRequestException" — confirmed by testing "." against
        # their live API. A real word, even a filler one, is required.
        name = (driver_name or 'Driver').split()
        res = telenity.import_entity(msisdn, name[0], ' '.join(name[1:]) or 'Last')
        entity_id, tracking = res['entity_id'], res['is_tracked']
        # a driver who consented by IVR beforehand comes back already tracked
        consent = 'ALLOWED' if tracking else 'PENDING'

    cur = con.cursor()
    cur.execute('UPDATE truck_sims SET is_active=FALSE, removed_at=NOW() WHERE vehicle_no=%s AND is_active',
                (vehicle_no,))
    cur.execute('''INSERT INTO truck_sims
                   (vehicle_no, msisdn, driver_name, source, is_active, consent_status,
                    entity_id, tracking_enabled, last_consent_sent_at)
                   VALUES (%s,%s,%s,%s,TRUE,%s,%s,%s,%s)''',
                (vehicle_no, msisdn, driver_name or None, source, consent, entity_id, tracking,
                 ist_now().strftime('%Y-%m-%d %H:%M:%S')))
    con.commit()
    return {'entity_id': entity_id, 'tracking': tracking, 'consent': consent}

def release_sim(con, vehicle_no):
    """Stop tracking, remove the driver at Telenity, free the licence."""
    cur = con.cursor()
    cur.execute('SELECT msisdn, entity_id FROM truck_sims WHERE vehicle_no=%s AND is_active', (vehicle_no,))
    row = cur.fetchone()
    if not row:
        return 0
    msisdn, entity_id = row
    if not MOCK_MODE:
        try:
            if entity_id:
                telenity.set_tracking(entity_id, tracked=False)
            telenity.delete_entity(msisdn)
        except telenity.TelenityError as e:
            print(f'  ! release {vehicle_no}: {e}', flush=True)
    cur.execute('UPDATE truck_sims SET is_active=FALSE, removed_at=NOW(), tracking_enabled=FALSE '
                'WHERE vehicle_no=%s AND is_active', (vehicle_no,))
    n = cur.rowcount
    con.commit()
    return n

def resend_consent(con, vehicle_no):
    """Re-send the consent SMS, refusing inside the cooldown. Jio only."""
    cur = con.cursor()
    cur.execute('''SELECT msisdn, consent_status, tracking_enabled, last_consent_sent_at
                   FROM truck_sims WHERE vehicle_no=%s AND is_active''', (vehicle_no,))
    row = cur.fetchone()
    if not row:
        return {'ok': False, 'message': 'No active SIM for this truck'}
    rec = {'msisdn': row[0], 'consent_status': row[1],
           'tracking_enabled': row[2], 'last_consent_sent_at': row[3]}
    allowed, reason = can_resend(rec)
    if not allowed:
        return {'ok': False, 'message': reason}
    if MOCK_MODE:
        result = {'ok': True, 'message': 'Consent SMS re-sent (mock)'}
    else:
        result = telenity.jio_reinitiate(rec['msisdn'])
    if result['ok']:
        cur.execute('UPDATE truck_sims SET last_consent_sent_at=%s WHERE vehicle_no=%s AND is_active',
                    (ist_now().strftime('%Y-%m-%d %H:%M:%S'), vehicle_no))
        con.commit()
    return result

# ── consent sweep ────────────────────────────────────────────────────────────

def sweep_consents(get_db, release=None, vehicle_no=None):
    """
    Check drivers who have not started tracking, and switch tracking on the moment
    consent lands. Without this nothing is ever tracked — Telenity never enable it
    for us. Returns how many became live.
    """
    release = release or (lambda c: c.close())
    con = get_db(); cur = con.cursor()
    sql = '''SELECT vehicle_no, msisdn, entity_id, consent_status FROM truck_sims
             WHERE is_active AND NOT COALESCE(tracking_enabled, FALSE)'''
    params = ()
    if vehicle_no:
        sql += ' AND vehicle_no=%s'; params = (vehicle_no,)
    cur.execute(sql, params)
    rows = cur.fetchall(); release(con)

    started = 0
    for vno, msisdn, entity_id, _ in rows:
        try:
            if MOCK_MODE:
                info = {'status': 'ALLOWED', 'expires_on': None}
            else:
                info = telenity.consent_status(msisdn)

            enabled, err = False, None
            if telenity.consent_allowed(info['status']):
                if MOCK_MODE:
                    enabled = True
                else:
                    if not entity_id:
                        found = telenity.entity_search(msisdn)
                        entity_id = found.get('id') if found else None
                    if entity_id:
                        res = telenity.set_tracking(entity_id, tracked=True)
                        enabled, err = res['ok'], res.get('reason')
                    else:
                        err = 'no entity id — was the number imported?'

            con = get_db(); cur = con.cursor()
            cur.execute('''UPDATE truck_sims
                           SET consent_status=%s, consent_expires_on=%s, consent_checked_at=NOW(),
                               tracking_enabled=%s, entity_id=COALESCE(%s, entity_id), last_error=%s
                           WHERE vehicle_no=%s AND is_active''',
                        (info['status'], info.get('expires_on'), enabled, entity_id, err, vno))
            con.commit(); release(con)
            if enabled:
                started += 1
                print(f'  ✓ {vno} consent {info["status"]} — tracking enabled', flush=True)
        except Exception as e:
            print(f'  ✗ {vno} consent check failed: {e}', flush=True)
    return started

# ── polling ──────────────────────────────────────────────────────────────────

def poll_sims(get_db, vehicle_no=None, release=None, delay=0):
    """
    Store a position for every tracked driver. One bulk call covers the fleet;
    only successful fixes are stored, failures are recorded as a readable reason.
    """
    release = release or (lambda c: c.close())
    con = get_db(); cur = con.cursor()
    sql = '''SELECT s.vehicle_no, s.msisdn FROM truck_sims s
             JOIN trucks t ON t.vehicle_no = s.vehicle_no AND t.is_connected = 1
             WHERE s.is_active'''
    params = ()
    if vehicle_no:
        sql += ' AND s.vehicle_no=%s'; params = (vehicle_no,)
    cur.execute(sql, params)
    sims = cur.fetchall(); release(con)
    if not sims:
        return 0
    by_msisdn = {m: v for v, m in sims}

    mode = 'MOCK' if MOCK_MODE else 'telenity'
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping SIMs ({mode}): {len(sims)} active...', flush=True)

    fixes = []
    if MOCK_MODE:
        for vno, msisdn in sims:
            con = get_db()
            fix = _mock_fix(con, vno, msisdn)
            release(con)
            if fix:
                fixes.append(fix)
    else:
        try:
            for fix in telenity.bulk_locations(limit=200):
                if fix['msisdn'] in by_msisdn:
                    fix['vehicle_no'] = by_msisdn[fix['msisdn']]
                    fixes.append(fix)
        except telenity.TelenityError as e:
            print(f'  ✗ bulk location failed: {e}', flush=True)
            return 0

    stored = 0
    con = get_db(); cur = con.cursor()
    for fix in fixes:
        vno = fix['vehicle_no']
        if fix.get('retrieved') and fix.get('lat') and fix.get('lng'):
            cur.execute('''INSERT INTO sim_pings
                           (vehicle_no, msisdn, lat, lng, detailed_address, result_status,
                            speed_kmh, distance_km, source, pinged_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (vno, fix['msisdn'], fix['lat'], fix['lng'], fix.get('address'),
                         fix.get('status'), fix.get('speed_kmh'), fix.get('distance_km'),
                         fix.get('source', 'mock' if MOCK_MODE else 'telenity'),
                         ist_now().strftime('%Y-%m-%d %H:%M:%S')))
            cur.execute('UPDATE truck_sims SET last_error=NULL WHERE vehicle_no=%s AND is_active', (vno,))
            stored += 1
            print(f'  ✓ {vno} — {fix["lat"]:.4f},{fix["lng"]:.4f} {fix.get("address") or ""}', flush=True)
        else:
            reason = fix.get('status_text') or 'no fix'
            cur.execute('UPDATE truck_sims SET last_error=%s WHERE vehicle_no=%s AND is_active', (reason, vno))
            print(f'  — {vno} — {reason}', flush=True)
    con.commit(); release(con)
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping SIMs complete — {stored}/{len(sims)}.', flush=True)
    return stored

# ── mock ─────────────────────────────────────────────────────────────────────

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

def _mock_fix(con, vehicle_no, msisdn):
    """
    Plausible position derived from the truck's last toll crossings, seeded per hour
    so repeat calls within an hour agree and successive hours advance smoothly.
    """
    cur = con.cursor()
    cur.execute('''SELECT lat, lng, crossed_at FROM crossings
                   WHERE vehicle_no=%s AND lat IS NOT NULL AND lat != 0
                   ORDER BY crossed_at DESC LIMIT 2''', (vehicle_no,))
    pts = [{'lat': r[0], 'lng': r[1], 'crossed_at': r[2]} for r in cur.fetchall()][::-1]
    if not pts:
        return None

    now = ist_now()
    rnd = random.Random(int(hashlib.md5(f'{msisdn}{now:%Y%m%d%H}'.encode()).hexdigest()[:12], 16))
    last = pts[-1]
    if len(pts) < 2:
        lat = last['lat'] + rnd.uniform(-0.02, 0.02)
        lng = last['lng'] + rnd.uniform(-0.02, 0.02)
    else:
        prev, since = pts[-2], _to_dt(last.get('crossed_at'))
        hours = max(0.0, (now - since).total_seconds() / 3600) if since else 1.0
        dist = min(hours * MOCK_SPEED_KMH, MOCK_MAX_KM)
        lat, lng = _project(prev['lat'], prev['lng'], last['lat'], last['lng'], dist)
        lat += rnd.uniform(-0.01, 0.01)
        lng += rnd.uniform(-0.01, 0.01)

    return {'vehicle_no': vehicle_no, 'msisdn': msisdn, 'retrieved': True,
            'lat': round(lat, 6), 'lng': round(lng, 6),
            'address': None, 'status': 0, 'status_text': 'Success',
            'speed_kmh': None, 'distance_km': None, 'source': 'mock'}

# ── reads for the API layer ──────────────────────────────────────────────────

def get_sim(con, vehicle_no):
    cur = con.cursor()
    cur.execute('''SELECT msisdn, driver_name, consent_status, tracking_enabled,
                          consent_expires_on, last_error
                   FROM truck_sims WHERE vehicle_no=%s AND is_active''', (vehicle_no,))
    r = cur.fetchone()
    if not r:
        return None
    row = {'msisdn': r[0], 'driver_name': r[1], 'consent_status': r[2],
           'tracking_enabled': r[3], 'valid_till': r[4], 'last_error': r[5]}
    row['state'] = sim_state(row)
    row['state_label'] = STATE_LABELS[row['state']]
    return row

def list_sims(con):
    """Everything the driver SIM screen needs, one row per truck."""
    cur = con.cursor()
    cur.execute('''SELECT s.vehicle_no, s.msisdn, s.driver_name, s.consent_status,
                          s.tracking_enabled, s.consent_expires_on, s.last_error,
                          s.last_consent_sent_at, s.assigned_at, t.owner
                   FROM truck_sims s
                   LEFT JOIN trucks t ON t.vehicle_no = s.vehicle_no
                   WHERE s.is_active ORDER BY s.vehicle_no''')
    out = []
    for r in cur.fetchall():
        row = {'vehicle_no': r[0], 'msisdn': r[1], 'driver_name': r[2],
               'consent_status': r[3], 'tracking_enabled': r[4],
               'valid_till': r[5], 'last_error': r[6],
               'last_consent_sent_at': r[7], 'assigned_at': str(r[8])[:16] if r[8] else None,
               'owner': r[9]}
        row['state'] = sim_state(row)
        row['state_label'] = STATE_LABELS[row['state']]
        allowed, reason = can_resend(row)
        row['can_resend'] = allowed
        row['resend_reason'] = reason
        row.pop('last_consent_sent_at', None)
        out.append(row)
    return out

def get_sim_pings(con, vehicle_no, days=10):
    cur = con.cursor()
    cur.execute(f'''SELECT lat, lng, detailed_address, source, pinged_at, speed_kmh
                    FROM sim_pings
                    WHERE vehicle_no=%s AND pinged_at >= NOW() - INTERVAL '{int(days)} days'
                    ORDER BY pinged_at''', (vehicle_no,))
    return [{'lat': r[0], 'lng': r[1], 'address': r[2], 'source': r[3],
             'pinged_at': str(r[4]), 'speed_kmh': r[5]} for r in cur.fetchall()]

def latest_pings(con):
    cur = con.cursor()
    cur.execute('''SELECT DISTINCT ON (vehicle_no) vehicle_no, lat, lng, detailed_address,
                          source, pinged_at
                   FROM sim_pings ORDER BY vehicle_no, pinged_at DESC''')
    return [{'vehicle_no': r[0], 'lat': r[1], 'lng': r[2], 'address': r[3],
             'source': r[4], 'pinged_at': str(r[5])} for r in cur.fetchall()]

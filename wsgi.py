"""
Render deployment entry point for Toll-Trace.cargo
Clean Flask app with no dependency on server.py
"""
import os, json, time, urllib.request, hmac, hashlib, math, datetime
import psycopg2, psycopg2.extras
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, request, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
import sim_location, telenity

os.chdir(os.path.dirname(os.path.abspath(__file__)))

ZOHO_URL     = 'https://www.zohoapis.in/creator/custom/mirchi-lime/Fetch_Fast_Tag_Data?publickey=m2H9YHHA901WRUOAAFRY04WzD'
NEON_URL     = os.environ.get('DATABASE_URL')
TOKEN_SECRET = os.environ.get('TOKEN_SECRET')
WA_TOKEN     = os.environ.get('WA_TOKEN')
WA_PHONE_ID  = os.environ.get('WA_PHONE_ID')
WA_TO        = os.environ.get('WA_TO')
SIM_PING_HOURS       = os.environ.get('SIM_PING_HOURS', '6-23')      # hours the SIM poller runs
SIM_PING_MINUTES     = os.environ.get('SIM_PING_MINUTES', '0,30')    # every 30 min within those hours
PUBLIC_DRIVER_PHONE  = os.environ.get('PUBLIC_DRIVER_PHONE', '1')    # '0' hides driver number on public track links

def make_token(vehicle_no):
    return hmac.new(TOKEN_SECRET.encode(), vehicle_no.encode(), hashlib.sha256).hexdigest()[:16]

def vehicle_from_token(token):
    con = get_db(); cur = con.cursor()
    cur.execute('SELECT vehicle_no FROM trucks', ())
    rows = cur.fetchall(); con.close()
    for (vno,) in rows:
        if make_token(vno) == token:
            return vno
    return None

application = Flask(__name__, static_folder='.', static_url_path='')

def get_db():
    con = psycopg2.connect(NEON_URL)
    return con

def fetchall(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

def fetchone(cur):
    cols = [d[0] for d in cur.description]
    row  = cur.fetchone()
    return dict(zip(cols, row)) if row else None

# ── Static files ──
DASHBOARD_KEY = os.environ.get('DASHBOARD_KEY', 'ml2026secure')

def not_found_page():
    html = '''<!DOCTYPE html><html><head><title>404 Not Found</title>
    <style>body{margin:0;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;background:#f5f7fa;text-align:center;}
    </style></head><body>
    <div><div style="font-size:80px;font-weight:900;color:#e0e0e0;">404</div>
    <div style="font-size:22px;font-weight:700;color:#333;margin-top:8px;">Page Not Found</div>
    <div style="font-size:14px;color:#888;margin-top:10px;">This page does not exist.</div></div>
    </body></html>'''
    return html, 404

@application.route('/')
def index():
    key = request.args.get('key','')
    if key != DASHBOARD_KEY:
        return not_found_page()
    return send_from_directory('.', 'hundred_trucks.html')

@application.route('/<path:filename>')
def static_files(filename):
    if filename.startswith('api/'):
        return not_found_page()
    # track.html is public, everything else requires key
    if filename == 'track.html':
        return send_from_directory('.', filename)
    key = request.args.get('key','')
    if key != DASHBOARD_KEY:
        return not_found_page()
    return send_from_directory('.', filename)

# ── API routes ──

@application.route('/api/hundred-trucks')
def hundred_trucks():
    con = get_db(); cur = con.cursor()
    cur.execute('''
        SELECT t.vehicle_no, t.owner, t.state, t.is_connected, t.fetched_at, t.is_manual,
               COUNT(DISTINCT c.plaza) AS unique_plazas,
               COUNT(c.id)             AS crossing_count,
               EXISTS(SELECT 1 FROM truck_sims s WHERE s.vehicle_no = t.vehicle_no AND s.is_active) AS has_sim
        FROM trucks t
        LEFT JOIN crossings c ON c.vehicle_no = t.vehicle_no
        GROUP BY t.vehicle_no, t.owner, t.state, t.is_connected, t.fetched_at, t.is_manual
        ORDER BY t.state, t.vehicle_no
    ''')
    rows = fetchall(cur)
    con.close()
    return jsonify({'trucks': rows, 'total_crossings': 0, 'total_plazas': 0})

@application.route('/api/hundred-truck-detail')
def hundred_truck_detail():
    vno = request.args.get('vehicle_no', '').strip().upper()
    con = get_db(); cur = con.cursor()
    cur.execute('SELECT * FROM trucks WHERE vehicle_no=%s', (vno,))
    truck = fetchone(cur) or {}
    cur.execute('SELECT plaza, lat, lng, direction, crossed_at FROM crossings WHERE vehicle_no=%s ORDER BY crossed_at', (vno,))
    crossings = fetchall(cur)
    cur.execute('SELECT plaza, lat, lng, COUNT(*) as count FROM crossings WHERE vehicle_no=%s GROUP BY plaza, lat, lng ORDER BY count DESC', (vno,))
    plaza_summary = fetchall(cur)
    dates = [str(c['crossed_at']) for c in crossings if c['crossed_at']]
    sim       = sim_location.get_sim(con, vno)
    sim_pings = sim_location.get_sim_pings(con, vno)
    con.close()
    return jsonify({
        'truck': truck, 'crossings': crossings, 'plaza_summary': plaza_summary,
        'unique_plazas': len(plaza_summary),
        'date_from': dates[0][:10] if dates else '—',
        'date_to':   dates[-1][:10] if dates else '—',
        'sim': sim, 'sim_pings': sim_pings,
    })

@application.route('/api/hundred-truck-details-batch')
def hundred_truck_details_batch():
    """Return crossings for all connected trucks — last 90 days."""
    con = get_db(); cur = con.cursor()
    cur.execute('''
        SELECT vehicle_no, plaza, lat, lng, direction, crossed_at
        FROM crossings
        WHERE vehicle_no IN (SELECT vehicle_no FROM trucks WHERE is_connected=1)
          AND lat IS NOT NULL AND lat != 0
          AND crossed_at >= NOW() - INTERVAL '90 days'
        ORDER BY vehicle_no, crossed_at
    ''')
    rows = fetchall(cur)
    con.close()
    by_truck = {}
    for r in rows:
        vno = r['vehicle_no']
        if vno not in by_truck:
            by_truck[vno] = []
        by_truck[vno].append(r)
    return jsonify({'trucks': by_truck})

@application.route('/api/hundred-truck-positions')
def hundred_truck_positions():
    """Return latest crossing + total count per connected truck — fast initial load."""
    con = get_db(); cur = con.cursor()
    cur.execute('''
        SELECT DISTINCT ON (c.vehicle_no)
               c.vehicle_no, c.plaza, c.lat, c.lng, c.crossed_at,
               counts.total
        FROM crossings c
        JOIN (
            SELECT vehicle_no, COUNT(*) as total
            FROM crossings
            WHERE vehicle_no IN (SELECT vehicle_no FROM trucks WHERE is_connected=1)
            GROUP BY vehicle_no
        ) counts ON c.vehicle_no = counts.vehicle_no
        WHERE c.vehicle_no IN (SELECT vehicle_no FROM trucks WHERE is_connected=1)
          AND c.lat IS NOT NULL AND c.lat != 0
        ORDER BY c.vehicle_no, c.crossed_at DESC
    ''')
    rows = fetchall(cur)
    con.close()
    return jsonify({'positions': {r['vehicle_no']: r for r in rows}})

@application.route('/api/hundred-plazas')
def hundred_plazas():
    con = get_db(); cur = con.cursor()
    cur.execute('''
        SELECT plaza, AVG(lat) as lat, AVG(lng) as lng,
               COUNT(DISTINCT vehicle_no) as truck_count,
               COUNT(*) as crossing_count
        FROM crossings
        WHERE lat IS NOT NULL AND lng IS NOT NULL AND lat != 0 AND lng != 0
          AND (vehicle_no IN (SELECT vehicle_no FROM trucks WHERE is_connected=1)
               OR vehicle_no IN (SELECT truck_no FROM credit_trips))
        GROUP BY plaza ORDER BY truck_count DESC
    ''')
    rows = fetchall(cur); con.close()
    return jsonify({'plazas': rows})

@application.route('/api/hundred-truck-locations-at')
def hundred_truck_locations_at():
    at = request.args.get('at', '')
    con = get_db(); cur = con.cursor()
    cur.execute('''
        SELECT * FROM (
            SELECT DISTINCT ON (c.vehicle_no)
                   c.vehicle_no, t.owner, t.state,
                   c.plaza as last_plaza, c.lat, c.lng, c.crossed_at as last_seen
            FROM crossings c
            JOIN trucks t ON c.vehicle_no = t.vehicle_no AND t.is_connected = 1
            WHERE c.crossed_at <= %s AND c.lat IS NOT NULL AND c.lat != 0
            ORDER BY c.vehicle_no, c.crossed_at DESC
        ) connected

        UNION ALL

        SELECT * FROM (
            SELECT DISTINCT ON (c.vehicle_no)
                   c.vehicle_no, ct.tranco as owner, NULL as state,
                   c.plaza as last_plaza, c.lat, c.lng, c.crossed_at as last_seen
            FROM crossings c
            JOIN credit_trips ct ON c.vehicle_no = ct.truck_no
            WHERE c.crossed_at <= %s AND c.lat IS NOT NULL AND c.lat != 0
            ORDER BY c.vehicle_no, c.crossed_at DESC
        ) credit
    ''', (at, at))
    rows = fetchall(cur); con.close()
    return jsonify({'trucks': rows})

@application.route('/api/hundred-truck-locations')
def hundred_truck_locations():
    con = get_db(); cur = con.cursor()
    cur.execute('''
        SELECT * FROM (
            SELECT DISTINCT ON (c.vehicle_no)
                   c.vehicle_no, t.owner, t.state,
                   c.plaza as last_plaza, c.lat, c.lng, c.crossed_at as last_seen
            FROM crossings c
            JOIN trucks t ON c.vehicle_no = t.vehicle_no AND t.is_connected = 1
            WHERE c.lat IS NOT NULL AND c.lat != 0
            ORDER BY c.vehicle_no, c.crossed_at DESC
        ) connected

        UNION ALL

        SELECT * FROM (
            SELECT DISTINCT ON (c.vehicle_no)
                   c.vehicle_no, ct.tranco as owner, NULL as state,
                   c.plaza as last_plaza, c.lat, c.lng, c.crossed_at as last_seen
            FROM crossings c
            JOIN credit_trips ct ON c.vehicle_no = ct.truck_no
            WHERE c.lat IS NOT NULL AND c.lat != 0
            ORDER BY c.vehicle_no, c.crossed_at DESC
        ) credit
    ''')
    rows = fetchall(cur); con.close()
    return jsonify({'trucks': rows})

@application.route('/api/hundred-all-crossings')
def hundred_all_crossings():
    con = get_db(); cur = con.cursor()
    cur.execute('''
        SELECT c.vehicle_no, t.owner, t.state,
               c.plaza, c.lat, c.lng, c.crossed_at
        FROM crossings c
        JOIN trucks t ON c.vehicle_no = t.vehicle_no
        WHERE c.lat IS NOT NULL AND c.lat != 0
        ORDER BY c.crossed_at
    ''')
    rows = fetchall(cur); con.close()
    return jsonify({'crossings': rows})

@application.route('/api/hundred-routes')
def hundred_routes():
    con = get_db(); cur = con.cursor()
    # connected trucks
    cur.execute('SELECT vehicle_no, owner, state FROM trucks ORDER BY state, vehicle_no')
    trucks = [{'vehicle_no': t['vehicle_no'], 'owner': t['owner'] or 'Unknown', 'is_connected': True} for t in fetchall(cur)]
    # credit trucks (not already in trucks table)
    connected_nos = {t['vehicle_no'] for t in trucks}
    cur.execute('SELECT DISTINCT truck_no FROM credit_trips')
    for r in fetchall(cur):
        if r['truck_no'] not in connected_nos:
            trucks.append({'vehicle_no': r['truck_no'], 'owner': 'Credit Trip', 'is_connected': False})
    result = []
    for t in trucks:
        vno = t['vehicle_no']
        cur.execute('SELECT DISTINCT plaza, crossed_at FROM crossings WHERE vehicle_no=%s ORDER BY crossed_at', (vno,))
        crossings = fetchall(cur)
        if not crossings:
            continue
        plazas = []
        for c in crossings:
            if not plazas or plazas[-1] != c['plaza']:
                plazas.append(c['plaza'])
        result.append({
            'vehicle_no':   vno,
            'owner':        t['owner'],
            'is_connected': t['is_connected'],
            'plazas':       plazas,
            'unique_count': len(set(plazas)),
            'first_seen':   str(crossings[0]['crossed_at'])[:10],
            'last_seen':    str(crossings[-1]['crossed_at'])[:10],
        })
    con.close()
    return jsonify({'routes': result})

@application.route('/api/hundred-plaza-report')
def hundred_plaza_report():
    con = get_db(); cur = con.cursor()
    cur.execute('SELECT plaza, COUNT(DISTINCT vehicle_no) as truck_count FROM crossings GROUP BY plaza ORDER BY truck_count DESC')
    plazas = fetchall(cur)
    result = []
    for p in plazas:
        cur.execute('SELECT DISTINCT vehicle_no FROM crossings WHERE plaza=%s ORDER BY vehicle_no', (p['plaza'],))
        trucks = [r['vehicle_no'] for r in fetchall(cur)]
        result.append({'plaza': p['plaza'], 'truck_count': p['truck_count'], 'trucks': trucks})
    con.close()
    return jsonify({'plazas': result})

@application.route('/api/hundred-plaza-detail')
def hundred_plaza_detail():
    plaza = request.args.get('plaza', '').strip()
    con = get_db(); cur = con.cursor()
    cur.execute('''
        SELECT c.vehicle_no,
               COALESCE(t.owner, ct.tranco, 'Unknown') as owner,
               t.state,
               COUNT(*) as cross_count,
               MIN(c.crossed_at) as first_cross,
               MAX(c.crossed_at) as last_cross
        FROM crossings c
        LEFT JOIN trucks t ON c.vehicle_no = t.vehicle_no AND t.is_connected = 1
        LEFT JOIN credit_trips ct ON c.vehicle_no = ct.truck_no
        WHERE c.plaza = %s
          AND (t.vehicle_no IS NOT NULL OR ct.truck_no IS NOT NULL)
        GROUP BY c.vehicle_no, t.owner, ct.tranco, t.state
        ORDER BY cross_count DESC, c.vehicle_no
    ''', (plaza,))
    rows = fetchall(cur); con.close()
    return jsonify({'plaza': plaza, 'trucks': rows})

@application.route('/api/keep-alive')
def keep_alive():
    return jsonify({'status': 'ok', 'time': datetime.datetime.utcnow().isoformat()})

@application.route('/api/zoho-status')
def zoho_status():
    """
    Driven by _zoho_failures, not _retry_attempts — a truck stays 'down' here for
    the whole outage, even once its 15/30/60 min retries are exhausted and there's
    nothing left scheduled. Only an actual successful ping clears it (see
    _ping_one_truck), so this can't flip back to 'up' just because retrying stopped.
    """
    if not _zoho_failures:
        return jsonify({'status': 'up'})
    now = datetime.datetime.now()
    down_for_min = round((now - min(s['since'] for s in _zoho_failures.values())).total_seconds() / 60)
    last_error   = next(iter(_zoho_failures.values()))['error']
    # only trucks still in the 15/30/60 timer have a concrete next attempt — once
    # exhausted, the truck simply waits for the next scheduled hourly ping instead
    still_retrying = [s['next_retry_at'] for v, s in _retry_attempts.items() if v in _zoho_failures]
    next_retry_in_min = max(0, round((min(still_retrying) - now).total_seconds() / 60)) if still_retrying else None
    return jsonify({
        'status': 'down',
        'failing_trucks': list(_zoho_failures.keys()),
        'down_for_min': down_for_min,
        'next_retry_in_min': next_retry_in_min,
        'last_error': last_error,
    })

@application.route('/api/force-zoho-check')
def force_zoho_check():
    """
    Runs the real hourly ping_enroute logic right now instead of waiting for the
    next scheduled hour — so the status badge reflects reality immediately rather
    than whatever it happened to catch last. Takes ~15-20s (one call per truck).
    """
    ping_enroute()
    return zoho_status()

@application.route('/api/make-token')
def api_make_token():
    vno = request.args.get('vehicle_no', '').strip().upper()
    if not vno:
        return jsonify({'error': 'missing vehicle_no'}), 400
    return jsonify({'token': make_token(vno)})

@application.route('/api/track')
def track_by_token():
    token = request.args.get('token', '').strip()
    if not token:
        return jsonify({'error': 'missing token'}), 400
    vno = vehicle_from_token(token)
    if not vno:
        return jsonify({'error': 'invalid token'}), 404
    con = get_db(); cur = con.cursor()
    cur.execute('SELECT * FROM trucks WHERE vehicle_no=%s', (vno,))
    truck = fetchone(cur) or {}
    cur.execute('SELECT plaza, lat, lng, direction, crossed_at FROM crossings WHERE vehicle_no=%s ORDER BY crossed_at', (vno,))
    crossings = fetchall(cur)
    sim       = sim_location.get_sim(con, vno)
    sim_pings = sim_location.get_sim_pings(con, vno)
    con.close()
    dates = [str(c['crossed_at']) for c in crossings if c['crossed_at']]
    # Public link: the trail always ships, the driver's number only when enabled
    has_sim = bool(sim)
    if sim:
        sim = {'msisdn': sim.get('msisdn'), 'driver_name': sim.get('driver_name')}
        if PUBLIC_DRIVER_PHONE != '1':
            sim.pop('msisdn', None)
    return jsonify({
        'vehicle_no': vno,
        'truck': truck,
        'crossings': crossings,
        'date_from': dates[0][:10] if dates else '—',
        'date_to':   dates[-1][:10] if dates else '—',
        'sim': sim, 'sim_pings': sim_pings, 'has_sim': has_sim,
    })

def send_zoho_wa(phone_number, truck_no, tracking_url):
    url = 'https://www.zohoapis.in/creator/custom/mirchi-lime/Send_WA_for_Truck_Tracking?publickey=arVNwYdrt2E1Cg7wkybNG8quA'
    body = json.dumps({
        'truck_no': truck_no,
        'phone_number': phone_number,
        'tracking_url': tracking_url
    }).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())

def send_whatsapp(to, message):
    url  = f'https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages'
    body = json.dumps({
        'messaging_product': 'whatsapp',
        'to': to,
        'type': 'text',
        'text': {'body': message}
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        'Authorization': f'Bearer {WA_TOKEN}',
        'Content-Type': 'application/json'
    })
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())

@application.route('/api/debug-enroute')
def debug_enroute():
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT truck_no, trip_status, is_manual FROM enroute_trips ORDER BY truck_no")
    rows = [{'truck_no': r[0], 'trip_status': r[1], 'is_manual': r[2]} for r in cur.fetchall()]
    con.close()
    return jsonify({'count': len(rows), 'trucks': rows})

@application.route('/api/remove-enroute')
def remove_enroute():
    vno = request.args.get('vehicle_no', '').strip().upper()
    if not vno:
        return jsonify({'error': 'missing vehicle_no'}), 400
    try:
        con = get_db(); cur = con.cursor()
        cur.execute('DELETE FROM enroute_trips WHERE truck_no=%s', (vno,))
        cur.execute('UPDATE trucks SET is_connected=0 WHERE vehicle_no=%s AND (is_manual IS FALSE OR is_manual IS NULL)', (vno,))
        con.commit(); con.close()
        return jsonify({'status': 'removed', 'vehicle_no': vno})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/send-report')
def send_report():
    try:
        to = (request.args.get('to') or WA_TO or REPORT_TO).strip()
        con = get_db(); cur = con.cursor()
        # live trucks
        cur.execute("SELECT truck_no FROM enroute_trips WHERE trip_status='active' OR trip_status IS NULL")
        live_trucks = [r[0] for r in cur.fetchall()]
        # completed today
        cur.execute("SELECT truck_no FROM enroute_trips WHERE trip_status='completed'")
        completed_trucks = [r[0] for r in cur.fetchall()]
        con.close()

        now_ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
        today_str = now_ist.strftime('%d %b %y').lstrip('0')
        time_str  = now_ist.strftime('%I:%M %p').lstrip('0')
        con2 = get_db(); cur2 = con2.cursor()
        cur2.execute('SELECT COUNT(*) FROM trucks WHERE is_connected=1')
        total_connected = cur2.fetchone()[0]
        con2.close()

        # Message 1: summary via Meta WA
        summary = (
            f"*Daily Truck Report*\n\n"
            f"📅 *Date:* {today_str}\n"
            f"⏰ *Time:* {time_str} IST\n"
            f"🚛 *Total Connected:* {total_connected}\n"
            f"🟢 *Live Trucks:* {len(live_trucks)}\n"
            f"✅ *Completed:* {len(completed_trucks)}\n\n"
            f"*Mirchi-Lime*"
        )
        try:
            send_whatsapp(to, summary)
        except Exception as e:
            print(f'Summary WA failed: {e}', flush=True)
        time.sleep(1)

        # Message 2+: one per live truck with tracking link
        BASE_URL = 'https://hundred-trucks-analysis.onrender.com'
        sent = []
        for vno in live_trucks:
            token = make_token(vno)
            tracking_url = f'{BASE_URL}/track.html?token={token}'
            try:
                send_zoho_wa(to, vno, tracking_url)
                sent.append(vno)
            except Exception as e:
                print(f'  WA send failed for {vno}: {e}', flush=True)
            time.sleep(1)

        return jsonify({'status': 'sent', 'summary_sent': True, 'trucks_sent': sent})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/send-wa-test')
def send_wa_test():
    try:
        result = send_whatsapp(WA_TO, 'Test message from Hundred Trucks dashboard.')
        print(f'WhatsApp test sent to {WA_TO}', flush=True)
        return jsonify({'status': 'sent', 'result': result})
    except Exception as e:
        print(f'WhatsApp test ERROR: {e}', flush=True)
        return jsonify({'status': 'error', 'error': str(e)})


@application.route('/api/check-completion')
def api_check_completion():
    try:
        check_trip_completion()
        return jsonify({'status': 'done'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/ping-truck')
def ping_truck():
    vno = request.args.get('vehicle_no', '').strip().upper()
    if not vno:
        return jsonify({'error': 'missing vehicle_no'}), 400
    try:
        records = fetch_zoho(vno)
        new_rows = save_crossings(vno, records)
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Manual ping: {vno} — {len(records)} crossings ({new_rows} new)', flush=True)
        return jsonify({'vehicle_no': vno, 'total': len(records), 'new': new_rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/add-enroute')
def add_enroute():
    vno = request.args.get('vehicle_no', '').strip().upper()
    if not vno:
        return jsonify({'error': 'missing vehicle_no'}), 400
    try:
        con = get_db(); cur = con.cursor()
        # Add to enroute_trips if not already there
        cur.execute('SELECT 1 FROM enroute_trips WHERE truck_no=%s', (vno,))
        if cur.fetchone():
            con.close()
            return jsonify({'status': 'already exists', 'vehicle_no': vno})
        cur.execute('''
            INSERT INTO enroute_trips (truck_no, tranco, shipper, trip_id, indent_number, pickup_pin, drop_pin, loading_point, reporting_date, reporting_time, is_manual, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, NOW())
        ''', (vno, '', '', '', '', '', '', '', '', ''))
        # Add to trucks table if not there
        cur.execute('SELECT 1 FROM trucks WHERE vehicle_no=%s', (vno,))
        if not cur.fetchone():
            state = vno[:2].upper()
            cur.execute('INSERT INTO trucks (vehicle_no, owner, state, is_connected, is_manual) VALUES (%s, %s, %s, 1, TRUE)', (vno, '', state))
        else:
            cur.execute('UPDATE trucks SET is_connected=1, is_manual=TRUE WHERE vehicle_no=%s', (vno,))
        con.commit(); con.close()
        # Ping FASTag immediately
        records = fetch_zoho(vno)
        new_rows = save_crossings(vno, records)
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Manually added {vno} to enroute — {len(records)} crossings ({new_rows} new)', flush=True)
        return jsonify({'status': 'added', 'vehicle_no': vno, 'crossings': len(records), 'new': new_rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/remove-manual')
def remove_manual():
    vno = request.args.get('vehicle_no', '').strip().upper()
    if not vno:
        return jsonify({'error': 'missing vehicle_no'}), 400
    try:
        con = get_db(); cur = con.cursor()
        cur.execute('DELETE FROM enroute_trips WHERE truck_no=%s AND is_manual IS TRUE', (vno,))
        cur.execute('UPDATE trucks SET is_connected=0, is_manual=FALSE WHERE vehicle_no=%s AND is_manual IS TRUE', (vno,))
        con.commit(); con.close()
        return jsonify({'status': 'removed', 'vehicle_no': vno})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/set-sim')
def set_sim():
    vno    = request.args.get('vehicle_no', '').strip().upper()
    raw    = request.args.get('msisdn', '').strip()
    driver = request.args.get('driver_name', '').strip()
    if not vno:
        return jsonify({'error': 'missing vehicle_no'}), 400
    if not raw:
        return jsonify({'error': 'missing msisdn'}), 400
    msisdn = sim_location.normalize_msisdn(raw)
    if not msisdn:
        return jsonify({'error': 'invalid msisdn'}), 400
    try:
        con = get_db()
        # this registers the driver at Telenity, which sends them the consent SMS
        res = sim_location.assign_sim(con, vno, msisdn, driver)
        con.close()
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] SIM assigned: {vno} — {msisdn} '
              f'(entity {res["entity_id"]}, consent {res["consent"]})', flush=True)
        return jsonify({'status': 'assigned', 'vehicle_no': vno, 'msisdn': msisdn,
                        'driver_name': driver, 'consent': res['consent'],
                        'tracking': res['tracking'], 'entity_id': res['entity_id'],
                        'mock': sim_location.MOCK_MODE})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/sim-list')
def sim_list():
    """Everything the driver SIM screen shows."""
    con = get_db()
    rows = sim_location.list_sims(con)
    con.close()
    lic = None
    if not sim_location.MOCK_MODE:
        try:
            lic = telenity.licence()
        except Exception as e:
            print(f'[sim-list] licence check failed: {e}', flush=True)
    return jsonify({'sims': rows, 'licence': lic, 'mock': sim_location.MOCK_MODE})

@application.route('/api/sim-latest')
def sim_latest():
    con = get_db()
    rows = sim_location.latest_pings(con)
    con.close()
    return jsonify({'trucks': rows})

@application.route('/api/remove-sim')
def remove_sim():
    vno = request.args.get('vehicle_no', '').strip().upper()
    if not vno:
        return jsonify({'error': 'missing vehicle_no'}), 400
    try:
        con = get_db()
        n = sim_location.release_sim(con, vno)
        con.close()
        return jsonify({'status': 'removed', 'vehicle_no': vno, 'removed': n})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/resend-consent')
def resend_consent():
    """Re-send the consent SMS. Refused inside the cooldown — see sim_location."""
    vno = request.args.get('vehicle_no', '').strip().upper()
    if not vno:
        return jsonify({'error': 'missing vehicle_no'}), 400
    try:
        con = get_db()
        res = sim_location.resend_consent(con, vno)
        con.close()
        return jsonify({'vehicle_no': vno, **res})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/sweep-consents')
def sweep_consents_now():
    vno = request.args.get('vehicle_no', '').strip().upper() or None
    try:
        started = sim_location.sweep_consents(get_db, vehicle_no=vno)
        return jsonify({'tracking_started': started, 'mock': sim_location.MOCK_MODE})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/ping-sims')
def ping_sims_now():
    vno = request.args.get('vehicle_no', '').strip().upper() or None
    try:
        done = sim_location.poll_sims(get_db, vno)
        return jsonify({'pinged': done, 'mock': sim_location.MOCK_MODE})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

CREDIT_TRIPS_URL  = 'https://www.zohoapis.in/creator/custom/mirchi-lime/Live_ML_Credit_Trips?publickey=u0nbdmwSP912Bdfm3DkFaqVB6'
ENROUTE_TRIPS_URL = 'https://www.zohoapis.in/creator/custom/mirchi-lime/Fetch_Enroute_Trips?publickey=2yHyzpgDNmJgHtdJCKR1jCb8b'

def ensure_credit_trips_table():
    con = get_db(); cur = con.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS credit_trips (
            id SERIAL PRIMARY KEY,
            truck_no   TEXT NOT NULL,
            pickup     TEXT,
            drop_pin   TEXT,
            tranco     TEXT,
            lr_number  TEXT,
            lr_date    TEXT,
            synced_at  TIMESTAMP DEFAULT NOW()
        )
    ''')
    con.commit(); con.close()

def sync_credit_trips():
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Syncing credit trips from Zoho...', flush=True)
    try:
        req  = urllib.request.Request(CREDIT_TRIPS_URL)
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        trips = data.get('result', {}).get('data', [])
        con = get_db(); cur = con.cursor()
        # Full replace — delete all, insert current
        cur.execute('DELETE FROM credit_trips')
        for t in trips:
            cur.execute('''
                INSERT INTO credit_trips (truck_no, pickup, drop_pin, tranco, lr_number, lr_date, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ''', (
                str(t.get('truck_no','')).strip(),
                str(t.get('pickup','')).strip(),
                str(t.get('drop','')).strip(),
                str(t.get('tranco','')).strip(),
                str(t.get('lr_number','')).strip(),
                str(t.get('lr_date','')).strip(),
            ))
        con.commit(); con.close()
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Credit trips synced: {len(trips)} trips.', flush=True)
    except Exception as e:
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Credit trips sync ERROR: {e}', flush=True)

@application.route('/api/force-sync')
def force_sync():
    try:
        sync_credit_trips()
        sync_enroute_trips()
        return jsonify({'status': 'synced'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/credit-trips')
def credit_trips():
    try:
        con = get_db(); cur = con.cursor()
        cur.execute('SELECT truck_no, pickup, drop_pin, tranco, lr_number, lr_date, synced_at FROM credit_trips ORDER BY lr_date DESC, truck_no')
        rows = fetchall(cur); con.close()
        trips = []
        for r in rows:
            dlat, dlng, dcity = resolve_pincode(r['drop_pin'])
            trips.append({
                'truck_no':   r['truck_no'],
                'pickup':     r['pickup'],
                'drop':       r['drop_pin'],
                'drop_lat':   dlat,
                'drop_lng':   dlng,
                'drop_city':  dcity,
                'tranco':     r['tranco'],
                'lr_number':  r['lr_number'],
                'lr_date':    r['lr_date'],
            })
        return jsonify({'result': {'data': trips, 'code': '200'}, 'code': 3000})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def ensure_enroute_trips_table():
    con = get_db(); cur = con.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS enroute_trips (
            id            SERIAL PRIMARY KEY,
            truck_no      TEXT NOT NULL,
            tranco        TEXT,
            shipper       TEXT,
            trip_id       TEXT,
            indent_number TEXT,
            pickup_pin    TEXT,
            drop_pin      TEXT,
            loading_point TEXT,
            reporting_date TEXT,
            reporting_time TEXT,
            synced_at     TIMESTAMP DEFAULT NOW()
        )
    ''')
    # Add columns if they don't exist (for existing tables)
    cur.execute("ALTER TABLE enroute_trips ADD COLUMN IF NOT EXISTS reporting_time TEXT")
    cur.execute("ALTER TABLE enroute_trips ADD COLUMN IF NOT EXISTS is_manual BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE trucks ADD COLUMN IF NOT EXISTS is_manual BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE enroute_trips ADD COLUMN IF NOT EXISTS trip_status TEXT DEFAULT 'active'")
    cur.execute("ALTER TABLE enroute_trips ADD COLUMN IF NOT EXISTS near_dest_since TIMESTAMP")
    cur.execute('''
        CREATE TABLE IF NOT EXISTS pincode_cache (
            pincode TEXT PRIMARY KEY,
            lat     FLOAT,
            lng     FLOAT,
            city    TEXT
        )
    ''')
    con.commit(); con.close()

REPORT_TO = os.environ.get('REPORT_TO', '+918925987293')

def auto_send_report():
    # DB-based dedup: skip if another worker already ran this report within the last 10 minutes
    try:
        con = get_db(); cur = con.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS report_log (
                id SERIAL PRIMARY KEY,
                ran_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cur.execute("SELECT ran_at FROM report_log WHERE ran_at > NOW() - INTERVAL '10 minutes'")
        if cur.fetchone():
            con.close()
            print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Auto-report skipped — already ran within 10 min.', flush=True)
            return
        cur.execute("INSERT INTO report_log (ran_at) VALUES (NOW())")
        con.commit(); con.close()
    except Exception as e:
        print(f'report_log check failed: {e}', flush=True)

    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Auto-sending truck report to {REPORT_TO}...', flush=True)
    try:
        con = get_db(); cur = con.cursor()
        cur.execute("SELECT truck_no FROM enroute_trips WHERE trip_status='active' OR trip_status IS NULL")
        live_trucks = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT truck_no FROM enroute_trips WHERE trip_status='completed'")
        completed_trucks = [r[0] for r in cur.fetchall()]
        cur.execute('SELECT COUNT(*) FROM trucks WHERE is_connected=1')
        total_connected = cur.fetchone()[0]
        con.close()

        now_ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
        today_str = now_ist.strftime('%d %b %y').lstrip('0')
        time_str  = now_ist.strftime('%I:%M %p').lstrip('0')

        summary = (
            f"*Daily Truck Report*\n\n"
            f"Date: {today_str}\n"
            f"Time: {time_str} IST\n"
            f"Total Connected: {total_connected}\n"
            f"Live Trucks: {len(live_trucks)}\n"
            f"Completed: {len(completed_trucks)}\n\n"
            f"*Mirchi-Lime*"
        )
        try:
            send_whatsapp(REPORT_TO, summary)
        except Exception as e:
            print(f'  Summary WA failed: {e}', flush=True)
        time.sleep(1)

        BASE_URL = 'https://hundred-trucks-analysis.onrender.com'
        for vno in live_trucks:
            token = make_token(vno)
            tracking_url = f'{BASE_URL}/track.html?token={token}'
            try:
                send_zoho_wa(REPORT_TO, vno, tracking_url)
                print(f'  Sent link for {vno}', flush=True)
            except Exception as e:
                print(f'  WA send failed for {vno}: {e}', flush=True)
            time.sleep(1)

        print(f'  Auto-report done. {len(live_trucks)} trucks sent.', flush=True)
    except Exception as e:
        print(f'  Auto-report ERROR: {e}', flush=True)

def sync_enroute_trips():
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Syncing enroute trips from Zoho...', flush=True)
    try:
        req  = urllib.request.Request(ENROUTE_TRIPS_URL)
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        trips = data.get('result', {}).get('data', [])
        con = get_db(); cur = con.cursor()
        # Snapshot existing data before replace for change detection
        cur.execute('SELECT truck_no, pickup_pin, drop_pin FROM enroute_trips')
        old_map = {r[0]: {'pickup': r[1], 'drop': r[2]} for r in cur.fetchall()}

        cur.execute('DELETE FROM enroute_trips WHERE is_manual IS NOT TRUE')
        zoho_vnos = set()
        new_trucks = removed_trucks = 0

        # Deduplicate: if same truck appears multiple times, keep only the latest trip (by reporting_date)
        deduped = {}
        for t in trips:
            vno = t.get('truck_number','').strip()
            if not vno:
                continue
            lp = t.get('loading_points', [{}])[0]
            rep_date = lp.get('reporting_date','').strip()
            if vno not in deduped or rep_date > deduped[vno]['_rep_date']:
                t['_rep_date'] = rep_date
                deduped[vno] = t
        trips = list(deduped.values())

        for t in trips:
            vno    = t.get('truck_number','').strip()
            tranco = t.get('tranco','').strip()
            lp     = t.get('loading_points', [{}])[0]
            zoho_vnos.add(vno)
            cur.execute('''
                INSERT INTO enroute_trips (truck_no, tranco, shipper, trip_id, indent_number, pickup_pin, drop_pin, loading_point, reporting_date, reporting_time, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ''', (
                vno, tranco,
                t.get('shipper','').strip(),
                t.get('trip_id','').strip(),
                t.get('indent_number','').strip(),
                t.get('pickup_pincode','').strip(),
                t.get('drop_pincode','').strip(),
                lp.get('loading_point','').strip(),
                lp.get('reporting_date','').strip(),
                lp.get('reporting_time', {}).get('SQLTime', '') if isinstance(lp.get('reporting_time'), dict) else str(lp.get('reporting_time','')).strip(),
            ))
            # Detect pickup/drop changes
            new_pickup = t.get('pickup_pincode','').strip()
            new_drop   = t.get('drop_pincode','').strip()
            if vno in old_map:
                if old_map[vno]['drop'] != new_drop:
                    print(f'  ⚠ Destination changed: {vno} — {old_map[vno]["drop"]} → {new_drop}', flush=True)
                if old_map[vno]['pickup'] != new_pickup:
                    print(f'  ⚠ Pickup changed: {vno} — {old_map[vno]["pickup"]} → {new_pickup}', flush=True)

            # Auto-add new trucks
            cur.execute('SELECT 1 FROM trucks WHERE vehicle_no=%s', (vno,))
            if not cur.fetchone():
                state = vno[:2].upper() if len(vno) >= 2 else 'TN'
                cur.execute(
                    'INSERT INTO trucks (vehicle_no, owner, state, is_connected, is_manual) VALUES (%s, %s, %s, 1, FALSE)',
                    (vno, tranco, state)
                )
                new_trucks += 1
                print(f'  + Auto-added: {vno} ({tranco})', flush=True)
            else:
                # Re-enable if it was previously disconnected
                cur.execute('UPDATE trucks SET is_connected=1, owner=%s WHERE vehicle_no=%s', (tranco, vno))

        # Disconnect trucks no longer in Zoho (skip manually added trucks)
        cur.execute('SELECT vehicle_no FROM trucks WHERE is_connected=1 AND is_manual IS NOT TRUE')
        for (db_vno,) in cur.fetchall():
            if db_vno not in zoho_vnos:
                cur.execute('UPDATE trucks SET is_connected=0 WHERE vehicle_no=%s', (db_vno,))
                removed_trucks += 1
                print(f'  - Disconnected: {db_vno} (no longer in Zoho)', flush=True)

        con.commit(); con.close()
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Enroute trips synced: {len(trips)} trips, +{new_trucks} added, -{removed_trucks} disconnected.', flush=True)
    except Exception as e:
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Enroute trips sync ERROR: {e}', flush=True)

def resolve_pincode(pin):
    if not pin:
        return None, None, None
    try:
        con = get_db(); cur = con.cursor()
        cur.execute('SELECT lat, lng, city FROM pincode_cache WHERE pincode=%s', (pin,))
        row = cur.fetchone()
        if row:
            con.close()
            return row[0], row[1], row[2]
        # Query Nominatim
        url = f'https://nominatim.openstreetmap.org/search?q={pin}+India&format=json&limit=1&addressdetails=1'
        req = urllib.request.Request(url, headers={'User-Agent': 'TollTrace/1.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if not data:
            con.close()
            return None, None, None
        lat = float(data[0]['lat'])
        lng = float(data[0]['lon'])
        addr = data[0].get('address', {})
        city = addr.get('city') or addr.get('town') or addr.get('village') or addr.get('county') or pin
        cur.execute('INSERT INTO pincode_cache (pincode, lat, lng, city) VALUES (%s,%s,%s,%s) ON CONFLICT (pincode) DO NOTHING', (pin, lat, lng, city))
        con.commit(); con.close()
        return lat, lng, city
    except Exception as e:
        print(f'[pincode resolve] {pin}: {e}', flush=True)
        return None, None, None

@application.route('/api/enroute-trips')
def enroute_trips():
    try:
        con = get_db(); cur = con.cursor()
        cur.execute('SELECT truck_no, tranco, shipper, trip_id, indent_number, pickup_pin, drop_pin, loading_point, reporting_date, reporting_time, trip_status, synced_at FROM enroute_trips ORDER BY truck_no')
        rows = fetchall(cur); con.close()
        trips = []
        for r in rows:
            plat, plng, pcity = resolve_pincode(r['pickup_pin'])
            dlat, dlng, dcity = resolve_pincode(r['drop_pin'])
            trips.append({
                'truck_number':   r['truck_no'],
                'tranco':         r['tranco'],
                'shipper':        r['shipper'],
                'trip_id':        r['trip_id'],
                'indent_number':  r['indent_number'],
                'pickup_pincode': r['pickup_pin'],
                'pickup_lat':     plat,
                'pickup_lng':     plng,
                'pickup_city':    pcity,
                'drop_pincode':   r['drop_pin'],
                'drop_lat':       dlat,
                'drop_lng':       dlng,
                'drop_city':      dcity,
                'trip_status':    r['trip_status'] or 'active',
                'loading_points': [{'loading_point': r['loading_point'], 'reporting_date': r['reporting_date'], 'reporting_time': r['reporting_time']}],
            })
        return jsonify({'result': {'code': '200', 'data': trips}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@application.route('/api/credit-truck-route')
def credit_truck_route():
    vno = request.args.get('vehicle_no', '').strip().upper()
    if not vno:
        return jsonify({'crossings': []})
    try:
        con = get_db(); cur = con.cursor()
        cur.execute(
            'SELECT plaza, crossed_at, lat, lng FROM crossings WHERE vehicle_no=%s AND lat!=0 AND lng!=0 ORDER BY crossed_at',
            (vno,)
        )
        rows = cur.fetchall()
        con.close()
        crossings = [{'plaza': r[0], 'crossed_at': str(r[1]), 'lat': float(r[2]), 'lng': float(r[3])} for r in rows]
        return jsonify({'vehicle_no': vno, 'crossings': crossings})
    except Exception as e:
        return jsonify({'crossings': [], 'error': str(e)})

@application.route('/api/fasttag', methods=['POST'])
def fasttag():
    body = request.get_data()
    try:
        req  = urllib.request.Request(ZOHO_URL, data=body, headers={'Content-Type': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read()
        return application.response_class(data, mimetype='application/json')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def fetch_zoho(vehicle_no):
    body = json.dumps({'vehicle_no': vehicle_no}).encode()
    req  = urllib.request.Request(ZOHO_URL, data=body, headers={'Content-Type': 'application/json'})
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read())
    result = data.get('result', {})
    code = str(result.get('code'))
    # Zoho returns HTTP 200 even when the ULIP call it wraps failed — only
    # result.status/code says whether this response is real. Without this
    # check a ULIP outage looks identical to "0 crossings" and never raises,
    # so /api/zoho-status stays 'up' the whole time.
    # code 404 ("No FASTag transactions found (errCode: 740)") is not a
    # failure — it just means this truck has no crossings right now (e.g. an
    # inactive FASTag) — so it must not raise or it'll falsely mark a quiet
    # truck as a ULIP outage.
    if code == '404':
        return []
    if code != '200':
        raise Exception(result.get('message', 'Zoho FASTag call failed'))
    return result.get('data', [])

def save_crossings(vehicle_no, records):
    con = get_db(); cur = con.cursor()
    saved = 0
    for r in records:
        plaza      = r.get('tollPlazaName', '').strip()
        crossed_at = r.get('readerReadTime', '').strip()
        geocode    = r.get('tollPlazaGeocode', '')
        lat, lng   = 0, 0
        if geocode and ',' in geocode:
            parts = geocode.split(',')
            lat, lng = float(parts[0]), float(parts[1])
        direction = r.get('laneDirection', '').strip()
        seq_no    = str(r.get('seqNo', ''))
        vtype     = r.get('vehicleType', '').strip()
        if not plaza or not crossed_at:
            continue
        cur.execute('SELECT 1 FROM crossings WHERE vehicle_no=%s AND plaza=%s AND crossed_at=%s',
                    (vehicle_no, plaza, crossed_at))
        if not cur.fetchone():
            cur.execute('INSERT INTO crossings (vehicle_no, plaza, lat, lng, direction, seq_no, vehicle_type, crossed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                        (vehicle_no, plaza, lat, lng, direction, seq_no, vtype, crossed_at))
            saved += 1
    ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    cur.execute('UPDATE trucks SET fetched_at=%s WHERE vehicle_no=%s',
                (ist_now.strftime('%Y-%m-%d %H:%M:%S'), vehicle_no))
    con.commit(); con.close()
    return saved

def refresh_all_trucks():
    con = get_db(); cur = con.cursor()
    cur.execute('SELECT vehicle_no FROM trucks ORDER BY vehicle_no')
    trucks = [r[0] for r in cur.fetchall()]
    con.close()
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Scheduler: refreshing all {len(trucks)} trucks...', flush=True)
    ok = fail = 0
    for vno in trucks:
        try:
            records = fetch_zoho(vno)
            if records:
                new_rows = save_crossings(vno, records)
                ok += 1
                print(f'  ✓ {vno} — {len(records)} crossings ({new_rows} new)', flush=True)
            else:
                print(f'  — {vno} — no data', flush=True)
        except Exception as e:
            fail += 1
            print(f'  ✗ {vno} — ERROR: {e}', flush=True)
        time.sleep(3)
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Refresh complete. ✓ {ok} done  ✗ {fail} failed', flush=True)

def haversine_km(lat1, lng1, lat2, lng2):
    R = 6371
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1; dlng = lng2 - lng1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def check_trip_completion():
    try:
        con = get_db(); cur = con.cursor()
        cur.execute("SELECT truck_no, drop_pin, near_dest_since FROM enroute_trips WHERE trip_status='active' AND drop_pin IS NOT NULL AND drop_pin != ''")
        trips = cur.fetchall()
        for (vno, drop_pin, near_since) in trips:
            dlat, dlng, _ = resolve_pincode(drop_pin)
            if not dlat or not dlng:
                continue
            # get last crossing with valid coords
            cur.execute("SELECT lat, lng FROM crossings WHERE vehicle_no=%s AND lat IS NOT NULL AND lat!=0 ORDER BY crossed_at DESC LIMIT 1", (vno,))
            row = cur.fetchone()
            if not row:
                continue
            dist = haversine_km(row[0], row[1], dlat, dlng)
            if dist <= 50:
                if near_since is None:
                    cur.execute("UPDATE enroute_trips SET near_dest_since=NOW() WHERE truck_no=%s", (vno,))
                    print(f'  📍 {vno} within 50 km of destination ({dist:.1f} km) — timer started', flush=True)
                else:
                    hours_near = (datetime.datetime.utcnow() - near_since).total_seconds() / 3600
                    if hours_near >= 1:
                        cur.execute("UPDATE enroute_trips SET trip_status='completed' WHERE truck_no=%s", (vno,))
                        cur.execute("UPDATE trucks SET is_connected=0 WHERE vehicle_no=%s", (vno,))
                        print(f'  ✅ {vno} trip marked COMPLETED (within 50 km for {hours_near:.1f} hrs)', flush=True)
            else:
                # reset timer if truck moved away
                if near_since is not None:
                    cur.execute("UPDATE enroute_trips SET near_dest_since=NULL WHERE truck_no=%s", (vno,))
        con.commit(); con.close()
    except Exception as e:
        print(f'[check_trip_completion] ERROR: {e}', flush=True)

# ── Auto-retry for Zoho hiccups ─────────────────────────────────────────────
# On 6 Sep, Zoho briefly returned malformed responses and every FASTag fetch
# crashed with "'str' object has no attribute 'get'" — silently, for ~24h,
# until someone noticed the data had gone stale. Rather than waiting for the
# next hourly cron, a truck that fails now gets retried sooner: 15 min, then
# 30, then 60 — before falling back to the normal hourly schedule. Retries
# are per-truck (a real Zoho outage fails every truck, so they all end up
# retried anyway; a single truck's own hiccup no longer has to wait an hour).
RETRY_DELAYS_MIN = [15, 30, 60]
_retry_attempts = {}   # vehicle_no -> {'attempt', 'error', 'next_retry_at'} — drives the 15/30/60 timer
_zoho_failures  = {}   # vehicle_no -> {'error', 'since'} — drives /api/zoho-status; only cleared on success,
                        # so a long outage stays reported as 'down' even after retries are exhausted and
                        # the 15/30/60 timer above has nothing left scheduled

def _ping_one_truck(vno, label):
    """Fetch + save one truck's crossings, scheduling a retry on failure."""
    try:
        records = fetch_zoho(vno)
        new_rows = save_crossings(vno, records)
        print(f'  ✓ {vno} — {len(records)} crossings ({new_rows} new)', flush=True)
        _retry_attempts.pop(vno, None)
        _zoho_failures.pop(vno, None)
    except Exception as e:
        print(f'  ✗ {vno} — ERROR: {e}', flush=True)
        if vno in _zoho_failures:
            _zoho_failures[vno]['error'] = str(e)
        else:
            _zoho_failures[vno] = {'error': str(e), 'since': datetime.datetime.now()}
        _schedule_retry(vno, label, str(e))

def _schedule_retry(vno, label, error=''):
    attempt = _retry_attempts.get(vno, {}).get('attempt', 0)
    if attempt >= len(RETRY_DELAYS_MIN):
        print(f'  {vno}: giving up after {attempt} retries — will resume at the next scheduled {label} ping', flush=True)
        _retry_attempts.pop(vno, None)
        return
    delay = RETRY_DELAYS_MIN[attempt]
    run_at = datetime.datetime.now() + datetime.timedelta(minutes=delay)
    _retry_attempts[vno] = {'attempt': attempt + 1, 'error': error, 'next_retry_at': run_at}
    print(f'  {vno}: will retry in {delay} min (attempt {attempt + 1}/{len(RETRY_DELAYS_MIN)})', flush=True)
    scheduler.add_job(_retry_one_truck, 'date', run_date=run_at,
                       args=[vno, label], id=f'retry_{vno}_{run_at.timestamp()}',
                       misfire_grace_time=300)

def _retry_one_truck(vno, label):
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Retrying {label} ping for {vno}...', flush=True)
    _ping_one_truck(vno, label)

def ping_enroute():
    # First sync fresh trip list from Zoho, then ping FASTag for each connected truck
    sync_enroute_trips()
    try:
        con = get_db(); cur = con.cursor()
        # Always ping all connected trucks (is_connected=1), not just Zoho enroute list
        # — Zoho may clear trips after delivery, leaving fetched_at stuck otherwise
        cur.execute('SELECT vehicle_no FROM trucks WHERE is_connected=1')
        trucks = [r[0] for r in cur.fetchall()]
        con.close()
    except Exception as e:
        # Previously unguarded — a single DB hiccup here used to silently
        # skip every truck for the whole hour with no sign anything failed.
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping enroute: could not read truck list — {e}', flush=True)
        return
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping enroute: {len(trucks)} trucks...', flush=True)
    for vno in trucks:
        _ping_one_truck(vno, 'enroute')
        time.sleep(3)
    check_trip_completion()
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping enroute complete.', flush=True)

def ping_sims():
    # consent first: a driver who replied since the last sweep starts being tracked now
    sim_location.sweep_consents(get_db)
    sim_location.poll_sims(get_db)

def ping_credit():
    # First sync fresh trip list from Zoho, then ping FASTag for each truck
    sync_credit_trips()
    try:
        con = get_db(); cur = con.cursor()
        cur.execute('SELECT DISTINCT truck_no FROM credit_trips')
        trucks = [r[0] for r in cur.fetchall()]
        con.close()
    except Exception as e:
        print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping credit: could not read truck list — {e}', flush=True)
        return
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping credit: {len(trucks)} trucks...', flush=True)
    for vno in trucks:
        _ping_one_truck(vno, 'credit')
        time.sleep(3)
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] Ping credit complete.', flush=True)


# Start background scheduler — use a lock file to ensure only one process starts it
import fcntl
_scheduler_lock_file = '/tmp/hundred_scheduler.lock'
_lock_fh = None
try:
    _lock_fh = open(_scheduler_lock_file, 'w')
    fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Got the lock — this process starts the scheduler
    ensure_credit_trips_table()
    ensure_enroute_trips_table()
    _sim_con = get_db(); sim_location.ensure_sim_tables(_sim_con); _sim_con.close()
    sync_credit_trips()    # sync on startup
    scheduler = BackgroundScheduler(timezone='Asia/Kolkata')
    scheduler.add_job(ping_enroute,       'cron', hour='6-23', minute=0)         # every hour 6 AM–11 PM IST (skip 12 AM–5 AM to save DB compute)
    scheduler.add_job(ping_credit,        'cron', hour=11, minute=0)            # 11:00 AM IST (Zoho sync + FASTag ping)
    scheduler.add_job(ping_credit,        'cron', hour=13, minute=0)            # 1:00 PM IST (Zoho sync + FASTag ping)
    scheduler.add_job(ping_credit,        'cron', hour=17, minute=0)            # 5:00 PM IST (Zoho sync + FASTag ping)
    scheduler.add_job(sync_credit_trips,  'cron', hour=11, minute=0)            # 11:00 AM IST
    scheduler.add_job(sync_credit_trips,  'cron', hour=13, minute=0)            # 1:00 PM IST
    scheduler.add_job(sync_credit_trips,  'cron', hour=17, minute=0)            # 5:00 PM IST
    scheduler.add_job(sync_enroute_trips, 'cron', hour='9,11,13,15,17,19,21,23', minute=0)  # every 2 hrs 9 AM–11 PM IST
    scheduler.add_job(ping_sims,          'cron', hour=SIM_PING_HOURS, minute=SIM_PING_MINUTES)  # consent sweep + SIM fixes, every 30 min
    scheduler.add_job(auto_send_report,   'cron', hour=11, minute=5)            # 11:05 AM IST (after Zoho sync)
    scheduler.add_job(auto_send_report,   'cron', hour=19, minute=5)            # 7:05 PM IST (after Zoho sync)
    scheduler.start()
    print('APScheduler started — pinging enroute every hour, credit 3x/day.', flush=True)
except BlockingIOError:
    print('APScheduler already running in another process — skipping.', flush=True)

if __name__ == '__main__':
    application.run(debug=True, port=8090)

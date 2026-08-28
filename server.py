#!/usr/bin/env python3
"""
Server for 100 Trucks FASTag Analysis Platform
"""
import http.server, json, urllib.request, webbrowser, threading, os, time, hmac, hashlib
import psycopg2, psycopg2.extras
from psycopg2 import pool as pg_pool
from dotenv import load_dotenv
load_dotenv()
from urllib.parse import urlparse, parse_qs
import sim_location, telenity

PORT         = 8081
NEON_URL     = os.environ.get('DATABASE_URL')
TOKEN_SECRET = os.environ.get('TOKEN_SECRET', 'tolltrace-secret-key-2026')

_db_pool = None
def _get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = pg_pool.ThreadedConnectionPool(1, 20, NEON_URL)
    return _db_pool

# Simple TTL cache: {key: (expires_at, data)}
_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry[0]:
            return entry[1]
    return None

def cache_set(key, data, ttl=60):
    with _cache_lock:
        _cache[key] = (time.time() + ttl, data)

def cache_drop(*keys, prefix=None):
    with _cache_lock:
        for k in keys:
            _cache.pop(k, None)
        if prefix:
            for k in [k for k in _cache if k.startswith(prefix)]:
                _cache.pop(k, None)

def make_token(vehicle_no):
    return hmac.new(TOKEN_SECRET.encode(), vehicle_no.encode(), hashlib.sha256).hexdigest()[:16]

def vehicle_from_token(token):
    con = get_db(); cur = con.cursor()
    cur.execute('SELECT vehicle_no FROM trucks')
    rows = cur.fetchall(); release_db(con)
    for (vno,) in rows:
        if make_token(vno) == token:
            return vno
    return None

def get_db():
    return _get_pool().getconn()

def release_db(con):
    _get_pool().putconn(con)

def fetchall(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

def fetchone(cur):
    cols = [d[0] for d in cur.description]
    row  = cur.fetchone()
    return dict(zip(cols, row)) if row else None

# ||For this plaza, show me which trucks crossed it, how many times, and when first and last||
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/hundred-plaza-detail'):
            qs    = parse_qs(urlparse(self.path).query)
            plaza = qs.get('plaza', [''])[0].strip()
            con   = get_db(); cur = con.cursor()
            cur.execute('''
                SELECT c.vehicle_no, t.owner, t.state,
                       COUNT(*) as cross_count,
                       MIN(crossed_at) as first_cross,
                       MAX(crossed_at) as last_cross
                FROM crossings c
                JOIN trucks t ON c.vehicle_no = t.vehicle_no
                WHERE c.plaza = %s
                GROUP BY c.vehicle_no, t.owner, t.state
                ORDER BY cross_count DESC, c.vehicle_no
            ''', (plaza,))
            rows = fetchall(cur); release_db(con)
            self._json({'plaza': plaza, 'trucks': rows})

        elif self.path.startswith('/api/hundred-plaza-report'):
            con = get_db(); cur = con.cursor()
            cur.execute('SELECT plaza, COUNT(DISTINCT vehicle_no) as truck_count FROM crossings GROUP BY plaza ORDER BY truck_count DESC')
            plazas = fetchall(cur)
            result = []
            for p in plazas:
                cur.execute('SELECT DISTINCT vehicle_no FROM crossings WHERE plaza=%s ORDER BY vehicle_no', (p['plaza'],))
                trucks = [r['vehicle_no'] for r in fetchall(cur)]
                result.append({'plaza': p['plaza'], 'truck_count': p['truck_count'], 'trucks': trucks})
            release_db(con)
            self._json({'plazas': result})

        elif self.path.startswith('/api/hundred-routes'):
            con = get_db(); cur = con.cursor()
            cur.execute('SELECT vehicle_no, owner, state FROM trucks ORDER BY state, vehicle_no')
            trucks = fetchall(cur)
            result = []
            for t in trucks:
                vno = t['vehicle_no']
                cur.execute('SELECT DISTINCT plaza, crossed_at FROM crossings WHERE vehicle_no=%s ORDER BY crossed_at', (vno,))
                crossings = fetchall(cur)
                plazas = []
                for c in crossings:
                    if not plazas or plazas[-1] != c['plaza']:
                        plazas.append(c['plaza'])
                result.append({
                    'vehicle_no': vno, 'owner': t['owner'] or 'Unknown', 'state': t['state'],
                    'plazas': plazas, 'unique_count': len(set(plazas)),
                    'first_seen': str(crossings[0]['crossed_at'])[:10] if crossings else '—',
                    'last_seen':  str(crossings[-1]['crossed_at'])[:10] if crossings else '—',
                })
            release_db(con)
            self._json({'routes': result})

        elif self.path.startswith('/api/hundred-truck-positions'):
            # latest crossing + total count per connected truck — fast initial load for Live Routes
            con = get_db(); cur = con.cursor()
            cur.execute('''
                SELECT DISTINCT ON (c.vehicle_no)
                       c.vehicle_no, c.plaza, c.lat, c.lng, c.crossed_at, counts.total
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
            rows = fetchall(cur); release_db(con)
            self._json({'positions': {r['vehicle_no']: r for r in rows}})

        elif self.path.startswith('/api/hundred-truck-locations-at'):
            qs = parse_qs(urlparse(self.path).query)
            at = qs.get('at', [''])[0]
            con = get_db(); cur = con.cursor()
            cur.execute('''
                SELECT c.vehicle_no, t.owner, t.state,
                       c.plaza as last_plaza, c.lat, c.lng, c.crossed_at as last_seen
                FROM crossings c
                JOIN trucks t ON c.vehicle_no = t.vehicle_no
                WHERE c.id IN (
                    SELECT id FROM crossings c2
                    WHERE c2.vehicle_no = c.vehicle_no AND c2.crossed_at <= %s
                    ORDER BY c2.crossed_at DESC LIMIT 1
                )
                AND c.lat IS NOT NULL AND c.lat != 0
            ''', (at,))
            rows = fetchall(cur); release_db(con)
            self._json({'trucks': rows})

        elif self.path.startswith('/api/hundred-truck-locations'):
            con = get_db(); cur = con.cursor()
            cur.execute('''
                SELECT c.vehicle_no, t.owner, t.state,
                       c.plaza as last_plaza, c.lat, c.lng, c.crossed_at as last_seen
                FROM crossings c
                JOIN trucks t ON c.vehicle_no = t.vehicle_no
                WHERE c.id IN (
                    SELECT id FROM crossings c2
                    WHERE c2.vehicle_no = c.vehicle_no
                    ORDER BY c2.crossed_at DESC LIMIT 1
                )
                AND c.lat IS NOT NULL AND c.lat != 0
            ''')
            rows = fetchall(cur); release_db(con)
            self._json({'trucks': rows})

        elif self.path.startswith('/api/hundred-plazas'):
            con = get_db(); cur = con.cursor()
            cur.execute('''
                SELECT plaza, AVG(lat) as lat, AVG(lng) as lng,
                       COUNT(DISTINCT vehicle_no) as truck_count,
                       COUNT(*) as crossing_count
                FROM crossings
                WHERE lat IS NOT NULL AND lng IS NOT NULL AND lat != 0 AND lng != 0
                GROUP BY plaza ORDER BY truck_count DESC
            ''')
            rows = fetchall(cur); release_db(con)
            self._json({'plazas': rows})

        elif self.path.startswith('/api/hundred-truck-detail'):
            qs  = parse_qs(urlparse(self.path).query)
            vno = qs.get('vehicle_no', [''])[0].strip().upper()
            cache_key = f'truck-detail-{vno}'
            cached = cache_get(cache_key)
            if cached:
                self._json(cached)
            else:
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
                release_db(con)
                result = {
                    'truck': truck, 'crossings': crossings, 'plaza_summary': plaza_summary,
                    'unique_plazas': len(plaza_summary),
                    'date_from': dates[0][:10] if dates else '—',
                    'date_to':   dates[-1][:10] if dates else '—',
                    'sim': sim, 'sim_pings': sim_pings,
                }
                cache_set(cache_key, result, ttl=120)
                self._json(result)

        elif self.path.startswith('/api/make-token'):
            qs  = parse_qs(urlparse(self.path).query)
            vno = qs.get('vehicle_no', [''])[0].strip().upper()
            if not vno:
                self._json({'error': 'missing vehicle_no'}); return
            self._json({'token': make_token(vno)})

        elif self.path.startswith('/api/track'):
            qs    = parse_qs(urlparse(self.path).query)
            token = qs.get('token', [''])[0].strip()
            if not token:
                self._json({'error': 'missing token'}); return
            vno = vehicle_from_token(token)
            if not vno:
                self._json({'error': 'invalid token'}); return
            con = get_db(); cur = con.cursor()
            cur.execute('SELECT * FROM trucks WHERE vehicle_no=%s', (vno,))
            truck = fetchone(cur) or {}
            cur.execute('SELECT plaza, lat, lng, direction, crossed_at FROM crossings WHERE vehicle_no=%s ORDER BY crossed_at', (vno,))
            crossings = fetchall(cur)
            sim       = sim_location.get_sim(con, vno)
            sim_pings = sim_location.get_sim_pings(con, vno)
            release_db(con)
            dates = [str(c['crossed_at']) for c in crossings if c['crossed_at']]
            self._json({
                'vehicle_no': vno, 'truck': truck, 'crossings': crossings,
                'date_from': dates[0][:10] if dates else '—',
                'date_to':   dates[-1][:10] if dates else '—',
                'sim': sim, 'sim_pings': sim_pings, 'has_sim': bool(sim),
            })

        elif self.path.startswith('/api/enroute-trips'):
            cached = cache_get('enroute-trips')
            if cached:
                self._json(cached); return
            try:
                con = get_db(); cur = con.cursor()
                cur.execute('SELECT truck_no, tranco, shipper, trip_id, indent_number, pickup_pin, drop_pin, loading_point, reporting_date, reporting_time FROM enroute_trips ORDER BY truck_no')
                rows = fetchall(cur); release_db(con)
                trips = [{
                    'truck_number':   r['truck_no'],
                    'tranco':         r['tranco'],
                    'shipper':        r['shipper'],
                    'trip_id':        r['trip_id'],
                    'indent_number':  r['indent_number'],
                    'pickup_pincode': r['pickup_pin'],
                    'drop_pincode':   r['drop_pin'],
                    'loading_points': [{'loading_point': r['loading_point'], 'reporting_date': r['reporting_date'], 'reporting_time': r['reporting_time']}],
                } for r in rows]
                result = {'result': {'code': '200', 'data': trips}}
                cache_set('enroute-trips', result, ttl=120)
                self._json(result)
            except Exception as e:
                self._json({'error': str(e)})

        elif self.path.startswith('/api/credit-truck-route'):
            qs  = parse_qs(urlparse(self.path).query)
            vno = qs.get('vehicle_no', [''])[0].strip().upper()
            if not vno:
                self._json({'crossings': []}); return
            cached = cache_get(f'credit-route-{vno}')
            if cached:
                self._json(cached); return
            ZOHO_URL = 'https://www.zohoapis.in/creator/custom/mirchi-lime/Fetch_Fast_Tag_Data?publickey=m2H9YHHA901WRUOAAFRY04WzD'
            try:
                body = json.dumps({'vehicle_no': vno}).encode()
                req  = urllib.request.Request(ZOHO_URL, data=body, headers={'Content-Type': 'application/json'})
                resp = urllib.request.urlopen(req, timeout=15)
                data = json.loads(resp.read())
                records = data.get('result', {}).get('data', [])
                crossings = []
                for r in records:
                    plaza = r.get('tollPlazaName', '').strip()
                    crossed_at = r.get('readerReadTime', '').strip()
                    geocode = r.get('tollPlazaGeocode', '')
                    lat, lng = 0.0, 0.0
                    if geocode and ',' in geocode:
                        parts = geocode.split(',')
                        try: lat, lng = float(parts[0]), float(parts[1])
                        except: pass
                    if plaza and crossed_at and lat and lng:
                        crossings.append({'plaza': plaza, 'crossed_at': crossed_at, 'lat': lat, 'lng': lng})
                crossings.sort(key=lambda x: x['crossed_at'])
                result = {'vehicle_no': vno, 'crossings': crossings}
                cache_set(f'credit-route-{vno}', result, ttl=1800)
                self._json(result)
            except Exception as e:
                self._json({'crossings': [], 'error': str(e)})

        elif self.path.startswith('/api/set-sim'):
            qs     = parse_qs(urlparse(self.path).query)
            vno    = qs.get('vehicle_no', [''])[0].strip().upper()
            raw    = qs.get('msisdn', [''])[0].strip()
            driver = qs.get('driver_name', [''])[0].strip()
            if not vno:  self._json({'error': 'missing vehicle_no'}); return
            if not raw:  self._json({'error': 'missing msisdn'}); return
            msisdn = sim_location.normalize_msisdn(raw)
            if not msisdn:
                self._json({'error': 'invalid msisdn'}); return
            con = get_db()
            try:
                res = sim_location.assign_sim(con, vno, msisdn, driver)
            except Exception as e:
                release_db(con); self._json({'error': str(e)}); return
            release_db(con)
            cache_drop(f'truck-detail-{vno}', 'hundred-trucks')
            self._json({'status': 'assigned', 'vehicle_no': vno, 'msisdn': msisdn,
                        'driver_name': driver, 'mock': sim_location.MOCK_MODE, **res})

        elif self.path.startswith('/api/sim-list'):
            con = get_db()
            rows = sim_location.list_sims(con)
            release_db(con)
            lic = None
            if not sim_location.MOCK_MODE:
                try: lic = telenity.licence()
                except Exception: pass
            self._json({'sims': rows, 'licence': lic, 'mock': sim_location.MOCK_MODE})

        elif self.path.startswith('/api/sim-latest'):
            con = get_db()
            rows = sim_location.latest_pings(con)
            release_db(con)
            self._json({'trucks': rows})

        elif self.path.startswith('/api/remove-sim'):
            qs  = parse_qs(urlparse(self.path).query)
            vno = qs.get('vehicle_no', [''])[0].strip().upper()
            if not vno:
                self._json({'error': 'missing vehicle_no'}); return
            con = get_db()
            n = sim_location.release_sim(con, vno)
            release_db(con)
            cache_drop(f'truck-detail-{vno}', 'hundred-trucks')
            self._json({'status': 'removed', 'vehicle_no': vno, 'removed': n})

        elif self.path.startswith('/api/resend-consent'):
            qs  = parse_qs(urlparse(self.path).query)
            vno = qs.get('vehicle_no', [''])[0].strip().upper()
            if not vno:
                self._json({'error': 'missing vehicle_no'}); return
            con = get_db()
            res = sim_location.resend_consent(con, vno)
            release_db(con)
            self._json({'vehicle_no': vno, **res})

        elif self.path.startswith('/api/sweep-consents'):
            qs  = parse_qs(urlparse(self.path).query)
            vno = qs.get('vehicle_no', [''])[0].strip().upper() or None
            started = sim_location.sweep_consents(get_db, release=release_db, vehicle_no=vno)
            cache_drop('hundred-trucks', prefix='truck-detail-')
            self._json({'tracking_started': started, 'mock': sim_location.MOCK_MODE})

        elif self.path.startswith('/api/ping-sims'):
            qs  = parse_qs(urlparse(self.path).query)
            vno = qs.get('vehicle_no', [''])[0].strip().upper() or None
            done = sim_location.poll_sims(get_db, vno, release=release_db)
            if vno: cache_drop(f'truck-detail-{vno}', 'hundred-trucks')
            else:   cache_drop('hundred-trucks', prefix='truck-detail-')
            self._json({'pinged': done, 'mock': sim_location.MOCK_MODE})

        elif self.path == '/api/clear-cache':
            with _cache_lock:
                _cache.clear()
            self._json({'status': 'cache cleared'})

        elif self.path.startswith('/api/credit-trips'):
            cached = cache_get('credit-trips-raw')
            if cached:
                self._json_raw(cached); return
            import urllib.request as _ur
            try:
                url = 'https://www.zohoapis.in/creator/custom/mirchi-lime/Live_ML_Credit_Trips?publickey=u0nbdmwSP912Bdfm3DkFaqVB6'
                resp = _ur.urlopen(url, timeout=10)
                data = resp.read()
                cache_set('credit-trips-raw', data, ttl=300)
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Access-Control-Allow-Origin','*')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json({'error': str(e)})

        elif self.path.startswith('/api/hundred-all-crossings'):
            con = get_db(); cur = con.cursor()
            cur.execute('''
                SELECT c.vehicle_no, t.owner, t.state,
                       c.plaza, c.lat, c.lng, c.crossed_at
                FROM crossings c
                JOIN trucks t ON c.vehicle_no = t.vehicle_no
                WHERE c.lat IS NOT NULL AND c.lat != 0
                ORDER BY c.crossed_at
            ''')
            rows = fetchall(cur); release_db(con)
            self._json({'crossings': rows})

        elif self.path.startswith('/api/hundred-trucks'):
            cached = cache_get('hundred-trucks')
            if cached:
                self._json(cached)
            else:
                con = get_db(); cur = con.cursor()
                cur.execute('''
                    SELECT t.vehicle_no, t.owner, t.state, t.is_connected, t.fetched_at,
                           COUNT(DISTINCT c.plaza) as unique_plazas,
                           COUNT(c.id) as crossing_count,
                           EXISTS(SELECT 1 FROM truck_sims s WHERE s.vehicle_no = t.vehicle_no AND s.is_active) AS has_sim
                    FROM trucks t
                    LEFT JOIN crossings c ON t.vehicle_no = c.vehicle_no
                    GROUP BY t.vehicle_no, t.owner, t.state, t.is_connected, t.fetched_at
                    ORDER BY t.state, t.vehicle_no
                ''')
                rows = fetchall(cur)
                cur.execute('SELECT COUNT(*) as cnt FROM crossings')
                total_crossings = cur.fetchone()[0]
                cur.execute('SELECT COUNT(DISTINCT plaza) as cnt FROM crossings')
                total_plazas = cur.fetchone()[0]
                release_db(con)
                result = {'trucks': rows, 'total_crossings': total_crossings, 'total_plazas': total_plazas}
                cache_set('hundred-trucks', result, ttl=120)
                self._json(result)

        else:
            super().do_GET()

#||converts data to JSON and sends it back to the browser||
    def _json_raw(self, body):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

# ||tells the browser "yes you are allowed to talk to me||
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

#||keeps the terminal clean by silencing all request logs||
    def log_message(self, format, *args):
        pass

#||This block starts the server, opens the browser and keeps everything running.||
def ensure_enroute_trips_table():
    con = get_db(); cur = con.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS enroute_trips (
            id SERIAL PRIMARY KEY, truck_no TEXT NOT NULL, tranco TEXT, shipper TEXT,
            trip_id TEXT, indent_number TEXT, pickup_pin TEXT, drop_pin TEXT,
            loading_point TEXT, reporting_date TEXT, reporting_time TEXT, synced_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    cur.execute("ALTER TABLE enroute_trips ADD COLUMN IF NOT EXISTS reporting_time TEXT")
    con.commit(); release_db(con)

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ensure_enroute_trips_table()
    _sim_con = get_db(); sim_location.ensure_sim_tables(_sim_con); release_db(_sim_con)
    server = http.server.HTTPServer(('', PORT), Handler)
    print(f'100 Trucks server running at http://localhost:{PORT}')
    threading.Timer(1, lambda: webbrowser.open(f'http://localhost:{PORT}/hundred_trucks.html')).start()
    server.serve_forever()

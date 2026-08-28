#!/usr/bin/env python3
"""
Backend health test for hundred-trucks-analysis.onrender.com
Run: python3 test_backend.py
"""

import urllib.request, urllib.parse, json, sys, time
from datetime import datetime, timezone, timedelta

BASE = 'https://hundred-trucks-analysis.onrender.com'
IST  = timezone(timedelta(hours=5, minutes=30))

PASS = '\033[92m✓\033[0m'
FAIL = '\033[91m✗\033[0m'
WARN = '\033[93m⚠\033[0m'

results = []

def fetch(path, timeout=20):
    url = BASE + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {'__http_status__': e.code, **json.loads(e.read())}
        except:
            return {'__error__': f'HTTP {e.code}'}
    except Exception as e:
        return {'__error__': str(e)}

def check(name, passed, detail='', warn=False):
    icon = WARN if (warn and not passed) else (PASS if passed else FAIL)
    status = 'WARN' if (warn and not passed) else ('PASS' if passed else 'FAIL')
    print(f'  {icon}  {name}')
    if detail:
        print(f'       {detail}')
    results.append({'name': name, 'status': status})

def section(title):
    print(f'\n\033[1m── {title}\033[0m')

# ─────────────────────────────────────────────
section('1. Server Health')

d = fetch('/api/keep-alive')
check('Server is awake', '__error__' not in d, d.get('__error__','') or f"time={d.get('time','?')}")

# ─────────────────────────────────────────────
section('2. Trucks Endpoint')

d = fetch('/api/hundred-trucks')
ok = '__error__' not in d and 'trucks' in d
check('Endpoint returns trucks list', ok, d.get('__error__','') or f"{len(d.get('trucks',[]))} trucks total")

if ok:
    trucks = d['trucks']
    connected = [t for t in trucks if t.get('is_connected') in (1, True)]
    manual    = [t for t in trucks if t.get('is_manual') in (1, True)]
    check('At least one connected truck', len(connected) > 0, f"{len(connected)} connected")
    check('is_manual field present in response', all('is_manual' in t for t in trucks), 'needed for Manual tab')
    check('unique_plazas field present', all('unique_plazas' in t for t in trucks), 'needed to avoid "undefined plazas"')
    check('crossing_count field present', all('crossing_count' in t for t in trucks))
    check('Manual trucks in DB', len(manual) > 0, f"{len(manual)} manual: {[t['vehicle_no'] for t in manual]}", warn=True)

# ─────────────────────────────────────────────
section('3. Last Ping Freshness')

if ok:
    now_ist = datetime.now(IST)
    stale = []
    fresh = []
    no_ping = []
    for t in connected:
        fa = t.get('fetched_at')
        if not fa:
            no_ping.append(t['vehicle_no']); continue
        try:
            dt = datetime.fromisoformat(fa.replace(' ', 'T')).replace(tzinfo=IST)
            age_h = (now_ist - dt).total_seconds() / 3600
            if age_h > 2:
                stale.append((t['vehicle_no'], round(age_h, 1)))
            else:
                fresh.append(t['vehicle_no'])
        except:
            no_ping.append(t['vehicle_no'])

    check('All connected trucks pinged within 2 hours',
          len(stale) == 0,
          (f"Stale: {stale}" if stale else f"{len(fresh)} trucks are fresh") +
          (f" | No ping: {no_ping}" if no_ping else ''),
          warn=len(stale) > 0)

# ─────────────────────────────────────────────
section('4. Manual Truck: MP04HE4561')

d = fetch('/api/hundred-trucks')
mp = next((t for t in d.get('trucks', []) if t.get('vehicle_no') == 'MP04HE4561'), None)
check('MP04HE4561 exists in trucks', mp is not None)
if mp:
    check('MP04HE4561 is_connected=1', mp.get('is_connected') in (1, True), f"is_connected={mp.get('is_connected')}")
    check('MP04HE4561 is_manual=True',  mp.get('is_manual') in (1, True),  f"is_manual={mp.get('is_manual')}")

# ─────────────────────────────────────────────
section('5. Ping a Truck (on-demand)')

# pick first connected truck
if ok and connected:
    vno = connected[0]['vehicle_no']
    t0 = time.time()
    pd = fetch(f'/api/ping-truck?vehicle_no={urllib.parse.quote(vno)}', timeout=30)
    elapsed = round(time.time() - t0, 1)
    ping_ok = '__error__' not in pd and 'vehicle_no' in pd
    check(f'ping-truck works ({vno})', ping_ok,
          pd.get('__error__','') or f"new={pd.get('new',0)}, total={pd.get('total',0)}, took {elapsed}s")
else:
    check('ping-truck (skipped — no connected trucks)', False, warn=True)

# ─────────────────────────────────────────────
section('6. Tracking Link')

td = fetch('/api/make-token?vehicle_no=MP04HE4561')
token_ok = 'token' in td and len(td.get('token', '')) > 0
check('make-token returns token', token_ok, td.get('token', td.get('__error__','')))

if token_ok:
    token = td['token']
    track = fetch(f'/api/track?token={token}')
    check('track endpoint resolves token', '__error__' not in track and 'vehicle_no' in track,
          track.get('__error__','') or f"resolved to {track.get('vehicle_no')}")
    print(f'       Link: {BASE}/track.html?token={token}')

# ─────────────────────────────────────────────
section('7. Credit Trips')

cd = fetch('/api/credit-trips')
ct_ok = '__error__' not in cd
check('credit-trips endpoint responds', ct_ok, cd.get('__error__','') or f"{len(cd.get('trips', cd.get('trucks', [])))} trips")

# ─────────────────────────────────────────────
section('8. Remove-Manual Protects is_manual Trucks')

# We verify the logic without actually removing anything:
# just check the endpoint exists and rejects missing param
rm = fetch('/api/remove-manual')
check('remove-manual rejects empty vehicle_no',
      rm.get('__http_status__') == 400 and rm.get('error') == 'missing vehicle_no',
      f"got: {rm}")

# ─────────────────────────────────────────────
section('9. Driver SIM Tracking')

SIM_TRUCK  = 'MP04HE4561'
SIM_MSISDN = '919999999999'

sm = fetch('/api/set-sim')
check('set-sim rejects empty vehicle_no',
      sm.get('__http_status__') == 400 and sm.get('error') == 'missing vehicle_no',
      f"got: {sm}")

sm = fetch(f'/api/set-sim?vehicle_no={SIM_TRUCK}&msisdn=notanumber')
check('set-sim rejects a bad msisdn',
      sm.get('__http_status__') == 400 and sm.get('error') == 'invalid msisdn',
      f"got: {sm}")

sm = fetch(f'/api/set-sim?vehicle_no={SIM_TRUCK}&msisdn={SIM_MSISDN}&driver_name=Test+Driver', timeout=30)
sim_assigned = sm.get('status') == 'assigned'
check('set-sim assigns a driver SIM', sim_assigned,
      sm.get('__error__','') or f"msisdn={sm.get('msisdn')}, first ping={sm.get('pinged')}, "
                                f"mode={'MOCK' if sm.get('mock') else 'telenity'}")

if sim_assigned:
    if sm.get('mock'):
        check('Telenity credentials configured', False,
              'running on mock locations — set TELENITY_URL + TELENITY_KEY to go live', warn=True)

    d = fetch('/api/hundred-trucks')
    st = next((t for t in d.get('trucks', []) if t.get('vehicle_no') == SIM_TRUCK), None)
    check('has_sim flag exposed on trucks list', bool(st and st.get('has_sim')),
          f"has_sim={st.get('has_sim') if st else 'truck missing'}")

    pd = fetch(f'/api/ping-sims?vehicle_no={SIM_TRUCK}', timeout=30)
    check('ping-sims records a fix', pd.get('pinged', 0) >= 1,
          pd.get('__error__','') or f"pinged={pd.get('pinged')}")

    dd = fetch(f'/api/hundred-truck-detail?vehicle_no={SIM_TRUCK}')
    pings = dd.get('sim_pings') or []
    check('truck-detail carries the SIM trail', len(pings) >= 1, f"{len(pings)} pings")
    if pings:
        p = pings[0]
        check('ping rows are complete', all(k in p for k in ('lat','lng','address','source','pinged_at')),
              f"keys={sorted(p.keys())}")
        check('pinged_at parses like crossed_at',
              isinstance(p['pinged_at'], str) and len(p['pinged_at']) >= 19 and p['pinged_at'][4] == '-',
              f"pinged_at={p['pinged_at']!r}")
    check('truck-detail carries the SIM number', (dd.get('sim') or {}).get('msisdn') == SIM_MSISDN,
          f"sim={dd.get('sim')}")

    sl = fetch('/api/sim-list')
    row = next((r for r in sl.get('sims', []) if r.get('vehicle_no') == SIM_TRUCK), None)
    check('sim-list carries the driver SIM screen data', bool(row) and
          {'state','state_label','can_resend','resend_reason','valid_till'} <= set(row or {}),
          f"state={row.get('state') if row else 'row missing'}")
    if row:
        check('resend is blocked straight after assigning', not row['can_resend'],
              row.get('resend_reason',''))

    rc = fetch(f'/api/resend-consent?vehicle_no={SIM_TRUCK}')
    check('resend-consent refuses inside the cooldown', rc.get('ok') is False,
          rc.get('message',''))

    sw = fetch(f'/api/sweep-consents?vehicle_no={SIM_TRUCK}', timeout=30)
    check('sweep-consents runs', 'tracking_started' in sw,
          f"started={sw.get('tracking_started')}")

    ld = fetch('/api/sim-latest')
    vnos = [r.get('vehicle_no') for r in ld.get('trucks', [])]
    check('sim-latest returns one row per truck', len(vnos) == len(set(vnos)), f"{len(vnos)} rows")

    tk = fetch('/api/make-token?vehicle_no=' + SIM_TRUCK)
    if tk.get('token'):
        tr = fetch('/api/track?token=' + tk['token'])
        check('public track link carries the SIM trail', 'sim_pings' in tr,
              f"{len(tr.get('sim_pings') or [])} pings, has_sim={tr.get('has_sim')}")

    rm = fetch(f'/api/remove-sim?vehicle_no={SIM_TRUCK}')
    check('remove-sim clears the assignment', rm.get('status') == 'removed', f"got: {rm}")
    d = fetch('/api/hundred-trucks')
    st = next((t for t in d.get('trucks', []) if t.get('vehicle_no') == SIM_TRUCK), None)
    check('has_sim false again after removal', bool(st) and not st.get('has_sim'),
          f"has_sim={st.get('has_sim') if st else 'truck missing'}")

# ─────────────────────────────────────────────
total   = len(results)
passed  = sum(1 for r in results if r['status'] == 'PASS')
warned  = sum(1 for r in results if r['status'] == 'WARN')
failed  = sum(1 for r in results if r['status'] == 'FAIL')

print(f'\n\033[1m── Summary\033[0m')
print(f'  Total: {total}  |  \033[92mPass: {passed}\033[0m  |  \033[93mWarn: {warned}\033[0m  |  \033[91mFail: {failed}\033[0m')

if failed:
    print('\n  Failed tests:')
    for r in results:
        if r['status'] == 'FAIL':
            print(f'    ✗  {r["name"]}')

sys.exit(0 if failed == 0 else 1)

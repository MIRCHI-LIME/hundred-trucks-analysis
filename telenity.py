"""
Telenity SmartTrail API client.

Everything that talks to Telenity lives here; our own logic (schema, mock, polling)
stays in sim_location.py, so a change at their end touches one file.

Two hosts with two separate credentials and two token lifetimes:
  smarttrail.telenity.com   session token from /trail-rest/login, ~6 hours
  india-agw.telenity.com    OAuth bearer for the consent APIs, 1 hour

Note where their documentation is wrong — both verified against the live API:
  · /trail-rest/login takes "Authorization: Basic <key>", NOT the "Token:" header
    printed in section 4.1; that header returns 401.
  · the consent token endpoint needs Content-Type application/x-www-form-urlencoded,
    not the application/json shown in section 4.4.1.
  · the Delete API uses the same lowercase "token" header as every other call, not
    the "x-access-token" printed in section 4.6.
"""
import os, json, time, threading, urllib.request, urllib.error

TRAIL_HOST  = os.environ.get('TELENITY_TRAIL_HOST', 'https://smarttrail.telenity.com')
AGW_HOST    = os.environ.get('TELENITY_AGW_HOST',   'https://india-agw.telenity.com')
TRAIL_BASIC = os.environ.get('TELENITY_TRAIL_BASIC', '')   # Basic key for /trail-rest/login
AGW_BASIC   = os.environ.get('TELENITY_AGW_BASIC',   '')   # Basic key for the consent token
TIMEOUT     = int(os.environ.get('TELENITY_TIMEOUT', '25'))

CONFIGURED = bool(TRAIL_BASIC and AGW_BASIC)

# Session token lifetimes. Theirs are 6 h and 1 h; we refresh early and also on any 401.
_TRAIL_TTL = 5 * 3600
_AGW_MARGIN = 120

class TelenityError(Exception):
    """
    An API call failed in a way the caller should know about.
    str(e) includes the HTTP status and raw body — without that, a caller that
    only prints str(e) (server.py's except blocks do exactly this) shows nothing
    but a generic fallback message and the real cause is lost.
    """
    def __init__(self, message, status=None, body=None):
        self.status, self.body = status, body
        detail = f' [HTTP {status}] {json.dumps(body)[:300]}' if status is not None else ''
        super().__init__(f'{message}{detail}')

# Location result codes, section 4.5.4, mapped once so nothing downstream sees raw numbers.
RESULT_CODES = {
    0:        'Success',
    7:        'Subscription not found',
    26:       'Out of coverage',
    48:       'Phone switched off',
    50:       'Network error',
    53:       'Address not mapped',
    5044:     'Call barred',
    6044:     'Teleservice not provisioned',
    19840102: 'Number not added in the account',
}

def result_text(code):
    try:
        return RESULT_CODES.get(int(code), f'Unknown status {code}')
    except (TypeError, ValueError):
        return 'Unknown status'

# ── transport ────────────────────────────────────────────────────────────────

def _http(url, headers=None, method='GET', body=None, raw_body=False):
    data = None
    if body is not None:
        data = body.encode() if raw_body else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        text = resp.read().decode('utf-8', 'replace')
        return resp.status, (json.loads(text) if text.strip() else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, {'raw': text}
    except Exception as e:
        raise TelenityError(f'{method} {url} failed: {e}') from e

# ── tokens ───────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_trail = {'token': None, 'at': 0, 'customer_id': None}
_agw   = {'token': None, 'exp': 0}

def login(force=False):
    """Authenticate against SmartTrail. Returns the full response, cached."""
    with _lock:
        fresh = _trail['token'] and (time.time() - _trail['at']) < _TRAIL_TTL
        if fresh and not force:
            return _trail
        if not TRAIL_BASIC:
            raise TelenityError('TELENITY_TRAIL_BASIC is not set')
        status, body = _http(f'{TRAIL_HOST}/trail-rest/login',
                             {'Authorization': f'Basic {TRAIL_BASIC}',
                              'Content-Type': 'application/json'})
        if status != 200 or not body.get('token'):
            raise TelenityError('SmartTrail login failed', status, body)
        _trail.update(token=body['token'], at=time.time(),
                      customer_id=(body.get('customer') or {}).get('id'))
        return _trail

def customer_id():
    """Our tenant id — it lives at customer.id, which their doc sample omits."""
    return login()['customer_id']

def agw_token(force=False):
    """Bearer for the consent APIs."""
    with _lock:
        if _agw['token'] and time.time() < _agw['exp'] and not force:
            return _agw['token']
        if not AGW_BASIC:
            raise TelenityError('TELENITY_AGW_BASIC is not set')
        status, body = _http(f'{AGW_HOST}/oauth/token?grant_type=client_credentials',
                             {'Authorization': f'Basic {AGW_BASIC}', 'Accept': '*/*',
                              # their doc says application/json here; that returns 400
                              'Content-Type': 'application/x-www-form-urlencoded'},
                             method='POST')
        if status != 200 or not body.get('access_token'):
            raise TelenityError('Consent auth failed', status, body)
        _agw.update(token=body['access_token'],
                    exp=time.time() + int(body.get('expires_in', 3600)) - _AGW_MARGIN)
        return _agw['token']

def _trail_call(path, method='GET', body=None):
    """SmartTrail call with the session token, retried once if it has expired."""
    for attempt in (0, 1):
        headers = {'token': login(force=bool(attempt))['token'],
                   'Content-Type': 'application/json'}
        status, data = _http(f'{TRAIL_HOST}{path}', headers, method, body)
        if status == 401 and attempt == 0:
            continue
        return status, data

def _agw_call(path):
    for attempt in (0, 1):
        headers = {'Authorization': f'Bearer {agw_token(force=bool(attempt))}',
                   'Accept': '*/*', 'Cache-Control': 'no-cache',
                   'Content-Type': 'application/json'}
        status, data = _http(f'{AGW_HOST}{path}', headers)
        if status == 401 and attempt == 0:
            continue
        return status, data

# ── entities ─────────────────────────────────────────────────────────────────

def import_entity(msisdn, first_name='Driver', last_name='.'):
    """
    Register a driver. This is what sends the consent SMS.
    Returns {'entity_id', 'is_tracked'} — is_tracked is true only if consent
    already existed, e.g. the driver consented by IVR before being imported,
    or the number was already registered from an earlier import.
    """
    status, body = _trail_call('/trail-rest/entities/import', 'POST',
        {'entityImportList': [{'firstName': first_name, 'lastName': last_name,
                               'msisdn': msisdn}]})
    ok = (body.get('successList') or [None])[0] if status == 200 else None
    if not ok:
        fail = (body.get('failureList') or [{}])[0] if isinstance(body, dict) else {}
        # Telenity refuse to re-import a number that is already an entity on their
        # side (e.g. left over from earlier testing) — but the failure item still
        # carries the existing entityId and its current tracked state, so this is
        # recoverable rather than a real failure.
        if fail.get('entityId') and 'already exists' in str(fail.get('errorDesc', '')).lower():
            return {'entity_id': fail['entityId'], 'is_tracked': bool(fail.get('isTracked'))}
        msg = body.get('errorMessage') or fail.get('errorMessage') or 'import failed'
        raise TelenityError(msg, status, body)
    return {'entity_id': ok.get('entityId'), 'is_tracked': bool(ok.get('isTracked'))}

def entity_search(msisdn):
    """Look a number up. Returns the entity dict or None."""
    status, body = _trail_call(f'/trail-rest/entities?search={msisdn}')
    if status != 200:
        raise TelenityError('entity search failed', status, body)
    rows = body.get('data') or []
    return rows[0] if rows else None

def set_tracking(entity_id, tracked=True, active=True):
    """
    Switch tracking on or off. Only works once consent is allowed — a 403 means
    the driver has not consented yet, which is a normal state, not a fault.
    """
    status, body = _trail_call(f'/trail-rest/entities/{entity_id}', 'PUT',
                               {'isActive': active, 'isTracked': tracked})
    if status == 403:
        return {'ok': False, 'reason': 'consent not allowed yet'}
    if status == 429:
        return {'ok': False, 'reason': 'rate limited by operator, try later'}
    if status == 400:
        return {'ok': False, 'reason': (body.get('explanation') if isinstance(body, dict) else None)
                                       or 'rejected — consent expired, or too soon to reactivate'}
    if status != 200:
        raise TelenityError('modify failed', status, body)
    return {'ok': True, 'is_tracked': bool(body.get('isTracked'))}

def delete_entity(msisdn):
    """Remove a driver and release the licence (the release can take 24 hours)."""
    status, body = _trail_call('/trail-rest/tracking/remove', 'POST',
                               {'msisdnList': [msisdn]})
    if status == 200 and body.get('success'):
        return True
    raise TelenityError(body.get('errorMessage', 'delete failed'), status, body)

def licence():
    """Licence utilisation for our tenant."""
    status, body = _trail_call(f'/trail-rest/entities/licence-information/{customer_id()}')
    if status != 200:
        raise TelenityError('licence check failed', status, body)
    return {'total': body.get('totalLicence'), 'occupied': body.get('totalOccupiedNumber'),
            'used': body.get('utilizedLicence'), 'left': body.get('leftLicence')}

# ── consent ──────────────────────────────────────────────────────────────────

def consent_status(msisdn):
    """
    Consent state for a number. Note an unknown number also reports PENDING, so
    this cannot tell you whether a number was ever imported — our own record does.
    Returns {'status', 'expires_on', 'raw'}.
    """
    status, body = _agw_call(f'/apigw/NOFBconsent/v1/NOFBconsent?address=tel:+{msisdn}')
    if status == 400:
        return {'status': 'INVALID', 'expires_on': None, 'raw': body}
    if status != 200:
        raise TelenityError('consent check failed', status, body)
    node = (body or {}).get('Consent') or {}
    if not node and body.get('errorDescription'):
        return {'status': 'ERROR', 'expires_on': None, 'raw': body}
    return {'status': (node.get('status') or 'UNKNOWN').upper(),
            'expires_on': node.get('consentExpiresOn'),
            'raw': node}

def consent_allowed(status):
    """Both of Telenity's approval spellings mean the same thing to us."""
    return str(status).upper() in ('ALLOWED', 'CONSENT_APPROVED', 'ACTIVE')

def jio_reinitiate(msisdn):
    """
    Resend the consent SMS. Jio only — there is no equivalent for Airtel, Vi or
    BSNL, whose drivers use the IVR line instead. Repeated calls lock the number
    at the operator for 24 hours, so callers must rate-limit this themselves.
    """
    status, body = _agw_call(f'/apigw/NOFBconsent/v1/JIOReinitiate?address=tel:+{msisdn}')
    msg = (body or {}).get('data', '')
    return {'ok': status == 200, 'message': msg or f'HTTP {status}'}

# ── locations ────────────────────────────────────────────────────────────────

def _parse_terminal(node):
    cur = node.get('currentLocation') or {}
    code = node.get('locationResultStatus')
    retrieved = node.get('locationRetrievalStatus') == 'Retrieved'
    # A code of 0 normally means Success, but Telenity also send code 0 alongside
    # "Not Retrieved" while the first fix is still being computed — showing that
    # combination as status_text "Success" would read as a fix that never arrived.
    text = 'No fix yet — still being computed' if (not retrieved and not code) else result_text(code)
    return {
        'msisdn':     (node.get('address') or '').replace('tel:+', ''),
        'entity_id':  node.get('entityId'),
        'retrieved':  retrieved,
        'lat':        cur.get('latitude'),
        'lng':        cur.get('longitude'),
        'address':    cur.get('detailedAddress'),
        'timestamp':  cur.get('timestamp'),
        'status':     code,
        'status_text': text,
    }

def location(msisdn, last_result=True):
    """
    One driver's position. last_result=True gives the newest attempt, which may be
    a failure; False gives the last position that actually succeeded.
    """
    status, body = _trail_call(
        f'/trail-rest/location/msisdnList/{msisdn}?lastResult={str(bool(last_result))}')
    if status != 200:
        raise TelenityError('location failed', status, body)
    rows = body.get('terminalLocation') or []
    if not rows:
        errs = body.get('errorMessageList') or ['not found']
        return {'msisdn': msisdn, 'retrieved': False, 'lat': None, 'lng': None,
                'address': None, 'timestamp': None, 'status': None,
                'status_text': errs[0], 'entity_id': None}
    return _parse_terminal(rows[0])

def bulk_locations(limit=200, offset=0, last_result=True, tracked=None):
    """Every driver in the account in one call. Limit is capped at 200 by Telenity."""
    limit = max(1, min(int(limit), 200))
    path = (f'/trail-rest/location/msisdnList/?limit={limit}&offset={offset}'
            f'&lastResult={str(bool(last_result)).lower()}')
    if tracked is not None:
        path += f'&tracked={str(bool(tracked)).lower()}'
    status, body = _trail_call(path)
    if status != 200:
        raise TelenityError('bulk location failed', status, body)
    out = []
    for row in body.get('locationAdditionalInfoList') or []:
        term = row.get('terminalLocation') or {}
        fix = _parse_terminal(term)
        fix['device_name'] = row.get('deviceName')
        fix['speed_kmh']   = row.get('averageSpeed')
        fix['distance_km'] = row.get('traveledDistance')
        out.append(fix)
    return out

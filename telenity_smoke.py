#!/usr/bin/env python3
"""
Walk one real driver through the whole Telenity lifecycle, one step at a time.

    python3 telenity_smoke.py 919876543210 "Ramesh Kumar"     # full walkthrough
    python3 telenity_smoke.py 919876543210 --status           # just check where it is
    python3 telenity_smoke.py 919876543210 --remove           # delete, free the licence

Touches Telenity only — it does not read or write our database, so nothing here
can disturb the dashboard. Importing sends a real SMS and consumes a licence, so
that step always asks first.
"""
import sys, time
from dotenv import load_dotenv
load_dotenv()
import telenity

G, Y, R, B, X = '\033[92m', '\033[93m', '\033[91m', '\033[1m', '\033[0m'

def say(msg='', colour=''):  print(f'{colour}{msg}{X}', flush=True)
def step(n, msg):            say(f'\n{B}{n}. {msg}{X}')

def show_licence():
    lic = telenity.licence()
    colour = R if lic['left'] == 0 else (Y if lic['left'] <= 2 else G)
    say(f"   licences: {colour}{lic['left']} free{X} of {lic['total']}  ({lic['used']} in use)")
    return lic

def show_consent(msisdn):
    c = telenity.consent_status(msisdn)
    allowed = telenity.consent_allowed(c['status'])
    say(f"   consent: {(G if allowed else Y)}{c['status']}{X}"
        + (f"  ·  expires {c['expires_on']}" if c.get('expires_on') else ''))
    return c

def show_location(msisdn, last_result=True):
    fix = telenity.location(msisdn, last_result=last_result)
    if fix['retrieved']:
        say(f"   {G}position{X}: {fix['lat']}, {fix['lng']}")
        if fix.get('address'): say(f"   place   : {fix['address']}")
        say(f"   time    : {fix['timestamp']}")
        say(f"   {B}compare with where they actually are:{X}")
        say(f"   https://www.google.com/maps?q={fix['lat']},{fix['lng']}")
    else:
        say(f"   {Y}no fix yet{X}: {fix['status_text']}")
    return fix

def main():
    if len(sys.argv) < 2:
        say(__doc__); return 1
    msisdn = ''.join(ch for ch in sys.argv[1] if ch.isdigit())
    if len(msisdn) == 10: msisdn = '91' + msisdn
    if len(msisdn) != 12:
        say(f'{R}That does not look like an Indian mobile: {sys.argv[1]}{X}'); return 1
    rest = sys.argv[2:]
    name = next((a for a in rest if not a.startswith('--')), 'Test Driver')

    if not telenity.CONFIGURED:
        say(f'{R}No Telenity credentials — check .env{X}'); return 1

    say(f'{B}Telenity walkthrough for +{msisdn}{X}')
    who = telenity.login()
    say(f"   account: {who['customer_id']}")

    # ── status only ──────────────────────────────────────────────────────────
    if '--status' in rest:
        step(1, 'Where this number stands')
        show_licence()
        found = telenity.entity_search(msisdn)
        say(f"   registered: {(G+'yes'+X) if found else (Y+'no — never imported'+X)}"
            + (f"  (entity {found['id']}, tracked={found.get('tracked')})" if found else ''))
        show_consent(msisdn)
        if found: show_location(msisdn)
        return 0

    # ── remove ───────────────────────────────────────────────────────────────
    if '--remove' in rest:
        step(1, 'Removing the driver and freeing the licence')
        found = telenity.entity_search(msisdn)
        if found and found.get('id'):
            telenity.set_tracking(found['id'], tracked=False)
        telenity.delete_entity(msisdn)
        say(f'   {G}removed{X} — note the licence can take up to 24 hours to come back')
        show_licence()
        return 0

    # ── full walkthrough ─────────────────────────────────────────────────────
    step(1, 'Licences before we start')
    if show_licence()['left'] <= 0:
        say(f'{R}   No licences free — remove one first.{X}'); return 1

    step(2, 'Register the driver')
    say(f'   This sends {B}+{msisdn}{X} a real SMS and uses one licence.')
    say(f'   Make sure {B}{name}{X} is expecting it and knows to reply {B}Y{X}.')
    if input('   Type "yes" to continue: ').strip().lower() != 'yes':
        say('   stopped, nothing sent'); return 0
    try:
        res = telenity.import_entity(msisdn, name.split()[0], ' '.join(name.split()[1:]) or 'Driver')
    except telenity.TelenityError as e:
        say(f'{R}   import failed: {e}{X}'); return 1
    say(f"   {G}registered{X}  entity {res['entity_id']}  ·  already tracked: {res['is_tracked']}")
    if res['is_tracked']:
        say(f'   {G}Consent was already on file{X} — they must have used the IVR line before now.')

    step(3, 'Waiting for the driver to reply')
    say('   Ask them to reply Y to the SMS, or call the IVR line and press 1.')
    say('   Checking every 15 seconds; Ctrl-C to stop and resume later with --status.')
    allowed, waited = res['is_tracked'], 0
    while not allowed and waited < 600:
        time.sleep(15); waited += 15
        c = telenity.consent_status(msisdn)
        allowed = telenity.consent_allowed(c['status'])
        say(f'   {waited:>3}s  {c["status"]}')
    if not allowed:
        say(f'{Y}   No consent after 10 minutes.{X} Not a failure — IVR consent can take 15 minutes')
        say('   to register. Re-run with --status later to check.')
        return 0
    say(f'   {G}consent received{X}')

    step(4, 'Switching tracking on')
    entity_id = res['entity_id'] or (telenity.entity_search(msisdn) or {}).get('id')
    out = telenity.set_tracking(entity_id, tracked=True)
    if not out['ok']:
        say(f'{R}   could not enable: {out["reason"]}{X}'); return 1
    say(f'   {G}tracking enabled{X}')

    step(5, 'Waiting for the first position')
    say('   Telenity say the first fix takes 2 to 3 minutes.')
    fix, waited = None, 0
    while waited < 300:
        fix = show_location(msisdn)
        if fix['retrieved']:
            break
        time.sleep(30); waited += 30
        say(f'   … {waited}s')
    say('')
    if fix and fix['retrieved']:
        say(f'{B}   Now check the accuracy.{X} Open the maps link above and compare it')
        say('   with where the driver is actually standing. That answers the one')
        say('   question Telenity have not: whether this is metres or kilometres.')
    say(f'\n   Done. {B}--status{X} re-checks, {B}--remove{X} frees the licence.')
    show_licence()
    return 0

if __name__ == '__main__':
    sys.exit(main())

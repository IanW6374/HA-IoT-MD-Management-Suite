#!/usr/bin/env python3
"""Small ingress-ready Home Assistant add-on for managing IoTMD v2 devices."""

import json
import os
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_DIRECTORY = Path(__file__).resolve().parent
if str(APP_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(APP_DIRECTORY))

from fleet_repository import FleetRepository
from fleet_policy import PolicySigner
from fleet_service import FleetController


DATA_DIRECTORY = Path(os.environ.get('IOTMD_MANAGEMENT_DATA', '/data'))
OPTIONS_PATH = DATA_DIRECTORY / 'options.json'
STATE_PATH = DATA_DIRECTORY / 'fleet.db'
SIGNING_KEY_PATH = DATA_DIRECTORY / 'fleet-signing-key.pem'
PUBLIC_KEY_PATH = DATA_DIRECTORY / 'fleet-verification-key.bin'


def bounded_text(value, maximum=256):
    return str(value or '')[:maximum]


class FleetStore(FleetRepository):
    """Fleet repository with the add-on's standard database location."""

    def __init__(self, path=STATE_PATH, event_retention=5000, now=None):
        super().__init__(path, event_retention=event_retention, now=now)


HTML = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>IoTMD Management Suite</title><style>
:root{font-family:system-ui;color:#172830;background:#edf3f5}body{margin:0;padding:24px}main{max-width:1400px;margin:auto}
h1{font-size:2.4rem}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}.card{background:white;border:1px solid #cedde2;border-radius:16px;padding:18px;box-shadow:0 5px 18px #17313b12}
.ok{color:#14864a}.bad{color:#b82b2b}.muted{color:#60727b}button{background:#087f8c;color:white;border:0;border-radius:9px;padding:10px 14px;font-weight:700}input,select{width:100%;box-sizing:border-box;padding:9px;margin:4px 0 10px;border:1px solid #b9cbd1;border-radius:8px}label{font-weight:650}.row{display:flex;gap:12px;align-items:center;justify-content:space-between}.events{max-height:300px;overflow:auto}code{font-size:.82rem}@media(max-width:600px){body{padding:12px}.row{align-items:stretch;flex-direction:column}}
</style></head><body><main><div class="row"><div><p class="muted">HOME ASSISTANT ADD-ON</p><h1>IoTMD Management Suite</h1></div><div><a href="releases/">Release files</a> <button onclick="refresh()">Refresh</button></div></div>
<section><h2>Devices</h2><div id="devices" class="cards"></div></section>
<section class="card"><h2>Register device</h2><form id="register"><div class="cards"><label>ID<input name="id" required></label><label>Name<input name="name"></label><label>Host<input name="host" required></label><label>Port<input name="port" type="number" value="8444"></label><label>CA path<input name="ca_path" value="/ssl/iotmd-ca.pem" required></label><label>Client certificate path<input name="cert_path" value="/ssl/iotmd-fleet.pem" required></label><label>Client key path<input name="key_path" value="/ssl/iotmd-fleet-key.pem" required></label><label>Cohort<input name="cohort" value="default"></label></div><button>Register</button></form></section>
<section class="card"><h2>Policy / command</h2><form id="policy"><div class="cards"><label>Device<select id="policy-device" name="device_id"></select></label><label>Channel<select name="channel"><option>alpha</option><option>beta</option><option>stable</option></select></label><label>Command<select name="action"><option value="">No command</option><option>check-update</option><option>download-update</option><option>activate-update</option><option>rollback</option></select></label><label>Start minute<input name="start_minute" type="number" value="120"></label><label>Duration minutes<input name="duration_minutes" type="number" value="120"></label><label>Maximum failures<input name="maximum_failures" type="number" value="2"></label></div><button>Sign and apply policy</button></form><pre id="result"></pre></section>
<section class="card"><h2>Staged rollout</h2><form id="rollout"><div class="cards"><label>Release sequence<input name="release_sequence" type="number" min="1" required></label><label>Ordered cohorts<input name="cohorts" value="canary,main" required></label><label>Failure stop threshold<input name="maximum_failures" type="number" min="1" value="1"></label><label>Channel<select name="channel"><option>alpha</option><option>beta</option><option>stable</option></select></label></div><button>Create rollout</button></form><div id="rollouts" class="cards"></div></section>
<section class="card"><h2>Fleet verification key</h2><p>Provision this public key on each enrolled device. It is independent from update signing.</p><a href="api/fleet-public-key">Download raw public key</a></section>
</main><script>
async function api(path,options){let r=await fetch(path,options);if(!r.ok)throw new Error(await r.text());return r.json()}function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refresh(){let data=await api('api/devices'),rolloutData=await api('api/rollouts'),box=document.getElementById('devices'),select=document.getElementById('policy-device'),rollouts=document.getElementById('rollouts');box.innerHTML='';select.innerHTML='';rollouts.innerHTML='';for(let d of data.devices){let seen=d.last_seen?new Date(d.last_seen*1000).toLocaleString():'Never',healthy=!d.last_error;box.innerHTML+=`<article class="card"><div class="row"><strong>${esc(d.name)}</strong><span class="${healthy?'ok':'bad'}">${healthy?'Healthy':'Unavailable'}</span></div><p><code>${esc(d.id)}</code></p><p>${esc(d.host)}:${d.port}</p><p class="muted">Last seen: ${esc(seen)}</p><p class="bad">${esc(d.last_error)}</p><p>Application: ${esc(d.inventory?.device?.application_version||'unknown')}</p><p>Cohort: ${esc(d.cohort)}</p></article>`;select.innerHTML+=`<option value="${esc(d.id)}">${esc(d.name)}</option>`}for(let r of rolloutData.rollouts){let cohort=r.cohorts[r.cohort_index]||'complete';rollouts.innerHTML+=`<article class="card"><div class="row"><strong>${esc(r.id)}</strong><span class="${r.status==='stopped'?'bad':'ok'}">${esc(r.status)}</span></div><p>Release ${esc(r.release_sequence)} · ${esc(cohort)}</p><p>${esc(r.successes)} complete / ${esc(r.failures)} failed</p><button onclick="rolloutAction('${esc(r.id)}','dispatch')">Dispatch active cohort</button> <button onclick="rolloutAction('${esc(r.id)}','advance')">Advance</button></article>`}}document.getElementById('register').onsubmit=async e=>{e.preventDefault();let o=Object.fromEntries(new FormData(e.target));o.port=Number(o.port);await api('api/devices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});refresh()};document.getElementById('policy').onsubmit=async e=>{e.preventDefault();let o=Object.fromEntries(new FormData(e.target));o.start_minute=Number(o.start_minute);o.duration_minutes=Number(o.duration_minutes);o.maximum_failures=Number(o.maximum_failures);if(o.action)o.command={action:o.action,release_sequence:0};delete o.action;try{document.getElementById('result').textContent=JSON.stringify(await api('api/policy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)}),null,2)}catch(x){document.getElementById('result').textContent=x.message}};document.getElementById('rollout').onsubmit=async e=>{e.preventDefault();let o=Object.fromEntries(new FormData(e.target));o.release_sequence=Number(o.release_sequence);o.maximum_failures=Number(o.maximum_failures);o.cohorts=o.cohorts.split(',').map(x=>x.trim()).filter(Boolean);await api('api/rollouts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});refresh()};async function rolloutAction(id,action){try{await api('api/rollouts/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});refresh()}catch(x){document.getElementById('result').textContent=x.message}}refresh();setInterval(refresh,60000)
</script></body></html>'''


def read_options():
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except Exception:
        return {}


OPTIONS = read_options()
STORE = FleetStore(event_retention=int(OPTIONS.get('event_retention', 5000)))
SIGNER = PolicySigner(SIGNING_KEY_PATH, PUBLIC_KEY_PATH)
CONTROLLER = FleetController(
    STORE, SIGNER, timeout=int(OPTIONS.get('request_timeout_s', 10))
)


class Handler(BaseHTTPRequestHandler):
    server_version = 'IoTMDFleet/2-alpha'

    def _json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        if length <= 0 or length > 131072:
            raise ValueError('request body size is invalid')
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError('request body must be an object')
        return value

    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/') or '/'
        try:
            if path == '/':
                body = HTML.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == '/api/devices':
                self._json(200, {'devices': STORE.list_devices()})
            elif path == '/api/events':
                self._json(200, {'events': STORE.list_events(500)})
            elif path == '/api/rollouts':
                self._json(200, {'rollouts': STORE.list_rollouts()})
            elif path == '/api/fleet-public-key':
                body = PUBLIC_KEY_PATH.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Disposition', 'attachment; filename="fleet-verification-key.bin"')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == '/health':
                self._json(200, {
                    'status': 'ok', 'devices': STORE.count_devices(),
                    'storage': 'sqlite',
                })
            else:
                self._json(404, {'error': 'not found'})
        except Exception as exc:
            self._json(500, {'error': bounded_text(exc)})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')
        try:
            request = self._body()
            if path == '/api/devices':
                result = STORE.register(request)
                threading.Thread(
                    target=CONTROLLER.poll_device, args=(result['id'],), daemon=True
                ).start()
                self._json(201, result)
            elif path == '/api/policy':
                self._json(202, CONTROLLER.apply_policy(request))
            elif path == '/api/poll':
                CONTROLLER.poll_device(request.get('device_id', ''))
                self._json(200, {'status': 'complete'})
            elif path == '/api/rollouts':
                self._json(201, STORE.create_rollout(request))
            elif path == '/api/rollouts/dispatch':
                rollout_id = str(request.get('id', ''))
                if not STORE.get_rollout(rollout_id):
                    raise ValueError('rollout does not exist')
                self._json(202, STORE.enqueue_job(
                    'rollout', rollout_id,
                    idempotency_key=str(
                        request.get('idempotency_key') or
                        ('rollout:' + rollout_id + ':' + str(int(time.time())))
                    )
                ))
            elif path == '/api/rollouts/result':
                self._json(200, STORE.record_rollout_result(
                    request.get('id', ''), request.get('device_id', ''),
                    request.get('result', ''), request.get('detail', '')
                ))
            elif path == '/api/rollouts/advance':
                self._json(200, STORE.advance_rollout(request.get('id', '')))
            else:
                self._json(404, {'error': 'not found'})
        except (ValueError, KeyError) as exc:
            self._json(400, {'error': bounded_text(exc)})
        except urllib.error.HTTPError as exc:
            self._json(exc.code, {'error': bounded_text(exc.read().decode())})
        except Exception as exc:
            self._json(502, {'error': bounded_text(exc)})

    def log_message(self, pattern, *args):
        print('%s - %s' % (self.address_string(), pattern % args), flush=True)


def poll_loop():
    interval = max(10, int(OPTIONS.get('poll_interval_s', 60)))
    while True:
        for identifier in STORE.device_ids(enabled_only=True):
            STORE.enqueue_job(
                'poll', identifier, idempotency_key=(
                    'poll:' + identifier + ':' + str(int(time.time()) // interval)
                )
            )
        time.sleep(interval)


def job_loop():
    while True:
        job = STORE.claim_job()
        if job is None:
            time.sleep(1)
            continue
        try:
            if job['kind'] == 'poll':
                CONTROLLER.poll_device(job['target'])
            elif job['kind'] == 'rollout':
                CONTROLLER.dispatch_rollout(job['target'])
            else:
                raise ValueError('unsupported fleet job: ' + str(job['kind']))
        except Exception as exc:
            STORE.fail_job(job['id'], exc)
        else:
            STORE.complete_job(job['id'])


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=job_loop, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 8098), Handler).serve_forever()


if __name__ == '__main__':
    main()

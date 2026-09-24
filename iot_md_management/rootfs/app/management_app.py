#!/usr/bin/env python3
"""Small ingress-ready Home Assistant app for managing IoT MD v2 devices."""

import json
import os
import hashlib
import html
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

APP_DIRECTORY = Path(__file__).resolve().parent
if str(APP_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(APP_DIRECTORY))

from fleet_repository import FleetRepository
from fleet_policy import PolicySigner
from fleet_service import FleetController
from configuration_profiles import normalize_profile
from release_catalog import ArtifactVerifier, CatalogSigner, ReleaseCatalog


DATA_DIRECTORY = Path(os.environ.get('IOT_MD_MANAGEMENT_DATA', '/data'))
OPTIONS_PATH = DATA_DIRECTORY / 'options.json'
STATE_PATH = DATA_DIRECTORY / 'fleet.db'
SIGNING_KEY_PATH = DATA_DIRECTORY / 'fleet-signing-key.pem'
PUBLIC_KEY_PATH = DATA_DIRECTORY / 'fleet-verification-key.bin'
RELEASE_STATE_PATH = DATA_DIRECTORY / 'release-inventory.json'
RELEASE_ROOT = Path(os.environ.get('IOT_MD_RELEASE_ROOT', '/share/iot-md-releases'))
TRUSTED_UPDATE_KEY_PATH = APP_DIRECTORY / 'iot-md-update-verification-key.hex'


def bounded_text(value, maximum=256):
    return str(value or '')[:maximum]


class FleetStore(FleetRepository):
    """Fleet repository with the add-on's standard database location."""

    def __init__(self, path=STATE_PATH, event_retention=5000, now=None):
        super().__init__(path, event_retention=event_retention, now=now)


HTML = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>IoT MD Management Suite</title><style>
:root{color-scheme:light dark;--bg:#eef3f5;--panel:#fff;--ink:#17262d;--muted:#61727a;--line:#d8e3e7;--accent:#087e8b;--accent-dark:#05606a;--good:#188754;--bad:#b53333;--shadow:0 12px 34px rgba(17,42,52,.08)}
@media(prefers-color-scheme:dark){:root{--bg:#10181c;--panel:#182329;--ink:#edf5f7;--muted:#a8b8bf;--line:#304149;--accent:#36b7c5;--accent-dark:#29949f;--good:#52cc8a;--bad:#ee7474;--shadow:0 12px 34px rgba(0,0,0,.24)}}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.topbar{position:sticky;z-index:5;top:0;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:13px clamp(16px,4vw,38px);border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--panel) 92%,transparent);backdrop-filter:blur(10px)}.brand{display:flex;align-items:center;gap:11px;color:var(--ink);font-weight:800;text-decoration:none}.brand-mark{display:grid;place-items:center;width:38px;height:38px;border-radius:11px;background:linear-gradient(145deg,var(--accent),var(--accent-dark));color:#fff;font-size:.75rem;letter-spacing:.06em}.topbar nav{display:flex;gap:4px;flex-wrap:wrap}.topbar nav a{padding:7px 10px;border-radius:8px;color:var(--muted);font-weight:650;text-decoration:none}.topbar nav a:hover,.topbar nav a.active{background:var(--bg);color:var(--ink)}main{width:min(1400px,calc(100% - 32px));margin:0 auto;padding:42px 0 68px}.hero{margin:0 0 28px}.hero h1{max-width:820px;margin:3px 0 9px;font-size:clamp(2rem,5vw,3.25rem);line-height:1.05;letter-spacing:-.035em}.hero p:not(.eyebrow){max-width:760px;color:var(--muted);font-size:1.02rem}.split,.row,.section-head{display:flex;align-items:center;justify-content:space-between;gap:16px}.split{align-items:flex-end;gap:24px}.eyebrow{margin:0!important;color:var(--accent)!important;font-size:.74rem!important;font-weight:800;letter-spacing:.12em;text-transform:uppercase}h2{margin:0 0 10px;font-size:1.15rem}.section-head{align-items:flex-end;margin-bottom:12px}.section-head h2{margin:0}.cards,.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.status-grid{margin-bottom:22px}.status-card{padding:20px;border:1px solid var(--line);border-radius:16px;background:var(--panel);box-shadow:var(--shadow)}.status-card span{display:block;color:var(--muted);font-size:.76rem;font-weight:800;text-transform:uppercase}.status-card strong{display:block;margin-top:5px;font-size:1.75rem}.card{margin-bottom:18px;padding:23px;border:1px solid var(--line);border-radius:16px;background:var(--panel);box-shadow:var(--shadow)}#devices{margin-bottom:18px}.card p{color:var(--muted)}.actions{display:flex;gap:9px;flex-wrap:wrap}.button,button{display:inline-flex;align-items:center;justify-content:center;padding:10px 14px;border:1px solid var(--accent);border-radius:9px;background:var(--accent);color:#fff;font:inherit;font-weight:750;text-decoration:none;cursor:pointer}.button:hover,button:hover{background:var(--accent-dark)}.button.secondary,button.secondary{border-color:var(--line);background:transparent;color:var(--ink)}label{display:grid;gap:6px;font-weight:700}input,select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--ink);font:inherit}input:focus,select:focus{border-color:var(--accent);outline:3px solid color-mix(in srgb,var(--accent) 18%,transparent)}form>button{margin-top:14px}.ok{color:var(--good)}.bad{color:var(--bad)}.muted{color:var(--muted)}.hidden{display:none!important}dl{display:grid;grid-template-columns:minmax(160px,1fr) 2fr;margin:0}dt,dd{padding:10px 0;border-bottom:1px solid var(--line)}dt{color:var(--muted);font-weight:750}dd{margin:0;overflow-wrap:anywhere}pre{overflow:auto}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86em}section{scroll-margin-top:82px}@media(max-width:760px){.topbar{position:static;align-items:flex-start;flex-direction:column}.topbar nav{width:100%;overflow:auto;flex-wrap:nowrap}main{padding-top:28px}.split,.row{align-items:flex-start;flex-direction:column}.cards,.status-grid{grid-template-columns:1fr}dl{grid-template-columns:1fr}dd{padding-top:0}}
.release-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:16px}.release-card{min-width:0;margin:0}.release-card .revision{min-width:0;margin:14px 0}.release-card .revision code{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.release-card label{margin-top:16px}.hero>.actions{justify-content:flex-end}@media(max-width:760px){.release-grid{grid-template-columns:1fr}.hero>.actions{justify-content:flex-start}}
.button.danger,button.danger{border-color:var(--bad);background:transparent;color:var(--bad)}.button.danger:hover,button.danger:hover{background:var(--bad);color:#fff}.check{display:flex;align-items:center;gap:9px}.check input{width:auto}.device-edit{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}[data-page-section]{display:none}body[data-page="overview"] [data-page-section="overview"],body[data-page="releases"] [data-page-section="releases"],body[data-page="devices"] [data-page-section="devices"],body[data-page="deployments"] [data-page-section="deployments"],body[data-page="profiles"] [data-page-section="profiles"],body[data-page="settings"] [data-page-section="settings"]{display:block}
</style></head><body data-page="__PAGE__"><header class="topbar"><a class="brand" href="./"><span class="brand-mark">MD</span><span>IoT MD Management Suite</span></a><nav aria-label="Primary"><a data-page-link="overview" href="./">Overview</a><a data-page-link="releases" href="releases">Releases</a><a data-page-link="devices" href="devices">Devices</a><a data-page-link="deployments" href="deployments">Deployments</a><a data-page-link="profiles" href="profiles">Profiles</a><a data-page-link="settings" href="settings">Settings</a></nav></header><main>
<div data-page-section="overview"><section class="hero split"><div><p class="eyebrow">Home Assistant</p><h1>IoT MD management at a glance</h1><p>Fleet inventory, signed policy, staged rollout and secure release management.</p></div><button type="button" onclick="refresh()">Refresh</button></section><section class="status-grid" aria-label="Management summary"><div class="status-card"><span>Devices</span><strong id="summary-devices">—</strong></div><div class="status-card"><span>Verified releases</span><strong id="summary-releases">—</strong></div><div class="status-card"><span>Active rollouts</span><strong id="summary-rollouts">—</strong></div><div class="status-card"><span>GitHub sync</span><strong id="summary-sync">—</strong></div></section><section class="card"><p class="eyebrow">Purpose</p><h2>One trusted management plane</h2><p>Register devices, publish signed policy and catalogs, and monitor staged deployments from the focused tabs above.</p></section></div>
<div data-page-section="releases"><section class="hero split"><div><p class="eyebrow">Supply chain</p><h1>Verified releases</h1><p>Synchronize tagged IoT-Modular-Device releases, verify every artifact, and assign each trusted version to one release channel.</p></div><div class="actions"><a class="button secondary" href="releases/">Browse release files</a><button id="release-sync" type="button" onclick="syncReleases()">Synchronize GitHub Releases</button></div></section><section><div class="section-head"><h2>Release inventory</h2></div><p id="release-status" class="muted"></p><div id="releases" class="release-grid"></div></section></div>
<div data-page-section="devices"><section class="hero"><p class="eyebrow">Fleet</p><h1>Devices</h1><p>Register mutually authenticated IoT-MD devices and inspect their latest reported state.</p></section><div id="devices" class="cards"></div><section class="card"><p class="eyebrow">Enrollment</p><h2>Register device</h2><p>Mutual-TLS credentials are configured once in the Home Assistant add-on configuration. The management ID is a local label; fleet policy uses the immutable device identity discovered during polling.</p><form id="register"><div class="cards"><label>Management ID<input name="id" placeholder="IoT-MD-002" required></label><label>Name<input name="name" placeholder="Workshop controller"></label><label>Host<input name="host" placeholder="iot-md-002.local (without https://)" required></label><label>Port<input name="port" type="number" value="8444" placeholder="8444"></label><label>Cohort<input name="cohort" value="default" placeholder="default or canary"></label></div><button>Register</button></form></section></div>
<div data-page-section="deployments"><section class="hero"><p class="eyebrow">Software deployment</p><h1>Deploy a verified release</h1><p>Choose what to install and where. IoT MD signs and enforces the underlying device policy automatically.</p></section><section class="card"><form id="policy"><div class="cards"><label>Device<select id="policy-device" name="device_id" required></select></label><label>Release<select id="deployment-release" name="release" required></select></label><label>Deployment action<select name="deployment_action"><option value="stage">Stage for later installation</option><option value="install">Stage and install in the maintenance window</option></select></label><label>Maintenance start<input name="start_time" type="time" value="02:00" required></label><label>Maintenance end<input name="end_time" type="time" value="04:00" required></label><label>Stop after failures<input name="maximum_failures" type="number" min="1" max="20" value="1"></label></div><p class="muted">Times use the selected device's configured local timezone. An end time before the start time continues into the next day.</p><button>Deploy release</button></form><pre id="result"></pre></section><section><div class="section-head"><h2>Controlled fleet deployments</h2></div><p class="muted">The ordered cohort list is derived from the cohorts assigned to enabled devices.</p><details class="card"><summary>Advanced cohort deployment</summary><form id="rollout"><div class="cards"><label>Release<select id="rollout-release" name="release" required></select></label><label>Ordered cohorts<input id="rollout-cohorts" name="cohorts" required></label><label>Stop after failures<input name="maximum_failures" type="number" min="1" value="1"></label></div><button>Create controlled deployment</button></form></details><div id="rollouts" class="cards"></div></section></div>
<div data-page-section="profiles"><section class="hero"><p class="eyebrow">Standard configuration</p><h1>Configuration profiles</h1><p>Save a reusable set of non-secret operational settings and apply it to a managed device.</p></section><section class="card"><p class="eyebrow">Profile editor</p><h2>Create or update a profile</h2><form id="profile-editor"><div class="cards"><label>Name<input name="name" maxlength="64" required placeholder="Standard production"></label><label>Description<input name="description" maxlength="256" placeholder="Common production settings"></label><label>Time zone<input name="timezone_name" value="Europe/London" required></label><label>NTP servers<input name="ntp_servers" value="pool.ntp.org,time.google.com" required></label><label>Log level<select name="loglevel"><option>INFO</option><option>ERROR</option><option>DEBUG</option></select></label><label>Log buffer lines<input name="log_buffer_lines" type="number" min="50" max="2000" value="200"></label><label>Home Assistant discovery prefix<input name="ha_discovery_prefix" value="homeassistant"></label><label class="check"><input name="ha_discovery" type="checkbox" checked>Enable Home Assistant discovery</label><label>MQTT server<input name="mqtt_server" placeholder="mqtt.local"></label><label>MQTT port<input name="mqtt_port" type="number" min="1" max="65535" value="8883"></label><label>MQTT username<input name="mqtt_username"></label><label>MQTT base topic<input name="mqtt_base_topic" value="iotmd"></label><label>MQTT QoS<select name="mqtt_qos"><option value="0">0</option><option value="1">1</option></select></label><label class="check"><input name="mqtt_enabled" type="checkbox">Enable MQTT</label><label>Syslog host<input name="syslog_host" placeholder="syslog.local"></label><label>Syslog port<input name="syslog_port" type="number" min="1" max="65535" value="514"></label><label>Syslog transport<select name="syslog_transport"><option value="udp">UDP</option><option value="tcp">TCP</option><option value="tls">TLS</option></select></label><label class="check"><input name="syslog_enabled" type="checkbox">Enable remote syslog</label></div><p class="muted">Profiles cannot contain passwords, certificates, API trust, device identity, or network addressing.</p><button>Save profile</button></form><pre id="profile-result"></pre></section><section class="card"><p class="eyebrow">Apply</p><h2>Push profile to device</h2><form id="profile-apply"><div class="cards"><label>Device<select id="profile-device" name="device_id" required></select></label><label>Profile<select id="profile-select" name="profile" required></select></label></div><p class="muted">The device validates the profile, records an audit event, and marks a restart as required.</p><button>Apply profile</button></form></section><section><div class="section-head"><h2>Saved profiles</h2></div><div id="profiles" class="cards"></div></section></div>
<div data-page-section="settings"><section class="hero"><p class="eyebrow">Configuration</p><h1>Management settings</h1><p>Runtime options are managed from the Home Assistant app configuration.</p></section><section class="card"><p class="eyebrow">GitHub release sync</p><h2>Active configuration</h2><dl><dt>Enabled</dt><dd id="setting-sync-enabled">—</dd><dt>Repository</dt><dd><code>__GITHUB_REPOSITORY__</code></dd><dt>Release endpoint</dt><dd><code>__RELEASE_BASE_URL__</code></dd><dt>Synchronization interval</dt><dd>__RELEASE_SYNC_INTERVAL__ seconds</dd><dt>Automatic Stable promotion</dt><dd>__AUTO_PROMOTE_STABLE__</dd><dt>Automatic Beta promotion</dt><dd>__AUTO_PROMOTE_BETA__</dd></dl></section><section class="card"><p class="eyebrow">Trust</p><h2>Management Suite verification key</h2><p>One Suite identity authorizes fleet policy, commands and local Stable, Beta and Alpha catalogs. Artifact signatures remain independently bound to the offline update key.</p><a class="button secondary" href="api/fleet-public-key">Download Management Suite verification key</a></section></div>
</main><script>
async function api(path,options){let r=await fetch(path,options),text=await r.text(),value;try{value=JSON.parse(text)}catch(_){value=null}if(!r.ok)throw new Error(value?.error||text||('HTTP '+r.status));return value}function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const activePage=document.body.dataset.page||'overview';for(const section of document.querySelectorAll('[data-page-section]'))section.classList.toggle('hidden',section.dataset.pageSection!==activePage);for(const link of document.querySelectorAll('[data-page-link]')){const active=link.dataset.pageLink===activePage;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page')}
async function refresh(){let data=await api('api/devices'),rolloutData=await api('api/rollouts'),releaseData=await api('api/releases'),box=document.getElementById('devices'),select=document.getElementById('policy-device'),rollouts=document.getElementById('rollouts'),releases=document.getElementById('releases'),releaseStatus=document.getElementById('release-status'),syncButton=document.getElementById('release-sync');box.innerHTML='';select.innerHTML='';rollouts.innerHTML='';releases.innerHTML='';let inventory=releaseData.inventory,last=inventory.last_sync?new Date(inventory.last_sync*1000).toLocaleString():'Never';syncButton.disabled=!releaseData.sync.enabled||releaseData.sync.running;releaseStatus.className=inventory.last_error?'bad':'muted';releaseStatus.textContent=(!releaseData.sync.enabled?'GitHub synchronization is disabled in app settings':releaseData.sync.running?'Synchronization in progress · ':'Last synchronized: '+last)+(inventory.last_error?' · '+inventory.last_error:'');document.getElementById('summary-devices').textContent=data.devices.length;document.getElementById('summary-releases').textContent=inventory.releases.length;document.getElementById('summary-rollouts').textContent=rolloutData.rollouts.filter(r=>r.status!=='complete'&&r.status!=='stopped').length;document.getElementById('summary-sync').textContent=releaseData.sync.enabled?(releaseData.sync.running?'Running':'Enabled'):'Disabled';document.getElementById('setting-sync-enabled').textContent=releaseData.sync.enabled?'Yes':'No';for(let r of inventory.releases){let assigned=(r.channels||[]).includes('stable')?'stable':(r.channels||[]).includes('beta')?'beta':(r.channels||[]).includes('alpha')?'alpha':'none';releases.innerHTML+=`<article class="card release-card"><div class="row"><strong>${esc(r.tag)}</strong><span class="ok">Verified</span></div><p class="revision" title="${esc(r.source_revision)}"><code>${esc(r.source_revision)}</code></p><p>Sequence ${esc(r.release_sequence)} · ${r.prerelease?'Pre-release':'Production'}</p><label>Release channel<select class="release-channel" data-tag="${esc(r.tag)}" onchange="setReleaseChannel(this)"><option value="none"${assigned==='none'?' selected':''}>Not promoted</option><option value="stable"${assigned==='stable'?' selected':''}>Stable</option><option value="beta"${assigned==='beta'?' selected':''}>Beta</option><option value="alpha"${assigned==='alpha'?' selected':''}>Alpha</option></select></label></article>`}if(!inventory.releases.length)releases.innerHTML='<article class="card"><p>No verified releases have been imported.</p></article>';for(let d of data.devices){let seen=d.last_seen?new Date(d.last_seen*1000).toLocaleString():'Never',healthy=!d.last_error;box.innerHTML+=`<article class="card"><div class="row"><strong>${esc(d.name)}</strong><span class="${healthy?'ok':'bad'}">${healthy?'Healthy':'Unavailable'}</span></div><p><code>${esc(d.id)}</code></p><p>${esc(d.host)}:${d.port}</p><p class="muted">Last seen: ${esc(seen)}</p><p class="bad">${esc(d.last_error)}</p><p>Application: ${esc(d.inventory?.device?.application_version||'unknown')}</p><p>Cohort: ${esc(d.cohort)}</p><details class="device-edit"><summary>Edit device</summary><form onsubmit="updateDevice(event,'${esc(d.id)}')"><div class="cards"><label>Name<input name="name" value="${esc(d.name)}" required></label><label>Host<input name="host" value="${esc(d.host)}" required></label><label>Port<input name="port" type="number" min="1" max="65535" value="${esc(d.port)}" required></label><label>Cohort<input name="cohort" value="${esc(d.cohort)}" required></label><label class="check"><input name="enabled" type="checkbox"${d.enabled?' checked':''}>Enable management</label></div><button>Save device</button></form></details></article>`;select.innerHTML+=`<option value="${esc(d.id)}">${esc(d.name)} · ${esc(d.cohort)}</option>`}let cohortInput=document.getElementById('rollout-cohorts'),cohorts=[...new Set(data.devices.filter(d=>d.enabled).map(d=>d.cohort).filter(Boolean))];if(document.activeElement!==cohortInput)cohortInput.value=cohorts.join(',');for(let r of rolloutData.rollouts){let cohort=r.cohorts[r.cohort_index]||'complete';rollouts.innerHTML+=`<article class="card"><div class="row"><strong>${esc(r.id)}</strong><span class="${r.status==='stopped'?'bad':'ok'}">${esc(r.status)}</span></div><p>Release ${esc(r.release_sequence)} · ${esc(cohort)}</p><p>${esc(r.successes)} complete / ${esc(r.failures)} failed</p><button onclick="rolloutAction('${esc(r.id)}','dispatch')">Dispatch active cohort</button> <button onclick="rolloutAction('${esc(r.id)}','advance')">Advance</button></article>`}}async function syncReleases(){try{await api('api/releases/sync',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});refresh()}catch(x){document.getElementById('release-status').textContent=x.message}}async function setReleaseChannel(control){control.disabled=true;try{await api('api/releases/promote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:control.dataset.tag,channel:control.value})});await refresh()}catch(x){control.disabled=false;document.getElementById('release-status').className='bad';document.getElementById('release-status').textContent=x.message;await refresh()}}document.getElementById('register').onsubmit=async e=>{e.preventDefault();let o=Object.fromEntries(new FormData(e.target));o.port=Number(o.port);await api('api/devices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});refresh()};document.getElementById('policy').onsubmit=async e=>{e.preventDefault();let o=Object.fromEntries(new FormData(e.target));o.maximum_failures=Number(o.maximum_failures);if(o.action)o.command={action:o.action,release_sequence:0};delete o.action;try{document.getElementById('result').textContent=JSON.stringify(await api('api/policy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)}),null,2)}catch(x){document.getElementById('result').textContent=x.message}};document.getElementById('rollout').onsubmit=async e=>{e.preventDefault();let o=Object.fromEntries(new FormData(e.target));o.release_sequence=Number(o.release_sequence);o.maximum_failures=Number(o.maximum_failures);o.cohorts=o.cohorts.split(',').map(x=>x.trim()).filter(Boolean);await api('api/rollouts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});refresh()};async function rolloutAction(id,action){try{await api('api/rollouts/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});refresh()}catch(x){document.getElementById('result').textContent=x.message}}refresh();setInterval(refresh,15000)
async function pollDevice(id,button){button.disabled=true;button.textContent='Retrying…';try{const result=await api('api/poll',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:id})});const failed=Boolean(result.device?.last_error);button.textContent=failed?'Retry failed':'Connected';button.className=failed?'danger':'secondary';button.title=failed?result.device.last_error:'';setTimeout(refresh,1200)}catch(error){button.textContent='Retry failed';button.className='danger';button.title=error.message;button.disabled=false}}
async function updateDevice(event,id){event.preventDefault();let form=event.target,values=Object.fromEntries(new FormData(form));values.port=Number(values.port);values.enabled=form.elements.enabled.checked;await api('api/devices/'+encodeURIComponent(id),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});await refresh();await refreshProfiles()}
async function deleteDevice(id){if(!confirm('Remove '+id+' from IoT MD Management? Device data and collected events will be deleted.'))return;await api('api/devices/'+encodeURIComponent(id),{method:'DELETE'});await refresh()}
function enhanceDeviceCards(){for(const card of document.querySelectorAll('#devices article.card')){if(card.dataset.enhanced)continue;const id=card.querySelector('code')?.textContent;if(!id)continue;const status=card.querySelector('.row span');if(status?.textContent==='Healthy'&&[...card.querySelectorAll('p')].some(p=>p.textContent.trim()==='Last seen: Never')){status.textContent='Connecting';status.className='muted'}const actions=document.createElement('div');actions.className='actions';const retry=document.createElement('button');retry.type='button';retry.className='secondary';retry.textContent='Retry connection';retry.onclick=()=>pollDevice(id,retry);const remove=document.createElement('button');remove.type='button';remove.className='danger';remove.textContent='Remove device';remove.onclick=()=>deleteDevice(id);actions.append(retry,remove);card.append(actions);card.dataset.enhanced='true'}}
const deviceObserver=new MutationObserver(enhanceDeviceCards);deviceObserver.observe(document.getElementById('devices'),{childList:true});enhanceDeviceCards()
function deploymentCommands(sequence,install){let actions=['check-update','download-update'];if(install)actions.push('activate-update');return actions.map(action=>({action:action,release_sequence:sequence}))}
async function refreshDeploymentChoices(){let data=await api('api/releases'),items=data.inventory.releases.filter(r=>r.verified&&(r.channels||[]).length),controls=[document.getElementById('deployment-release'),document.getElementById('rollout-release')];for(const control of controls){let selected=control.value;control.innerHTML='';for(const release of items){let option=document.createElement('option'),channel=release.channels[0];option.value=String(release.release_sequence);option.dataset.channel=channel;option.textContent=(release.version||release.tag)+' · '+channel+' · sequence '+release.release_sequence;control.append(option)}if(selected&&[...control.options].some(option=>option.value===selected))control.value=selected;if(!control.options.length){let option=document.createElement('option');option.textContent='Promote a verified release first';option.value='';control.append(option);control.disabled=true}else control.disabled=false}}
document.getElementById('policy').onsubmit=async event=>{event.preventDefault();let form=event.target,values=Object.fromEntries(new FormData(form)),release=document.getElementById('deployment-release').selectedOptions[0],sequence=Number(release.value),install=values.deployment_action==='install',result=document.getElementById('result');if(!sequence){result.textContent='Promote a verified release before deploying.';return}let request={device_id:values.device_id,channel:release.dataset.channel,start_time:values.start_time,end_time:values.end_time,maximum_failures:Number(values.maximum_failures),automatic_download:true,automatic_activation:install,commands:deploymentCommands(sequence,install)};try{result.textContent='Creating signed deployment…';result.textContent=JSON.stringify(await api('api/policy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)}),null,2);await refresh()}catch(error){result.textContent=error.message}}
document.getElementById('rollout').onsubmit=async event=>{event.preventDefault();let values=Object.fromEntries(new FormData(event.target)),release=document.getElementById('rollout-release').selectedOptions[0],result=document.getElementById('result'),request={release_sequence:Number(release.value),channel:release.dataset.channel,maximum_failures:Number(values.maximum_failures),cohorts:values.cohorts.split(',').map(value=>value.trim()).filter(Boolean)};try{let rollout=await api('api/rollouts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});result.textContent='Controlled deployment created. Dispatch the active cohort when ready.\\n'+JSON.stringify(rollout,null,2);await refresh()}catch(error){result.textContent=error.message}}
refreshDeploymentChoices();setInterval(refreshDeploymentChoices,15000)
async function refreshProfiles(){let profileData=await api('api/profiles'),deviceData=await api('api/devices'),box=document.getElementById('profiles'),profileSelect=document.getElementById('profile-select'),deviceSelect=document.getElementById('profile-device'),selectedProfile=profileSelect.value,selectedDevice=deviceSelect.value;box.innerHTML='';profileSelect.innerHTML='';deviceSelect.innerHTML='';for(const profile of profileData.profiles){profileSelect.innerHTML+=`<option value="${esc(profile.name)}">${esc(profile.name)}</option>`;box.innerHTML+=`<article class="card"><div class="row"><strong>${esc(profile.name)}</strong><button class="danger" type="button" onclick="deleteProfile(decodeURIComponent('${esc(encodeURIComponent(profile.name))}'))">Delete</button></div><p>${esc(profile.description)}</p><p><code>${esc(Object.entries(profile.settings).map(([key,value])=>key+'='+JSON.stringify(value)).join(' · '))}</code></p></article>`}if(selectedProfile&&[...profileSelect.options].some(option=>option.value===selectedProfile))profileSelect.value=selectedProfile;if(!profileData.profiles.length){profileSelect.innerHTML='<option value="">Create a profile first</option>';profileSelect.disabled=true;box.innerHTML='<article class="card"><p>No configuration profiles have been saved.</p></article>'}else profileSelect.disabled=false;for(const device of deviceData.devices){if(device.enabled)deviceSelect.innerHTML+=`<option value="${esc(device.id)}">${esc(device.name)} · ${esc(device.cohort)}</option>`}if(selectedDevice&&[...deviceSelect.options].some(option=>option.value===selectedDevice))deviceSelect.value=selectedDevice}
document.getElementById('profile-editor').onsubmit=async event=>{event.preventDefault();let form=event.target,values=Object.fromEntries(new FormData(form)),settings={timezone_name:values.timezone_name,ntp_servers:values.ntp_servers.split(',').map(value=>value.trim()).filter(Boolean),loglevel:values.loglevel,log_buffer_lines:Number(values.log_buffer_lines),ha_discovery:form.elements.ha_discovery.checked,ha_discovery_prefix:values.ha_discovery_prefix,mqtt_enabled:form.elements.mqtt_enabled.checked,mqtt_server:values.mqtt_server,mqtt_port:Number(values.mqtt_port),mqtt_username:values.mqtt_username,mqtt_base_topic:values.mqtt_base_topic,mqtt_qos:Number(values.mqtt_qos),syslog_enabled:form.elements.syslog_enabled.checked,syslog_host:values.syslog_host,syslog_port:Number(values.syslog_port),syslog_transport:values.syslog_transport},request={format_version:1,name:values.name,description:values.description,settings:settings},result=document.getElementById('profile-result');try{let profile=await api('api/profiles',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});result.textContent='Saved '+profile.name;await refreshProfiles()}catch(error){result.textContent=error.message}}
document.getElementById('profile-apply').onsubmit=async event=>{event.preventDefault();let values=Object.fromEntries(new FormData(event.target)),result=document.getElementById('profile-result');try{result.textContent='Applying profile…';let response=await api('api/profiles/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});result.textContent=JSON.stringify(response,null,2)}catch(error){result.textContent=error.message}}
async function deleteProfile(name){if(!confirm('Delete configuration profile '+name+'?'))return;await api('api/profiles/'+encodeURIComponent(name),{method:'DELETE'});await refreshProfiles()}
refreshProfiles();setInterval(refreshProfiles,15000)
</script></body></html>'''


def read_options():
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except Exception:
        return {}


OPTIONS = read_options()
STORE = FleetStore(event_retention=int(OPTIONS.get('event_retention', 5000)))
SIGNER = PolicySigner(SIGNING_KEY_PATH, PUBLIC_KEY_PATH)
CATALOG_SIGNER = CatalogSigner(SIGNING_KEY_PATH, PUBLIC_KEY_PATH)
RELEASES = ReleaseCatalog(
    RELEASE_STATE_PATH, RELEASE_ROOT, ArtifactVerifier(TRUSTED_UPDATE_KEY_PATH),
    CATALOG_SIGNER, OPTIONS.get('github_repository', 'IanW6374/IoT-Modular-Device'),
    OPTIONS.get('release_base_url', 'https://iot-upgrade.home.arpa:8443'),
    OPTIONS.get('github_token', ''),
)
CONTROLLER = FleetController(
    STORE, SIGNER, timeout=int(OPTIONS.get('request_timeout_s', 10)), tls={
        'ca_path': OPTIONS.get('device_api_ca_path', '/ssl/iot-md-ca.pem'),
        'cert_path': OPTIONS.get(
            'device_api_client_cert_path', '/ssl/iot-md-fleet.pem'
        ),
        'key_path': OPTIONS.get(
            'device_api_client_key_path', '/ssl/iot-md-fleet-key.pem'
        ),
    }
)


PORTAL_PAGES = {
    '/': 'overview', '/releases': 'releases', '/devices': 'devices',
    '/deployments': 'deployments', '/profiles': 'profiles',
    '/settings': 'settings',
}


def render_portal(page):
    values = {
        '__PAGE__': page,
        '__GITHUB_REPOSITORY__': OPTIONS.get(
            'github_repository', 'IanW6374/IoT-Modular-Device'
        ),
        '__RELEASE_BASE_URL__': OPTIONS.get(
            'release_base_url', 'https://iot-upgrade.home.arpa:8443'
        ),
        '__RELEASE_SYNC_INTERVAL__': OPTIONS.get('release_sync_interval_s', 3600),
        '__AUTO_PROMOTE_STABLE__': 'Enabled' if OPTIONS.get(
            'auto_promote_stable', False
        ) else 'Disabled',
        '__AUTO_PROMOTE_BETA__': 'Enabled' if OPTIONS.get(
            'auto_promote_beta', False
        ) else 'Disabled',
    }
    body = HTML
    for marker, value in values.items():
        body = body.replace(marker, html.escape(str(value)))
    return body.encode()


class Handler(BaseHTTPRequestHandler):
    server_version = 'IoT-MD-Fleet/2'

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
            if path in PORTAL_PAGES:
                body = render_portal(PORTAL_PAGES[path])
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
            elif path == '/api/profiles':
                self._json(200, {'profiles': STORE.list_profiles()})
            elif path == '/api/releases':
                with RELEASE_SYNC_LOCK:
                    sync = dict(RELEASE_SYNC_STATE)
                self._json(200, {'inventory': RELEASES.snapshot(), 'sync': sync})
            elif path == '/api/fleet-public-key':
                body = PUBLIC_KEY_PATH.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Disposition', 'attachment; filename="management-suite-verification-key.bin"')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == '/health':
                self._json(200, {
                    'status': 'ok', 'devices': STORE.count_devices(),
                    'storage': 'sqlite', 'releases': len(RELEASES.snapshot()['releases']),
                    'management_key_sha256': hashlib.sha256(
                        PUBLIC_KEY_PATH.read_bytes()
                    ).hexdigest(),
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
            elif path == '/api/profiles':
                self._json(200, STORE.save_profile(normalize_profile(request)))
            elif path == '/api/profiles/apply':
                profile = STORE.get_profile(request.get('profile', ''))
                if not profile:
                    raise ValueError('configuration profile does not exist')
                self._json(202, CONTROLLER.apply_profile(
                    request.get('device_id', ''), profile
                ))
            elif path == '/api/poll':
                device = CONTROLLER.poll_device(request.get('device_id', ''))
                self._json(200, {
                    'status': 'failed' if device.get('last_error') else 'connected',
                    'device': device,
                })
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
            elif path == '/api/releases/sync':
                self._json(202, start_release_sync())
            elif path == '/api/releases/promote':
                self._json(200, RELEASES.promote(
                    str(request.get('tag', '')), str(request.get('channel', ''))
                ))
            else:
                self._json(404, {'error': 'not found'})
        except (ValueError, KeyError) as exc:
            self._json(400, {'error': bounded_text(exc)})
        except urllib.error.HTTPError as exc:
            self._json(exc.code, {'error': bounded_text(exc.read().decode())})
        except Exception as exc:
            self._json(502, {'error': bounded_text(exc)})

    def do_PATCH(self):
        path = urlparse(self.path).path.rstrip('/')
        prefix = '/api/devices/'
        try:
            if not path.startswith(prefix):
                self._json(404, {'error': 'not found'})
                return
            identifier = unquote(path[len(prefix):])
            if not identifier or '/' in identifier:
                raise ValueError('device id is invalid')
            self._json(200, STORE.update_device(identifier, self._body()))
        except ValueError as exc:
            self._json(400, {'error': bounded_text(exc)})
        except Exception as exc:
            self._json(500, {'error': bounded_text(exc)})

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip('/')
        try:
            if path.startswith('/api/devices/'):
                identifier = unquote(path[len('/api/devices/'):])
                if not identifier or '/' in identifier:
                    raise ValueError('device id is invalid')
                self._json(200, STORE.delete_device(identifier))
            elif path.startswith('/api/profiles/'):
                name = unquote(path[len('/api/profiles/'):])
                if not name or '/' in name:
                    raise ValueError('configuration profile name is invalid')
                self._json(200, STORE.delete_profile(name))
            else:
                self._json(404, {'error': 'not found'})
        except ValueError as exc:
            self._json(404, {'error': bounded_text(exc)})
        except Exception as exc:
            self._json(500, {'error': bounded_text(exc)})

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


RELEASE_SYNC_LOCK = threading.Lock()
RELEASE_SYNC_STATE = {
    'enabled': bool(OPTIONS.get('github_sync_enabled', False)),
    'running': False, 'started_at': 0, 'completed_at': 0,
}


def _release_sync():
    try:
        result = RELEASES.sync()
        imported = set(result.get('imported', []))
        inventory = result.get('inventory', {})
        if imported and OPTIONS.get('auto_promote_stable', False):
            candidate = next((
                item for item in inventory.get('releases', [])
                if item['tag'] in imported and not item.get('prerelease')
            ), None)
            if candidate:
                RELEASES.promote(candidate['tag'], 'stable')
        if imported and OPTIONS.get('auto_promote_beta', False):
            candidate = next((
                item for item in inventory.get('releases', [])
                if item['tag'] in imported and item.get('prerelease')
            ), None)
            if candidate:
                RELEASES.promote(candidate['tag'], 'beta')
    finally:
        with RELEASE_SYNC_LOCK:
            RELEASE_SYNC_STATE['running'] = False
            RELEASE_SYNC_STATE['completed_at'] = int(time.time())


def start_release_sync():
    with RELEASE_SYNC_LOCK:
        if not RELEASE_SYNC_STATE['enabled']:
            raise ValueError('GitHub Release synchronization is disabled in add-on settings')
        if RELEASE_SYNC_STATE['running']:
            return dict(RELEASE_SYNC_STATE)
        RELEASE_SYNC_STATE['running'] = True
        RELEASE_SYNC_STATE['started_at'] = int(time.time())
    threading.Thread(target=_release_sync, daemon=True).start()
    return dict(RELEASE_SYNC_STATE)


def release_sync_loop():
    interval = max(300, int(OPTIONS.get('release_sync_interval_s', 3600)))
    time.sleep(10)
    while True:
        start_release_sync()
        time.sleep(interval)


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=job_loop, daemon=True).start()
    if RELEASE_SYNC_STATE['enabled']:
        threading.Thread(target=release_sync_loop, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 8098), Handler).serve_forever()


if __name__ == '__main__':
    main()

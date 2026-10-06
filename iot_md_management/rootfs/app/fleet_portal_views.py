"""Compose fleet list/editor views without disturbing action/USB workflows."""

from pathlib import Path


def compose(html):
    assets = Path(__file__).with_name('assets')
    # Replace renderers rather than emitting conflicting function definitions.
    prefixes = ('function deviceConnectionBadges(', 'function renderReleases(',
                'function renderProfiles(', 'async function setReleaseChannel(')
    html = '\n'.join(line for line in html.split('\n') if not line.startswith(prefixes))
    html = html.replace('const activePage=',
                        assets.joinpath('fleet_views.js').read_text() + '\nconst activePage=', 1)
    html = html.replace('</style>', assets.joinpath('fleet_views.css').read_text() + '</style>', 1)
    start = html.index('<nav aria-label="Primary">')
    end = html.index('</nav>', start) + len('</nav>')
    html = html[:start] + '''<button id="nav-toggle" class="nav-toggle secondary" type="button" aria-controls="primary-nav" aria-expanded="false">Menu</button><nav aria-label="Primary" id="primary-nav" class="nav-actions">
<a class="nav-link" data-page-link="overview" href="./">Overview</a>
<div class="nav-group"><button class="nav-link nav-menu-trigger" type="button" data-page-link="actions" aria-haspopup="true" aria-expanded="false" aria-controls="actions-menu">Actions</button><div id="actions-menu" class="nav-dropdown"><a class="nav-link" data-action-nav="new" href="actions?view=new">New</a><a class="nav-link" data-action-nav="inflight" href="actions?view=inflight">In-Flight <span id="inflight-count" class="badge">0</span></a></div></div>
<div class="nav-group"><button class="nav-link nav-menu-trigger" type="button" data-page-link="devices" aria-haspopup="true" aria-expanded="false" aria-controls="devices-menu">Devices <span id="devices-menu-health" class="device-health-led unknown" role="img" aria-label="Fleet health: waiting for reports" title="Fleet health: waiting for reports"></span></button><div id="devices-menu" class="nav-dropdown"><a class="nav-link" data-fleet-nav="enrol" href="devices?view=enrol">Enrol</a><a class="nav-link" data-fleet-nav="list" href="devices?view=list">List</a></div></div>
<a class="nav-link" data-page-link="releases" href="releases">Releases</a>
<div class="nav-group"><button class="nav-link nav-menu-trigger" type="button" data-page-link="profiles" aria-haspopup="true" aria-expanded="false" aria-controls="profiles-menu">Profiles</button><div id="profiles-menu" class="nav-dropdown"><a class="nav-link" data-fleet-nav="new" href="profiles?view=new">New</a><a class="nav-link" data-fleet-nav="list" href="profiles?view=list">List</a></div></div>
<a class="nav-link" data-page-link="activity" href="activity">Activity</a><a class="nav-link" data-page-link="settings" href="settings">Settings</a></nav>''' + html[end:]
    for page, label in (('actions', 'Actions'), ('devices', 'Devices'), ('profiles', 'Profiles')):
        html = html.replace('data-page-link="' + page + '" aria-haspopup="true"', 'data-page-link="' + page + '" aria-label="' + label + '" aria-haspopup="true"')
    html = html.replace('aria-controls="devices-menu"', 'aria-controls="devices-menu" aria-describedby="devices-menu-health"', 1)
    html = html.replace('<section class="panel device-browser">', '<section class="panel device-browser" data-fleet-view="list">', 1)
    html = html.replace('<section class="panel"><p class="eyebrow">Enrollment</p>', '<section class="panel" data-fleet-view="enrol"><p class="eyebrow">Enrollment</p>', 1)
    html = html.replace('<label>Management ID<input name="id" placeholder="IoT-MD-001" required></label>', '')
    html = html.replace('The Management ID is a local label; policy uses the immutable device identity discovered after connection.', 'Use the hostname to connect; policy uses the immutable device identity discovered after connection.')
    html = html.replace('placeholder="Uses the device description when blank"', 'placeholder="Optional description"')
    html = html.replace('Use the hostname to connect; policy uses the immutable device identity discovered after connection.', 'Use the hostname to connect; policy uses the immutable device identity discovered after connection. Leave Description blank to discover the device description.')
    html = html.replace('<h2>Register device</h2>', '<h2>Enrol device</h2>').replace('<button>Register device</button>', '<button>Enrol device</button>')
    html = html.replace('<section class="panel"><h2>Create or update profile</h2>', '<section class="panel" data-fleet-view="new"><h2>Create or update profile</h2>', 1)
    html = html.replace('<div id="profiles" class="grid"></div>', '''<section class="panel catalog-browser" data-fleet-view="list"><div class="catalog-tools"><label>Search profiles<input id="profile-search" type="search" placeholder="Name, description or included setting" oninput="setCatalogSearch('profiles',this.value)"></label><span id="profile-list-count" class="muted" role="status"></span><a class="button secondary" href="profiles?view=new">New profile</a></div><div id="profiles"></div></section>''', 1)
    html = html.replace('<div id="releases" class="release-grid"></div>', '''<section class="panel catalog-browser"><div class="catalog-tools"><label>Search releases<input id="release-search" type="search" placeholder="Version, channel, sequence or revision" oninput="setCatalogSearch('releases',this.value)"></label><span id="release-list-count" class="muted" role="status"></span></div><div id="releases"></div></section>''', 1)
    html = html.replace("if(activePage==='profiles'){try{extendProfileEditor();", "if(activePage==='profiles'&&fleetSubview('profiles')==='new'){try{extendProfileEditor();")
    html = html.replace('<th scope="col">Status</th><th scope="col">Description</th>', '<th scope="col">API</th><th scope="col">Device health</th><th scope="col">Description</th>')
    html = html.replace('device.enabled?deviceConnectionBadges(device):\'<span class="badge">Disabled</span>\'', 'deviceConnectionBadges(device)')
    html = html.replace('<td data-label="Status">${deviceConnectionBadges(device)}</td>', '<td data-label="API">${deviceConnectionBadges(device)}</td><td data-label="Device health">${healthLED(deviceHealthStatus(device))}</td>')
    html = html.replace('<td data-label="Hostname"><button', '<td data-label="Hostname"><div class="device-hostname-actions"><button')
    html = html.replace('<strong>${esc(device.host)}</strong></button></td>', '<strong>${esc(device.host)}</strong></button>${deviceRefreshButton(device)}</div></td>')
    html = html.replace('function renderDevices(){', 'function renderDevices(){\n renderFleetHealth();', 1)
    html = html.replace('colspan="7"', 'colspan="8"')
    html = html.replace('<span>Healthy devices</span>', '<span>API connected</span>')
    html = html.replace('state.devices.filter(item=>!item.last_error)', "state.devices.filter(item=>apiConnectionStatus(item).tone==='good')")
    html = html.replace('<option value="healthy">Healthy</option>', '<option value="healthy">API connected</option>')
    html = html.replace('<option value="unavailable">Unavailable</option>', '<option value="unavailable">API unavailable</option>')
    html = html.replace('<option value="disabled">Disabled</option>', '<option value="stale">Stale</option><option value="unknown">Not yet connected</option><option value="disabled">Disabled</option>')
    html = html.replace("status=!device.enabled?'disabled':device.last_error?'unavailable':'healthy'", "status=apiConnectionStatus(device).status")
    promotion = '<dt>Automatic Stable promotion</dt><dd>__AUTO_PROMOTE_STABLE__</dd><dt>Automatic Beta promotion</dt><dd>__AUTO_PROMOTE_BETA__</dd><dt>Automatic Alpha promotion</dt><dd>__AUTO_PROMOTE_ALPHA__</dd>'
    html = html.replace(promotion, '')
    html = html.replace('<section class="panel"><h2>Management trust</h2>', '''<section class="panel"><h2>Auto-promotion</h2><p class="muted">Managed in Home Assistant app configuration. Promote the newest newly imported release of each selected type after verification.</p><fieldset class="promotion-options"><legend class="visually-hidden">Saved auto-promotion choices</legend><label class="check"><input type="checkbox" __AUTO_PROMOTE_ALPHA_CHECKED__ disabled><span>Alpha</span></label><label class="check"><input type="checkbox" __AUTO_PROMOTE_BETA_CHECKED__ disabled><span>Beta</span></label><label class="check"><input type="checkbox" __AUTO_PROMOTE_STABLE_CHECKED__ disabled><span>Stable</span></label></fieldset></section><section class="panel"><h2>Management trust</h2>''', 1)
    return html

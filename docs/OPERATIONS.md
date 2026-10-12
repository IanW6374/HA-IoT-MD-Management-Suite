# Security and operations

## Live update monitoring

`device_worker_limit` bounds admitted device work (2–32, default 8); half of the
handlers are reserved for live progress. Each device still has one scheduled
handler at a time. Accepted deployment submissions hand off to live monitoring
without collecting configuration/events inline. An uncertain mutation response
is recorded and reconciled using reads, never automatically replayed.

Confirmed operation-storage rejections carry authenticated API v3 proof that
the handler was not dispatched (`phase: reservation`, literal `dispatched:
false`, recognized reservation code). Management fails that new target/job
and durably records a rejected request. It does not reuse the idempotency key
or retry the mutation. Generic 503s, incomplete error bodies and failures after
dispatch remain uncertain; a missing matching command alone is not proof of
rejection. This does not retroactively resolve an earlier uncertain job whose
device response lacked that evidence.

`progress_timeout_s` is the budget for one status read including device-lock and
transport-capacity wait. API discovery and final health confirmation each use
independent `request_timeout_s` budgets. A transient read timeout retains valid
discovery; certificate/configuration changes, protocol/auth failures or an
observed new boot identity invalidate it. The small authenticated
`/api/v3/fleet/update-status` capability avoids full inventory work. API v3
devices without it use inventory while receiving the enabling update; v2 stays
retired. Trial state or a cached health report can never prove completion.

Successful status reads recur after `progress_interval_s`, subject to fleet
size and available handlers. Recently responsive devices retry promptly;
unreachable devices retain capped exponential backoff. For example, 100 devices
and four live handlers with three-second reads require approximately 75 seconds
per saturated round, before scheduling overhead. Freshness thresholds account
for observed round capacity; errors still show last-known progress as retrying.

`GET /api/scheduler` reads cached diagnostics only: reserved worker counts,
active devices, expected refresh cadence, in-flight work, queue age/next retry
and per-device `progress_reads`. Each read includes its endpoint, failure stage,
duration, response bytes and stage timing (reservation, TLS context,
connect/headers, body and decoding). Connect/headers includes TLS establishment;
it is not a separately measured handshake duration. Diagnostics contain no
request bodies, credentials or signing material. The In-Flight tooltip names
the failed workflow stage and next retry. Browser periodic reads are scoped to
the visible page and single-flight; deployment/device status retain independent
refresh paths and revision guards.

### Retained tracing for a controlled physical-device test

Actions and Logs / Activity offer **Download diagnostics** for a deployment,
including after it completes. `GET /api/progress-traces?deployment=ID` downloads
the same cached JSON; optional `device=ID` restricts it to one enrolled target.
Without a deployment filter, all enrolled devices' retained groups are exported.
The endpoint does not poll devices, dispatch commands, cancel or restart them.

Traces are retained in a separate, additive Management SQLite table, committed
alongside poll state in one durable transaction per live workflow. Unchanged
older groups are not rewritten. Full inventory polls cannot overwrite traces,
and routine device-list reads do not select/decode trace history. Each device
retains four most-recently observed
deployments, each with 32 recent attempts and eight separately retained failed
attempts. Cumulative attempt/failure/read counts show truncation. Successful
reads cannot evict the separate failure history. Completion and an add-on
restart do not erase traces. Older groups eventually age out; export promptly.
Trace arrays are omitted from normal device/deployment responses, so repeated
browser refreshes do not download this history.
The schema-5 to schema-6 add-on upgrade is additive and retains device records,
credentials, queued jobs and existing action history; no clean seed is needed.

Every attempt records UTC epoch start/end, monotonic duration, workflow phase,
response receipt and categorized failure. Read evidence includes endpoint,
start offset, available budget, HTTP status (zero if unavailable), duration,
bytes and reservation/TLS-context/connect-and-headers/body/decode timings.
Exports whitelist fields and failure categories: they do not include response
bodies, request payloads, headers, idempotency keys or credential file paths.
Scheduler data in an export is the current snapshot, not historical admission
timing. Only live reconciliation reads are traced; mutation submissions and
full inventory workflows are not included. This is deliberately not a change
to retry, timeout, authorization or operation replay behaviour.

Before testing a genuine newer universal release, obtain separate approval for
receive-only USB capture and start it before dispatch. Export diagnostics after
any stall and after completion, and correlate timestamps with UART evidence.
Offline tests verify retention and boundedness, not real-device flash/TLS
performance; tracing is evidence collection, not a proven freeze correction.

The Actions page requests only active deployments plus up to 64 previously
active identifiers to observe terminal transitions. It does not transfer the
200-batch history on every live refresh; a bounded recent terminal cache keeps
cancellation/completion results visible. The Logs page reads the retained
history separately. This limits live response growth with historical batches.

Offline regression tests cover separate deadlines, slow response bodies,
reboots, confirmation failure, cancellation and 3/50/100-device scaled-latency
load. A signed physical three-device upgrade with pre-started passive capture
and scheduler timings remains necessary to qualify real flash/TLS performance.

The LAN-facing release listener uses the configured `/ssl` certificate and key.
Devices must trust its issuing CA. Home Assistant supports multiple named files
in `/ssl`; this add-on reads only the explicitly configured pair.

Fleet API connections are mutual TLS. Each device record names the device CA,
suite client certificate and suite client key. The suite creates one ECDSA
Management Suite identity in `/data` for fleet policy and release catalogs;
distribute only its public key to devices. Do not place the offline firmware/application
release-signing private key in Home Assistant. The corresponding public key is
pinned inside the add-on solely to verify imported artifacts.

## Release synchronization and promotion

GitHub synchronization is disabled by default. Enabling it permits both the
manual Ingress action and scheduled checks; disabling it prevents outbound
GitHub requests while retaining existing inventory and promoted catalogs. The
source must be a public GitHub repository in `owner/name` form. An optional
fine-grained read-only token raises GitHub API limits for private or busy
installations. Synchronization reads published, non-draft GitHub Releases and
requires versioned application and core bundles plus
`provenance-*.intoto.jsonl` and `sbom-*.cdx.json`. Universal bundles are
verified and inventoried when present.

An import succeeds only when:

- the tag, embedded versions and monotonic release sequences agree;
- the pinned offline update public key verifies each bundle signature;
- every embedded payload hash and GitHub asset digest agrees;
- provenance binds every bundle digest and identifies one IoT-MD source commit;
- the SBOM is valid JSON.

Imported assets are moved atomically from an incoming directory into
`/share/iot-md-releases/bundles`. Promotion signs a format-3 Stable, Beta or Alpha
catalog with the shared Management Suite key and writes `latest.json` atomically.
Each channel retains its eight newest promoted versions in `versions.json`; every
entry contains the same independently signed descriptors, while `latest.json`
continues to expose the newest version to older devices.
When a verified universal bundle is present, it is advertised first so automatic
device upgrades use the same paired transaction as a manual universal upload.
The application and core descriptors remain in the catalog for setup and
recovery. Devices verify the fleet/catalog key first, then independently verify
the selected bundle with their immutable update key. Automatic Stable/Beta
promotion is optional and off by default.

A successful synchronization also removes inventory entries that no longer
exist in the authoritative GitHub Releases response. Unreferenced local assets
and channel catalogs that point to a deleted release are removed at the same
time. A full 100-release response is treated as potentially paginated and does
not trigger deletion reconciliation.

Configure `release_base_url` to the exact HTTPS host and port covered by the
add-on TLS certificate. Install the same issuing CA as the device's
Release-server trusted CA. Download the Management Suite public key from the
Ingress Trust section and import it under **Maintenance > Certificates >
Management Suite verification key** on each device.

Back up add-on data to retain inventory, rollouts, events and the Suite signing
identity. Back up `/share/iot-md-releases` separately if release files must be
retained. Restoring the release store without the matching Suite identity
requires re-promoting channels and installing the new Suite public key on
devices. Rotate HTTPS/client certificates before expiry and update enrolled
paths atomically. Retention bounds stored events but does not delete release
artifacts.

Only `GET` and `HEAD` are accepted on port 8443. The listener exposes channel
`latest.json` descriptors, bounded `versions.json` inventories and immutable files
below `/bundles`; all other paths return 404. Release administration and file
browsing are available only through Home Assistant-authenticated Ingress.

# IoT MD Management Suite

Public Home Assistant add-on repository for fleet and secure release management
of [IoT Modular Device](https://github.com/IanW6374/IoT-Modular-Device).

The single add-on provides device enrollment, mTLS inventory/health polling,
a unified update/profile deployment workflow, encrypted complete-device backup
and restore, durable deployment and audit history, signed fleet policy, GitHub
Release synchronization and a dedicated HTTPS release endpoint. It imports `.iotapp`,
`.iotcore` and `.iotuni` assets only after verifying their offline signatures,
payload hashes, release sequence, SLSA provenance and SBOM. The offline IoT MD
update-signing private key is never installed in Home Assistant.

Successful synchronization treats the GitHub Releases list as authoritative:
releases deleted upstream are removed from verified inventory together with
their unreferenced local files and obsolete channel catalogs.

## Device settings

Devices has separate Enrol and List submenu pages. Enrollment uses the hostname,
without a separate Management ID. On startup existing fleet references migrate
atomically to hostname-based keys; the add-on retains a private pre-migration
SQLite snapshot alongside `fleet.db`. Duplicate hostnames or conflicting history
block migration rather than merging data. Existing hardware identities, signing
keys, encrypted backup envelopes and recovery passwords are unchanged.

Device summaries use the hostname, separate API and device-health LEDs, and a
truncated description. Hover or focus an LED for diagnostics. One refresh icon
beside the hostname polls inventory, configuration, runtime diagnostics and
events for either a connected or unavailable device. Device health uses reported lifecycle, service,
task and qualification observation states—not lifetime error counters. Unknown
or stale reports never display as healthy. The portal auto-promotion checkboxes
reflect the Alpha / Beta / Stable choices managed in HA app configuration.
The Devices menu health LED combines API and device health across enabled
devices, including stale or unknown reports. Disabled devices are excluded.
Select a hostname to open its settings, backup schedule and recovery points.
The description comes from the device when enrollment leaves it blank. Editing
or clearing it later writes to the enrolled device's configuration API, which
requires `configuration:write` permission. Saved descriptions are read back
from the device before Management confirms the change; failed writes or
verification keep the unsaved edits visible.
The Management name remains a local label, independent of the device description.

Enrol devices by hostname under **Devices > Enrol**; no Name field is needed.
Port defaults to 8444 and Group defaults to `default`, but both are required
and can be changed. **Devices > Groups** lists device counts and creates saved
groups, including empty groups, for the enrolment and device-settings selectors.
Existing group assignments and device history are preserved.

## Profiles and complete backups

Profiles are reusable configuration patches or baselines. The profile builder
starts empty, lets an administrator search for and add only the settings the
profile owns, and keeps advanced certificates and trust material out of the
normal editing path. A deployment can still select an individual profile item,
such as enabling syslog, without pushing the remainder of the profile.

Use **Profiles > Create** to create a profile. Add individual settings or a whole
section (for example MQTT). **Use baseline set** adds every supported setting,
including Wi-Fi SSID and MQTT username, except the device name to avoid giving
an entire fleet the same identity. Device name can still be added explicitly.
Select **Include secrets and certificates** to add all password, certificate,
private-key and trust fields as well. Empty baseline text and credentials
are omitted rather than clearing existing device values. Previously
saved profile secrets are retained, encrypted at rest and masked in the list.
Required fields have a compact icon with an accessible hover description;
optional fields remain unmarked. Mandatory checkbox acknowledgements are
included, and conditional requirements follow the currently enabled feature.

**Logs > Activity** holds searchable completed actions and recovery points,
including per-device results and restore controls. **Logs > Audit** searches
retained administrative and device events, returning the latest 500 matches
from each source. Outcome dots have colour-coded hover and keyboard-focus
descriptions. Existing Activity links remain valid.

Backups are separate from profiles. An IoT-MD device creates a complete
AES-GCM-encrypted backup containing settings, secrets, module configuration,
certificates, private keys, API trust and management state. Management stores
only that opaque envelope; the unique recovery password is independently
encrypted with the add-on's local data key. Automatic daily or weekly capture,
per-device retention, manual recovery points, redacted restore preview and
confirmed same-device or compatible-device restore are available on the
Backups page.

## Seed a new device over USB

Open **Actions > Create > Seed device** in Chrome or Edge over HTTPS, and connect
a blank ESP32-S3 to the computer running the browser. Choose its private
`.factory.bin` image and retain the matching setup-password file generated by
the IoT-MD firmware build. Each factory image contains its own setup credential
and NVS encryption material: create a separate image for each device, and keep
factory artifacts out of public GitHub releases.

Type `SEED`, acknowledge the retained credential, and select the connected
board in the browser's USB chooser. If Home Assistant's embedded page blocks
serial access, use **Open seeding in a new tab**; the button is shown only when
the embedded panel blocks access. Unsupported-browser and HTTPS warnings are
shown together, and the seeding form stays disabled until its prerequisites
are met. Safari lacks Web Serial support;
use Chrome or Edge for this action. No Home Assistant host USB access is needed.

Management inspects the board before erasing, refuses security keys or fuses
already provisioned, writes without changing signed image headers, and checks
the complete image digest before resetting. Keep the USB tab open and power
connected throughout first-boot security initialization. Then complete the
device's first-run setup, including its signed application, using the matching
setup password. The final milestone is **Request reboot**, not first-run
confirmation. **Image verified** means the image was verified and reset was
requested; the hotspot and successful startup remain explicitly unconfirmed.
If no setup hotspot appears, press RESET/EN without holding BOOT and leave the
device powered throughout security initialization. Do not re-seed a board
once first boot has enabled its security fuses.

Factory images and passwords never leave the browser computer. Management
retains only image name, SHA-256 fingerprint and browser-reported progress in
**Actions > In-Flight** and **Logs > Activity**. A lost browser heartbeat is shown as
an interrupted operation with unknown hardware outcome, without an automatic
hardware retry. Protected devices use the existing secured-device recovery
workflow below instead of factory flashing.

## Clean USB recovery of a secured device

Open **Actions > Create > Clean USB recovery** in desktop Chrome or Edge over
HTTPS. Connect the device to the browser computer and select its **UART**
interface, not the native JTAG interface. Leave BOOT released: this action
requires a bootable IoT-MD core and its running MicroPython UART REPL, not
ROM download mode. If both core slots are unbootable, this workflow cannot
recover the board; do not erase it or write a plaintext factory image.

Choose compatible signed `.iotcore` and complete `.iotapp` bundles and a
retained strong setup-password `.txt` file. Confirm that all user state will
be erased and type `RECOVER`. Take an encrypted configuration backup first
if the device is accessible. Private release-signing keys are not required;
the device validates bundles against its existing verification identity.

Recovery interrupts the application, checks hardware security, writes and
reads back the inactive encrypted core partition, validates its secure-boot
image, then erases user settings, credentials, certificates, application
files and logs. The hardware security keys, encrypted-NVS key material and
release verification identity are preserved. It verifies the new running
core and stages the signed application on boot. Alpha 97 or newer core is
required for clean recovery: both bundles are transferred and read back
before a single reset. The new frozen core validates the application and
returns a matching staging receipt before the browser reports completion. Keep
power and this tab connected throughout. Complete setup using the retained
password file and restore the encrypted backup if required.

Keep the USB workspace visible and the computer awake until recovery finishes.
Management requests screen sleep prevention where supported, but browsers release
this protection when the page becomes hidden; it does not guarantee that the
computer or USB connection cannot suspend. Recovery uses the core's raw-paste
receive-window protocol, not short browser timers, to pace command transfers.
The watchdog remains enabled with an explicit 60-second recovery timeout.
If flow control is unavailable, recovery stops before sending the command.

Eight live milestones appear in **Actions > In-Flight**, with configuration
reset, application transfer, handoff and boot validation reported separately. Failed progress stays
visible there and survives page reload; the final
record in **Logs > Activity**, filtered to USB seeding. Completion does not independently
confirm the hotspot; check for `IoT-MD-Setup` yourself. Image bytes, password
contents and UART commands never go to Management or browser storage—only
operation names, digests, progress and result metadata are retained. On an
interruption, inspect the board before retrying; there is no automatic
hardware retry. Hardware qualification of this browser recovery workflow
is still required before production use.

Recovery confirmation tolerates USB disappearing during reset and reopens only
the selected port. If the boot receipt was missed, it allows up to three minutes
for core validation, then reads the saved receipt over UART and checks device
identity, core version, OTA slot, application digest and complete staged state.
This fallback briefly interrupts setup and requests a normal restart to restore
it; it never repeats an upload or erase. The normal path remains one reset and
does not interrupt setup. An unavailable result is shown as interrupted/unknown,
not evidence that the device failed. These checks work with the Alpha 97 bundles.

If recovery stops after configuration reset, select **Resume application
staging only**, retaining the same signed core/application bundles and setup
password file. The device must still be unprovisioned; the running core's
partition digest must match the selected core bundle and the setup password
must match the device. Resume performs no core write or configuration erase;
it verifies, stages the application, clears stale recovery requests and
requests startup. Do not automatically repeat the full recovery after a
lost connection. Reopen the browser page after updating Management to load
the corrected USB transport.

## Install

Version 2.2.0 uses the clean `iot_md_management` application identity. Remove
an earlier installation before installing this version so Home Assistant
cannot retain a replaced slug or application data.

Add this repository URL under **Settings > Add-ons > Add-on store >
Repositories**:

```text
https://github.com/IanW6374/HA-IoT-MD-Management-Suite
```

Install **IoT MD Management Suite**, choose the certificate and key filenames
already present in Home Assistant `/ssl`, start the add-on and open its Ingress
panel. Port 8443 must be reachable by managed devices.

The default source is `IanW6374/IoT-Modular-Device`. Enable GitHub Release
synchronization in the add-on settings, then use **Synchronize GitHub
Releases**, inspect the verified inventory, then promote a release to Stable,
Beta or Alpha. Promotion prefers the universal bundle for automatic upgrades
and retains application/core descriptors for setup and recovery. Descriptors
are served without caching; immutable bundles are cached. A channel keeps its
eight newest promoted versions available to current devices while retaining a
newest-release catalog for older device compatibility. Optional automatic
promotion is disabled by default. Stable, Beta and Alpha promotion can each
be enabled independently; when both
prerelease options are enabled, Alpha-tagged versions stay on Alpha and other
prereleases use Beta. Enroll devices using the
shared CA, client certificate and client key configured once in the add-on,
then provision the displayed Management Suite verification public key on each
device. Scheduled deployments stage immediately and activate in each target's
reported automatic-update slot; **Install now** is an audited administrator
exception.

Profiles are selective: each setting can be included independently when the
profile is created, and a deployment can apply either the whole profile or a
chosen subset. The default editor groups Wi-Fi and MQTT secrets with their
related settings. Advanced settings add portal/API configuration and encrypted
certificate, private-key and trust deployment. Network changes use the IoT-MD
reboot-and-confirm rollback trial.

The generic IoT Certificate Authority and IoT Syslog remain separate
add-ons and can be used without IoT MD.

See [security and operations](docs/OPERATIONS.md) for trust boundaries,
certificate rotation, backups and release publishing.

Licensed under Apache-2.0.

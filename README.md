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

## Profiles and complete backups

Profiles are reusable configuration patches or baselines. The profile builder
starts empty, lets an administrator search for and add only the settings the
profile owns, and keeps advanced certificates and trust material out of the
normal editing path. A deployment can still select an individual profile item,
such as enabling syslog, without pushing the remainder of the profile.

Backups are separate from profiles. An IoT-MD device creates a complete
AES-GCM-encrypted backup containing settings, secrets, module configuration,
certificates, private keys, API trust and management state. Management stores
only that opaque envelope; the unique recovery password is independently
encrypted with the add-on's local data key. Automatic daily or weekly capture,
per-device retention, manual recovery points, redacted restore preview and
confirmed same-device or compatible-device restore are available on the
Backups page.

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

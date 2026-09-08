# IoT MD Management Suite

Public Home Assistant add-on repository for fleet and secure release management
of [IoT Modular Device](https://github.com/IanW6374/IoT-Modular-Device).

The single add-on provides device enrollment, mTLS inventory/health polling,
signed fleet policy, queued commands, staged rollouts, GitHub Release
synchronization and a dedicated HTTPS release endpoint. It imports `.iotapp`,
`.iotcore` and `.iotuni` assets only after verifying their offline signatures,
payload hashes, release sequence, SLSA provenance and SBOM. The offline IoT MD
update-signing private key is never installed in Home Assistant.

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
Beta or Alpha. Descriptors are served without caching; immutable bundles are cached.
Optional automatic promotion is disabled by default. Enroll devices with a CA,
client certificate and client key from `/ssl`, then provision the displayed
shared Management Suite verification public key on each device.

The generic IoT Certificate Authority and IoT Syslog remain separate
add-ons and can be used without IoT MD.

See [security and operations](docs/OPERATIONS.md) for trust boundaries,
certificate rotation, backups and release publishing.

Licensed under Apache-2.0.

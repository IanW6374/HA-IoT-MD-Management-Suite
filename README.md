# IoTMD Management Suite

Public Home Assistant add-on repository for fleet and secure release management
of [IoT Modular Device](https://github.com/IanW6374/IoT-Modular-Device).

The single add-on provides device enrollment, mTLS inventory/health polling,
signed fleet policy, queued commands, staged rollouts and a dedicated HTTPS
release endpoint. It only distributes pre-signed `.iotapp`, `.iotcore` and
`.iotuni` artifacts; the offline IoTMD update-signing key is never installed in
Home Assistant.

## Install

Add this repository URL under **Settings > Add-ons > Add-on store >
Repositories**:

```text
https://github.com/IanW6374/HA-IoTMD-Management-Suite
```

Install **IoTMD Management Suite**, choose the certificate and key filenames
already present in Home Assistant `/ssl`, start the add-on and open its Ingress
panel. Port 8443 must be reachable by managed devices.

Copy the release layout produced by IoTMD `tools/publish_release.py` into
`/share/iotmd-releases`. Descriptors are served without caching; immutable
bundles are cached. Enroll devices with a CA, client certificate and client key
from `/ssl`, then provision the displayed fleet verification public key on each
device.

The generic IoT Certificate Authority and IoT Syslog Server remain separate
add-ons and can be used without IoTMD.

See [security and operations](docs/OPERATIONS.md) for trust boundaries,
certificate rotation, backups and release publishing.

Licensed under Apache-2.0.

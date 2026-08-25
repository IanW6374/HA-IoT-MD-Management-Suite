# IoT MD Management Suite

Public Home Assistant add-on repository for fleet and secure release management
of [IoT Modular Device](https://github.com/IanW6374/IoT-Modular-Device).

The single add-on provides device enrollment, mTLS inventory/health polling,
signed fleet policy, queued commands, staged rollouts and a dedicated HTTPS
release endpoint. It only distributes pre-signed `.iotapp`, `.iotcore` and
`.iotuni` artifacts; the offline IoT MD update-signing key is never installed in
Home Assistant.

## Install

Version 2.1.1 uses the clean `iot_md_management` application identity. Remove
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

Copy the release layout produced by IoT MD `tools/publish_release.py` into
`/share/iot-md-releases`. Descriptors are served without caching; immutable
bundles are cached. Enroll devices with a CA, client certificate and client key
from `/ssl`, then provision the displayed fleet verification public key on each
device.

The generic IoT Certificate Authority and IoT Syslog remain separate
add-ons and can be used without IoT MD.

See [security and operations](docs/OPERATIONS.md) for trust boundaries,
certificate rotation, backups and release publishing.

Licensed under Apache-2.0.

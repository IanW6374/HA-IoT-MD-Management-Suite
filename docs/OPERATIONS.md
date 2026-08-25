# Security and operations

The LAN-facing release listener uses the configured `/ssl` certificate and key.
Devices must trust its issuing CA. Home Assistant supports multiple named files
in `/ssl`; this add-on reads only the explicitly configured pair.

Fleet API connections are mutual TLS. Each device record names the device CA,
suite client certificate and suite client key. The suite creates a separate
ECDSA fleet-policy signing identity in `/data`; distribute only its public key
to devices. Do not place the offline firmware/application release-signing key in
Home Assistant.

Back up add-on data to retain inventory, rollouts, events and the fleet policy
identity. Back up `/share/iotmd-releases` separately if release files must be
retained. Rotate HTTPS/client certificates before expiry and update enrolled
paths atomically. Retention bounds stored events but does not delete release
artifacts.

Only `GET` and `HEAD` are accepted on port 8443. The listener exposes channel
`latest.json` descriptors and immutable files below `/bundles`; all other paths
return 404. Release administration and file browsing are available only through
Home Assistant-authenticated Ingress.

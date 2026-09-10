# Security and operations

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
When a verified universal bundle is present, it is advertised first so automatic
device upgrades use the same paired transaction as a manual universal upload.
The application and core descriptors remain in the catalog for setup and
recovery. Devices verify the fleet/catalog key first, then independently verify
the selected bundle with their immutable update key. Automatic Stable/Beta
promotion is optional and off by default.

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
`latest.json` descriptors and immutable files below `/bundles`; all other paths
return 404. Release administration and file browsing are available only through
Home Assistant-authenticated Ingress.

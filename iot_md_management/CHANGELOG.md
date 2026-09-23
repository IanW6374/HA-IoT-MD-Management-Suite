# Changelog

## 2.2.9

- Configure the Device API CA, fleet client certificate and client key once in
  the Home Assistant add-on configuration and apply that identity to every
  managed device. Existing database columns are retained only for upgrade
  compatibility and no longer override the core settings.
- Give Retry connection immediate progress and success/failure feedback before
  refreshing the device card.
- Remove "e.g." from device enrollment field hints and keep the form focused on
  device-specific identity, address and cohort values.

## 2.2.8

- Show newly registered devices as Connecting until their first successful poll,
  and add an explicit retry action.
- Allow administrators to remove a registered device and its collected events.
- Add concrete enrollment examples for device identity, host and certificate
  paths.
- Replace the opaque remote-disconnect message with guidance to verify that the
  Management Suite client certificate is enrolled for client authentication and
  has the device API `read` scope.

## 2.2.7

- Retain the eight newest promoted versions in each release channel so current
  devices can offer an authenticated automatic-upgrade version selector.
- Continue publishing the newest promoted version through `latest.json` for
  compatibility with existing devices, and expose the bounded inventory through
  the read-only TLS `versions.json` endpoint.
- Rebuild both channel documents when a promoted release is moved, unpromoted
  or removed during GitHub reconciliation, and migrate existing promoted
  channels automatically when the updated add-on starts.

## 2.2.6

- Reconcile verified inventory with the authoritative GitHub Releases list on
  synchronization, removing deleted releases, their unreferenced local assets
  and any channel catalog that still points to a deleted release.
- Retain the existing inventory without deletion when GitHub returns a full
  100-release page because the response may be incomplete.

## 2.2.5

- Prefer the verified universal artifact in promoted release catalogs so
  automatic device upgrades use the same paired staging, activation and
  rollback transaction as manual universal uploads.
- Retain signed application and core descriptors in the catalog for setup and
  recovery workflows.

## 2.2.4

- Accept standard in-toto provenance assets ending in `.intoto.jsonl` during
  GitHub release synchronization. Previously these assets were discarded by
  the filename filter before provenance and SBOM validation.

## 2.2.3

- Add Alpha as a release-promotion channel in the portal and signed catalog
  service, matching the IoT-MD automatic-upgrade channel selector.

## 2.2.2

- Correct the default signed release endpoint to
  `https://iot-upgrade.home.arpa:8443`.

## 2.2.1

- Replace the crowded per-release promotion buttons with one Stable, Beta or
  Not promoted selector, including explicit channel removal.
- Make the verified-release inventory responsive and prevent long source
  revisions or controls from overlapping adjacent cards.
- Default the signed release endpoint to
  `https://iotmd-update.home.arpa:8443`.

## 2.2.0

- Synchronize published IoT-MD GitHub Releases into a durable inventory.
- Make GitHub synchronization an explicit, default-off add-on setting.
- Verify pinned artifact signatures, payload hashes, GitHub digests, SLSA
  provenance and SBOM before importing any release.
- Add explicit Stable and Beta promotion controls with optional automatic
  promotion.
- Sign local format-3 channel catalogs with the existing fleet-policy identity.
- Expose one Management Suite public key for fleet and catalog trust while keeping
  the offline artifact-signing private key outside Home Assistant.
- Split the ingress portal into focused Overview, Releases, Devices, Policy,
  Rollouts and Settings tabs using the shared IoT application layout.

## 2.1.1

- Establish the clean IoT MD Management Suite application identity.
- Align the ingress header, brand mark, navigation, typography, colour palette
  and cards with IoT Certificate Authority.
- Provide device enrollment, mTLS inventory polling and health visibility.
- Provide signed fleet policy, queued commands and staged rollouts.
- Serve signed release artifacts through the dedicated TLS endpoint.
- Store releases beneath `/share/iot-md-releases` and application state in the
  Home Assistant-managed data directory.

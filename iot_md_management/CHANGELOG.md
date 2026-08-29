# Changelog

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

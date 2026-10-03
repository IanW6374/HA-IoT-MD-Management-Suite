# Changelog

## 2.8.1

- Identify the browser and show unsupported-browser and HTTPS requirements
  together, including when Safari is opened over HTTP.
- Grey out and disable all seeding inputs and subsequent actions until browser,
  connection and USB permissions requirements are met.
- Rename the USB workspace link to **Open seeding in a new tab** and show it
  only when it can resolve embedded-panel USB permission restrictions.

## 2.8.0

- Add **Actions > New > Seed device** for a blank ESP32-S3 connected by USB to
  the browser computer. Factory bytes and the setup credential stay local.
- Inspect board security before writing, preserve signed image headers, verify
  the written image, and start first boot only after successful verification.
- Show live USB milestones in In-Flight and retain browser-reported results in
  Activity. Lost browser connections are recorded as unknown outcomes.
- Provide a separate USB workspace for embedded-page permission restrictions.
  Use HTTPS and a browser supporting Web Serial, such as Chrome or Edge;
  Safari currently does not support direct browser USB seeding.


## 2.7.6

- Hide empty deployment or backup categories while another type of action is
  in flight, avoiding contradictory empty-state messages.
- Show one shared empty state only when no fleet action is running.

## 2.7.5

- Move **New** and **In-Flight** into a real submenu beneath the primary
  **Actions** navigation item instead of presenting them as an in-page filter.
- Give each Actions submenu destination its own heading while retaining the
  automatic transition to In-Flight after an action is accepted.

## 2.7.4

- Split Actions into focused **New** and **In-Flight** views and switch to
  In-Flight automatically after a deployment or backup is accepted.
- Move completed deployment and backup history into Activity, with filters and
  expandable per-device results.
- Allow encrypted recovery points to be previewed, restored or deleted directly
  from Activity, including links from device recovery-point lists.

## 2.7.3

- Place **Back up now** before **Current backup operations** so the selected
  action reads in normal input-to-progress order.
- Derive encrypted-backup PBKDF2 keys on the Management host and transfer only
  the one-time derived key over mutual TLS, preventing constrained devices from
  blocking their portal and API event loop during managed backup operations.
- Retain compatibility with existing encrypted recovery points.

## 2.7.2

- Start the Actions workspace without an implicit action, target scope or
  activation choice; later sections remain visibly locked until their
  prerequisite selection is complete.
- Fix Back up and Restore choices being immediately reset to Deploy by stale
  URL state.
- Count accepted, staged and activating devices against the correct graphical
  milestone, including records created by earlier Management versions.
- Recognize the temporary disconnect caused by an immediate installation as a
  restart awaiting version confirmation instead of leaving it at Staging.
- Divide each device card into distinct Device settings, Automated backups and
  Recovery points sections for clearer expanded views.

## 2.7.1

- Match Home Assistant's compact 14 px Roboto type scale, 32 px maximum page
  heading, 1,200 px content width, tighter controls and lower-radius panels.
- Keep the consolidated Actions and device backup restore workflow introduced
  in 2.7.0.

## 2.7.0

- Consolidate Deploy and Backups into one Actions workspace with Deploy, Back
  up and Restore modes while retaining direct route aliases.
- Add Restore shortcuts to each device's valid backup list; the selected
  recovery point opens directly with its preview and restore controls expanded.
- Remove the duplicate count and percentage caption below graphical milestones;
  the ring retains the count and its accessible tooltip retains the percentage.
- Align the portal type scale, heading sizes, panel density and page spacing
  more closely with Home Assistant and the other IoT add-ons.

## 2.6.1

- Keep the activation section locked when a selected profile has no profile
  items selected, including after using Clear in the profile-item picker.

## 2.6.0

- Keep deployment progress monotonic per device and show an independent device
  count and percentage ring at every milestone, so devices may occupy different
  stages without making aggregate progress move backwards.
- Apply the same multi-target progress treatment to encrypted backup jobs and
  surface active deployments and backups on the Overview page while they run.
- Turn Attention required into a filtered, actionable issue list with individual
  or bulk acknowledgement and automatic removal when a condition resolves.
- Move automated backup schedules to each device, expose them under Edit device,
  and show that device's latest valid recovery points alongside its schedule.
- Rename the recovery page to Device Backups, retain manual and automated work in
  one live status area, and keep completed recovery points in Backup history.
- Guide multi-step deployments by muting and disabling later sections until the
  current selection is complete, and standardize spacing before device enrolment.

## 2.5.1

- Track multi-device deployments by the number of devices that have cleared
  each milestone, keeping completed milestones as green ticks and showing an
  explicit device count at the earliest incomplete step.
- Preserve expanded device-progress, result, editor and recovery-point
  disclosures during live refreshes.
- Include the safe Portal, API and certificate-mode settings when adding the
  standard profile baseline.

## 2.5.0

- Default Verified releases to promoted versions, with an explicit All view for channel administration.
- Show live graphical milestone progress for deployments and encrypted backup jobs.
- Align operation states and controls with the IoT-MD update experience.

## 2.4.9

- Replace the restore-preview count-only message with a structured comparison
  of every selected configuration area: current value, backup value and
  Changed, Unchanged or Missing state.
- Derive the actual number of changes from the device's secret-safe preview
  rows instead of labelling every reviewed row as a change.

## 2.4.8

- Give manual backups the same selected-device, group and all-enabled target
  scopes as Deploy, queueing one encrypted backup job per resolved device.
- Stop the periodic Recovery-points refresh from rebuilding an unchanged list
  while a restore preview is open, preserving the disclosure and its result.

## 2.4.7

- Render the encrypted marker as a compact inline badge immediately after the
  complete-configuration recovery-point title.
- Pair with IoT-MD Alpha 90, which accepts the bounded encrypted envelope on
  restore preview even when its retained core has an older general body limit.

## 2.4.6

- Explain that an authorization failure is being retried automatically and
  identify the required `configuration:write` device-client scope.
- Refresh Recovery points periodically while the page is open so a completed
  retry appears after returning from another tab without a manual refresh.
- Reconcile a newly stored recovery point with its pending request, replacing
  an earlier attempt error with the final successful outcome.
- Resume tracking an active backup after a page reload, reuse the existing job
  instead of creating a duplicate, and allow extra time for an administrator
  to grant a missing `configuration:write` scope with ten-second retries.

## 2.4.5

- Follow manual encrypted-backup jobs from queue through creation, transfer
  and persistence instead of refreshing once before the device can finish.
- Refresh Recovery points automatically when the backup completes and expose
  retry or terminal failure details rather than leaving a stale queued notice.
- Keep the Create backup action disabled while its background job is active.

## 2.4.4

- Replace the browser-dependent profile datalist with a grouped setting
  selector that exposes every standard and advanced setting explicitly.
- Label advanced Portal, API, certificate and trust groups in the selector,
  reveal the advanced panel automatically when one is selected, and prevent
  already-added settings from being selected twice.

## 2.4.3

- Fix a generated portal JavaScript syntax error that prevented devices,
  releases, profiles and deployment data from rendering in 2.4.2; persisted
  management data was not removed.
- Add an Automatic Alpha Promotion add-on option and show its effective state
  on the Management settings page.
- Keep Alpha-tagged prereleases on the Alpha channel when both automatic Alpha
  and Beta promotion are enabled, while other prereleases continue to Beta.
- Validate the generated portal JavaScript during the test suite before a
  release can be published.

## 2.4.2

- Render successful device, release, profile, deployment and activity responses
  independently so one failed endpoint can no longer blank the entire portal.
- Display the name and error from any failed data endpoint at the top of the
  page while retaining all successfully loaded data.
- Initialize the profile builder only on the Profiles page and the backup data
  only on the Backups page, isolating optional UI features from other tabs.
- Add migration coverage proving an existing schema-3 device inventory is
  retained when the encrypted-backup schema is introduced.

## 2.4.1

- Load the enabled-device inventory directly with the Backups page so the
  manual backup and restore target selectors cannot render before devices.
- Remove the superfluous Profile type selector; Use baseline set now simply
  adds the recommended settings to the same selective profile builder.
- Put name and description on a full-width first row with the setting picker
  below it.
- Replace the prominent Remove button with a small badge beside each selected
  setting name and hide the advanced container until it owns a selected item.

## 2.4.0

- Replace the full profile matrix with a patch-first searchable setting picker
  and an optional baseline seed while retaining deployment-time item selection.
- Label saved profiles as Patch or Baseline and keep encrypted secrets beside
  their relevant settings without exposing their stored values.
- Add scheduled and manual complete-device backups. Devices encrypt the full
  configuration before transfer and Management separately encrypts each unique
  recovery password at rest.
- Add per-device retention, backup history, redacted restore preview, typed
  confirmation and selective or complete restore to a compatible managed
  device.

## 2.3.4

- Add automatic-update parameters to reusable configuration profiles.
- Encrypt Wi-Fi and MQTT profile passwords at rest and expose only fixed masks through the portal and public API.
- Remove the decorative deployment-form milestone strip while retaining live progress for in-flight deployments.

## 2.3.3

- Remove duplicate status, version and internal identifier details from deployment history.
- Keep one concise outcome line for a single device and expandable per-device results for groups.
- Poll active deployments more frequently and move completed work into history without a page refresh.

## 2.3.2

- Retain graphical milestone progress for deployments that are still in flight.
- Present finished deployment history using the same compact chronological timeline as Activity.
- Show single-device results inline and place multi-device results in one expandable section.

## 2.3.1

- Reduce Overview to four linked fleet summaries and remove the duplicated
  deployment and activity sections.
- Keep release cards at a consistent four-column width, with responsive
  breakpoints for smaller screens, and abbreviate long source revisions while
  retaining the complete value as hover text.
- Organize profile settings into Profile details, Time and logging, Home
  Assistant, MQTT and Remote syslog groups.

## 2.3.0

- Replace the test-oriented portal with a consistent graphical fleet
  experience covering Overview, Deploy, Devices, Releases, Profiles, Activity
  and Settings.
- Add one universal deployment workflow for an update, a configuration
  profile, or both, targeting selected devices, one or more cohorts, or the
  whole enabled fleet.
- Stage scheduled updates immediately and derive activation windows from each
  device's reported automatic-update schedule; retain an explicit, audited
  administrator override for immediate installation.
- Persist deployment progress and per-device outcomes, reconcile them against
  live device state, and retain management actions in a durable audit timeline.
- Move profile application into the deployment workflow and replace raw policy
  and command JSON with concise, status-aware progress cards.
- Upgrade existing version-1 data stores in place with deployment and audit
  history tables.

## 2.2.23

- Stop displaying an update as installing when the device has returned online
  after activation but still reports its previous release sequence.
- Present that terminal rollback/startup outcome as a retryable deployment
  failure.

## 2.2.22

- Keep the deployment action status-aware through checking, staging,
  installation, device restart and confirmed inventory refresh.
- Present a concise version/device summary instead of exposing the number of
  queued fleet commands.
- Offer a clear retry action when the device stops an ordered deployment after
  a failed prerequisite.

## 2.2.21

- Distinguish a matching signing identity with an incompatible frozen device
  policy canonicalizer from a genuine key mismatch.
- Direct affected development devices to a universal/core update supporting
  typed format-2 fleet commands instead of repeatedly re-enrolling the key.

## 2.2.20

- Refresh device inventory immediately before signing a deployment and refuse
  to submit policy when the device's active Management signing-key fingerprint
  cannot be read.
- Surface the inventory polling error or both mismatched fingerprints and use
  the current **CA & signing trust** portal location in remediation guidance.

## 2.2.19

- Compare the device-reported active fleet verification-key fingerprint with
  the Management signing identity before creating a deployment policy.
- Report both fingerprints when trust differs, instead of sending a policy
  that the device must reject.

## 2.2.18

- Allow up to 30 seconds for fleet-policy verification and its HTTP response,
  avoiding a generic read timeout while constrained devices complete ECDSA
  verification and report an actionable trust error.
- Display the SHA-256 fingerprint of the active Management Suite verification
  key on Settings so administrators can confirm which identity is enrolled.

## 2.2.17

- Re-derive the downloadable Management Suite verification key from the
  persisted private signing identity on every start, repairing a missing or
  stale public-key file without rotating the identity.
- Replace the raw fleet-policy signature error with instructions for explicitly
  re-enrolling the current Management Suite signing key on the target device.

## 2.2.16

- Add a **Stage and install now** deployment action that signs an all-day
  maintenance window and queues check, download and activation commands
  immediately.
- Show maintenance start and end controls only for explicitly scheduled
  installations.

## 2.2.15

- Let direct and controlled deployments select a verified Application, Core or
  Universal update file instead of treating every artifact with the same
  release sequence as interchangeable.
- Import and promote releases containing any supported update bundle; an
  application-only or core-only GitHub release no longer requires a matching
  companion artifact.
- Sign fleet policy format 2 commands with the selected update type and make
  controlled deployments check that exact artifact before downloading it.
- Rename deployment-facing release actions to Updates and show the selected
  artifact type in deployment choices, queued-command status and rollout cards.

## 2.2.14

- Replace raw deployment and profile JSON responses with concise inline status
  summaries that preserve the current page and selected workflow context.
- Make release deployment controls status-aware while matching commands remain
  queued on the selected device, with consistent busy, success and error states.
- Align primary, secondary, destructive and disabled controls with the IoT-MD
  portal interaction model.

## 2.2.13

- Allow registered devices to be edited in place, including cohort, name,
  address, port and management-enabled state.
- Derive controlled-deployment cohorts from enabled device assignments instead
  of suggesting unrelated `canary,main` values.
- Put Device before Release in the direct deployment workflow.
- Add persistent, reusable non-secret configuration profiles and permissioned
  profile deployment to compatible IoT-MD devices.

## 2.2.12

- Restore the top-level portal navigation after a malformed deployment status
  message prevented client-side page initialization.
- Make route section visibility CSS-driven so a client-side script failure can
  no longer expose every portal section as one continuous page.

## 2.2.11

- Replace the separate Policy and Rollouts navigation with one plain-language
  Deployments workflow for choosing a verified release, target device, staging
  behavior and installation window. Keep cohort deployment as an advanced,
  optional control. Remove the obsolete Policy and Rollouts portal routes.
- Replace maintenance minutes-after-midnight and duration inputs with local
  start and end time controls, including overnight and all-day windows.
- Preserve the compact signed policy representation by converting the selected
  times to `start_minute` and `duration_minutes` in the controller.
- Present Device API JSON errors directly instead of nesting escaped JSON in a
  second error object.

## 2.2.10

- Target signed policies with the immutable Device API `device_id` discovered
  from inventory instead of the Management Suite's friendly record ID.
- Clarify that Management ID is a local label and require a successful inventory
  poll before a policy can be signed for a device whose identity is unknown.

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

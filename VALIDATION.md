# Validation completed

All **45 automated checks passed**. The integration checks built actual small
RPMs with `rpmbuild`, downloaded them from a local HTTP server, verified their
digests with `rpmkeys`, and generated real repository metadata with
`createrepo_c`.

Verified behaviors:

- Numeric RPM version ordering, epochs, package releases, prereleases and
  snapshot suffixes use librpm comparison.
- Three distinct versions are retained for each package name/architecture;
  changed upstream filenames do not alter that grouping.
- A fresh upstream copy wins over a duplicate of the same RPM version.
- Unsafe download filenames are rejected; signed URL tokens are not recorded
  as identities or printed in download logs.
- Citrix discovery chooses RPM assets instead of DEB assets and requires the
  published checksum.
- Rambox skips betas and falls back to a stable release with an RPM when the
  latest release has none; it can handle a generic single-RPM filename too.
- An interrupted/truncated download or incorrect upstream checksum does not
  modify the public repository or prune its package history.
- A damaged RPM payload fails integrity verification.
- Existing flat repositories migrate to content-addressed `Packages/` paths;
  a downloaded RPM's bytes remain unchanged.
- Existing hash-prefixed packages migrate to readable RPM-header-based names
  without downloading the same bytes again. Metadata advertises the new paths,
  exactly three versions remain, and repeated runs do not add more suffixes.
- The short checksum suffix distinguishes different package contents; full
  checksums are verified and an existing URL containing different bytes is
  not overwritten.
- Metadata generation failure or metadata validation failure leaves the
  previous published repository intact.
- Repeated unchanged updates skip metadata generation.
- Download acceptance state updates correctly even when a new upstream
  revision has identical package bytes.
- A concurrent update cannot acquire the same repository lock.
- Abandoned staging from a killed process is cleaned on the next run.
- Current and previous metadata objects are retained across an update while
  package retention stays at three versions.
- Dry runs publish and delete nothing.
- All three applications load their discovery code from separate service
  modules. Bitwarden's official filename/digest handling is tested separately.
- A newly created service is discovered and publishes a real RPM repository
  without adding its name to the common engine.
- Selecting one service does not import broken unrelated vendor code.
- `--repo all` runs only installed service directories and continues to other
  services when one service fails.
- Fresh selective installation includes only the selected service and its
  extra dependencies. Other installed services are not redeployed.
- Actual code deployment into a temporary installation installs only the
  selected module and runs its installed CLI; another deployment preserves
  unselected vendor code and existing state files.
- Migration from the former bundled updater preserves its installed service
  modules while immediate sync remains limited to the requested services.
- Cron migration preserves unselected services, unrelated jobs and comments;
  repeat installation does not add duplicate schedules.
- Invalid IDs, package names, dependencies and schedules are rejected.
- Installation requires an explicit selection; every service has its own
  installer entry point.
- The starter template refuses its placeholder URL and revalidates fixed
  latest-RPM URLs that have no ETag or Last-Modified revision.

Separate shell checks passed for the installer syntax and cron migration;
the cron transformation preserves unrelated jobs.

Live upstream discovery was also checked successfully:

- Bitwarden: `Bitwarden-2026.9.1-x86_64.rpm`, published SHA-256 available.
- Citrix: `ICAClient-rhel-gcc-8-26.04.10.1-0.x86_64.rpm`, corresponding USB
  and App Protection packages, all with published SHA-256 checksums.
- Rambox: `Rambox-2.7.1-linux-x64.rpm`, digest available. The latest release
  entry was `v3.0.0` with no RPM; the stable RPM fallback selected `v2.7.1`.

The local test environment was Ubuntu 24.04, Python 3.12, RPM 4.18.2 and
createrepo_c 0.17.3. Your reported LXC uses Fedora 44, RPM 6.0.1 and
createrepo_c 1.2.1. Fedora dependency names and relevant current RPM/DNF
interfaces were checked against official documentation. RPM integrity checks
explicitly select the digest verification level for this read-only operation;
no global RPM policy is changed.

You reported that the previous bundle works on your LXC. This revision's
installer changes (selective deployment, migration, Europe/Sofia timezone,
service restarts and wider Apache listing) have passed shell syntax checks but
have not been run on your LXC
from this environment. Full current vendor RPMs were not downloaded here.
The installer validates Apache's configuration on your container and runs a
sync. Review its output, local date and browser listing after applying it.

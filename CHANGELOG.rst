===================================================
Lightning IT Collection Release Notes Release Notes
===================================================

.. contents:: Topics

v1.24.0
=======

Minor Changes
-------------

- Document Nexus candidates, exact-byte validated Galaxy promotion, transition limitations, and per-Collection cutover evidence.
- Release preparation and back-sync rely on native central review events instead of dispatching the retired local review workflow.
- logrotate - add an operating-system RPM role with validated explicit rules and the existing vendor timer.
- logrotate - optionally prepare private existing-account-owned native Podman log files and persistent SELinux labels in the standard component namespace before application startup, preserving existing content and refusing foreign owners, links or unsafe directory metadata.
- logrotate - reject protected data/backup paths and document per-file retention and polling limits explicitly.
- logrotate - replace the unshipped experimental component-log helper role with the RPM-based role.

Bugfixes
--------

- logrotate - Preserve case-distinct Linux file paths and native component directories during deduplication.
- logrotate - Reject symbolic-link components in all source and protected paths before mutation.
- logrotate - Require runtime-owned mode-0600 replacement files for prepared container rename rotation.
- logrotate - accept canonical positive archive-count text forwarded through Ansible variables while rejecting fractions, booleans and out-of-range values.
- logrotate - defer timer tasks and restart handlers during fresh-install check mode until the RPM vendor unit exists.
- logrotate - fix gzip numbering and verify effective archive names with the vendor parser before preparation without changing global configuration.
- logrotate - include dot-prefixed numbered archives in label and hardlink checks, and defer SELinux label validation in check mode when the target Python bindings have not yet been installed; real applies still fail.
- logrotate - keep archives beside validated log sources and explicitly override inherited olddir, copying, hardlink and shredding settings to preserve protected data.
- logrotate - limit SELinux mappings and non-recursive relabeling to selected native log files, their numbered archives and required directories; retire the former role-owned namespace mapping without traversing protected data.
- logrotate - pass timer calendars after the command option boundary so option-like values cannot bypass calendar validation before timer configuration is published.
- logrotate - preserve traversal of private runtime probe ancestry under a restrictive root umask without changing the ownership or access mode of the probe logs.
- logrotate - reject a selected source silently skipped by the vendor because an earlier overlapping rule enables ignoreduplicates, even when the debug command returns success.
- logrotate - reject existing hardlinked rotation sources before installing a rule.
- logrotate - reject numbered active sources and archive families that collide with selected active paths or protected trees before preparation.
- logrotate - repeat the effective vendor configuration check after native log file preparation, so existing wildcard rules cannot silently overlap a newly created source.
- logrotate - tolerate only unrelated existing date-archive collisions during forced debug validation, preserving candidate and parser failure checks.
- logrotate - validate candidates at their actual include position in a private read-only directory view so later global resets cannot hide inherited names.
- logrotate - validate real source paths against existing vendor rules before substituting private archive-layout fixtures.
- podman - manage rootless storage file-context mappings through community.general.sefcontext and report restorecon changes only for actual relabels, avoiding repeated changes on an already configured EL10 host.

v1.23.0
=======

Minor Changes
-------------

- Add users_accounts_global as a final user-definition layer whose values take precedence over base and extra definitions for the same account.
- Delegate SELinux, XRDP, and every ``protected-incus`` scenario to the pinned ``modulix-validation`` reusable workflow with an immutable candidate, owner-scoped Incus cleanup, meaningful success markers, and normalized evidence.
- collection_tooling - Synchronize the centrally managed Renovate policy and guarded automation workflows.

v1.22.1
=======

Minor Changes
-------------

- developer_tools - Add configurable Node.js/npm/npx installation and markdownlint-cli2 validation support.
- docs - Apply the shared enterprise README structure.
- docs - Consolidate generated governance metadata and license policy on shared-assets-lit.
- release_model - Add managed compatibility matrix documentation and structured release evidence fields.

Bugfixes
--------

- shared_tooling - Dispatch collection publishing from protected main with the managed release environment and Galaxy credential, make lint compatibility detection rely on the execution environment, and ignore generated local collection-install and Python cache artifacts.

v1.22.0
=======

Minor Changes
-------------

- developer_tools - Add optional actionlint installation from the upstream release archive with checksum verification.

v1.21.0
=======

Bugfixes
--------

- Mark rootless Podman SELinux fcontext add and modify tasks as changed only when their guarded semanage commands run, keeping the role lint-clean while preserving idempotent task gating.

v1.20.0
=======

Minor Changes
-------------

- lit.rhel - Verify automated collection release workflow cycle 2.

v1.19.0
=======

Minor Changes
-------------

- lit.rhel - Verify automated collection release workflow cycle 1.

v1.18.0
=======

Removed Features (previously deprecated)
----------------------------------------

- rhel - Remove stale semantic-release npm configuration from the collection release workflow.

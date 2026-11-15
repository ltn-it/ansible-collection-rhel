# lit.rhel.logrotate

Install the operating system `logrotate` RPM, validate explicit application rules,
and manage the existing vendor timer. This role installs no Python log helper,
metadata reader, conmon wrapper, or additional service.

## Requirements

RHEL-family systems with systemd, configured RPM repositories and root privileges.
The consumer owns log writer configuration, reopen behavior and labels outside
the optionally managed standard container-log namespace. That preparation requires
a pre-existing non-root account and the Podman SELinux policy providing
`container_log_t` when SELinux is active.
All paths must be explicit absolute regular-file paths, without globs or symlinks.
Declare data and backup trees in `logrotate_protected_directories`.

The fast `logrotate-basic` Molecule scenario builds and installs a fresh collection,
then checks its input assertions, backup exclusion, template output and controller
idempotence. It does not install an RPM or exercise a real log writer/systemd.
Those runtime cases remain required before deployment.

This role does **not** by itself establish a hard file-size or component quota:

- `maxsize` is evaluated only when logrotate runs. The writer must enforce any
  required hard ceiling between checks.
- `rotate` counts archives **per input file**, not per component across files.
- `maxage` removes old archives when the corresponding log is rotated; it is
  not an independent expiry service for missing log files.
- `copytruncate` can lose records between copying and truncation. Prefer a writer
  that can reopen after rename where the application supports it.

See the [upstream manual](https://github.com/logrotate/logrotate/blob/main/logrotate.8.in).
Do not present these directives as proof of an aggregate retention requirement.

## Variables

See [defaults/main.yml](defaults/main.yml). Set `logrotate_name` and
`logrotate_paths`; defaults select daily rotation or `maxsize 100M`, seven archives,
14 days, immediate compression, numeric archive suffixes and mode `0640`.
`logrotate_user`/`logrotate_group` set `su` and the new file owner/group.
Select `logrotate_copytruncate` only with an understood writer tradeoff, or supply
the consumer's trusted `logrotate_postrotate` reopen commands. No default command
signals or restarts an application.

Archives remain beside their validated source files. Each stanza explicitly
disables inherited `olddir`, `copy`, `renamecopy`, hardlink rotation and shredding;
rename mode also disables inherited `copytruncate`. Existing hardlinked active
sources are rejected before rule installation. These local settings keep global
archive destinations or copy policies from bypassing protected-directory checks.

An empty `logrotate_timer_calendar` preserves the vendor schedule and removes only
the role-owned `60-lit-logrotate.conf` override. A nonempty calendar changes the
single vendor timer for **all** rules: consumers must agree on one shared value.
The role validates it with `systemd-analyze calendar`. An application rule is
validated with the installed RPM's `logrotate --debug` before replacement.
This role never forces rotation or creates/deletes application data or backups.

Archive counts accept positive integers or their canonical decimal text form,
so Ansible role-variable forwarding also works when native templating is disabled.
Booleans, fractions, signs, leading zeros and values above 1000 are rejected.

For a rootless container setup, `logrotate_prepare_container_files: true`
prepares only paths under `/var/log/podman-components/<component>/<file>.log`.
Supply the already existing non-root `logrotate_container_user` and primary
`logrotate_container_group` explicitly. The protected root is owned by root;
component directories are private to that account and newly created files use
mode 0600. Existing content and timestamps are preserved. Foreign owners,
non-private files/directories, links and paths outside the flat namespace are
not repurposed. Other rotation sources, such as separately managed backup
operation logs, are not created by this option. Active SELinux uses the standard
persistent `container_log_t` mappings restricted to selected files, numbered
archives (including dot-prefixed names) and exact required directories.
Relabeling is non-recursive; protected
subtrees and unrelated files are not relabeled. Existing archives with multiple
hard links are rejected before relabeling. The former role-owned namespace-wide
mapping is retired only when its current type is `container_log_t`; a foreign
type at that target stops the role. If target Python bindings are missing,
check mode explicitly defers label validation instead of pretending that a
predicted package installation made them available. Real applies fail on missing
bindings after prerequisite installation. No capture program or additional service
is installed. Normal operating-system and account preparation remains required.

## Dependencies

No additional collection role dependency. Uses `ansible.builtin` modules and
`community.general.sefcontext` for the optional SELinux preparation.

## Example Playbook

```yaml
---
- name: Configure application log rotation
  hosts: application_servers
  become: true
  roles:
    - role: lit.rhel.logrotate
      logrotate_name: application
      logrotate_paths: [/var/log/application/application.log]
      logrotate_protected_directories: [/srv/application/data, /srv/application/backups]
      logrotate_timer_calendar: "*-*-* *:*:00"
```

The example configures polling rules, not a proven hard storage ceiling. Validate
real rotation, compression, reopen behavior, expiry, idempotence and the writer's
limits on the intended test target before delivery. Package build/syntax checks
alone do not prove runtime behavior.

## License

MIT.

## Author

Lightning IT.

All source and protected paths, including their ancestors, must be free of
symbolic links. This is checked before preparation or rule installation.
With container-file preparation and rename rotation, set `logrotate_user` and
`logrotate_group` to the declared runtime account/group and
`logrotate_create_mode: "0600"`. Incompatible replacement metadata is rejected
before mutation; `copytruncate` keeps the original prepared inode and metadata.

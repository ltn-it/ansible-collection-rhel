# lit.rhel.logrotate

Install the operating system `logrotate` RPM, validate explicit application rules,
and manage the existing vendor timer. This role installs no Python log helper,
metadata reader, conmon wrapper, or additional service.

## Requirements

RHEL-family systems with systemd, configured RPM repositories and root privileges.
The consumer owns log writer configuration, reopen behavior and SELinux labels.
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

An empty `logrotate_timer_calendar` preserves the vendor schedule and removes only
the role-owned `60-lit-logrotate.conf` override. A nonempty calendar changes the
single vendor timer for **all** rules: consumers must agree on one shared value.
The role validates it with `systemd-analyze calendar`. An application rule is
validated with the installed RPM's `logrotate --debug` before replacement.
This role never forces rotation or creates/deletes application data or backups.

## Dependencies

No additional collection role dependency. Uses `ansible.builtin` modules only.

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

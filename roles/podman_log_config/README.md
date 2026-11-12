# Rootless component log policy

Experimental host-local configuration for one rootless Podman container per
component. Native size/daily rotation and operational command output share one
archive budget. Defaults enforce 100 MiB per active file, at most seven archived
files including working files, fourteen days maximum age and less than 1 GiB
reserved log bytes per component. No minimum history is promised.

## Requirements

- RHEL-family Linux with Python 3.9+, systemd, rootless Podman and conmon 2.2.1.
- Existing unprivileged runtime user, home, runtime directory and graphroot.
- Privileged installation; collector and captured commands run as the runtime
  user. Native log files and the archive directory must share one filesystem.
- The consumer must configure `k8s-file`, `--log-opt=max-size=104857600` and select
  the installed conmon wrapper for its persistent Podman execution path. Existing
  services can be explicitly selected through `podman_log_config_quadlet_path`.
  Otherwise their configuration remains the consumer's responsibility.
- Application control must admit the exact reviewed helper/module/wrapper bytes
  through its existing controlled process. This role neither disables protection
  nor automatically adds trust entries.

This role is not production-accepted. Its local basic scenario covers actual
file/configuration installation, idempotence and synthetic filesystem/process
behavior. Actual conmon concurrency, reboot, service restart/recreation, real disk
exhaustion, application readiness and restore integration still require native
cross-service acceptance in the owning validation workflow.

## Variables

See `defaults/main.yml`. Required inputs are component/container identifiers,
runtime user/group/UID/home, graphroot and a dedicated component log directory.
Set `podman_log_config_operational: true` to allow captured operational output.
Do not point the component directory at database storage, transaction logs or a
backup directory. Configurations are instance-specific; helper code is shared.

`podman_log_config_manage_service: true` installs and enables a system timer
running every thirty seconds as that user. Setting it to false installs the same
files without accessing the system service manager; this is intended for isolated
installation checks and is not evidence of an active logging policy.

`podman_log_config_timer_enabled` defaults to true. With service management enabled,
setting it to false disables/stops this component's timer and stops its in-flight
collector before reconciling an explicitly selected native writer. Files and
private directories still use the actual runtime ownership. This supports a staged
consumer: prepare files with no native source selected and collection disabled,
preserve the old logs, reconcile the selected native source while collection stays
disabled, verify the completed archive handoff, then enable collection. Enabling
the timer with an unchanged native drop-in does not restart the application.
An interrupted collector can leave recoverable archive work; the consumer must
handle that state before pinning another native log. This switch does not itself
preserve logs, coordinate backup/restore jobs or verify application readiness.
It has no service-manager effect when `podman_log_config_manage_service` is false.

Set `podman_log_config_quadlet_path` to an existing rootless `.kube` or `.container`
file to install its native log-policy drop-in. Its generated service is derived
from the filename; set `podman_log_config_quadlet_service` only when the source
defines a different service name. Before a real change, the existing service's
`SourcePath` must match that exact file. A changed drop-in reloads the user manager
and restarts only the selected service. Real runs also load the installed helper
under the runtime UID with NoNewPrivileges and inspect the running native writer's
identity, log path, size and rotation arguments. This inspection does not collect
or rotate logs. If an earlier restart failed after writing the drop-in, an
unchanged retry reconciles the still-unapplied runtime policy. An already active
policy avoids a duplicate restart after a lost acknowledgement. The final runtime
inspection must pass before the role can enable collection; a zero service-manager
exit alone is insufficient. An inspection failure outside the recognized inactive
policy/running-state cases stops before a service restart. Check mode does not
claim runtime-policy acceptance.
No base manifest or unrelated unit is replaced. The log policy is identical for components
sharing one Quadlet source. Rootful units and implicit service discovery are not
supported by this interface.

Before the first real native activation, install/review/admit the exact helper and
wrapper bytes with service management disabled. Then perform the separately
authorized native activation with that existing trust in place. The consumer
must verify a suitable backup, idle backup/restore operations and application
readiness/rollback for its actual topology. This role does not infer such guards
from a successful metadata or controller check.

The native wrapper explicitly keeps two native archives. In conmon 2.2.1 even a
one-archive setting shifts `.1` to `.2`; the budget must reserve both. Together
with one claimed raw file and one temporary gzip, this leaves three published
compressed archives. Retention removes the oldest published logs first. Gzip
framing may make incompressible archives slightly larger than 100 MiB; the size
reservation includes this overhead. Native `.new` is an empty active replacement.
See the versioned [native rotation implementation](https://github.com/containers/conmon/blob/v2.2.1/src/ctr_logging.c)
and [control protocol](https://github.com/containers/conmon/blob/v2.2.1/src/ctrl.c).

All native writers owned by the runtime user are checked for open claimed inodes.
Unreadable process metadata fails closed. A pending writer is deferred; it is
never compressed while open. Recognized unfinished gzip files are regenerated
from retained raw input. Publication recovery avoids duplicate archive retention.
Unexpected files, links, oversized input and ambiguous native identity stop work.
Only explicitly recognized log files in the private store can be pruned.

Operational commands use the installed Python entry point with `--config`, then
`--capture` followed by an absolute executable and its arguments. No shell is
invoked. Combine stdout and stderr; return the child status. The actual consuming
systemd service must use `KillMode=control-group` and appropriate timeout/cleanup
to reap descendants if log capture fails. Avoid secrets in command output. Native
conmon files retain the container stdout/stderr format; operational files are raw
command output. This role does not add an external log forwarding service.

## Dependencies

No additional Ansible collection modules. Prepared Podman/OS state is consumed,
not installed or repaired. Application orchestration remains with the consumer's
existing persistent `lit.foundational.podman_systemd` path.

## Example Playbook

```yaml
- name: Configure one prepared component log policy
  hosts: application_hosts
  become: true
  roles:
    - role: lit.rhel.podman_log_config
      vars:
        podman_log_config_component: database-example
        podman_log_config_container: example-database
        podman_log_config_user: runtime
        podman_log_config_group: runtime
        podman_log_config_uid: 12000
        podman_log_config_home: /home/runtime
        podman_log_config_graphroot: /srv/runtime/storage
        podman_log_config_directory: /srv/runtime/logs/database-example
        podman_log_config_operational: true
```

Local validation from the collection root:

```sh
pre-commit run --all-files
bash scripts/devtools-molecule.sh podman-log-config-basic
```

Upgrade by installing the complete same-version helper set and revalidating all
consumers before changing shared bytes. Existing archive names are stable across
container recreation because the private store is independent of its ID. Native
inspection binds each source path to the current container's graphroot/ID; do not
remove an old container before its remaining log archive migration is verified.
Rollback: stop/disable only this component's timer, restore the consumer's prior
native configuration and reviewed helper set, then verify application readiness.
Keep collected logs; do not treat backup/transaction data as rollback cleanup.
Uninstallation/data deletion is intentionally not automated by this role.

## License

MIT

## Author

Lightning IT

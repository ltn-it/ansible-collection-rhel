"""Exercise the shipped parser probe's error boundary without running the OS validator."""

import contextlib
import grp
import io
import json
import os
import pathlib
import pwd
import subprocess
import sys
import types
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


class EffectiveLayoutTests(unittest.TestCase):
    def probe(self, rc, error):
        tasks = yaml.safe_load((ROOT / "roles/logrotate/tasks/effective_layout.yml").read_text())
        task = next(t for t in tasks if t["name"].startswith("Probe effective archive names"))
        program = task["ansible.builtin.command"]["argv"][2]
        values = dict(user="fixture", group="fixture", sources=["/var/log/example/application.log"],
                      configuration='"/var/log/example/application.log"\n{ daily }\n')
        uid, gid = os.getuid(), os.getgid()

        def vendor(arguments, **_kwargs):
            base = pathlib.Path(arguments[-1]).parent
            path = base / "logs/0/application.log"
            return subprocess.CompletedProcess(arguments, rc, str(path) + " -> " + str(path) + ".1.gz\n",
                                               error.replace("CANDIDATE", str(path)))

        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(values))), mock.patch.object(
            sys, "argv", ["fixture", "/etc/logrotate.conf"]
        ), mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
            pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=uid)
        ), mock.patch.object(grp, "getgrnam", return_value=types.SimpleNamespace(gr_gid=gid)), mock.patch.object(
            subprocess, "run", side_effect=vendor
        ), contextlib.redirect_stdout(output):
            exec(compile(program, "packaged-layout-probe", "exec"), {})  # noqa: S102 -- exact shipped code, mocked OS call.
        return json.loads(output.getvalue())

    def test_accepts_only_unrelated_existing_archive_collisions(self):
        result = self.probe(1, "error: destination /var/log/cron-20261115 already exists, skipping rotation\n")
        self.assertTrue(result["ok"])
        self.assertEqual(result["unrelated_existing_date_archives"], 1)

    def test_rejects_a_candidate_archive_collision(self):
        self.assertFalse(self.probe(1, "error: destination CANDIDATE.1.gz already exists, skipping rotation\n")["ok"])

    def test_rejects_syntax_errors_even_with_an_unrelated_collision(self):
        errors = ("error: destination /var/log/cron-20261115 already exists, skipping rotation\n"
                  "error: /etc/logrotate.conf:1 unknown option\n")
        self.assertFalse(self.probe(1, errors)["ok"])

    def test_rejects_nonzero_exit_without_the_exact_benign_diagnostic(self):
        self.assertFalse(self.probe(1, "")["ok"])
        self.assertFalse(self.probe(2, "error: destination /var/log/cron-20261115 already exists, skipping rotation\n")["ok"])


if __name__ == "__main__":
    unittest.main()

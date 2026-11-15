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
import tempfile
import types
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


class EffectiveLayoutTests(unittest.TestCase):
    def probe(self, rc, error, rename_extension=False, before="", after="", unsafe_snapshot=False,
              conflict=False, ignored_source=None, restricted_umask=False):
        tasks = yaml.safe_load((ROOT / "roles/logrotate/tasks/effective_layout.yml").read_text())
        task = next(t for t in tasks if t["name"].startswith("Probe effective archive names"))
        program = task["ansible.builtin.command"]["argv"][2]
        values = dict(name="fixture", user="fixture", group="fixture", sources=["/var/log/example/application.log"],
                      configuration='"/var/log/example/application.log"\n{ daily }\n')
        uid, gid = os.getuid(), os.getgid()

        def vendor(arguments, **_kwargs):
            base = pathlib.Path(arguments[-1]).parent
            path = base / "logs/0/application.log"
            self.assertEqual((base / 'logs').stat().st_mode & 0o111, 0o111)
            self.assertEqual(arguments[-2], str(global_config))
            self.assertEqual(arguments[-3], str(original))
            self.assertEqual(arguments[0], "/usr/bin/unshare")
            self.assertIn("remount,bind,ro", arguments[6])
            shadow = pathlib.Path(arguments[-4])
            self.assertEqual((shadow / "unrelated").read_text(), "unchanged vendor rule\n")
            self.assertEqual((shadow / "unrelated").stat().st_uid, (original / "unrelated").stat().st_uid)
            if '--debug --force' not in arguments[6]:
                self.assertIn(values['sources'][0], (shadow / "fixture").read_text())
                self.assertNotIn(str(path), (shadow / "fixture").read_text())
                diagnostic = ('error: duplicate log entry /var/log/example/application.log\n' if conflict else
                              'fixture:3 ignore duplicate log entry for ' + ignored_source + '\n'
                              if ignored_source is not None else '')
                return subprocess.CompletedProcess(arguments, 1 if conflict else 0, '', diagnostic)
            self.assertIn(str(path), (shadow / "fixture").read_text())
            archive = path.with_name("application.1.log.gz") if rename_extension else pathlib.Path(str(path) + ".1.gz")
            return subprocess.CompletedProcess(arguments, rc, str(path) + " -> " + str(archive) + "\n",
                                               error.replace("CANDIDATE", str(path)))

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        original = pathlib.Path(temporary.name) / "rules"
        original.mkdir()
        (original / "unrelated").write_text("unchanged vendor rule\n")
        if unsafe_snapshot:
            os.link(original / "unrelated", original / "linked")
        global_config = original.parent / "global.conf"
        global_config.write_text(before + "include " + str(original) + "\n" + after)
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(values))), mock.patch.object(
            sys, "argv", ["fixture", str(global_config), str(original)]
        ), mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
            pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=uid)
        ), mock.patch.object(grp, "getgrnam", return_value=types.SimpleNamespace(gr_gid=gid)), mock.patch.object(
            subprocess, "run", side_effect=vendor
        ), contextlib.redirect_stdout(output):
            old_umask = os.umask(0o077) if restricted_umask else None
            try:
                exec(compile(program, "packaged-layout-probe", "exec"), {})  # noqa: S102 -- shipped code, mocked OS call.
            finally:
                if old_umask is not None:
                    os.umask(old_umask)
        self.assertEqual({path.name for path in original.iterdir()}, {"unrelated", "linked"} if unsafe_snapshot else {"unrelated"})
        self.assertEqual(global_config.read_text(), before + "include " + str(original) + "\n" + after)
        self.assertEqual((original / "unrelated").read_text(), "unchanged vendor rule\n")
        return json.loads(output.getvalue())

    def test_rejects_names_inherited_before_include_despite_a_later_reset(self):
        result = self.probe(0, "", rename_extension=True, before="extension .log\n", after="extension .other\n")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "incompatible_archive_names")

    def test_accepts_numbered_names_at_include_despite_incompatible_end_settings(self):
        self.assertTrue(self.probe(0, "", before="extension .other\n", after="extension .log\n")["ok"])

    def test_runtime_probe_ancestry_remains_searchable_under_private_root_umask(self):
        self.assertTrue(self.probe(0, "", restricted_umask=True)["ok"])

    def test_refuses_a_snapshot_that_cannot_preserve_configuration_identity(self):
        result = self.probe(0, "", unsafe_snapshot=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "probe_failed")

    def test_accepts_only_unrelated_existing_archive_collisions(self):
        result = self.probe(1, "error: destination /var/log/cron-20261115 already exists, skipping rotation\n")
        self.assertTrue(result["ok"])
        self.assertEqual(result["unrelated_existing_date_archives"], 1)

    def test_rejects_a_candidate_archive_collision(self):
        self.assertFalse(self.probe(1, "error: destination CANDIDATE.1.gz already exists, skipping rotation\n")["ok"])

    def test_rejects_a_real_path_conflict_before_substituting_layout_paths(self):
        result = self.probe(0, "", conflict=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "vendor_configuration_failed")

    def test_rejects_a_selected_source_ignored_despite_a_successful_vendor_exit(self):
        result = self.probe(0, "", ignored_source="/var/log/example/application.log")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "selected_source_ignored")

    def test_preserves_unrelated_deliberately_ignored_vendor_logs(self):
        self.assertTrue(self.probe(0, "", ignored_source="/var/log/other/application.log")["ok"])
        self.assertTrue(self.probe(0, "", ignored_source="/var/log/example/application.log.old")["ok"])

    def test_rejects_syntax_errors_even_with_an_unrelated_collision(self):
        errors = ("error: destination /var/log/cron-20261115 already exists, skipping rotation\n"
                  "error: /etc/logrotate.conf:1 unknown option\n")
        self.assertFalse(self.probe(1, errors)["ok"])

    def test_rejects_nonzero_exit_without_the_exact_benign_diagnostic(self):
        self.assertFalse(self.probe(1, "")["ok"])
        self.assertFalse(self.probe(2, "error: destination /var/log/cron-20261115 already exists, skipping rotation\n")["ok"])


if __name__ == "__main__":
    unittest.main()

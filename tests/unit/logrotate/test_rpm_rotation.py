"""Real RPM file rotation; requires an explicit freshly installed role path.

This does not prove systemd activation, live writer reopen, or hard storage caps.
"""
import gzip
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


@unittest.skipUnless(shutil.which('logrotate') and os.geteuid()==0 and os.environ.get('LIT_LOGROTATE_ROLE_PATH'),
                     'requires RPM logrotate, isolated root container and installed role path')
class RpmRotationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='lit-logrotate-rpm-')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.log=self.root/'application.log'
        self.config=self.root/'application.conf'
        role=Path(os.environ['LIT_LOGROTATE_ROLE_PATH'])
        self.assertTrue(role.is_dir())
        values=yaml.safe_load((role/'defaults/main.yml').read_text())
        values.update(logrotate_name='rpm-fixture',logrotate_paths=[str(self.log)],logrotate_user='root',logrotate_group='root')
        template=Environment(undefined=StrictUndefined).from_string((role/'templates/application.conf.j2').read_text())
        self.config.write_text(template.render(**values)+'\n')
        self.config.chmod(0o600)

    def run_rotation(self,*args):
        result=subprocess.run(['/usr/sbin/logrotate','--state',str(self.root/'state'),*args,str(self.config)],
                              capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr.decode())

    def test_default_100_mib_threshold_compresses_exact_bytes_and_recreates_file(self):
        with self.log.open('wb') as stream:stream.truncate(100*1024*1024+1)
        before=hashlib.sha256()
        with self.log.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):before.update(chunk)
        self.run_rotation('--debug')
        self.assertEqual(self.log.stat().st_size,100*1024*1024+1)
        self.run_rotation()
        after=hashlib.sha256()
        with gzip.open(str(self.log)+'.1.gz','rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):after.update(chunk)
        self.assertEqual(before.digest(),after.digest())
        self.assertEqual(self.log.stat().st_size,0)
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode),0o640)
        self.log.write_bytes(b'below size threshold')
        self.run_rotation()
        self.assertEqual(self.log.read_bytes(),b'below size threshold')
        self.assertEqual(len(list(self.root.glob('application.log.*.gz'))),1)

    def test_seven_archives_per_file_and_fourteen_day_expiry_on_rotation(self):
        for index in range(10):
            self.log.write_bytes(('entry-'+str(index)).encode())
            self.run_rotation('--force')
        archives=list(self.root.glob('application.log.*.gz'))
        self.assertEqual(len(archives),7)
        self.assertEqual({gzip.decompress(p.read_bytes()) for p in archives},
                         {('entry-'+str(index)).encode() for index in range(3,10)})
        newest=Path(str(self.log)+'.1.gz')
        old=time.time()-20*24*3600
        os.utime(newest,(old,old))
        self.log.write_bytes(b'fresh entry')
        self.run_rotation('--force')
        self.assertNotIn(b'entry-9',{gzip.decompress(p.read_bytes()) for p in self.root.glob('application.log.*.gz')})


if __name__=='__main__':unittest.main()

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name != 'nt' and shutil.which('bash'), 'Requires a POSIX shell')
class DockerLauncherTests(unittest.TestCase):
    def test_symlink_launch_from_another_directory_forwards_arguments(self):
        source = Path(__file__).resolve().with_name('jviewer-docker.sh')
        with tempfile.TemporaryDirectory(prefix='jviewer shell ') as folder:
            root = Path(folder)
            for command in ('docker', 'xhost'):
                fake = root / command
                fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$PWD" "$@" >> "$JVIEWER_TEST_LOG"\n')
                fake.chmod(0o755)
            link = root / 'jviewer-docker'
            link.symlink_to(source)
            log = root / 'calls.txt'
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], JVIEWER_TEST_LOG=str(log))
            subprocess.run(['bash', str(link), '--host', 'https://ipmi:8443', '--username',
                            'user with spaces', '--insecure'], cwd=root, env=env, check=True)
            calls = log.read_text().splitlines()
            self.assertEqual(calls, [str(source.parent), '+localhost', str(source.parent),
                                    'compose', 'build', str(source.parent), 'compose', 'run',
                                    '--rm', '--entrypoint=', 'jviewer', '/usr/local/bin/jviewer-starter.py',
                                    '--host', 'https://ipmi:8443', '--username', 'user with spaces', '--insecure'])


if __name__ == '__main__':
    unittest.main()

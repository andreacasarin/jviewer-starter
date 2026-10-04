import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from test_jviewer_starter import config, starter

JAVA_HOME = os.getenv('JVIEWER_TEST_JAVA_HOME') or os.getenv('JAVA_HOME')


@unittest.skipUnless(JAVA_HOME, 'Set JVIEWER_TEST_JAVA_HOME to an x86 Java 8 JDK for the integration test')
class MacCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        suffix = '.exe' if os.name == 'nt' else ''
        cls.java = str(Path(JAVA_HOME) / 'bin' / ('java' + suffix))
        cls.javac = str(Path(JAVA_HOME) / 'bin' / ('javac' + suffix))
        version = subprocess.run([cls.java, '-version'], capture_output=True, text=True, check=True)
        if 'version "1.8.' not in version.stderr:
            raise unittest.SkipTest('Integration test requires Java 8')

    def make_fixture(self, folder, supported=True):
        sources = {
            'com/ami/iusb/FloppyRedir.java': '''package com.ami.iusb;
              public class FloppyRedir { public native byte ReadKeybdLEDStatus(); }''',
            'com/ami/kvm/jviewer/softkeyboard/SoftKeyboard.java': '''package com.ami.kvm.jviewer.softkeyboard;
              public class SoftKeyboard { public byte leds; public void setLEDs(byte status) { leds = status; } }''',
            'com/ami/kvm/jviewer/gui/JViewerApp.java': '''package com.ami.kvm.jviewer.gui;
              import com.ami.iusb.FloppyRedir;
              import com.ami.kvm.jviewer.softkeyboard.SoftKeyboard;
              public class JViewerApp {
                private byte status;
                private SoftKeyboard m_softKeyboard = new SoftKeyboard();
                public void onKeybdLED(byte value) { status = new FloppyRedir().ReadKeybdLEDStatus(); }
                public void syncLED() { status = new FloppyRedir().ReadKeybdLEDStatus(); }
                public static void main(String[] args) {
                  JViewerApp app = new JViewerApp();
                  app.onKeybdLED((byte) 3);
                  app.syncLED();
                  if (app.status != 3 || app.m_softKeyboard.leds != 3) throw new AssertionError("LED state lost");
                  System.out.println("LED callback and sync completed without native libraries");
                }
              }'''
        }
        if not supported:
            sources['com/ami/kvm/jviewer/gui/JViewerApp.java'] = sources[
                'com/ami/kvm/jviewer/gui/JViewerApp.java'].replace('m_softKeyboard', 'changedKeyboard')
        paths = []
        for filename, source in sources.items():
            path = Path(folder) / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
            paths.append(str(path))
        classes = Path(folder) / 'classes'
        classes.mkdir()
        subprocess.run([self.javac, '-d', str(classes)] + paths, check=True, capture_output=True)
        original = Path(folder) / 'JViewer.jar'
        with zipfile.ZipFile(original, 'w') as jar:
            for path in classes.rglob('*.class'):
                jar.write(path, path.relative_to(classes).as_posix())
            jar.writestr('untouched.txt', 'unchanged resource')
            jar.writestr('META-INF/EXAMPLE.SF', 'old signature')
            jar.writestr('META-INF/EXAMPLE.RSA', 'old signature')
        return original

    def test_real_java_listener_regression_and_cache_invalidation(self):
        with tempfile.TemporaryDirectory(prefix='jviewer test ') as folder:
            original = self.make_fixture(folder)
            original_bytes = original.read_bytes()
            baseline = subprocess.run([self.java, '-cp', str(original),
                                       'com.ami.kvm.jviewer.gui.JViewerApp'], capture_output=True, text=True)
            self.assertNotEqual(baseline.returncode, 0)
            self.assertIn('UnsatisfiedLinkError', baseline.stderr)
            configuration = config(path=folder, jars=[str(original)], native_libraries=False, java=self.java)
            with patch.object(starter.platform, 'system', return_value='Darwin'), \
                 contextlib.redirect_stdout(io.StringIO()):
                with patch.object(starter.subprocess, 'run', wraps=subprocess.run) as commands:
                    if os.name == 'nt':
                        self.skipTest('Mac compatibility build is exercised on Linux and macOS')
                    classpath = starter.get_viewer_classpath(configuration)
                    self.assertEqual(original.read_bytes(), original_bytes)
                    self.assertEqual(commands.call_count, 2)
                    self.assertEqual(starter.get_viewer_classpath(configuration), classpath)
                    self.assertEqual(commands.call_count, 2)
                    with zipfile.ZipFile(original, 'a') as jar:
                        jar.writestr('firmware-update.txt', 'new resource')
                    updated = starter.get_viewer_classpath(configuration)
                    self.assertNotEqual(updated, classpath)
                    self.assertEqual(commands.call_count, 4)
            result = subprocess.run([self.java, '-Xverify:all', '-cp', updated,
                                     'com.ami.kvm.jviewer.gui.JViewerApp'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('completed without native libraries', result.stdout)
            with zipfile.ZipFile(original) as source, zipfile.ZipFile(updated) as target:
                changed = [name for name in target.namelist() if target.read(name) != source.read(name)]
                self.assertEqual(changed, ['com/ami/kvm/jviewer/gui/JViewerApp.class'])
                self.assertNotIn('META-INF/EXAMPLE.SF', target.namelist())
                self.assertNotIn('META-INF/EXAMPLE.RSA', target.namelist())

    def test_unknown_firmware_fails_without_modifying_original(self):
        if os.name == 'nt':
            self.skipTest('Mac compatibility build is exercised on Linux and macOS')
        with tempfile.TemporaryDirectory() as folder:
            original = self.make_fixture(folder, supported=False)
            before = original.read_bytes()
            configuration = config(path=folder, jars=[str(original)], native_libraries=False, java=self.java)
            with patch.object(starter.platform, 'system', return_value='Darwin'), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(subprocess.CalledProcessError):
                starter.get_viewer_classpath(configuration)
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(list(Path(folder).glob('JViewer-mac-*.jar')), [])


if __name__ == '__main__':
    unittest.main()

import contextlib
import importlib.util
import io
import os
from pathlib import Path
from types import SimpleNamespace
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
import zipfile

spec = importlib.util.spec_from_file_location('starter', Path(__file__).with_name('jviewer-starter.py'))
starter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(starter)


def jar_bytes(entries=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as jar:
        for name, value in (entries or {'viewer.class': b'example'}).items():
            jar.writestr(name, value)
    return data.getvalue()


def response(data, length=None):
    result = io.BytesIO(data)
    result.headers = {} if length is None else {'Content-Length': str(length)}
    return result


def descriptor():
    return ET.fromstring('''<jnlp codebase="https://ipmi:443/Java">
      <resources><jar href="release/JViewer.jar"/></resources>
      <resources os="Linux" arch="amd64"><nativelib href="release/Linux_x86_64.jar"/></resources>
      <resources os="Linux" arch="x86"><nativelib href="release/Linux_x86_32.jar"/></resources>
      <resources os="Windows" arch="amd64"><nativelib href="release/Win64.jar"/></resources>
      <application-desc>''' + ''.join('<argument>%s</argument>' % value for value in
          ['ipmi', '7578', 'PRIVATE_TOKEN', '0', '0', '0', '5120', '5123', '255',
           'Ctrl &amp; Alt', 'EN', 'PRIVATE_SESSION']) + '</application-desc></jnlp>')


def config(**kwargs):
    values = dict(server='https://ipmi', bits=64, timeout=5, refresh=False,
                  username='root', password='PRIVATE_PASSWORD', java='/jdk/bin/java', opener=Mock())
    values.update(kwargs)
    return SimpleNamespace(**values)


class ConfigurationTests(unittest.TestCase):
    def test_hosts(self):
        for host, expected in [('192.0.2.225', 'http://192.0.2.225'),
                               (' HTTPS://IPMI:443/ ', 'https://ipmi'),
                               ('ipmi:8080', 'http://ipmi:8080'),
                               ('https://[::1]:8443/', 'https://[::1]:8443')]:
            with self.subTest(host=host):
                self.assertEqual(starter.normalize_server(host), expected)

    def test_invalid_hosts(self):
        for host in ['', 'http://', 'ftp://ipmi', 'http://ipmi/page', 'http://user:pass@ipmi',
                     'https://ipmi?x=y', 'http://ipmi:bad', 'ip mi', 'https://ipmi#fragment']:
            with self.subTest(host=host), self.assertRaises(ValueError):
                starter.normalize_server(host)

    def test_java_path_with_spaces_and_actual_architecture(self):
        output = b' java.version = 1.8.0_345\r\n os.arch = amd64\r\n'
        with patch.dict(os.environ, {'JVIEWER_JAVA_HOME': '/Java With Spaces'}), \
             patch.object(starter.platform, 'system', return_value='Darwin'), \
             patch.object(starter.subprocess, 'run', return_value=SimpleNamespace(stdout=output)) as run:
            java, bits = starter.find_java()
        self.assertEqual((java, bits), (os.path.join('/Java With Spaces', 'bin', 'java'), 64))
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_unsupported_java(self):
        for version, arch in [('11.0.1', 'amd64'), ('1.8.0_345', 'aarch64'), ('1.8.0_345', 'x86_64_bad')]:
            with self.subTest(version=version, arch=arch), \
                 patch.dict(os.environ, {'JVIEWER_JAVA_HOME': '/jdk'}), \
                 patch.object(starter.subprocess, 'run', return_value=SimpleNamespace(
                     stdout=('java.version = %s\nos.arch = %s' % (version, arch)).encode())), \
                 self.assertRaises(RuntimeError):
                starter.find_java()

    def test_property_name_is_literal_and_last_line_works(self):
        self.assertEqual(starter.find_property('javaXversion = wrong\njava.version = 1.8.0', 'java.version'), '1.8.0')

    def test_tls_context_is_scoped_and_verifies_by_default(self):
        for insecure in (False, True):
            argv = ['starter', '--host', 'ipmi', '--username', 'root', '--password', 'test']
            if insecure:
                argv.append('--insecure')
            with patch.object(starter.sys, 'argv', argv), \
                 patch.object(starter, 'find_java', return_value=('/jdk/bin/java', 64)), \
                 patch.object(starter, 'HTTPSHandler') as handler, \
                 contextlib.redirect_stdout(io.StringIO()):
                starter.parse_configuration(starter.build_parser())
            context = handler.call_args.kwargs['context']
            self.assertEqual(context.check_hostname, not insecure)
            self.assertEqual(context.verify_mode, ssl.CERT_NONE if insecure else ssl.CERT_REQUIRED)

    def test_tls_flags_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            starter.build_parser().parse_args(['--insecure', '--ca-cert', 'ca.pem'])

    def test_timeout_and_missing_option_values_fail_before_prompting(self):
        for args in [['--timeout', '0'], ['--timeout', '-1'], ['--timeout', 'nan'], ['--timeout', 'inf'], ['--host']]:
            with self.subTest(args=args), patch.object(starter.sys, 'argv', ['starter'] + args), \
                 contextlib.redirect_stderr(io.StringIO()), patch('builtins.input') as prompt, \
                 self.assertRaises(SystemExit):
                starter.parse_configuration(starter.build_parser())
            prompt.assert_not_called()

    def test_quoted_java_options(self):
        with patch.dict(os.environ, {'JVIEWER_JAVA_OPTIONS': '-Dexample="value with spaces" -Xmx512m'}):
            self.assertEqual(starter.get_java_options('/cache'),
                             ['-Djava.library.path=/cache', '-Dexample=value with spaces', '-Xmx512m'])


class ProtocolTests(unittest.TestCase):
    def test_login_and_xml_arguments(self):
        configuration = config(server='https://ipmi:8443')
        configuration.opener.open.side_effect = [response(b"'SESSION_COOKIE' : 'abc123'"),
                                                  response(ET.tostring(descriptor()))]
        root = starter.fetch_jnlp(configuration)
        calls = configuration.opener.open.call_args_list
        self.assertEqual(calls[0].args[0].full_url, 'https://ipmi:8443/rpc/WEBSES/create.asp')
        self.assertIn('EXTRNIP=ipmi&', calls[1].args[0].full_url)
        self.assertEqual(calls[1].args[0].get_header('Cookie'), 'SessionCookie=abc123')
        self.assertEqual(root.find('application-desc').findall('argument')[9].text, 'Ctrl & Alt')
        for call in calls:
            self.assertEqual(call.kwargs['timeout'], 5)

    def test_invalid_login(self):
        configuration = config()
        configuration.opener.open.return_value = response(b"Login failed")
        with self.assertRaisesRegex(RuntimeError, 'login failed'):
            starter.fetch_jnlp(configuration)
        self.assertEqual(configuration.opener.open.call_count, 1)

    def test_invalid_jnlp(self):
        for payload in (b'<html>login</html>', b'not xml'):
            configuration = config()
            configuration.opener.open.side_effect = [response(b"'SESSION_COOKIE':'abc'"), response(payload)]
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                starter.fetch_jnlp(configuration)

    def test_wrong_jnlp_content_length(self):
        configuration = config()
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.read.side_effect = starter.IncompleteRead(b'<jnlp/>', 100)
        configuration.opener.open.return_value = broken
        request = starter.Request('https://ipmi/Java/jviewer.jnlp?x=y')
        self.assertEqual(starter.read_response(configuration, request), '<jnlp/>')
        with self.assertRaises(starter.IncompleteRead):
            starter.read_response(configuration, starter.Request('https://ipmi/login'))

    def test_platform_resources_follow_jnlp(self):
        for system, bits, expected in [('Darwin', 64, None), ('Linux', 64, 'Linux_x86_64.jar'),
                                       ('Linux', 32, 'Linux_x86_32.jar'), ('Windows', 64, 'Win64.jar')]:
            with self.subTest(system=system, bits=bits):
                resources = starter.select_resources(descriptor(), system, bits)
                self.assertEqual(resources[0], ('release/JViewer.jar', False))
                self.assertNotIn('release/JViewer-SOC.jar', [href for href, _ in resources])
                self.assertEqual(resources[1:] if expected else [],
                                 [('release/' + expected, True)] if expected else [])

    def test_required_linux_resources_are_not_silently_skipped(self):
        root = ET.fromstring('<jnlp><resources><jar href="JViewer.jar"/></resources></jnlp>')
        with self.assertRaisesRegex(RuntimeError, 'native libraries'):
            starter.select_resources(root, 'Linux', 64)

    def test_launch_preserves_exit_status_and_hides_tokens(self):
        configuration = config(path='/cache')
        stdout = io.StringIO()
        with patch.object(starter, 'fetch_jnlp', return_value=descriptor()), \
             patch.object(starter, 'update_jars'), \
             patch.object(starter, 'get_viewer_classpath', return_value='/cache/JViewer.jar'), \
             patch.object(starter.subprocess, 'call', return_value=7) as run, \
             contextlib.redirect_stdout(stdout):
            self.assertEqual(starter.run_jviewer(configuration), 7)
        self.assertIn('PRIVATE_TOKEN', run.call_args.args[0])
        self.assertIn('Ctrl & Alt', run.call_args.args[0])
        self.assertNotIn('PRIVATE_', stdout.getvalue())

    def test_invalid_launch_arguments_fail_before_downloads(self):
        with patch.object(starter, 'fetch_jnlp', return_value=ET.fromstring('<jnlp><application-desc/></jnlp>')), \
             patch.object(starter, 'update_jars') as update, self.assertRaises(RuntimeError):
            starter.run_jviewer(config())
        update.assert_not_called()

    def test_cli_errors_are_actionable_and_hide_session_queries(self):
        for error, message in [
            (starter.HTTPError('https://ipmi/JViewer.jar?session=PRIVATE_TOKEN', 404, 'Missing', {}, None), 'HTTP 404'),
            (starter.URLError(ssl.SSLCertVerificationError('self-signed')), '--ca-cert'),
            (starter.URLError(ConnectionRefusedError('refused')), 'network access'),
            (RuntimeError('IPMI login failed'), 'login failed'),
        ]:
            stderr = io.StringIO()
            with self.subTest(message=message), patch.object(starter, 'parse_configuration', side_effect=error), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(starter.main(), 1)
            self.assertIn(message, stderr.getvalue())
            self.assertNotIn('PRIVATE_TOKEN', stderr.getvalue())
            self.assertNotIn('Traceback', stderr.getvalue())

    def test_namespaced_jnlp_is_supported(self):
        configuration = config()
        payload = ET.tostring(descriptor()).replace(b'<jnlp ', b'<jnlp xmlns="http://example.test/jnlp" ')
        configuration.opener.open.side_effect = [response(b"'SESSION_COOKIE':'abc'"), response(payload)]
        root = starter.fetch_jnlp(configuration)
        self.assertEqual(starter.select_resources(root, 'Darwin', 64), [('release/JViewer.jar', False)])


class CacheTests(unittest.TestCase):
    def test_cache_is_per_server_and_java_architecture(self):
        first = starter.cache_path(config())
        self.assertNotEqual(first, starter.cache_path(config(server='https://another-ipmi')))
        self.assertNotEqual(first, starter.cache_path(config(bits=32)))

    def test_download_failure_preserves_existing_jar(self):
        for data, length in [(b'html login page', None), (jar_bytes(), 100000)]:
            with self.subTest(length=length), tempfile.TemporaryDirectory() as folder:
                destination = Path(folder) / 'JViewer.jar'
                original = jar_bytes({'original': b'working'})
                destination.write_bytes(original)
                configuration = config()
                configuration.opener.open.return_value = response(data, length)
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises((RuntimeError, zipfile.BadZipFile)):
                    starter.download_jar(configuration, 'https://ipmi/JViewer.jar', str(destination))
                self.assertEqual(destination.read_bytes(), original)
                self.assertEqual(list(Path(folder).iterdir()), [destination])

    def test_cached_native_files_are_restored_and_corrupt_jars_replaced(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(starter, 'cache_path', return_value=folder), \
             patch.object(starter.platform, 'system', return_value='Linux'), \
             contextlib.redirect_stdout(io.StringIO()):
            configuration = config()
            configuration.opener.open.side_effect = [response(jar_bytes()), response(jar_bytes({'libwrapper.so': b'native'}))]
            starter.update_jars(configuration, descriptor())
            native = Path(folder) / 'libwrapper.so'
            self.assertEqual(native.read_bytes(), b'native')
            native.unlink()
            starter.update_jars(configuration, descriptor())
            self.assertEqual(native.read_bytes(), b'native')
            self.assertEqual(configuration.opener.open.call_count, 2)
            (Path(folder) / 'JViewer.jar').write_bytes(b'partial')
            configuration.opener.open.side_effect = [response(jar_bytes())]
            starter.update_jars(configuration, descriptor())
            starter.validate_jar(str(Path(folder) / 'JViewer.jar'))
            self.assertEqual(configuration.opener.open.call_count, 3)

    def test_mac_downloads_only_declared_viewer_jar(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(starter, 'cache_path', return_value=folder), \
             patch.object(starter.platform, 'system', return_value='Darwin'), \
             contextlib.redirect_stdout(io.StringIO()):
            configuration = config()
            configuration.opener.open.return_value = response(jar_bytes())
            starter.update_jars(configuration, descriptor())
            self.assertFalse(configuration.native_libraries)
            self.assertEqual(configuration.opener.open.call_count, 1)
            self.assertEqual(configuration.opener.open.call_args.args[0], 'https://ipmi:443/Java/release/JViewer.jar')

    def test_explicit_classpath_excludes_stale_compatibility_jars(self):
        configuration = config(jars=['/cache/JViewer.jar', '/cache/Win64.jar'], native_libraries=True)
        self.assertEqual(starter.get_viewer_classpath(configuration), os.pathsep.join(configuration.jars))

    def test_mac_workaround_explains_missing_jdk(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(starter.platform, 'system', return_value='Darwin'):
            original = Path(folder) / 'JViewer.jar'
            original.write_bytes(jar_bytes())
            configuration = config(path=folder, jars=[str(original)], native_libraries=False,
                                   java=str(Path(folder) / 'missing-jdk' / 'bin' / 'java'))
            with self.assertRaisesRegex(RuntimeError, 'JDK with javac'):
                starter.get_viewer_classpath(configuration)
            self.assertEqual(list(Path(folder).glob('JViewer-mac-*.jar')), [])

    def test_refresh_redownloads_valid_jars(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(starter, 'cache_path', return_value=folder), \
             patch.object(starter.platform, 'system', return_value='Darwin'), \
             contextlib.redirect_stdout(io.StringIO()):
            (Path(folder) / 'JViewer.jar').write_bytes(jar_bytes())
            configuration = config(refresh=True)
            configuration.opener.open.return_value = response(jar_bytes())
            starter.update_jars(configuration, descriptor())
            self.assertEqual(configuration.opener.open.call_count, 1)


if __name__ == '__main__':
    unittest.main()

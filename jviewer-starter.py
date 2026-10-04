#! /usr/bin/env python3
# coding: utf-8
#
# Copyright 2017 Aaron Bulmahn (aarbudev@gmail.com)
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of Intel Corporation nor the names of its contributors
#       may be used to endorse or promote products derived from this software
#       without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from urllib.request import Request, HTTPSHandler, build_opener
from urllib.parse import urlencode, urlsplit, urlunsplit, urljoin
from urllib.error import HTTPError, URLError
from http.client import IncompleteRead
import argparse, sys, os, re, subprocess, platform, getpass, zipfile, ssl
import tempfile, shutil, hashlib, shlex, math
import xml.etree.ElementTree as ET

mainClass = "com.ami.kvm.jviewer.JViewer"


def find_property(settings, property):
    match = re.search(r"^\s*%s\s*=([^\r\n]*)" % re.escape(property), settings, re.MULTILINE)
    return match.group(1).strip() if match else ""


def find_java():
    home = os.getenv('JVIEWER_JAVA_HOME')
    executable = 'java.exe' if platform.system() == 'Windows' else 'java'
    java = os.path.join(home, 'bin', executable) if home else shutil.which(executable)
    if not java:
        raise RuntimeError('Java 8 was not found. Set JVIEWER_JAVA_HOME to an x86 Java 8 JDK.')
    try:
        result = subprocess.run([java, '-XshowSettings:properties', '-version'], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('Java startup failed: ' + error.stdout.decode('UTF-8', errors='replace').strip()) from error
    settings = result.stdout.decode('UTF-8', errors='replace')
    version = find_property(settings, 'java.version')
    arch = find_property(settings, 'os.arch')
    if not version.startswith('1.8.') or arch not in ('x86', 'i386', 'i686', 'amd64', 'x86_64'):
        raise RuntimeError('Unsupported Java: version=%s arch=%s. Use an x86 Java 8 JDK.' % (version, arch))
    return java, 64 if arch in ('amd64', 'x86_64') else 32


def normalize_server(server):
    server = server.strip()
    if '://' not in server:
        server = 'http://' + server
    parts = urlsplit(server)
    if (parts.scheme not in ('http', 'https') or not parts.hostname or
        parts.username is not None or parts.password is not None or
        parts.path not in ('', '/') or parts.query or parts.fragment):
        raise ValueError('Host must be a hostname, IP address, or HTTP(S) origin, without a path or credentials.')
    port = parts.port  # Validate the port before making a request.
    host = parts.hostname.lower()
    if any(char.isspace() for char in host):
        raise ValueError('Host cannot contain whitespace.')
    if ':' in host:
        host = '[' + host + ']'
    if port is not None and port != (443 if parts.scheme == 'https' else 80):
        host += ':' + str(port)
    return urlunsplit((parts.scheme, host, '', '', ''))


def parse_configuration(argparser):
    args = argparser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        argparser.error('--timeout must be greater than zero')
    args.server = normalize_server(args.host or input('IPMI host: '))
    args.java, args.bits = find_java()
    print('Using java: %s' % args.java)
    args.username = args.username or input('Username: ')
    args.password = args.password if args.password is not None else getpass.getpass()
    context = ssl.create_default_context(cafile=args.ca_cert)
    if args.insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    args.opener = build_opener(HTTPSHandler(context=context))
    return args


def read_response(configuration, request):
    with configuration.opener.open(request, timeout=configuration.timeout) as response:
        try:
            return response.read().decode('utf-8')
        except IncompleteRead as error:
            # Some BMC firmware reports the wrong Content-Length for its JNLP.
            if not isinstance(request, Request) or '/Java/jviewer.jnlp?' not in request.full_url:
                raise
            return error.partial.decode('utf-8')


def fetch_jnlp(configuration):
    credentials = {'WEBVAR_USERNAME': configuration.username, 'WEBVAR_PASSWORD': configuration.password}
    login = Request(configuration.server + '/rpc/WEBSES/create.asp',
                    data=urlencode(credentials).encode('utf-8'))
    response = read_response(configuration, login)
    cookie = re.search(r"['\"]SESSION_COOKIE['\"]\s*:\s*['\"]([a-zA-Z0-9]+)['\"]", response)
    if not cookie:
        raise RuntimeError('IPMI login failed: no session cookie returned. Check credentials and firmware login support.')
    query = urlencode({'EXTRNIP': urlsplit(configuration.server).hostname, 'JNLPSTR': 'JViewer'})
    request = Request(configuration.server + '/Java/jviewer.jnlp?' + query,
                      headers={'Cookie': 'SessionCookie=' + cookie.group(1)})
    try:
        root = ET.fromstring(read_response(configuration, request))
    except ET.ParseError as error:
        raise RuntimeError('The IPMI server returned invalid JNLP XML. The session may have expired.') from error
    for element in root.iter():
        element.tag = element.tag.rsplit('}', 1)[-1]
    if root.tag != 'jnlp':
        raise RuntimeError('The IPMI server did not return a JNLP launch descriptor.')
    return root


def select_resources(root, system, bits):
    names = {'Darwin': ('Mac', 'Mac OS X', 'Darwin'), 'Windows': ('Windows',), 'Linux': ('Linux',)}
    if system not in names:
        raise RuntimeError('Unsupported operating system: ' + system)
    arches = ('amd64', 'x86_64') if bits == 64 else ('x86', 'i386', 'i686')
    resources = []
    for group in root.findall('resources'):
        os_name = group.get('os')
        arch = group.get('arch')
        if os_name and not any(os_name.startswith(name) for name in names[system]):
            continue
        if arch and arch not in arches:
            continue
        for entry in group:
            if entry.tag in ('jar', 'nativelib'):
                href = entry.get('href')
                if not href:
                    raise RuntimeError('JNLP resource is missing its href.')
                if (href, entry.tag == 'nativelib') not in resources:
                    resources.append((href, entry.tag == 'nativelib'))
    if not any(not native for _, native in resources):
        raise RuntimeError('JNLP does not list a viewer JAR for this platform.')
    if system != 'Darwin' and not any(native for _, native in resources):
        raise RuntimeError('JNLP does not provide native libraries for %s %s-bit Java.' % (system, bits))
    return resources


def cache_path(configuration):
    system = platform.system()
    if system == 'Darwin':
        root = os.path.expanduser('~/Library/Application Support')
    elif system == 'Windows':
        root = os.environ.get('LOCALAPPDATA', os.path.expanduser('~/AppData/Local'))
    else:
        root = os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share'))
    key = hashlib.sha256(configuration.server.encode()).hexdigest()[:16]
    return os.path.join(root, 'jviewer-starter', key, '%s-%s' % (system, configuration.bits))


def validate_jar(path):
    with zipfile.ZipFile(path) as jar:
        if jar.testzip() is not None:
            raise zipfile.BadZipFile('JAR checksum failed: ' + path)
        if not jar.namelist():
            raise zipfile.BadZipFile('Empty JAR: ' + path)


def download_jar(configuration, url, destination):
    # Validate before atomically replacing a working cached file.
    with tempfile.TemporaryDirectory(dir=os.path.dirname(destination)) as temporary:
        output = os.path.join(temporary, 'download.jar')
        print('Downloading ' + os.path.basename(urlsplit(url).path))
        with configuration.opener.open(url, timeout=configuration.timeout) as response, open(output, 'wb') as target:
            shutil.copyfileobj(response, target)
            length = response.headers.get('Content-Length')
            if length is not None and target.tell() != int(length):
                raise RuntimeError('Incomplete JAR download: ' + os.path.basename(urlsplit(url).path))
        validate_jar(output)
        os.replace(output, destination)


def update_jars(configuration, root):
    resources = select_resources(root, platform.system(), configuration.bits)
    configuration.path = cache_path(configuration)
    os.makedirs(configuration.path, exist_ok=True)
    base = root.get('codebase') or configuration.server + '/Java/'
    jars = []
    natives = False
    for href, native in resources:
        url = urljoin(base.rstrip('/') + '/', href)
        if urlsplit(url).scheme not in ('http', 'https'):
            raise RuntimeError('Unsupported JNLP resource URL: ' + url)
        name = os.path.basename(urlsplit(url).path)
        if not name.endswith('.jar') or any(os.path.basename(path) == name for path in jars):
            raise RuntimeError('Invalid or duplicate JNLP JAR filename: ' + name)
        destination = os.path.join(configuration.path, name)
        valid = False
        if os.path.exists(destination):
            try:
                validate_jar(destination)
                valid = True
            except (zipfile.BadZipFile, OSError):
                pass
        if configuration.refresh or not valid:
            download_jar(configuration, url, destination)
        jars.append(destination)
        if native:
            natives = True
            # Restore extracted files even when the JAR was already cached.
            with zipfile.ZipFile(destination) as jar:
                for entry in jar.infolist():
                    if entry.is_dir() or entry.filename.startswith('META-INF/'):
                        continue
                    filename = os.path.basename(entry.filename.replace('\\', '/'))
                    if not filename or filename in ('.', '..'):
                        raise RuntimeError('Invalid native library filename in ' + name)
                    if filename.endswith('.jar'):
                        raise RuntimeError('Native archive must not overwrite cached JARs: ' + filename)
                    with tempfile.TemporaryDirectory(dir=configuration.path) as temporary:
                        output = os.path.join(temporary, filename)
                        with jar.open(entry) as source, open(output, 'wb') as target:
                            shutil.copyfileobj(source, target)
                        os.replace(output, os.path.join(configuration.path, filename))
    configuration.jars = jars
    configuration.native_libraries = natives


def get_java_options(path):
    return ['-Djava.library.path=' + path] + shlex.split(os.getenv('JVIEWER_JAVA_OPTIONS', ''))


def get_viewer_classpath(configuration):
    jars = list(configuration.jars)
    if platform.system() != 'Darwin' or configuration.native_libraries:
        return os.pathsep.join(jars)
    source = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'MacJViewerCompat.java')
    original = next((path for path in jars if os.path.basename(path) == 'JViewer.jar'), None)
    if original is None:
        raise RuntimeError('The macOS workaround requires JViewer.jar; this firmware layout is unsupported.')
    digest = hashlib.sha256()
    for filename in (source, original):
        with open(filename, 'rb') as file:
            for chunk in iter(lambda: file.read(65536), b''):
                digest.update(chunk)
    patched = os.path.join(configuration.path, 'JViewer-mac-' + digest.hexdigest()[:16] + '.jar')
    valid = False
    if os.path.exists(patched):
        try:
            validate_jar(patched)
            valid = True
        except zipfile.BadZipFile:
            pass
    if not valid:
        javac = os.path.join(os.path.dirname(configuration.java), 'javac')
        if not os.path.isfile(javac):
            raise RuntimeError('The macOS workaround needs a Java 8 JDK with javac next to java. Set JVIEWER_JAVA_HOME to that JDK.')
        print('Preparing macOS keyboard LED compatibility workaround')
        with tempfile.TemporaryDirectory(dir=configuration.path) as build:
            subprocess.run([javac, '-XDignore.symbol.file', '-d', build, source], check=True, timeout=60)
            output = os.path.join(build, 'JViewer-mac.jar')
            subprocess.run([configuration.java, '-cp', build, 'MacJViewerCompat', original, output], check=True, timeout=60)
            validate_jar(output)
            os.replace(output, patched)
    print('Mac native libraries are unavailable; local physical drive access and native keyboard LED synchronization are disabled')
    return os.pathsep.join(patched if path == original else path for path in jars)


def run_jviewer(configuration):
    root = fetch_jnlp(configuration)
    application = root.find('application-desc')
    if application is None:
        raise RuntimeError('JNLP is missing application-desc.')
    arguments = [(entry.text or '').strip() for entry in application.findall('argument')]
    if len(arguments) < 12 or not all(arguments):
        raise RuntimeError('JNLP is missing required JViewer connection arguments.')
    update_jars(configuration, root)
    viewer = application.get('main-class', mainClass)
    args = [configuration.java] + get_java_options(configuration.path)
    args += ['-cp', get_viewer_classpath(configuration), viewer] + arguments
    print('Starting JViewer for ' + configuration.server)
    return subprocess.call(args)


def build_parser():
    parser = argparse.ArgumentParser(description='Download and open the JViewer remote console',
        epilog='Requires x86 Java 8. The macOS workaround requires a JDK. Set JVIEWER_JAVA_HOME to select it.')
    parser.add_argument('--host', help='hostname, IP address, or HTTP(S) origin of the IPMI server')
    parser.add_argument('--username', help='IPMI username')
    parser.add_argument('--password', help='IPMI password (omit for a hidden prompt)')
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument('--insecure', action='store_true', help='disable HTTPS certificate verification for a trusted IPMI server')
    tls.add_argument('--ca-cert', help='PEM CA certificate used to verify HTTPS')
    parser.add_argument('--refresh', action='store_true', help='download and validate current firmware JARs again')
    parser.add_argument('--timeout', type=float, default=15, help='network timeout in seconds (default: 15)')
    return parser


def main():
    parser = build_parser()
    try:
        configuration = parse_configuration(parser)
        return run_jviewer(configuration)
    except HTTPError as error:
        print('Error: HTTP %s while requesting %s. Check the firmware resource or login endpoint.' %
              (error.code, error.url.split('?')[0]), file=sys.stderr)
    except URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            print('Error: HTTPS certificate verification failed. Use --ca-cert with a trusted CA, or --insecure for a trusted IPMI server.', file=sys.stderr)
        else:
            print('Error: IPMI connection failed: %s. Check the address, web port, and network access.' % error.reason, file=sys.stderr)
    except subprocess.CalledProcessError:
        print('Error: Java command failed. See the Java diagnostic above; the macOS workaround requires compatible JViewer firmware and a Java 8 JDK.', file=sys.stderr)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, ET.ParseError, IncompleteRead, subprocess.TimeoutExpired) as error:
        print('Error: %s' % error, file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print('Cancelled.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())

# JViewer starter

Launch an AMI JViewer IPMI console from Python without Java Web Start.
The direct macOS route is working with this repository's Java 8 keyboard LED
workaround and the supplied server's JViewer firmware.

## Requirements

- Python 3.10 or newer.
- An x86 (Intel/AMD) Java 8 runtime. On macOS, use a **Java 8 JDK** with
  `java` and `javac`, because the compatibility workaround needs the compiler.
- On Apple Silicon, an x64 Java 8 JDK running under Rosetta.
- Network access to the BMC's web interface and its KVM port. A reachable
  HTTPS login page alone does not establish that the video port is reachable.

There are no Python package dependencies. This project intentionally uses
Java 8: newer Java runtimes and ARM Java runtimes are rejected before login.

## Direct launch on macOS

Keep `jviewer-starter.py` and `MacJViewerCompat.java` together in the checkout.
Select your installed x64 Java 8 JDK, for example:

```bash
export JVIEWER_JAVA_HOME=/Library/Java/JavaVirtualMachines/zulu-8.jdk/Contents/Home
./jviewer-starter.py --host https://192.0.2.225 --username root --insecure
```

The password prompt is hidden. Omitting `--host` or `--username` prompts for
those values as well. A bare hostname/IP uses HTTP; an explicit HTTPS URL
preserves HTTPS. Optional web ports are supported, for example
`https://ipmi.example:8443`.

Certificate verification is enabled by default. `--insecure` disables it
for a trusted device with a self-signed certificate. If you have a trusted
PEM CA certificate, use `--ca-cert /path/to/ca.pem` instead.

`JVIEWER_JAVA_HOME` selects the runtime. If unset, the launcher uses `java`
from `PATH`. `JVIEWER_JAVA_OPTIONS` accepts shell-style quoting, for example:

```bash
export JVIEWER_JAVA_OPTIONS='-Xmx512m -Dexample="value with spaces"'
```

The Python script can also be symlinked into your `PATH`; the Java helper is
resolved relative to the actual script file. Keep the original checkout.

## Downloads, caching, and firmware updates

The launcher logs in, fetches a fresh JNLP descriptor, and downloads only the
viewer and native JARs listed for the operating system and **Java** architecture.
It does not assume that `JViewer-SOC.jar` or `Mac64.jar` exists.

Caches are isolated by server origin, operating system, and Java bitness under:

- macOS: `~/Library/Application Support/jviewer-starter/`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/jviewer-starter/`
- Windows: `%LOCALAPPDATA%\jviewer-starter\`

The previous shared cache is left in place. The first launch with the revised
launcher downloads into a new per-server cache. JARs are checked on every launch;
corrupt files are downloaded again. Downloads are validated before replacing
cached files, and native libraries are restored from cached native JARs.
Only the current JNLP's JARs enter the classpath.

After a BMC firmware update, force new downloads:

```bash
./jviewer-starter.py --host https://192.0.2.225 --username root --insecure --refresh
```

Use `--timeout 30` for a slower network (default: 15 seconds per network operation).
A failed refresh preserves previously cached JARs but stops that launch rather
than silently using an incomplete update.

## macOS keyboard LED workaround

Some firmware provides Windows/Linux native libraries only. In this case,
JViewer's native `FloppyRedir.GetLEDStatus()` call can crash its video listener:
the window opens, but stays black at 0 fps.

The launcher uses Java 8's bundled ASM to make a separate compatibility copy
of `JViewer.jar`. It replaces the two keyboard LED callbacks that use the native
library while preserving remote LED state in the soft keyboard. Native local LED
synchronization and physical drive redirection are unavailable without native
libraries. Other virtual media functionality is not covered by the regression test.

The original downloaded JAR is unchanged. The compatibility copy omits vendor
signature metadata because its bytecode has changed. It is rebuilt when either
the original JAR or the helper source changes. The helper checks the expected
fields and native calls and refuses unsupported firmware layouts.

The workaround uses an internal API in **Java 8**, so retain a known working
Java 8 JDK and run the integration tests before changing runtimes or firmware.
It is not a guarantee of compatibility with every AMI firmware version.

The locally verified runtime is Zulu 8.92.0.21, OpenJDK `1.8.0_482-b08`, x64.
The original video-listener fix was confirmed in a live console session; the
subsequent repository hardening was checked offline against that vendor JAR
and the supplied JNLP. New firmware still requires a live smoke test.

## Optional Docker launch

On macOS, start Docker and XQuartz. In XQuartz Settings → Security, enable
"Allow connections from network clients", then quit and reopen XQuartz.
The launcher looks for `xhost` in `PATH` and `/opt/X11/bin`.

```bash
./jviewer-docker.sh --host https://192.0.2.225 --username root --insecure
```

Docker uses x64 Linux and Java 8. The firmware must list compatible Linux native
libraries. The launcher works from another directory and through a symlink,
forwards its arguments, removes stopped containers, and keeps downloads in a
named volume. The image is rebuilt locally before launch; its base image is
`eclipse-temurin:8-jdk-jammy`. Docker builds and live X11 behavior are not exercised
by the offline tests. Direct Python launch is the verified macOS route here.

## Troubleshooting

- **Connection refused or timeout:** check the IP address, web port, and network.
- **Certificate verification failure:** configure `--ca-cert`, or use `--insecure`
  for your trusted device.
- **Login failed / invalid JNLP:** check credentials and whether the firmware still
  supports `/rpc/WEBSES/create.asp` and `/Java/jviewer.jnlp`.
- **HTTP 404 on a declared JAR:** the firmware's JNLP references a missing resource.
  The launcher will report the URL; it cannot manufacture a missing native library.
- **Blank screen:** inspect terminal output. `UnsatisfiedLinkError` in the listener
  points to native code; an authentication failure or connection error is separate.
  Confirm the server is powered on and the KVM port is reachable. Do not power-cycle
  the server merely to debug the viewer.
- **Unsupported Java:** select an x86 Java 8 runtime with `JVIEWER_JAVA_HOME`.
- **Missing compiler / unsupported workaround layout:** use a Java 8 JDK with `javac`
  or inspect the new firmware's JAR before changing the compatibility helper.

## Verification

Run offline Python regressions:

```bash
python3 -W error -B -m unittest discover -v
```

To also run the Java integration tests locally:

```bash
JVIEWER_TEST_JAVA_HOME="$JVIEWER_JAVA_HOME" python3 -W error -B -m unittest discover -v
```

The integration tests construct a synthetic JViewer fixture, reproduce the native
LED crash, run the patched callbacks with Java bytecode verification enabled,
check cache invalidation, and confirm that unknown layouts fail without modifying
the original. They require Java 8 and run on Linux/macOS; Windows runs the Python
regressions. GitHub Actions covers Python 3.10 and 3.13 on Linux and Windows.

Downloaded JARs, generated classes, and JNLP files are ignored by Git and excluded
from Docker build contexts. JNLP files contain short-lived session credentials;
do not commit them. No private server or password is required for CI.

## Origin

Based on [arbu/jviewer-starter](https://github.com/arbu/jviewer-starter) and
[dfuchslin/jviewer-starter](https://github.com/dfuchslin/jviewer-starter).
The original script's license is retained in `jviewer-starter.py`.

import os
import sys

__version__ = "0.4.2"
REPO = "Dolidelode/BetterRecentlyPlayedWith"
APP_NAME = "BetterRecentlyPlayedWith"

# Where Linux distributions keep their trusted certificates (Debian/Ubuntu/Arch, Fedora/RHEL, openSUSE, Alpine...).
CA_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
            "/etc/ssl/ca-bundle.pem", "/etc/pki/tls/cacert.pem", "/etc/ssl/cert.pem")


def fix_linux_bundle(env=os.environ, frozen=None, platform=sys.platform, exists=os.path.isfile) -> None:
    """The Linux programs are one file with their own Python and OpenSSL inside. Two things from the build machine
    must not leak onto the user's system:
    - certificates: the bundled OpenSSL looks where the build machine kept them, so every HTTPS call failed with
      CERTIFICATE_VERIFY_FAILED. Point it at the system's own list (SSL_CERT_FILE, unless the user set one).
    - libraries: PyInstaller puts its folder first in LD_LIBRARY_PATH, which programs we start (the browser through
      xdg-open, /bin/sh) inherit; they then load our older libreadline and crash. Give them the user's original
      LD_LIBRARY_PATH back (this process has already loaded its own libraries, so it isn't affected).
    Runs when brpw is first imported: OpenSSL remembers the certificate place after the first HTTPS call."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen or not platform.startswith("linux"):
        return
    if not env.get("SSL_CERT_FILE") and not env.get("SSL_CERT_DIR"):
        for f in CA_FILES:
            if exists(f):
                env["SSL_CERT_FILE"] = f
                break
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    else:
        env.pop("LD_LIBRARY_PATH", None)


fix_linux_bundle()

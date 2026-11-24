## What's new in 0.4.1

A fix for the Linux programs (`BetterRecentlyPlayedWith-linux` and `brpw-cli-linux`); Windows is unchanged.

- **Signing in works on Linux.** It failed with `CERTIFICATE_VERIFY_FAILED` (and so did every other connection): the programs looked for trusted certificates where the build machine keeps them. They now use your system's own list, or `SSL_CERT_FILE` if you set it.
- **The browser opens again.** On newer distributions such as Arch, opening the sign-in page or a link crashed `/bin/sh` with `symbol lookup error`, because it picked up libraries bundled inside the program. Programs the app starts now use your system's own libraries.

Download the new Linux file(s) and replace the old ones; your data and settings carry over. The full history of changes is in CHANGELOG.md.

# Included software

This bundle contains the original ctrlloop CLI and these third-party components:

- CUA Driver 0.28.2, with the two native patches included in `scripts/patches/`.
  Upstream: https://github.com/trycua/cua at
  `fc188250b4ca8549b8e61f937fdb1fb560770e86`.
  The upstream MIT license is included at `runtime/cua-driver/LICENSE.md`.
  `scripts/build-cua-driver.ps1` records the pinned source and build toolchain.
- CPython 3.11.15. Its license and bundled component notices are included at
  `runtime/python/LICENSE.txt`; Tcl/Tk licenses remain with the runtime files.
- TypeSafe SDK 0.7.1, python-dotenv 1.2.3, httpx2 2.13.0, and their installed
  dependencies. Package metadata and supplied licenses are retained under
  `runtime/python/Lib/site-packages/*.dist-info/`. `uv.lock` records exact versions.

`PROVENANCE.json` identifies the original application source and native binary
hashes. `SHA256SUMS.json` covers the files as originally packaged, excluding the
checksum file itself. New local credentials, reports and driver state are not
part of that inventory.

# Third-party notices

VirtualKVM application code is MIT-licensed; see `LICENSE` in the source tree or `licenses/VirtualKVM-MIT.txt` in a local distribution. This notice identifies direct runtime components. Their own license terms apply; an application MIT license does not replace them. Native installers also include FFmpeg/ffprobe and their runtime dependency closure. Inspect `runtime/licenses`, `runtime/manifest.json`, and the source index there for the precise included build and upstream source locations.

| Component | License / notice |
| --- | --- |
| Python | Python Software Foundation license and included third-party notices; [python.org](https://www.python.org/doc/license/) |
| PySide6-Essentials, PySide6-Addons and Qt modules used here | Qt for Python community licensing includes LGPL v3/GPL v3; commercial terms are also available. QtCore, QtGui, QtWidgets, QtNetwork and QtMultimedia have their own applicable notices. See [Qt for Python licensing](https://doc.qt.io/qtforpython-6/licenses.html) and the supplied license texts. |
| Shiboken6 | Qt for Python binding runtime; applicable LGPL/GPL or commercial terms, supplied with its distribution. |
| cryptography | Apache License 2.0 or BSD 3-Clause, with additional included third-party notices; [cryptography.io](https://cryptography.io/en/latest/) |
| OpenSSL used by Python / cryptography | Current OpenSSL releases use Apache License 2.0; use the notices for the particular runtime build. cryptography wheels may contain their own OpenSSL rather than the system copy. |
| cffi | MIT; [cffi.readthedocs.io](https://cffi.readthedocs.io/) |
| pycparser | BSD 3-Clause; [github.com/eliben/pycparser](https://github.com/eliben/pycparser) |
| PyObjC core, Cocoa and Quartz bridges on macOS | MIT; [pyobjc.readthedocs.io](https://pyobjc.readthedocs.io/) |
| PyInstaller bootloader | GPL with a bootloader exception permitting distribution of applications using the bootloader; see its supplied license and exception. The build tool is not an application runtime API. |

The packaging helper copies license files available in the installed distribution metadata into `licenses/` in the application data directory. In a PyInstaller directory build this is normally under `_internal/licenses`; in a macOS bundle it is in the bundled resources. Consult those texts for the exact component versions used in that build. The helper is a local build utility, not a certification of compliance for a separate published product.

Full GNU LGPL v3 and GPL v3 terms are also supplied in `docs/licenses` and copied into the package. These texts are included because some PySide6 wheels do not include full license files in their metadata; their inclusion does not change the applicable license of any module. The copies originate from the Debian `common-licenses` files in the development environment.

Qt libraries are loaded dynamically in the directory/bundle distribution. Do not remove license texts or prevent replacement of LGPL-covered libraries when preparing a distribution. Shipping a modified Qt build or changing packaging can create additional obligations.

## External media and audio components

Native installers bundle FFmpeg and ffprobe; source/development and plain PyInstaller builds use external tools unless `--bundle-ffmpeg` is specified. FFmpeg is licensed under LGPL or GPL depending on its build configuration; builds with libx264/libx265 commonly enable GPL components. Codecs, SRT, AAC implementations, hardware drivers, and other enabled libraries have their own notices. Review the exact FFmpeg build and its distribution terms if you distribute it separately.

Windows audio loopback/virtual audio drivers and macOS loopback devices such as BlackHole are external user-installed prerequisites when required for system-audio capture. They are not installed, bundled, or licensed by VirtualKVM.

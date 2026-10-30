## VirtualKVM desktop installers

The pilot installers include Python, Qt, FFmpeg and ffprobe. No separate runtime installation or post-install download is required.

- **Windows x64:** download and run `VirtualKVM-*-windows-x64-setup.exe`.
- **Mac Apple Silicon (macOS 15 or later):** download the `macos-arm64.pkg` wizard or the matching `.dmg`.
- **Mac Intel (macOS 15 or later):** download the `macos-x86_64.pkg` wizard or the matching `.dmg`.

Packages are published only after native install, offline media, upgrade, and platform-specific uninstall/disk-image checks pass for the exact tagged commit.

These are unsigned pilot builds unless explicitly signed by the maintainer. Apple Developer ID/notarization and Windows Authenticode certificates are not included. The operating system can ask you to confirm opening a downloaded package.

Screen Recording, Accessibility and the selected audio-device permissions still require the normal operating-system prompts. Installers never grant them or start a sharing host automatically. Existing preferences and shared files are preserved.

240 FPS is a configurable target, not a guarantee of physical capture/display throughput. Native audio devices, end-to-end latency, and real dual-monitor operation still need hardware validation.

Application code: MIT. Bundled media components have their own licenses; see the included `licenses` directory and runtime build/source manifest.

## VirtualKVM 0.2.0

A quieter, minimal interface with neutral colors and compact controls. Configure 1–8 source screens: drag a window layout, enter exact coordinates, use presets, or assign each source to a separate local monitor with optional fullscreen. Layouts persist; older two-screen preferences migrate automatically. Per-source aspect preservation handles mixed landscape/portrait screens and Retina orientation.

VirtualKVM shares screens already available to the host OS. It does not create virtual OS displays. Native monitor arrangement, rotation and DPI remain configured in Windows/macOS.

The pilot installers include Python, Qt, FFmpeg and ffprobe. No separate runtime installation or post-install download is required.

- **Windows x64:** run `VirtualKVM-0.2.0-windows-x64-setup.exe`.
- **Mac Apple Silicon (macOS 15 or later):** use the `macos-arm64.pkg` wizard or matching `.dmg`.
- **Mac Intel (macOS 15 or later):** use the `macos-x86_64.pkg` wizard or matching `.dmg`.

Packages are published only after source tests, native installation, installed-interface/resource validation, offline A/V, upgrade and platform-specific uninstall/disk-image checks pass for the exact tagged commit.

These are unsigned pilot builds: Apple Developer ID/notarization and Windows Authenticode certificates are not included. Screen Recording, Accessibility and selected audio-device permissions still use normal OS prompts. Installers do not start a sharing host automatically. Existing preferences and shared files are preserved.

240 FPS is a configurable target. Native capture/display throughput, physical multi-monitor placement, audio output and end-to-end latency still require hardware validation.

Application code: MIT. Bundled media components have their own licenses; see the included `licenses` directory and runtime build/source manifest.

#!/usr/bin/env bash
# Build a small, reproducible FFmpeg/SRT runtime from the official source release.
set -euo pipefail
test "$(uname -s)" = Darwin
project_root="$(cd "$(dirname "$0")/.." && pwd)"
media_root="$project_root/build/ffmpeg-native"
media_version=9.0.2
media_sha256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
mkdir -p "$media_root"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
if test "$(uname -m)" = x86_64; then
  bash "$project_root/scripts/build_media_deps_macos.sh" "$media_root/deps"
  export PATH="$media_root/deps/bin:$PATH"
  export PKG_CONFIG_PATH="$media_root/deps/lib/pkgconfig:$media_root/deps/share/pkgconfig"
  export PKG_CONFIG_LIBDIR="$PKG_CONFIG_PATH"
else
  brew install pkgconf nasm srt x264 x265
  export PKG_CONFIG_PATH="$(brew --prefix srt)/lib/pkgconfig:$(brew --prefix x264)/lib/pkgconfig:$(brew --prefix x265)/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
fi
runtime_ready=false
if test -x "$media_root/runtime/bin/ffmpeg" && test -x "$media_root/runtime/bin/ffprobe"; then
  if "$media_root/runtime/bin/ffmpeg" -hide_banner -version > /dev/null 2>&1 && \
     "$media_root/runtime/bin/ffprobe" -hide_banner -version > /dev/null 2>&1 && \
     "$media_root/runtime/bin/ffmpeg" -hide_banner -protocols 2>/dev/null | grep -q '^[[:space:]]*srt[[:space:]]*$'; then
    runtime_ready=true
  fi
fi
if test "$runtime_ready" = false; then
archive="$media_root/ffmpeg-$media_version.tar.xz"
curl --fail --location --retry 3 "https://ffmpeg.org/releases/ffmpeg-$media_version.tar.xz" --output "$archive"
printf '%s  %s\n' "$media_sha256" "$archive" | shasum -a 256 --check
tar -xJf "$archive" -C "$media_root"
cd "$media_root/ffmpeg-$media_version"
./configure --prefix="$media_root/runtime" --disable-autodetect \
  --disable-doc --disable-debug --disable-ffplay --disable-static --enable-shared \
  --enable-gpl --enable-version3 --enable-libx264 --enable-libx265 --enable-libsrt \
  --enable-avfoundation --enable-videotoolbox --enable-audiotoolbox \
  --enable-securetransport --enable-pthreads
make -j 3
make install
else
  printf 'Reusing the verified native FFmpeg cache.\n'
fi
"$media_root/runtime/bin/ffmpeg" -hide_banner -protocols
if test -n "${GITHUB_ENV:-}"; then
  printf 'VIRTUALKVM_FFMPEG=%s\n' "$media_root/runtime/bin/ffmpeg" >> "$GITHUB_ENV"
  printf 'VIRTUALKVM_FFPROBE=%s\n' "$media_root/runtime/bin/ffprobe" >> "$GITHUB_ENV"
fi
if test -n "${GITHUB_PATH:-}"; then
  printf '%s\n' "$media_root/runtime/bin" >> "$GITHUB_PATH"
fi

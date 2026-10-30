#!/usr/bin/env bash
# Build a small, reproducible FFmpeg/SRT runtime from the official source release.
set -euo pipefail
test "$(uname -s)" = Darwin
project_root="$(cd "$(dirname "$0")/.." && pwd)"
media_root="$project_root/build/ffmpeg-native"
media_version=9.0.2
media_sha256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
mkdir -p "$media_root"
brew install pkgconf nasm srt x264 x265
export PKG_CONFIG_PATH="$(brew --prefix srt)/lib/pkgconfig:$(brew --prefix x264)/lib/pkgconfig:$(brew --prefix x265)/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
archive="$media_root/ffmpeg-$media_version.tar.xz"
curl --fail --location --retry 3 "https://ffmpeg.org/releases/ffmpeg-$media_version.tar.xz" --output "$archive"
printf '%s  %s\n' "$media_sha256" "$archive" | shasum -a 256 --check
tar -xJf "$archive" -C "$media_root"
cd "$media_root/ffmpeg-$media_version"
./configure --prefix="$media_root/runtime" --disable-autodetect \
  --disable-doc --disable-debug --disable-ffplay --disable-static --enable-shared \
  --enable-gpl --enable-libx264 --enable-libx265 --enable-libsrt \
  --enable-avfoundation --enable-videotoolbox --enable-audiotoolbox \
  --enable-securetransport --enable-pthreads
make -j 3
make install
"$media_root/runtime/bin/ffmpeg" -hide_banner -protocols
if test -n "${GITHUB_ENV:-}"; then
  printf 'VIRTUALKVM_FFMPEG=%s\n' "$media_root/runtime/bin/ffmpeg" >> "$GITHUB_ENV"
  printf 'VIRTUALKVM_FFPROBE=%s\n' "$media_root/runtime/bin/ffprobe" >> "$GITHUB_ENV"
fi
if test -n "${GITHUB_PATH:-}"; then
  printf '%s\n' "$media_root/runtime/bin" >> "$GITHUB_PATH"
fi

#!/usr/bin/env bash
# Pinned Intel macOS media dependencies, using Apple's installed toolchain.
# Keep downloaded sources and this recipe beside the prefix for license/rebuild audit.
set -euo pipefail

if test "$#" -ne 1; then
  printf 'Usage: bash %s PREFIX\n' "$0" >&2
  exit 2
fi
test "$(uname -s)" = Darwin || { printf 'This recipe requires macOS.\n' >&2; exit 2; }
test "$(uname -m)" = x86_64 || { printf 'This recipe requires native Intel x86_64.\n' >&2; exit 2; }
test "$(sysctl -in sysctl.proc_translated 2>/dev/null || true)" != 1 || {
  printf 'Use the native arm64 dependency path on Apple Silicon.\n' >&2; exit 2;
}
case "$1" in
  /*) ;;
  *) printf 'PREFIX must be an absolute path.\n' >&2; exit 2 ;;
esac
case "$1" in
  /|/usr|/usr/local|/System|/Applications) printf 'Use an isolated build prefix.\n' >&2; exit 2 ;;
esac
recipe="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
mkdir -p "$1"
prefix="$(cd "$1" && pwd -P)"
sources="$(dirname "$prefix")/sources"
mkdir -p "$sources"
if test "$recipe" != "$sources/build_media_deps_macos.sh"; then
  cp "$recipe" "$sources/build_media_deps_macos.sh"
fi

pkgconf_version=3.0.10
pkgconf_sha=9371b2f6372da574d4c6dc96fe9b1463037bcf6afcb6671c1555afde7b17e2de
nasm_version=3.02
nasm_sha=87336eba53b4acfe917424ab5d500d2b0054d9f5148d35c2273ccf2cfb712f0d
openssl_version=4.1.0
openssl_sha=50c17baf54cb008f6ce42d50deb4b28050a1aad711636cabd4b241c7e90cc2fd
srt_version=1.5.7
srt_sha=017cd1e437ef2073a4dd10ddf7b55e86bc3d6ebac0393d13bd22f6a57055d32b
x265_version=4.3
x265_sha=83c53e4c8bbb8f1e33ed59e10a7d621d1d7801ca853910c3eb41f038b8ffb121
x264_commit=b35605ace3ddf7c1a5d67a2eb553f034aef41d55
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
[[ "$MACOSX_DEPLOYMENT_TARGET" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || {
  printf 'Invalid MACOSX_DEPLOYMENT_TARGET.\n' >&2; exit 2;
}
export CC="$(xcrun --find clang)"
export CXX="$(xcrun --find clang++)"
export SDKROOT="$(xcrun --show-sdk-path)"
export PATH="$prefix/bin:$PATH"
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig:$prefix/share/pkgconfig"
export PKG_CONFIG_LIBDIR="$PKG_CONFIG_PATH"
export CFLAGS="${CFLAGS:-} -O3 -arch x86_64 -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
export CXXFLAGS="${CXXFLAGS:-} -O3 -arch x86_64 -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
export LDFLAGS="${LDFLAGS:-} -arch x86_64 -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"

# Hosted runners already provide CMake. Only bootstrap a wheel if it is absent.
if ! command -v cmake >/dev/null 2>&1; then
  python3 -m venv "$sources/cmake-3.31.6"
  "$sources/cmake-3.31.6/bin/python" -m pip install --disable-pip-version-check \
    --only-binary=:all: cmake==3.31.6
  export PATH="$sources/cmake-3.31.6/bin:$PATH"
fi
cmake --version

check_archive() {
  test -f "$2" && printf '%s  %s\n' "$1" "$2" | shasum -a 256 --check
}

download() {
  local file="$1" expected="$2"
  shift 2
  if check_archive "$expected" "$file"; then return; fi
  local url
  for url in "$@"; do
    if curl --fail --location --retry 3 --connect-timeout 20 --max-time 180 \
        --proto '=https' --proto-redir '=https' "$url" --output "$file.download"; then
      # A successful HTTP response is not proof of archive integrity.
      check_archive "$expected" "$file.download"
      mv "$file.download" "$file"
      return
    fi
  done
  printf 'Could not download verified source archive: %s\n' "$file" >&2
  return 1
}

pkgconf_archive="$sources/pkgconf-$pkgconf_version.tar.xz"
nasm_archive="$sources/nasm-$nasm_version.tar.xz"
openssl_archive="$sources/openssl-$openssl_version.tar.gz"
srt_archive="$sources/srt-$srt_version.tar.gz"
x265_archive="$sources/x265_$x265_version.tar.gz"
x264_source="$sources/x264-$x264_commit"
manifest="$sources/intel-dependencies.expected"
cat > "$manifest" <<EOF
recipe_sha256=$(shasum -a 256 "$recipe" | awk '{print $1}')
prefix=$prefix
architecture=x86_64
deployment_target=$MACOSX_DEPLOYMENT_TARGET
sdk=$(xcrun --show-sdk-version)
pkgconf=$pkgconf_version:$pkgconf_sha
nasm=$nasm_version:$nasm_sha
openssl=$openssl_version:$openssl_sha
srt=$srt_version:$srt_sha
x265=$x265_version:$x265_sha
x264=$x264_commit:r3222
cflags=$CFLAGS
cxxflags=$CXXFLAGS
ldflags=$LDFLAGS
EOF

# The smoke program validates real shared-library linkage, not merely filenames.
cat > "$sources/intel-dependencies-smoke.c" <<'EOF'
#include <stdint.h>
#include <openssl/crypto.h>
#include <openssl/opensslv.h>
#include <srt/srt.h>
#include <x264.h>
#include <x265.h>
int main(void) {
    x264_param_t h264;
    x264_param_default(&h264);
    x265_param *h265 = x265_param_alloc();
    if (!h265) return 1;
    x265_param_free(h265);
    if (OpenSSL_version_num() != OPENSSL_VERSION_NUMBER) return 2;
    if (srt_startup() != 0) return 3;
    if (srt_getversion() != SRT_VERSION_VALUE) return 4;
    return srt_cleanup() != 0;
}
EOF

validate_prefix() {
  test -x "$prefix/bin/pkgconf" && test -x "$prefix/bin/pkg-config" && test -x "$prefix/bin/nasm" || return 1
  test "$("$prefix/bin/pkgconf" --version)" = "$pkgconf_version" || return 1
  "$prefix/bin/nasm" -v | grep -Eq "^NASM version $nasm_version([[:space:]]|$)" || return 1
  test "$("$prefix/bin/pkg-config" --modversion openssl)" = "$openssl_version" || return 1
  test "$("$prefix/bin/pkg-config" --modversion srt)" = "$srt_version" || return 1
  "$prefix/bin/pkg-config" --exact-version="$x265_version" x265 || return 1
  "$prefix/bin/pkg-config" --modversion x264 | grep -Eq '^0\.[0-9]+\.3222([[:space:]]|$)' || return 1
  local library
  for library in libcrypto libssl libsrt libx264 libx265; do
    test -f "$prefix/lib/$library.dylib" || return 1
    lipo -verify_arch x86_64 "$prefix/lib/$library.dylib" || return 1
  done
  "$CC" -O2 -arch x86_64 -mmacosx-version-min="$MACOSX_DEPLOYMENT_TARGET" \
    "$sources/intel-dependencies-smoke.c" -I"$prefix/include" -L"$prefix/lib" \
    "-Wl,-rpath,$prefix/lib" -lsrt -lx264 -lx265 -lssl -lcrypto -lc++ \
    -o "$prefix/bin/virtualkvm-media-deps-smoke" || return 1
  "$prefix/bin/virtualkvm-media-deps-smoke"
}

stamp="$prefix/.virtualkvm-intel-dependencies"
if test -f "$stamp" && cmp -s "$manifest" "$stamp" && \
   check_archive "$pkgconf_sha" "$pkgconf_archive" && \
   check_archive "$nasm_sha" "$nasm_archive" && \
   check_archive "$openssl_sha" "$openssl_archive" && \
   check_archive "$srt_sha" "$srt_archive" && \
   check_archive "$x265_sha" "$x265_archive" && \
   test "$(git -C "$x264_source" rev-parse HEAD 2>/dev/null || true)" = "$x264_commit" && \
   validate_prefix; then
  printf 'Reusing verified Intel media dependencies in %s\n' "$prefix"
  exit 0
fi

download "$pkgconf_archive" "$pkgconf_sha" \
  "https://distfiles.ariadne.space/pkgconf/pkgconf-$pkgconf_version.tar.xz" \
  "https://github.com/pkgconf/pkgconf/releases/download/pkgconf-$pkgconf_version/pkgconf-$pkgconf_version.tar.xz"
download "$nasm_archive" "$nasm_sha" \
  "https://www.nasm.us/pub/nasm/releasebuilds/$nasm_version/nasm-$nasm_version.tar.xz"
download "$openssl_archive" "$openssl_sha" \
  "https://github.com/openssl/openssl/releases/download/openssl-$openssl_version/openssl-$openssl_version.tar.gz"
download "$srt_archive" "$srt_sha" \
  "https://github.com/Haivision/srt/archive/refs/tags/v$srt_version.tar.gz"
download "$x265_archive" "$x265_sha" \
  "https://github.com/Multicorewareinc/x265/releases/download/$x265_version/x265_$x265_version.tar.gz"

if ! test -d "$x264_source"; then
  cloned=false
  for remote in https://code.videolan.org/videolan/x264.git https://github.com/mirror/x264.git; do
    clone_tmp="$(mktemp -d "$sources/.x264-clone.XXXXXX")"
    # Full commit history is required for upstream's version.sh to report r3222.
    if git clone --single-branch --branch master --no-checkout "$remote" "$clone_tmp/repository"; then
      mv "$clone_tmp/repository" "$x264_source"
      rmdir "$clone_tmp"
      cloned=true
      break
    fi
    rm -rf "$clone_tmp"
  done
  test "$cloned" = true
  git -C "$x264_source" checkout --detach "$x264_commit"
fi
test "$(git -C "$x264_source" rev-parse 'HEAD^{commit}')" = "$x264_commit"
test "$(git -C "$x264_source" rev-list --count HEAD)" = 3222
git -C "$x264_source" diff --quiet HEAD --
git -C "$x264_source" fsck --no-dangling
git -C "$x264_source" archive --format=tar --prefix="x264-$x264_commit/" HEAD | \
  gzip -n > "$sources/x264-$x264_commit.tar.gz"
shasum -a 256 "$sources/x264-$x264_commit.tar.gz" > "$sources/x264-$x264_commit.tar.gz.sha256"

# Fresh extraction keeps a changed build recipe from reusing stale Makefiles.
# Build intermediates live in this invocation's temporary directory only.
work="$(mktemp -d "$sources/.intel-build.XXXXXX")"
trap 'rm -rf "$work"' EXIT
tar -xJf "$pkgconf_archive" -C "$work"
tar -xJf "$nasm_archive" -C "$work"
tar -xzf "$openssl_archive" -C "$work"
tar -xzf "$srt_archive" -C "$work"
tar -xzf "$x265_archive" -C "$work"

(
  cd "$work/pkgconf-$pkgconf_version"
  ./configure --prefix="$prefix" --enable-shared --disable-static \
    --with-pkg-config-dir="$prefix/lib/pkgconfig:$prefix/share/pkgconfig" \
    --with-system-libdir=/usr/lib --with-system-includedir=/usr/include
  make -j 3
  make install
)
ln -sf pkgconf "$prefix/bin/pkg-config"
(
  cd "$work/nasm-$nasm_version"
  ./configure --prefix="$prefix"
  make -j 3
  make install
)
(
  cd "$x264_source"
  ./configure --prefix="$prefix" --libdir="$prefix/lib" --enable-shared \
    --enable-pic --disable-cli --disable-opencl --disable-lavf --disable-swscale \
    --disable-ffms --disable-gpac --disable-lsmash
  make -j 3
  make install
)
(
  cd "$work/openssl-$openssl_version"
  ./Configure darwin64-x86_64-cc --prefix="$prefix" --openssldir="$prefix/ssl" \
    --libdir=lib shared no-tests no-docs no-apps
  make -j 3 build_sw
  make install_sw
)

cmake_common=(
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
  "-DCMAKE_INSTALL_PREFIX=$prefix" -DCMAKE_INSTALL_LIBDIR=lib
  -DCMAKE_OSX_ARCHITECTURES=x86_64 "-DCMAKE_OSX_DEPLOYMENT_TARGET=$MACOSX_DEPLOYMENT_TARGET"
  "-DCMAKE_C_COMPILER=$CC" "-DCMAKE_CXX_COMPILER=$CXX"
  "-DCMAKE_PREFIX_PATH=$prefix" "-DCMAKE_INSTALL_RPATH=$prefix/lib"
  "-DCMAKE_INSTALL_NAME_DIR=$prefix/lib"
)
cmake -S "$work/srt-$srt_version" -B "$work/srt-build" "${cmake_common[@]}" \
  -DENABLE_APPS=OFF -DENABLE_EXAMPLES=OFF -DENABLE_TESTING=OFF -DENABLE_UNITTESTS=OFF \
  -DENABLE_SHARED=ON -DENABLE_STATIC=OFF -DENABLE_ENCRYPTION=ON \
  -DSRT_USE_OPENSSL_STATIC_LIBS=OFF -DUSE_OPENSSL_PC=ON "-DOPENSSL_ROOT_DIR=$prefix"
cmake --build "$work/srt-build" --parallel 3
cmake --install "$work/srt-build"
cmake -S "$work/x265_$x265_version/source" -B "$work/x265-build" "${cmake_common[@]}" \
  -DENABLE_SHARED=ON -DENABLE_CLI=OFF -DENABLE_TESTS=OFF -DENABLE_ASSEMBLY=ON \
  -DENABLE_PIC=ON -DENABLE_HDR10_PLUS=OFF -DENABLE_LIBVMAF=OFF -DENABLE_SVT_HEVC=OFF \
  -DHIGH_BIT_DEPTH=OFF -DNATIVE_BUILD=OFF -DLIB_INSTALL_DIR=lib \
  "-DNASM_EXECUTABLE=$prefix/bin/nasm"
cmake --build "$work/x265-build" --parallel 3
cmake --install "$work/x265-build"

validate_prefix
cp "$manifest" "$stamp"
printf 'Verified Intel media dependency prefix: %s\n' "$prefix"

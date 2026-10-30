"""Explicit, confined shared-folder access; this does not mount an OS drive."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import stat

MAX_FILE_SIZE = 1024 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
MAX_ENTRIES = 2048


class SharedError(ValueError):
    pass


def _linked(status) -> bool:
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & 0x400)


def _reopen_between_shared_reads() -> bool:
    # NTFS can defer file-version updates while *any* of our read handles stay
    # open. Release each Windows read handle before returning a protocol chunk.
    return os.name == "nt"


def _parts(value: str, *, root_allowed: bool = True) -> tuple[str, ...]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 2048:
        raise SharedError("Invalid shared-folder path")
    if value == "" and root_allowed:
        return ()
    parts = value.split("/")
    for part in parts:
        if (not part or part in (".", "..") or len(part.encode("utf-8")) > 240
                or part.endswith((".", " ")) or re.search(r'[\\:\x00-\x1f<>"|?*]', part)
                or part.startswith(".virtualkvm-")):
            raise SharedError("Shared paths must stay inside the selected folder")
        if part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise SharedError("Reserved shared-folder name")
    return tuple(parts)


class _Directory:
    """Hold nofollow descriptors on POSIX; verify reparse-free paths on Windows."""
    def __init__(self, root: Path, parts: tuple[str, ...]):
        self.path = root
        self.fd = None
        self._closed = False
        if _linked(root.lstat()) or root.resolve(strict=True) != root:
            raise SharedError("The shared root changed; choose the folder again")
        if os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(root, flags)
            try:
                for part in parts:
                    child = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                    self.path /= part
                self.fd = descriptor
            except BaseException:
                os.close(descriptor)
                raise
        else:
            for part in parts:
                self.path /= part
                status = self.path.lstat()
                if _linked(status) or not stat.S_ISDIR(status.st_mode):
                    raise SharedError("Links and reparse points are not shared")
            resolved = self.path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise SharedError("Shared path escaped the selected folder")

    def close(self):
        if not self._closed and self.fd is not None:
            os.close(self.fd)
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def status(self, name: str):
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False) if self.fd is not None else (self.path / name).lstat()

    def open_file(self, name: str) -> int:
        status = self.status(name)
        if _linked(status) or not stat.S_ISREG(status.st_mode) or status.st_nlink > 1:
            raise SharedError("Only regular files without links may be shared")
        if self.fd is not None:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        else:
            descriptor = os.open(self.path / name, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        actual = os.fstat(descriptor)
        if (not stat.S_ISREG(actual.st_mode) or actual.st_nlink > 1
                or (status.st_dev, status.st_ino) != (actual.st_dev, actual.st_ino)):
            os.close(descriptor)
            raise SharedError("Shared file changed while opening")
        return descriptor


class SharedDirectory:
    def __init__(self, root: Path | None, writable=False, *, forbidden: Path | None = None):
        self.root = None
        self.writable = bool(writable)
        if root is None:
            return
        candidate = Path(root).expanduser().resolve(strict=True)
        if not candidate.is_dir():
            raise SharedError("Choose an existing folder to share")
        if forbidden is not None:
            private = Path(forbidden).expanduser().resolve()
            if candidate.is_relative_to(private) or private.is_relative_to(candidate):
                raise SharedError("The host identity/settings folder and its parents cannot be shared")
        self.root = candidate

    @property
    def info(self):
        return {"enabled": self.root is not None, "writable": self.root is not None and self.writable,
                "name": self.root.name if self.root else ""}

    def _require(self, write=False):
        if self.root is None:
            raise SharedError("The host has not enabled a shared folder")
        if write and not self.writable:
            raise SharedError("The shared folder is read-only")

    def list(self, path=""):
        self._require()
        parts = _parts(path)
        entries = []
        with _Directory(self.root, parts) as directory:
            with os.scandir(directory.fd if directory.fd is not None else directory.path) as iterator:
                for entry in iterator:
                    if entry.name.startswith(".virtualkvm-"):
                        continue
                    try:
                        _parts(entry.name, root_allowed=False)
                    except SharedError:
                        continue
                    status = entry.stat(follow_symlinks=False)
                    if _linked(status) or not (stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)):
                        continue
                    if stat.S_ISREG(status.st_mode) and status.st_nlink > 1:
                        continue
                    entries.append({"name": entry.name, "type": "directory" if stat.S_ISDIR(status.st_mode) else "file",
                                    "size": status.st_size if stat.S_ISREG(status.st_mode) else 0,
                                    "mtime": status.st_mtime})
                    if len(entries) > MAX_ENTRIES:
                        raise SharedError("This folder exceeds the 2048-entry browsing limit; select a smaller folder")
        entries.sort(key=lambda entry: (entry["type"] != "directory", entry["name"].casefold()))
        return {"path": "/".join(parts), "entries": entries, "writable": self.writable, "name": self.root.name}

    def read(self, path):
        self._require()
        parts = _parts(path, root_allowed=False)
        directory = _Directory(self.root, parts[:-1])
        try:
            descriptor = directory.open_file(parts[-1])
            return SharedRead(descriptor, "/".join(parts), directory, parts[-1])
        except BaseException:
            directory.close()
            raise

    def upload(self, path, size):
        self._require(write=True)
        parts = _parts(path, root_allowed=False)
        if type(size) is not int or not 0 <= size <= MAX_FILE_SIZE:
            raise SharedError("Shared files are limited to 1 GiB")
        return SharedUpload(_Directory(self.root, parts[:-1]), parts[-1], size, "/".join(parts))

    def mkdir(self, path):
        self._require(write=True)
        parts = _parts(path, root_allowed=False)
        with _Directory(self.root, parts[:-1]) as directory:
            if directory.fd is not None:
                os.mkdir(parts[-1], mode=0o700, dir_fd=directory.fd)
            else:
                (directory.path / parts[-1]).mkdir(mode=0o700)
        return {"path": "/".join(parts)}


class SharedRead:
    def __init__(self, descriptor: int, relative_path: str, directory: _Directory, name: str):
        self.directory, self.name = directory, name
        self.stream = None
        self._reopen = _reopen_between_shared_reads()
        self._closed = False
        self.path = relative_path
        self.offset = 0
        self.digest = hashlib.sha256()
        try:
            self.stream = self._wrap_descriptor(descriptor)
            handle = os.fstat(self.stream.fileno())
            if self._reopen:
                # Capture the baseline only after closing the initial Windows
                # handle, so any pending NTFS version information is committed.
                self._close_stream()
            self.status = directory.status(name)
            self.size = self.status.st_size
            if (handle.st_dev, handle.st_ino) != (self.status.st_dev, self.status.st_ino):
                raise SharedError("Shared file changed during download")
            if self.size > MAX_FILE_SIZE:
                raise SharedError("Shared files are limited to 1 GiB")
            self._check_metadata()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _wrap_descriptor(descriptor):
        try:
            # Do not retain prefetched bytes across another process's rewrite.
            return os.fdopen(descriptor, "rb", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise

    def _close_stream(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.close()

    def _check_metadata(self):
        try:
            current = self.directory.status(self.name)
        except OSError as error:
            raise SharedError("Shared file changed during download") from error
        def version(status):
            return (status.st_size, status.st_mtime_ns, status.st_ctime_ns,
                    status.st_ino, status.st_dev)
        if (_linked(current) or not stat.S_ISREG(current.st_mode) or current.st_nlink > 1
                or version(current) != version(self.status)):
            raise SharedError("Shared file changed during download")

    def _read_reopened(self, length):
        self._check_metadata()
        self.stream = self._wrap_descriptor(self.directory.open_file(self.name))
        try:
            handle = os.fstat(self.stream.fileno())
            if (handle.st_dev, handle.st_ino) != (self.status.st_dev, self.status.st_ino):
                raise SharedError("Shared file changed during download")
            self._check_metadata()
            self.stream.seek(self.offset)
            block = self.stream.read(length)
        finally:
            # Direct Windows FileBasicInfo/ChangeTime queries also remain stale
            # while this handle is open. Check the path AFTER releasing it.
            self._close_stream()
        self._check_metadata()
        return block

    def read(self, offset, length):
        if type(offset) is not int or offset != self.offset or type(length) is not int or not 1 <= length <= CHUNK_SIZE:
            raise SharedError("Invalid shared-file read range")
        if self._reopen:
            block = self._read_reopened(min(length, self.size - self.offset))
        else:
            self._check_metadata()
            block = self.stream.read(min(length, self.size - self.offset))
            self._check_metadata()
        self.offset += len(block)
        self.digest.update(block)
        if not block and self.offset != self.size:
            raise SharedError("Shared file changed during download")
        done = self.offset == self.size
        checksum = None
        if done:
            self._check_metadata()
            checksum = self.digest.hexdigest()
        return block, {"offset": offset, "size": self.size, "eof": done, "sha256": checksum}

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._close_stream()
        finally:
            self.directory.close()


class SharedUpload:
    def __init__(self, directory: _Directory, name: str, size: int, relative_path: str):
        import secrets
        self.directory = directory
        self.name, self.path, self.size = name, relative_path, size
        self.received = 0
        self.digest = hashlib.sha256()
        self.stream = None
        self._owns_temporary = False
        self._closed = False
        self.temporary = ".virtualkvm-" + secrets.token_hex(16) + ".part"
        try:
            try:
                directory.status(name)
            except FileNotFoundError:
                pass
            else:
                raise SharedError("Destination file already exists")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(self.temporary, flags, 0o600, dir_fd=directory.fd) if directory.fd is not None else os.open(directory.path / self.temporary, flags, 0o600)
            self._owns_temporary = True
            self.stream = os.fdopen(descriptor, "wb")
        except BaseException:
            self.close()
            raise

    def append(self, block: bytes):
        if not block or len(block) > CHUNK_SIZE or self.received + len(block) > self.size:
            raise SharedError("Shared upload exceeds its declared size")
        self.stream.write(block)
        self.digest.update(block)
        self.received += len(block)

    def finish(self, checksum):
        actual = self.digest.hexdigest()
        if not isinstance(checksum, str) or self.received != self.size or not hmac.compare_digest(checksum, actual):
            raise SharedError("Shared upload size or SHA-256 mismatch")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        if self.directory.fd is not None:
            os.link(self.temporary, self.name, src_dir_fd=self.directory.fd,
                    dst_dir_fd=self.directory.fd, follow_symlinks=False)
            os.unlink(self.temporary, dir_fd=self.directory.fd)
        else:
            os.link(self.directory.path / self.temporary, self.directory.path / self.name)
            (self.directory.path / self.temporary).unlink()
        return {"path": self.path, "size": self.size, "sha256": actual}

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.stream:
            try:
                self.stream.close()
            except OSError:
                pass
        try:
            if not self._owns_temporary:
                return
            if self.directory.fd is not None:
                os.unlink(self.temporary, dir_fd=self.directory.fd)
            else:
                (self.directory.path / self.temporary).unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            self.directory.close()

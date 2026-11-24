"""Reader/writer for the Crypt of the NecroDancer ``WSP1`` package container.

Format reference: ``SPEC.md`` at the repository root.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAGIC = b"WSP1"
LEGACY_MAGIC = b"WSP0"
HEADER_SIZE = 9
COMPRESS_THRESHOLD = 100
DEFAULT_LEVEL = 8
UNKNOWN_SIZE = 0xFFFFFFFF


class PackageError(Exception):
    """Raised when a package is malformed or cannot be built."""


@dataclass
class Entry:
    """One file inside a package."""

    name: str
    data: bytes
    flags: int = 0
    compressed: bool | None = None
    payload: bytes | None = None
    """Exact bytes stored on disk (after the size field), when read from a file."""
    offset: int | None = None

    @property
    def stored_size(self) -> int:
        if self.payload is not None:
            return len(self.payload)
        return len(self.data)

    @property
    def record_size(self) -> int:
        """Bytes this entry occupies in the file, excluding its header name copy."""
        return 2 + len(self.name.encode("utf-8")) + 6 + self.stored_size


@dataclass
class Package:
    entries: list[Entry] = field(default_factory=list)
    use_zstd: bool = False
    path: Path | None = None
    magic: bytes = MAGIC

    @property
    def total_size(self) -> int:
        return sum(len(e.data) for e in self.entries)

    @property
    def stored_size(self) -> int:
        return sum(e.stored_size for e in self.entries)

    @property
    def header_size(self) -> int:
        return (
            HEADER_SIZE
            + sum(2 + len(e.name.encode("utf-8")) for e in self.entries)
            + 8 * len(self.entries)
        )

    def find(self, name: str) -> Entry:
        for entry in self.entries:
            if entry.name == name:
                return entry
        raise PackageError(f"no such file in package: {name!r}")

    def names(self) -> list[str]:
        return [e.name for e in self.entries]


def _need(buf: bytes, offset: int, size: int, what: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(buf):
        raise PackageError(
            f"truncated package while reading {what} "
            f"(offset {offset:#x}, need {size} bytes, file is {len(buf)})"
        )


def _u16(buf: bytes, offset: int) -> int:
    _need(buf, offset, 2, "u16")
    return struct.unpack_from("<H", buf, offset)[0]


def _u32(buf: bytes, offset: int) -> int:
    _need(buf, offset, 4, "u32")
    return struct.unpack_from("<I", buf, offset)[0]


def _read_string(buf: bytes, offset: int, what: str) -> tuple[str, int]:
    """Read a ``u16``-prefixed string, returning ``(text, next_offset)``."""
    length = _u16(buf, offset)
    start = offset + 2
    _need(buf, start, length, what)
    try:
        text = buf[start : start + length].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackageError(f"{what} is not valid UTF-8") from exc
    return text, start + length


def _decompress(payload: bytes, use_zstd: bool) -> bytes:
    if use_zstd:
        try:
            import zstandard  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise PackageError(
                "package uses Zstandard payloads; install the optional "
                "dependency with `uv sync --group zstd`"
            ) from exc
        try:
            return zstandard.ZstdDecompressor().decompress(payload)
        except zstandard.ZstdError as exc:
            raise PackageError(f"zstd payload is corrupt: {exc}") from exc

    if len(payload) < 4:
        raise PackageError("compressed payload is shorter than its 4-byte size prefix")
    expected = struct.unpack_from("<I", payload, 0)[0]
    try:
        if expected == UNKNOWN_SIZE:
            obj = zlib.decompressobj()
            return obj.decompress(payload[4:]) + obj.flush()
        data = zlib.decompress(payload[4:])
    except zlib.error as exc:
        raise PackageError(f"zlib payload is corrupt: {exc}") from exc
    if expected != UNKNOWN_SIZE and len(data) != expected:
        raise PackageError(
            f"zlib payload size mismatch: header says {expected}, inflated {len(data)}"
        )
    return data


def read_package(source: str | Path | bytes) -> Package:
    """Parse a package from a path or from raw bytes."""
    if isinstance(source, bytes):
        buf, path = source, None
    else:
        path = Path(source)
        buf = path.read_bytes()

    if len(buf) < HEADER_SIZE:
        raise PackageError("file is too short to be a package")
    magic = buf[:4]
    if magic == LEGACY_MAGIC:
        raise PackageError(
            "this is a WSP0 package; only WSP1 is supported (see SPEC.md)"
        )
    if magic != MAGIC:
        raise PackageError(f"bad magic {magic!r}, expected {MAGIC!r}")

    count = _u32(buf, 4)
    use_zstd = bool(buf[8])
    # A header entry costs at least 2 bytes of name length + 8 bytes of offset,
    # so a count that cannot possibly fit means the file is corrupt.
    if count * 10 > len(buf) - HEADER_SIZE:
        raise PackageError(
            f"header claims {count:,} files but the file is only {len(buf):,} bytes"
        )
    pos = HEADER_SIZE

    names: list[str] = []
    for index in range(count):
        name, pos = _read_string(buf, pos, f"header name {index}")
        names.append(name)

    _need(buf, pos, 8 * count, "offset table")
    offsets = list(struct.unpack_from(f"<{count}Q", buf, pos))

    entries: list[Entry] = []
    for index, (name, offset) in enumerate(zip(names, offsets)):
        stored_name, pos2 = _read_string(buf, offset, f"record name {index}")
        if stored_name != name:
            raise PackageError(
                f"record {index} name mismatch: header {name!r}, record {stored_name!r}"
            )
        _need(buf, pos2, 6, f"record header {index}")
        flags = buf[pos2]
        compressed = bool(buf[pos2 + 1])
        size = _u32(buf, pos2 + 2)
        start = pos2 + 6
        _need(buf, start, size, f"payload of {name!r}")
        payload = buf[start : start + size]
        entries.append(
            Entry(
                name=name,
                data=_decompress(payload, use_zstd) if compressed else payload,
                flags=flags,
                compressed=compressed,
                payload=payload,
                offset=offset,
            )
        )

    return Package(entries=entries, use_zstd=use_zstd, path=path, magic=magic)


def _encode_payload(
    entry: Entry, *, level: int, threshold: int, use_zstd: bool
) -> tuple[bytes, bool]:
    data = entry.data
    compress = (
        entry.compressed if entry.compressed is not None else len(data) >= threshold
    )
    if not compress:
        return data, False
    if use_zstd:
        try:
            import zstandard  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise PackageError(
                "writing Zstandard packages needs the optional dependency; "
                "run `uv sync --group zstd`"
            ) from exc
        return zstandard.ZstdCompressor(level=level).compress(data), True
    if len(data) >= UNKNOWN_SIZE:
        raise PackageError(f"{entry.name!r} is too large for the 32-bit size fields")
    return struct.pack("<I", len(data)) + zlib.compress(data, level), True


def build_package(
    entries: Iterable[Entry],
    *,
    sort: bool = True,
    level: int = DEFAULT_LEVEL,
    threshold: int = COMPRESS_THRESHOLD,
    use_zstd: bool = False,
    verbatim: bool = False,
) -> bytes:
    """Serialise entries into a ``WSP1`` package.

    ``verbatim`` reuses each entry's original payload/flag when present, which
    reproduces a parsed file byte for byte.
    """
    items = list(entries)
    if sort:
        items.sort(key=lambda e: e.name.encode("utf-8"))

    seen: set[str] = set()
    for entry in items:
        if not entry.name:
            raise PackageError("entry names must not be empty")
        if entry.name in seen:
            raise PackageError(f"duplicate entry name: {entry.name!r}")
        seen.add(entry.name)
        raw_len = len(entry.name.encode("utf-8"))
        if raw_len > 0xFFFF:
            raise PackageError(f"entry name is longer than 65535 bytes: {entry.name!r}")

    header = bytearray(MAGIC)
    header += struct.pack("<I", len(items))
    header += bytes((1 if use_zstd else 0,))
    for entry in items:
        raw = entry.name.encode("utf-8")
        header += struct.pack("<H", len(raw)) + raw

    base = len(header) + 8 * len(items)
    offsets: list[int] = []
    body = bytearray()

    for entry in items:
        if verbatim and entry.payload is not None and entry.compressed is not None:
            payload, compressed = entry.payload, entry.compressed
        else:
            payload, compressed = _encode_payload(
                entry, level=level, threshold=threshold, use_zstd=use_zstd
            )
        if len(payload) >= 1 << 32:
            raise PackageError(f"payload of {entry.name!r} exceeds 4 GiB")
        raw = entry.name.encode("utf-8")
        offsets.append(base + len(body))
        body += struct.pack("<H", len(raw)) + raw
        body += bytes((entry.flags & 0xFF, int(compressed)))
        body += struct.pack("<I", len(payload))
        body += payload

    header += b"".join(struct.pack("<Q", off) for off in offsets)
    return bytes(header + body)


def load_directory(root: str | Path) -> list[Entry]:
    """Collect every regular file below ``root`` as an entry."""
    root = Path(root)
    if not root.is_dir():
        raise PackageError(f"not a directory: {root}")
    entries = []
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        name = item.relative_to(root).as_posix()
        entries.append(Entry(name=name, data=item.read_bytes()))
    return entries


def safe_destination(root: Path, name: str) -> Path:
    """Map a package entry name onto a path under ``root``, refusing traversal."""
    pure = PurePosixPath(name)
    if pure.is_absolute() or name.startswith(("/", "\\")):
        raise PackageError(f"refusing to extract absolute path {name!r}")
    parts = pure.parts
    if not parts:
        raise PackageError(f"refusing to extract empty path {name!r}")
    for part in parts:
        if part in ("", ".", "..") or part.endswith(":") or "\\" in part:
            raise PackageError(f"refusing to extract unsafe path {name!r}")
    destination = root.joinpath(*parts)
    resolved_root = root.resolve()
    try:
        destination.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise PackageError(
            f"refusing to extract path outside {root}: {name!r}"
        ) from exc
    return destination

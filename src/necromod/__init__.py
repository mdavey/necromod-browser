"""Tools for Crypt of the NecroDancer ``WSP1`` packages."""

from .package import (
    COMPRESS_THRESHOLD,
    DEFAULT_LEVEL,
    MAGIC,
    Entry,
    Package,
    PackageError,
    build_package,
    load_directory,
    read_package,
    safe_destination,
)

__all__ = [
    "COMPRESS_THRESHOLD",
    "DEFAULT_LEVEL",
    "MAGIC",
    "Entry",
    "Package",
    "PackageError",
    "build_package",
    "load_directory",
    "read_package",
    "safe_destination",
]

__version__ = "0.1.0"

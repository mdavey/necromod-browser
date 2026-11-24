"""Command line interface for the ``WSP1`` package format."""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from collections import Counter
from pathlib import Path

from .package import (
    COMPRESS_THRESHOLD,
    DEFAULT_LEVEL,
    Entry,
    Package,
    PackageError,
    build_package,
    load_directory,
    read_package,
    safe_destination,
)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _method(entry: Entry) -> str:
    if not entry.compressed:
        return "stored"
    return (
        "zstd"
        if entry.payload and entry.payload[:4] == b"\x28\xb5\x2f\xfd"
        else "deflate"
    )


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _load(path: str) -> Package:
    return read_package(path)


def _default_outdir(package_path: str) -> Path:
    path = Path(package_path)
    return path.with_suffix("")


# --------------------------------------------------------------------------- info


def cmd_info(args: argparse.Namespace) -> int:
    package = _load(args.package)
    compressed = sum(1 for e in package.entries if e.compressed)
    largest = max(package.entries, key=lambda e: len(e.data), default=None)
    rows = [
        ("path", str(package.path) if package.path else "<bytes>"),
        ("magic", package.magic.decode("ascii", "replace")),
        ("files", f"{len(package.entries):,}"),
        ("useZstd", "yes" if package.use_zstd else "no"),
        ("stored", f"{package.stored_size:,} B ({_human(package.stored_size)})"),
        ("uncompressed", f"{package.total_size:,} B ({_human(package.total_size)})"),
        (
            "compressed",
            f"{compressed:,} ({compressed / len(package.entries):.0%})"
            if package.entries
            else "0",
        ),
        ("file size", f"{package.path.stat().st_size:,} B" if package.path else "-"),
    ]
    if largest is not None:
        rows.append(("largest", f"{largest.name} ({_human(len(largest.data))})"))
    if package.path is not None:
        file_size = package.path.stat().st_size
        rows.append(
            (
                "overhead",
                f"{file_size - package.stored_size:,} B (header, name tables, record fields)",
            )
        )
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key:<{width}}  {value}")
    return 0


# ----------------------------------------------------------------------------- ls


def cmd_ls(args: argparse.Namespace) -> int:
    package = _load(args.package)
    entries = list(package.entries)
    key = {
        "name": lambda e: e.name,
        "size": lambda e: len(e.data),
        "stored": lambda e: e.stored_size,
    }[args.sort]
    entries.sort(key=key, reverse=args.reverse)

    if args.json:
        json.dump(
            [
                {
                    "name": e.name,
                    "size": len(e.data),
                    "stored": e.stored_size,
                    "compressed": bool(e.compressed),
                    "flags": e.flags,
                    "offset": e.offset,
                }
                for e in entries
            ],
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    if not entries:
        print("(empty package)")
        return 0

    name_width = min(max(len(e.name) for e in entries), args.width)
    size_width = max(4, max(len(f"{len(e.data):,}") for e in entries))
    stored_width = max(6, max(len(f"{e.stored_size:,}") for e in entries))

    if args.long:
        header = (
            f"{'SIZE':>{size_width}}  {'STORED':>{stored_width}}  {'METHOD':<8}  NAME"
        )
        print(header)
        print("-" * len(header))
    for entry in entries:
        name = entry.name
        if len(name) > name_width:
            name = name[: name_width - 1] + "\u2026"
        if args.long:
            print(
                f"{len(entry.data):>{size_width},}  {entry.stored_size:>{stored_width},}  "
                f"{_method(entry):<8}  {name}"
            )
        else:
            print(f"{len(entry.data):>{size_width},}  {name}")

    total = sum(len(e.data) for e in entries)
    if args.long:
        print("-" * (size_width + stored_width + 20))
    print(
        f"{len(entries)} file(s), {total:,} B",
        file=sys.stderr if args.quiet else sys.stdout,
    )
    return 0


# ---------------------------------------------------------------------------- cat


def cmd_cat(args: argparse.Namespace) -> int:
    package = _load(args.package)
    entry = package.find(args.name)
    sys.stdout.buffer.write(entry.data)
    sys.stdout.buffer.flush()
    return 0


# ------------------------------------------------------------------------ extract


def cmd_extract(args: argparse.Namespace) -> int:
    package = _load(args.package)
    selected = list(package.entries)

    if args.name:
        wanted = []
        for pattern in args.name:
            matches = [e for e in package.entries if e.name == pattern]
            if not matches:
                matches = [
                    e for e in package.entries if fnmatch.fnmatch(e.name, pattern)
                ]
            if not matches:
                raise PackageError(f"no entry matches {pattern!r}")
            wanted.extend(matches)
        selected = wanted
    if args.filter:
        selected = [
            e for e in selected if any(fnmatch.fnmatch(e.name, p) for p in args.filter)
        ]

    outdir = Path(args.output) if args.output else _default_outdir(args.package)
    if not selected:
        print("nothing to extract", file=sys.stderr)
        return 0

    written = skipped = failed = 0
    for entry in selected:
        try:
            destination = safe_destination(outdir, entry.name)
        except PackageError as exc:
            failed += 1
            print(f"error: {exc}", file=sys.stderr)
            continue
        if destination.exists() and not args.force:
            skipped += 1
            if args.verbose:
                print(f"skip    {entry.name} (exists)")
            continue
        if args.dry_run:
            print(f"would extract {entry.name} -> {destination}")
            written += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry.data)
        written += 1
        if args.verbose:
            print(f"extract {entry.name} ({len(entry.data):,} B)")

    if args.dry_run:
        print(f"dry run: {written} file(s) would be extracted to {outdir}")
    else:
        print(
            f"extracted {written} file(s) to {outdir}"
            + (f", skipped {skipped}" if skipped else "")
        )
    if failed:
        print(f"{failed} file(s) refused", file=sys.stderr)
    return 1 if failed else 0


# --------------------------------------------------------------------------- pack


def cmd_pack(args: argparse.Namespace) -> int:
    entries = load_directory(args.directory)
    if args.exclude:
        entries = [
            e
            for e in entries
            if not any(fnmatch.fnmatch(e.name, p) for p in args.exclude)
        ]
    if not entries:
        raise PackageError(f"no files found under {args.directory}")

    if args.no_compress:
        for entry in entries:
            entry.compressed = False

    blob = build_package(
        entries,
        sort=not args.no_sort,
        level=args.level,
        threshold=args.threshold,
        use_zstd=args.zstd,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(blob)
    compressed = sum(
        1
        for e in entries
        if e.compressed is not False and len(e.data) >= args.threshold
    )
    print(
        f"wrote {output} ({len(blob):,} B): {len(entries)} file(s), "
        f"{compressed} compressed, {len(blob) - sum(len(e.data) for e in entries):+,} B vs raw",
        file=sys.stderr if args.quiet else sys.stdout,
    )
    return 0


# ------------------------------------------------------------------------- verify


def cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.package)
    raw = path.read_bytes()
    package = read_package(raw)  # decodes every payload as it parses
    print(
        f"{path}: magic={package.magic.decode()} files={len(package.entries)} "
        f"zstd={'yes' if package.use_zstd else 'no'}"
    )

    problems: list[str] = []

    names = package.names()
    counts = Counter(names)
    duplicates = sorted(n for n, c in counts.items() if c > 1)
    if duplicates:
        problems.append(f"duplicate entry names: {duplicates[:5]}")

    expected = package.header_size
    for entry in package.entries:
        if entry.offset != expected:
            problems.append(
                f"{entry.name}: offset {entry.offset:#x}, expected {expected:#x} "
                "(records are not contiguous)"
            )
            expected = entry.offset
        expected += entry.record_size
        if args.verbose:
            print(f"  ok  {entry.name} ({len(entry.data):,} B)")
    if expected != len(raw):
        problems.append(
            f"records end at {expected:#x} but the file is {len(raw):,} bytes"
        )

    for message in problems:
        print(f"  FAIL {message}", file=sys.stderr)

    print(
        f"  {len(package.entries):,} entries, {package.stored_size:,} B stored, "
        f"{package.total_size:,} B uncompressed, {len(problems)} problem(s)"
    )

    if args.rebuild:
        rebuilt = build_package(package.entries, sort=False, verbatim=True)
        if rebuilt == raw:
            print(f"  rebuild: identical ({len(raw):,} B)")
        else:
            problems.append("rebuild differs")
            offset = next(
                (i for i, (a, b) in enumerate(zip(rebuilt, raw)) if a != b),
                min(len(rebuilt), len(raw)),
            )
            print(
                f"  rebuild: DIFFERS at {offset:#x} "
                f"(rebuilt {len(rebuilt):,} B, original {len(raw):,} B)",
                file=sys.stderr,
            )

    return 0 if not problems else 1


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="necromod",
        description="Inspect and build Crypt of the NecroDancer WSP1 packages "
        "(.wsp / .necromod). See SPEC.md for the format.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    info = sub.add_parser("info", help="show header summary")
    info.add_argument("package")
    info.set_defaults(func=cmd_info)

    ls = sub.add_parser("ls", aliases=["list"], help="list entries")
    ls.add_argument("package")
    ls.add_argument(
        "-l", "--long", action="store_true", help="show stored size and method"
    )
    ls.add_argument("--json", action="store_true", help="emit JSON")
    ls.add_argument("--sort", choices=("name", "size", "stored"), default="name")
    ls.add_argument("-r", "--reverse", action="store_true")
    ls.add_argument("--width", type=int, default=72, help="max name width (default 72)")
    ls.add_argument("-q", "--quiet", action="store_true", help="hide the summary line")
    ls.set_defaults(func=cmd_ls)

    cat = sub.add_parser("cat", help="write one entry to stdout")
    cat.add_argument("package")
    cat.add_argument("name")
    cat.set_defaults(func=cmd_cat)

    extract = sub.add_parser(
        "extract", aliases=["x"], help="extract entries to a directory"
    )
    extract.add_argument("package")
    extract.add_argument("name", nargs="*", help="entry names or globs (default: all)")
    extract.add_argument(
        "-o", "--output", help="target directory (default: package stem)"
    )
    extract.add_argument(
        "-f", "--filter", action="append", help="glob filter, repeatable"
    )
    extract.add_argument(
        "-F", "--force", action="store_true", help="overwrite existing files"
    )
    extract.add_argument("-n", "--dry-run", action="store_true")
    extract.add_argument("-v", "--verbose", action="store_true")
    extract.set_defaults(func=cmd_extract)

    pack = sub.add_parser("pack", help="build a package from a directory")
    pack.add_argument("directory")
    pack.add_argument(
        "-o", "--output", required=True, help="output .wsp/.necromod path"
    )
    pack.add_argument(
        "-l", "--level", type=int, default=DEFAULT_LEVEL, help="zlib level 0-9"
    )
    pack.add_argument(
        "-t",
        "--threshold",
        type=int,
        default=COMPRESS_THRESHOLD,
        help=f"compress files of at least N bytes (default {COMPRESS_THRESHOLD})",
    )
    pack.add_argument(
        "--no-compress", action="store_true", help="store every entry verbatim"
    )
    pack.add_argument("--no-sort", action="store_true", help="keep discovery order")
    pack.add_argument(
        "--zstd", action="store_true", help="write Zstandard payloads (needs zstandard)"
    )
    pack.add_argument(
        "--exclude", action="append", help="glob of names to skip, repeatable"
    )
    pack.add_argument("-q", "--quiet", action="store_true")
    pack.set_defaults(func=cmd_pack)

    verify = sub.add_parser("verify", help="decode every entry and check invariants")
    verify.add_argument("package")
    verify.add_argument("-v", "--verbose", action="store_true")
    verify.add_argument(
        "--rebuild",
        action="store_true",
        help="re-serialise the parsed entries and compare byte for byte",
    )
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PackageError as exc:
        return _fail(str(exc))
    except FileNotFoundError as exc:
        return _fail(f"no such file: {exc.filename}")
    except IsADirectoryError as exc:
        return _fail(f"is a directory: {exc.filename}")
    except BrokenPipeError:  # e.g. `necromod cat ... | head`
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

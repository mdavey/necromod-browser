# `WSP1` package container (`.wsp` / `.necromod`)

Crypt of the NecroDancer resource package format.

* Reversed from: `NecroDancer.x64` v4.2.1-b5713 (Linux x86-64)
* All multi-byte integers are **unsigned little-endian**
* `bool` is **1 byte** (non-zero = true)
* Strings are **UTF-8 byte strings with a `u16` length prefix and no terminator**
* There is no padding / alignment

---

## 1. Layout

```
package:
    char     magic[4]           ; "WSP1"
    u32      fileCount
    bool     useZstd            ; payload compression algorithm for the whole package
    string   names[fileCount]   ; u16 len + bytes, sorted ascending (see §2)
    u64      offsets[fileCount] ; absolute file offsets of the entry records
    record   records[fileCount] ; same order as names[]

record:
    string   name               ; u16 len + bytes (identical to names[i])
    u8       flags              ; always 0 in all known files (reserved)
    bool     compressed
    u32      payloadSize        ; bytes that follow; INCLUDES the 4-byte u32 size prefix
    u8       payload[payloadSize]

payload (compressed == true, useZstd == false):
    u32      uncompressedSize   ; little-endian, see §4
    u8       zlib[payloadSize-4] ; RFC 1950 zlib stream (deflate)

payload (compressed == false):
    u8       raw[payloadSize]
```

There is **no footer, checksum or trailer**. The last byte of the last
payload is the last byte of the file.

### Constraints / invariants

* `fileCount` is a `u32`. Offsets are `u64`, so packages may exceed 4 GiB.
* `offsets[i]` points at the `u16` length of `records[i].name`.
* `names[i]` and `records[i].name` are the same string for the same `i`.
* `payloadSize == 0` is not exercised by any shipped file; a zero-length
  uncompressed entry is the expected way to store an empty file (the reader
  handles a zero-length extract). For a compressed entry the minimum value is
  4, the size prefix alone.
* Lookup in the shipped engine is a hash map keyed by the string, so a package
  containing duplicate names will silently drop all but one.

### ImHex compatible pattern

```cpp
/* WSP1 - Crypt of the NecroDancer package container (.wsp / .necromod) */

#pragma endian little
#pragma description WSP1 package - Crypt of the NecroDancer
#pragma magic [ 57 53 50 31 ] @ 0x00

import std.core;     // std::core::array_index
import std.string;   // std::string::SizedString
import std.mem;      // std::mem::Bytes
import std.sys;      // std::assert
import type.magic;   // type::Magic

using WspString = std::string::SizedString<u16>;

struct WspRecord<auto UseZstd> {
    WspString name;
    u8  flags;
    bool compressed;
    u32 payloadSize;                    // includes the 4-byte size prefix

    if (!compressed) {
        std::mem::Bytes<payloadSize> raw;
    } else if (UseZstd) {
        std::mem::Bytes<payloadSize> zstdFrame;
    } else {
        std::assert(payloadSize >= 4, "compressed record without the 4-byte size prefix");
        u32 uncompressedSize;
        std::mem::Bytes<payloadSize - 4> zlib;
    }
};

struct WspRecordRef {
    u32 index = std::core::array_index();
    WspRecord<parent.useZstd> record @ parent.offsets[index] [[inline]];
} [[name(std::string::to_string(record.name))]];

struct WspPackage {
    type::Magic<"WSP1"> magic;
    u32       fileCount;
    bool      useZstd                           [[comment("false in every shipped archive")]];
    WspString names[fileCount];                 // sorted ascending, see section 2
    u64       offsets[fileCount];               // absolute offsets of the records
    WspRecordRef records[fileCount];            // same order as names[]
};

WspPackage package @ 0x00;
```


---

## 2. Ordering

The official compiler (`res::PackageCompiler::compile`) sorts the file list
with `std::sort` on `std::string` (byte-wise) before writing, so
`names[0..n-1]` are in ascending byte order and the records follow that same
order. Readers must **not** rely on this: use `offsets[]`.

Example (Amplified): `AmplifiedBanner.png`, `AmplifiedIcon.png`, `mod.json`,
`package.id`.

---

## 3. Annotated example — `dlc/Amplified.necromod` (5 334 bytes)

```
0000  57 53 50 31              "WSP1"
0004  04 00 00 00              fileCount  = 4
0008  00                       useZstd    = false
0009  13 00 "AmplifiedBanner.png"
001e  11 00 "AmplifiedIcon.png"
0031  08 00 "mod.json"
003b  0a 00 "package.id"

0047  u64[4] = { 0x000067, 0x000a03, 0x0013e0, 0x0014bf }

0067  13 00                    name len   = 19
0069  41 6d 70 ...             "AmplifiedBanner.png"      (ends 007b)
007c  00                       flags      = 0
007d  01                       compressed = true
007e  81 09 00 00              payloadSize = 2433
0082  ad 09 00 00              uncompressedSize = 2477
0086  78 da ...                zlib stream (2429 bytes)   (ends 0a02)

0a03  11 00 "AmplifiedIcon.png" ...                        (entry 2)
13e0  08 00 "mod.json" ...
14bf  0a 00 "package.id"
14cb  00                       flags      = 0
14cc  00                       compressed = false
14cd  05 00 00 00              payloadSize = 5
14d1  34 2e 30 2e 30           "4.0.0"                    (ends 14d5 == EOF)
```

Note how `payloadSize` **includes** the 4-byte `uncompressedSize` field: the
compressed payload region starts at `0x0082`, not at `0x0086`. Getting this
wrong by 4 bytes is the single easiest mistake to make with this format.

---

## 4. Payload compression

The engine calls `res::Package::readData` → `res::Package::decompressData`.
If no Zstandard decompressor was created while parsing the header, it calls
`zu::decompress`, i.e. the small "zu" zlib wrapper the game vendors:

* `payload[0..3]` — uncompressed size (`u32` LE)
* `payload[4..]` — an RFC 1950 zlib stream (2-byte header, deflate data,
  4-byte Adler-32), produced by `compress2(...)`
* Special value `0xFFFFFFFF` in `payload[0..3]` means "size unknown"; the
  reference `zu` implementation then inflates into a progressively doubling
  buffer.

The 4-byte size is **not** trusted for allocation validation by the engine —
`zu::decompressTransfer` resizes to it and verifies the resulting length
matches, so a wrong value fails the decompression.

### Compression policy of the official compiler

```c
if (source.size() < 100)  compressed = false;          // stored verbatim
else                      zu::compress(buffer, /*level=*/8);
```

Confirmed empirically: the largest stored entry in the shipped data is 92
bytes; the smallest compressed entry is 100 bytes.

Because PNG/JPEG/etc. are already compressed, `payloadSize` for those is
usually slightly *larger* than the uncompressed size. Don't use size heuristics
to guess whether an entry is compressed — read the flag.

### `useZstd == true` variant

When the header flag is set, `res::Package::readHeader` constructs a
`ZStandard::Decompressor` and `decompressData` sends the payload to
`ZStandard::Decompressor::decompressToVector` instead of `zu::decompress`.
In that mode the payload is a **raw Zstandard frame** (no 4-byte size prefix),
and the decompressor may use a dictionary configured by
`wos.game.assets.compression.dictionaryPath` (`"zstd"` in `config.json`).

**Every shipped archive in this installation has `useZstd == 0`**, and the
compiler in this build always writes `false` (it has no zstd path). Treat the
zstd branch as documented-but-unverified; if you implement it, disable the
4-byte prefix and pass the payload straight to `ZSTD_decompress`.

---

## 5. Legacy `WSP0`

`res::detail::PackageFormat` declares two magics: `headerMagic = "WSP0"` and
`headerMagicV1 = "WSP1"`. `WSP0` is the older "legacy" layout produced when
`PackageCompiler::setLegacy(true)`. **No shipped file currently uses it**

The layout below is reconstructed from `Package::readHeader` / `findContent` / 
`PackageCompiler::compile` and has not been tested against real data.

```
package (WSP0):
    char  magic[4]        ; "WSP0"
    u32   fileCount
    u32   buckets[256]    ; hint -> absolute offset of the first matching record
    record records[...]   ; sorted by hint, contiguous, no global name/offset table

record:
    (identical to WSP1)
```

Lookup: `hint = nameHint(name) & 0xFF` where
`nameHint(s) = hash::dataHash32(removeFileExtension(s))`; seek to
`buckets[hint]`, then loop: read the record header, compare the name; if the
name doesn't match, stop as soon as `nameHint(name) & 0xFF` differs from
`hint`, otherwise `seekOff(payloadSize)` (a *relative* seek, which relies on
records of the same hint being adjacent) and read the next record.

---

## 6. What the game does with these files

Relevant `res::Package` members (offsets in the Linux x64 build, for future
work):

| offset | type | meaning |
|---|---|---|
| `0x88` | `u64` | currently selected record offset |
| `0x90` | `std::string` | currently selected record name |
| `0xb0` | `u8` | per-record `flags` (always 0) |
| `0xb8` | `std::vector<char>` | selected payload buffer |
| `0xd0` | `bool` | payload already read |
| `0xd1` | `bool` | `WSP1` (name→offset map present) |
| `0xd8` | `ZStandard::Decompressor*` | non-null iff header `useZstd` |
| `0xe0` | `std::vector<std::string>` | header name table |
| `0xf8` | `sparse_hash_map<std::string,u64>` | name → record offset |
| `0x150` | `u32` | `fileCount` |
| `0x158` | `std::vector<u32>` | `WSP0` bucket table |
| `0x170` | `std::vector<char>` | BLAKE2s-256 of the whole file (cache key only, not stored) |

Key functions: 

| Function name                   | Offset     |
| ------------------------------- | ---------- |
| `res::Package::readHeader`      | `0x467ad0` |
| `res::Package::open`            | `0x467960` |
| `res::Package::select`          | `0x468160` |
| `res::Package::findContent`     | `0x468240` |
| `res::Package::readData`        | `0x468600` |
| `res::Package::decompressData`  | `0x468840` | 
| `res::PackageCompiler::compile` | `0x46b820` |
| `zu::decompressTransfer<std::vector<char>>` | `0x62bc80` |

### `package.id` and `mod.json`

* `Package::open` looks up `"package.id"`, selects it and reads it, purely as a
  readability/identity check (the data is then discarded).
* `package.id` is a plain-text version string — `4.0.0`, `4.1.0`, `4.2.1`, …
  It matches the `version` field in `mod.json`.
* `mod.json` is the mod manifest consumed by the Lua mod loader, e.g.
  ```json
  {"name":"Amplified","displayName":"Amplified","version":"4.0.0",
   "description":"Enables content from Crypt of the NecroDancer: AMPLIFIED!",
   "banner":"AmplifiedBanner.png","icon":"AmplifiedIcon.png",
   "dependencies":[],"tags":[],"unlisted":true,
   "autoBundle":["SP","MP"],"api":{"scriptPath":""}}
  ```

### Mounting / patching

`config.json` (`wos.game.assets`, `wos.game.mods`) drives which packages are
mounted, all of them using this same container:

| role | file | note |
|---|---|---|
| base assets | `NecroDancer.wsp` | listed in `assets.packages` |
| compatibility | `Compat.wsp` | mounted by the `Amplified` entry in `mods.additionalPackages` (`order: -20`) |
| version patch | `versions/v4.2.1.wsp` | `mods.preload` + `mods.additionalPackages.Patch`, `order: -1000`, prefix `versions` |
| overlay patch | `Overlay-<ver>.wsp` (+ `Overlay-<ver>.sig`) | `order: -1050`, needs a signature file; none present in this install |
| DLC / mod package | `dlc/*.necromod` | `mods.loadPaths` entry `dlc` (`package: true`) |

`dlc` packages are mounted under the `ext` mount point; patch packages are
mounted at the root and override base assets. `paths::getPackageExtension()`
returns **`.wsp`** — `.necromod` is only a naming convention for drop-in
content, not a different format.

# Exordos Chunked Disk Image Format

Version: **1** (draft).

This document specifies how a raw disk image is stored as content-defined chunks
packed into OCI blobs, and what a reader must check before it writes such an
image to a disk. The rationale, measurements and the integration with the
Exordos builder and Seed OS are described in the `exordos` repository,
`docs/disk-image-format.md`.

The key words MUST, MUST NOT, SHOULD and MAY are to be interpreted as described
in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

## Standards it builds on

The format reuses existing formats wherever possible:

- The manifest is an [OCI image manifest](https://github.com/opencontainers/image-spec/blob/main/manifest.md)
  (image-spec 1.1) with an `artifactType`. Blobs are addressed by `sha256`
  digests, and `org.opencontainers.image.*` annotations are used as defined
  there. The blob directory of a static repository follows the
  [OCI image layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)
  (`blobs/sha256/<hex>`).
- The chunk index is a [casync](https://github.com/systemd/casync) blob index
  (`.caibx`), as written by casync and [desync](https://github.com/folbricht/desync).
- A compressed chunk is a [zstd frame](https://www.rfc-editor.org/rfc/rfc8878),
  byte for byte the content of a casync `.cacnk` chunk file.

Defined here: the pack, the pack map, the config, the rules that tie them to
the manifest, and how static repositories and registries hold them.

## Overview

```text
manifest (artifactType application/vnd.exordos.disk.v1)
├── config      image size, chunking parameters, digest algorithm
├── layers[0]   index      (.caibx: chunk end offsets and chunk IDs)
├── layers[1]   pack map   (index entry i → layer, offset, length)
└── layers[2..] packs      (compressed chunks, back to back)
```

A raw image of `size` bytes is split into chunks. The index lists them in disk
order. Each chunk is either a *zero chunk*, stored nowhere, or a *data chunk*,
stored once as a zstd frame in one of the packs. The pack map says which. Packs
are immutable and shared between images, so a new version of an image lists
the packs of the previous version that hold its unchanged chunks and adds new
packs only for new chunks.

## Chunking

- Chunk boundaries are content-defined, with the buzhash rolling hash of casync
  and desync. Parameters are the minimum, average and maximum chunk size.
- A chunk ID is a digest of the **uncompressed** chunk data: `sha256` or
  `sha512-256` (SHA-512/256, the casync default).
- Writers SHOULD use a minimum of 16 KiB, an average of 64 KiB and a maximum of
  256 KiB with `sha256` (`desync make -m 16:64:256 --digest sha256`). All
  images of a repository SHOULD use the same parameters: other parameters give
  other boundaries and chunk IDs, and nothing deduplicates.

Readers do not re-run the chunker. They rely on the index only.

## Objects

All integers in binary objects are little-endian.

### Config

Media type `application/vnd.exordos.disk.config.v1+json`. A JSON object:

```json
{
  "chunking": {"avg": 65536, "max": 262144, "min": 16384},
  "compression": "zstd",
  "digest": "sha256",
  "formatVersion": 1,
  "size": 6442450944
}
```

Field | Type | Description
--- | --- | ---
`formatVersion` | integer | Version of this specification, `1`
`size` | integer | Size of the raw image in bytes
`chunking` | object | `min`, `avg`, `max` chunk sizes in bytes, integers
`digest` | string | Chunk ID algorithm: `sha256` or `sha512-256`
`compression` | string | Compression of chunks in packs: `zstd`

Readers MUST ignore unknown fields.

### Index

Media type `application/vnd.exordos.disk.index.v1`. A casync blob index:

```text
index header, 48 bytes:
  u64 size = 48
  u64 type = 0x96824D9C7B129FF9          CA_FORMAT_INDEX
  u64 flags                              bit 0x2000000000000000: SHA-512/256 chunk IDs
  u64 chunk_size_min
  u64 chunk_size_avg
  u64 chunk_size_max
table header, 16 bytes:
  u64 size = 0xFFFFFFFFFFFFFFFF
  u64 type = 0xE75B9E112F17417D          CA_FORMAT_TABLE
table entries, 40 bytes each:
  u64 end                                end offset of the chunk in the image
  u8[32] id                              chunk ID
table tail, 40 bytes:
  u64 0
  u64 0
  u64 index_offset = 48
  u64 table_size                         blob size - 48
  u64 marker = 0x4B4F050E5549ECD1        CA_FORMAT_TABLE_TAIL_MARKER
```

- Entry *i* covers the bytes from the `end` of entry *i − 1* (0 for the first
  entry) up to its own `end`. Ends strictly increase, and no chunk is longer
  than `chunk_size_max`.
- The chunk ID algorithm is `sha512-256` if the flag bit
  `0x2000000000000000` is set, `sha256` otherwise. Readers MUST ignore the other
  flag bits. desync sets `0x9000000000000000` in blob indexes, and writers
  SHOULD do the same.
- Zero ranges of the image are chunked and indexed like any other data.

### Pack

Media type `application/vnd.exordos.disk.pack.v1`. A pack is compressed chunks
written back to back, with no header, trailer or padding. Only the pack map
locates a chunk in a pack.

- A compressed chunk is exactly one zstd frame. It decompresses to the chunk
  data, which hashes to the chunk ID.
- A pack MAY hold chunks that a given image does not use, when it is shared
  with other images.
- The compression level is not part of the format: chunk IDs cover
  uncompressed data, so a writer may recompress chunks freely.
- Writers SHOULD order the chunks of a new pack by their first appearance in
  the image and SHOULD keep packs around 32 MiB. The pack size bounds the
  number of layers and the unit a cache fetches on a miss.

### Pack map

Media type `application/vnd.exordos.disk.packmap.v1+zstd`. The blob is one or
more zstd frames. Decompressed, it is a 16-byte header followed by one 16-byte
entry per index entry, in index order:

```text
header:  u8[8] magic = "EXDPMAP1" | u32 entry_count | u32 reserved = 0
entry:   u32 layer | u32 length | u64 offset
```

Field | Description
--- | ---
`entry_count` | Number of entries that follow, equal to the number of index entries
`layer` | Position of the pack in `manifest.layers`, 2 or more. `0xFFFFFFFF` marks a zero chunk
`length` | Length of the compressed chunk in the pack, more than 0
`offset` | Offset of the compressed chunk in the pack

- A **zero chunk** is a chunk whose data is all zeros. Its entry is
  `layer = 0xFFFFFFFF`, `length = 0`, `offset = 0`. Writers MUST mark every
  all-zero chunk this way, and MUST NOT mark any other chunk.
- Entries of chunks with the same chunk ID MUST be identical: a chunk is stored
  once per image, and a reader fetches it once and writes it at every offset.

### Manifest

Media type `application/vnd.oci.image.manifest.v1+json`:

- `schemaVersion` is `2`, `mediaType` is the manifest media type, and
  `artifactType` is `application/vnd.exordos.disk.v1`.
- `config` is the descriptor of the config.
- `layers[0]` is the index, `layers[1]` is the pack map, and `layers[2..]` are
  packs. Each descriptor has the media type of its object.
- Every pack that the pack map refers to is listed, and no other pack is. A
  pack is listed once. This keeps registry garbage collection exact: deleting
  the manifest of a version releases the packs only that version used.
- Annotations are optional, except `com.exordos.disk.blobs` in static
  repositories. Writers SHOULD set `org.opencontainers.image.title` to the image
  name and `org.opencontainers.image.version` to the version of the element the
  image belongs to. Readers MUST NOT depend on them.

Digests in descriptors are `sha256:<64 lowercase hex digits>`.

## Reading

A reader receives a manifest and can fetch blobs by digest, and byte ranges of
blobs. It writes the raw image to a target: a block device or a file.

### Rules

A reader MUST check the following rules **before it writes anything to the
target**. Each rule has an error code, which the
[conformance vectors](#conformance-vectors) refer to:

Code | Rule
--- | ---
`not-a-disk-image` | The manifest is a JSON object with `schemaVersion` 2, the manifest `mediaType` and the disk `artifactType`. Annotation values are strings
`bad-layers` | `config`, `layers[0]`, `layers[1]` and `layers[2..]` are descriptors with the media types above. At least two layers. No pack is listed twice. Every listed pack is referenced by the pack map
`blob-mismatch` | Every fetched blob has the size and the digest of its descriptor
`unsupported-version` | `formatVersion` is an integer the reader supports
`bad-config` | The config has all fields with the types above, a known `digest` and `compression`
`bad-index` | The index has the headers, entry layout, tail and increasing ends described above. `0 < min <= avg <= max`, and no chunk is longer than `max`
`bad-packmap` | The pack map decompresses, has the magic, `reserved` = 0 and `entry_count` entries. A zero entry has `length` and `offset` 0. Any other entry has `2 <= layer < len(layers)`, `length > 0` and `offset + length` within the size of the pack descriptor. Entries of the same chunk ID are identical
`inconsistent` | Config and index agree on the digest and the chunk sizes. The last index end equals `config.size` (0 without chunks). The pack map has one entry per index entry
`chunk-mismatch` | Every zero entry belongs to a chunk whose ID is the ID of that many zero bytes

A reader MUST verify every data chunk before it writes it to the target:

Code | Rule
--- | ---
`chunk-mismatch` | The pack range is exactly one zstd frame with no trailing bytes. It decompresses to exactly the chunk size, and the data hashes to the chunk ID

After the first set of rules, every chunk ID is trusted: it is reachable from
the manifest through verified digests. After the second, every byte written is.

### Writing

- The target MUST be at least `config.size` bytes.
- Every zero chunk MUST read back as zeros after the write, whatever the target
  held before. A block device reader can use `BLKZEROOUT` or write zeros; a file
  reader can truncate the file and extend it.
- A reader SHOULD fetch each data chunk once and write it at every offset where
  it appears, and SHOULD merge neighbouring ranges of a pack into one request.
- A reader MUST flush the target before it reports success.

## Repositories

A reader needs two operations: *get the manifest*, and *get a blob or a byte
range of it by digest*. Each kind of repository maps them differently.

### Static repositories

A static repository is a directory tree served over HTTP (for example by nginx)
or read from a filesystem:

```text
<repository>/blobs/sha256/<hex>                            all blobs
<repository>/<element>/<version>/images/<name>.raw.zst     whole image, optional
<repository>/<element>/<version>/images/<name>.oci.json    manifest of the chunked image
```

- The manifest is stored as the sidecar `<name>.oci.json`. A reader given the
  location of `<name>.raw.zst` finds it by replacing the suffix.
- The annotation `com.exordos.disk.blobs` is REQUIRED. It is a URI reference to
  the blob directory, ending with `/`, relative to the manifest URL
  (`../../../blobs/` in the layout above). A reader resolves it with
  [RFC 3986](https://www.rfc-editor.org/rfc/rfc3986#section-5) and reads blob
  `sha256:<hex>` from `<blobs>sha256/<hex>`. Without the annotation the reader
  MUST reject the image with `no-blob-store`.
- An HTTP server MUST support single-range `Range` requests on blobs.
- Blobs are immutable. A cache in front of `blobs/` needs no invalidation.
- A publisher MUST upload all blobs before the sidecar, so that a reader never
  sees a manifest with missing blobs.

### OCI registries

- An image is stored under the repository `<namespace>/<image name>`, tagged
  with the element version where `+` is replaced by `_`.
- It is referenced as `oci://<registry>/<repository>@sha256:<manifest digest>`
  (`oci+http://` without TLS). A reader MUST verify the manifest against that
  digest.
- A reader uses `GET /v2/<repository>/manifests/<reference>` and
  `GET /v2/<repository>/blobs/<digest>` with `Range`. The registry MUST support
  `Range` on blob `GET`, which the distribution specification only recommends.

## Conformance vectors

[`vectors/`](vectors/README.md) holds a static repository with valid images and
images that break one rule each, and `vectors/cases.json` lists what a reader
must do with every one of them. A conforming reader extracts each valid image
to the listed SHA-256, and rejects each invalid image at the listed stage:
`load` means before writing anything, `read` means while verifying chunks.
Matching the error codes is recommended but not required.

## Changes

- Version 1: initial version.

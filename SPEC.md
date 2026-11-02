# Exordos Chunked Disk Image Format

Version: **1** (draft).

The key words MUST, MUST NOT, SHOULD and MAY express requirements on an implementation.

## Identity and layout

An image has an immutable **content manifest**, containing its content config
and ordered chunk index. Its **image ID** is the SHA-256 digest of the exact
content manifest bytes. Names, release versions, repository addresses, epochs,
compression levels and build references MUST NOT be included in this manifest
or its content config. Repacking MUST preserve these objects and the image ID.

This is the identity of a particular chunked representation. Rechunking the same
raw bytes may produce a different image ID.

A separate immutable **layout manifest** contains the pack map and the packs
needed to reconstruct the image. An **epoch** identifies a layout generation
within `(repositoryId, image ID)`. Epoch numbers are positive integers no larger
than `2^64 - 1`; booleans, floats and strings are invalid. Numbers MUST
increase and MUST NOT be reused. Gaps are allowed. The same image can have
different epochs in different repositories.

`repositoryId` is a persistent repository instance UUID in canonical lowercase
hyphenated form. It is an identity, not a URL. Restoring an older administrative
catalog MUST preserve allocated epoch numbers or establish a new repository ID.

The descriptor digest identifies the exact serialized layout. An epoch number
alone is not a sufficient reference.

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

## Content manifest

The manifest uses `application/vnd.oci.image.manifest.v1+json` and has
`schemaVersion = 2`, `artifactType = application/vnd.exordos.disk.content.v1`,
a content config descriptor and exactly one layer: the chunk index.

The config media type is
`application/vnd.exordos.disk.content.config.v1+json`. Its fields are
`formatVersion = 1`, `size`, `chunking` and `digest`, as described below. The
index uses `application/vnd.exordos.disk.index.v1` and the binary encoding below.
Content configs MUST NOT contain a compression field.

The content config is JSON:

```json
{
  "formatVersion": 1,
  "size": 6442450944,
  "chunking": {"min": 16384, "avg": 65536, "max": 262144},
  "digest": "sha256"
}
```

`size` is a non-negative integer, chunk sizes are positive integers with
`min <= avg <= max`, and `digest` is `sha256` or `sha512-256`. Booleans are
not integers. Readers MUST ignore unknown config fields. Writers MUST keep
release and placement metadata outside this config.

Readers MUST verify the content manifest against the expected image ID and
verify its config and index before accepting a layout for it.

## Layout manifest

The manifest uses `application/vnd.oci.image.manifest.v1+json` and has
`schemaVersion = 2` and `artifactType = application/vnd.exordos.disk.v1`.
Its `subject` is the content manifest descriptor. Its layers are:

- `layers[0]`: the exact index descriptor from the content manifest;
- `layers[1]`: the pack map;
- `layers[2..]`: each used pack once, with no unused packs.

The pack and pack-map encodings and media types are specified below.
In particular, pack-map layer numbers still start at 2. The repeated index
descriptor references the same blob; it does not duplicate the index data.

The layout config has media type
`application/vnd.exordos.disk.config.v1+json` and contains:

Field | Type | Meaning
--- | --- | ---
`formatVersion` | integer | `1`
`repositoryId` | string | Repository instance owning this epoch
`image` | string | SHA-256 digest of the content manifest
`epoch` | integer | Epoch number in this repository for this image
`compression` | string | `zstd`
`references` | array | Exact source epochs used to build the image

All fields are required. An image built without source images has
`references = []`. Readers MUST ignore unknown config fields.

The config image digest, subject descriptor and index descriptor MUST agree
with the verified content manifest. Readers MUST perform the checks in the
reading section for the index, pack
map, zero chunks, ranges and decoded chunks, using the content config for size,
chunking and chunk digests. Invalid epoch or reference metadata MUST be rejected
before writing to the target.

## Source image references

A reference pins the **content image and the exact source layout epoch**, not
a release name, mutable tag, current pointer or URL. Each entry is an object:

Field | Type | Meaning
--- | --- | ---
`repositoryId` | string | Source repository instance UUID
`image` | descriptor | Source content manifest
`epoch` | integer | Source layout epoch number
`layout` | descriptor | Exact source layout manifest

Both descriptors MUST have
`mediaType = application/vnd.oci.image.manifest.v1+json`,
a `sha256:` digest with 64 lowercase hexadecimal digits, and an integer
`size > 0`. Booleans are not integers for these fields. Repository IDs and
epoch numbers follow the rules above. Unknown entry fields MUST be ignored.

The following JSON is schematic: the descriptor digests and sizes illustrate
the wire shape, not published objects.

```json
{
  "formatVersion": 1,
  "repositoryId": "72f39208-1bb8-4d20-b87d-2058ee1e7619",
  "image": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
  "epoch": 1,
  "compression": "zstd",
  "references": [
    {
      "repositoryId": "72f39208-1bb8-4d20-b87d-2058ee1e7619",
      "image": {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "size": 1234
      },
      "epoch": 7,
      "layout": {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "size": 5678
      }
    }
  ]
}
```

### Writer rules

1. A writer MUST use a verified snapshot of each source image and layout. The
   snapshot's image ID, repository ID, epoch, subject and index MUST agree with
   the reference. A mutable source pointer MUST be resolved once to this
   snapshot; a later pointer value MUST NOT be substituted into the record.
2. A newly built image MUST record each source epoch from which it actually
   reused data. Reuse includes referencing existing compressed chunk ranges or
   copying source chunks into new packs. A search candidate that contributed
   no data MUST NOT appear in `references`.
3. There MAY be multiple source images, repositories and source epochs. For
   each `(repositoryId, image.digest, epoch)` there MUST be exactly one layout
   digest and size. Duplicate entries and conflicting descriptors are invalid.
   A content digest MUST NOT have conflicting descriptor sizes either.
   Conversely, one layout digest MUST identify only one
   `(repositoryId, image.digest, epoch)` tuple and one descriptor size.
4. A source with the same repository ID and image ID as the output MUST have
   an epoch smaller than the output epoch. Epoch numbers in different domains
   MUST NOT be compared. A writer MUST NOT manufacture an epoch for a source
   format that does not define one.
5. Writers MUST produce a deterministic reference order, sorted by repository
   ID, image digest, epoch number and layout digest. Readers MUST NOT use array
   order to select chunk ranges or resolve conflicts.

### Repacking

`references` describes the image's build sources. A repack of that image MUST
carry this array forward unchanged while allocating a new output epoch and
writing a new map. Moving the same data MUST NOT rewrite the recorded source
epoch to that source image's current epoch.

A rebuild is a new construction operation and MAY record different actual
sources even if its content manifest and image ID are identical. It still
allocates a fresh output epoch in that domain.

The copied array is provenance. It is not an assertion that the current layout
still uses a particular source pack. The layout's map and pack descriptors
define the current physical dependencies.

### Reading and garbage collection

An image is self-contained with respect to its logical description and layout:
readers MUST NOT require fetching source manifests, source layouts or parent
chains to reconstruct it. Reference metadata is validated locally; the source
objects MAY have been retired or removed after the build completed.

References MUST NOT implicitly keep an entire source epoch, its maps or its
packs alive. They are not additional OCI layers or GC roots. Explicit pins,
active and retained output epochs, and in-progress operations determine object
lifetime. An implementation MAY keep source metadata separately for audit.

For every active or retained output epoch, GC MUST preserve its content
manifest, content config and index, as well as the layout manifest, layout
config, pack map and referenced packs. The layout's `subject` is not sufficient
to establish retention of its content manifest; the repository MUST maintain
an explicit root or equivalent protection for that content manifest and its
blobs. This does not make source entries in `references` GC roots.

GC MUST use the remaining layouts' actual pack-map ranges when deciding which
data is live. The reference graph alone does not identify the reused subset.
A source epoch can be removed while an output continues to use shared packs;
those packs MUST remain until all physical references and holds are gone.

Publication MUST coordinate the output's physical references with GC. A saved
reference record does not itself protect a source pack during a build. Before
publishing the output layout, its dependencies MUST be present and protected;
stale locations require rebasing, copying data or aborting the publication.

When given a raw file and an index, a writer MUST verify the file size and every
raw chunk against that index, including zero, repeated and reused chunks.
Reading source metadata does not verify the compressed source frames. A
publisher MUST verify each reused chunk range against its indexed chunk ID,
or rely on an already verified source layout in a trusted immutable store.
Checking that a pack exists is not evidence of its integrity or its map binding.
The reference writer prepares a layout but does not publish it to a remote
repository or fetch the reused packs; these checks remain the publisher's duty.

## Binary objects

All integers in binary objects are little-endian.

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

## Reading

A reader receives a layout manifest and can fetch content and other blobs by
digest, and byte ranges of packs. It writes the raw image to a target: a block device or a file.

### Rules

A reader MUST check the following rules **before it writes anything to the
target**. Each rule has an error code, which the
[conformance vectors](#conformance-vectors) refer to:

Code | Rule
--- | ---
`not-a-disk-image` | The manifest is a JSON object with `schemaVersion` 2, the manifest `mediaType` and the appropriate content or layout `artifactType`. Layout annotation values are strings
`bad-layers` | Layout config, subject, index, map and packs have the descriptor types above; content has exactly one index layer. No pack is listed twice. Every listed pack is referenced by the pack map
`blob-mismatch` | Every fetched blob has the size and the digest of its descriptor
`unsupported-version` | `formatVersion` is an integer the reader supports
`bad-config` | The content config has the fields and types above, a known chunk digest algorithm and no compression field
`bad-layout` | The layout config has a canonical repository UUID, a SHA-256 image ID, an integer epoch in range and `zstd` compression
`bad-references` | The references array satisfies the source-reference rules above
`bad-index` | The index has the headers, entry layout, tail and increasing ends described above. `0 < min <= avg <= max`, and no chunk is longer than `max`
`bad-packmap` | The pack map decompresses, has the magic, `reserved` = 0 and `entry_count` entries. A zero entry has `length` and `offset` 0. Any other entry has `2 <= layer < len(layers)`, `length > 0` and `offset + length` within the size of the pack descriptor. Entries of the same chunk ID are identical
`inconsistent` | Layout, subject and content index descriptors agree; the content config and index agree on the digest and chunk sizes. The last index end equals `config.size` (0 without chunks). The pack map has one entry per index entry
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

- The current layout manifest is stored as the sidecar `<name>.oci.json`.
  A reader given the location of `<name>.raw.zst` finds it by replacing the suffix.
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
- The content image is referenced as `oci://<registry>/<repository>@sha256:<content manifest digest>`
  (`oci+http://` without TLS). A reader MUST verify the manifest against that
  digest.
- A reader uses `GET /v2/<repository>/manifests/<reference>` and
  `GET /v2/<repository>/blobs/<digest>` with `Range`. The registry MUST support
  `Range` on blob `GET`, which the distribution specification only recommends.

The static sidecar is the current layout snapshot. It contains the repository
ID, image ID and epoch through its verified config; no parent traversal is
required. A reader with an expected image or repository ID MUST compare them
before writing. A refreshed layout MUST preserve the expected image ID.

All immutable objects, including content manifests and layout snapshots, are
stored by digest. Static layout annotations locate the blob directory; content
manifests contain no transport annotations. Layout digests in source references
cover the exact bytes of the source snapshot, including its annotations.

For OCI, content and layout manifests are separate objects. A repository-managed
tag derived from the image ID selects the current layout; retained epochs have
explicit roots. The subject association alone is not a retention policy. The
registry binding does not implement publication, epoch allocation or GC.

The writer requires an explicit persistent repository UUID and an epoch supplied
by the repository manager. It does not allocate epochs or lock a remote
repository. Publication MUST serialize allocation and coordinate physical holds
with GC. Reusing an output epoch for different layout bytes is forbidden.

## Conformance vectors

[`vectors/`](vectors/README.md) holds a static repository with valid images and
images that break one rule each, and `vectors/cases.json` lists what a reader
must do with every one of them. A conforming reader extracts each valid image
to the listed SHA-256, and rejects each invalid image at the listed stage:
`load` means before writing anything, `read` means while verifying chunks.
Matching the error codes is recommended but not required.

## Changes

- Version 1: content identity, layout epochs, source references, packs and pack maps.

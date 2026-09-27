# Conformance vectors

`repo/` is a static repository (see [SPEC.md](../SPEC.md#static-repositories))
with small images: `repo/<case>/1.0.0/images/disk.oci.json` and a shared
`repo/blobs/`. [`cases.json`](cases.json) describes each case:

- A valid case has the `size`, `sha256`, number of `chunks`, `zeroChunks` and
  `packs` of the image. A reader MUST extract it to exactly these bytes.
- An invalid case breaks one rule of the specification. A reader MUST reject it
  at `stage`: `load` before writing anything to the target, `read` while
  verifying chunks. `error` is the code of the broken rule.

The images use the recommended chunking (16/64/256 KiB, desync) with 64 KiB
packs, so that they have several packs. They contain zero chunks, repeated
chunks and a size that is not a multiple of 512.

Case | Stage | Error | Description
--- | --- | --- | ---
`v1` | valid | — | An image without references
`v2` | valid | — | The next release: reuses the packs of v1 and adds new ones
`sha512-256` | valid | — | The image of v1 with SHA-512/256 chunk IDs
`not-a-disk-image` | load | `not-a-disk-image` | Wrong artifactType
`no-blob-store` | load | `no-blob-store` | A sidecar without the com.exordos.disk.blobs annotation
`layers-swapped` | load | `bad-layers` | Pack map before the index
`unreferenced-pack` | load | `bad-layers` | A pack that no pack map entry refers to
`blob-size` | load | `blob-mismatch` | The config descriptor size is off by one
`blob-digest` | load | `blob-mismatch` | Blob does not match the digest
`format-version` | load | `unsupported-version` | formatVersion 2
`config-digest` | load | `bad-config` | Unknown digest
`config-compression` | load | `bad-config` | Unknown compression
`chunking-mismatch` | load | `inconsistent` | Config and index chunk sizes differ
`digest-mismatch` | load | `inconsistent` | Config and index digests differ
`size-mismatch` | load | `inconsistent` | config.size is not the end of the last chunk
`index-tail` | load | `bad-index` | Corrupt table tail marker
`index-truncated` | load | `bad-index` | An index cut in the middle of an entry
`packmap-magic` | load | `bad-packmap` | Wrong pack map magic
`packmap-count` | load | `bad-packmap` | entry_count does not match the entries
`packmap-uncompressed` | load | `bad-packmap` | A pack map that is not zstd-compressed
`packmap-short` | load | `inconsistent` | A pack map with one entry less than the index
`packmap-layer` | load | `bad-packmap` | An entry refers to layer 1
`packmap-no-layer` | load | `bad-packmap` | An entry refers to a layer past the end
`packmap-range` | load | `bad-packmap` | A range that ends past the end of the pack
`packmap-zero-range` | load | `bad-packmap` | A zero entry with a length
`packmap-repeated` | load | `bad-packmap` | A repeated chunk ID with two different ranges
`false-zero` | load | `chunk-mismatch` | A data chunk marked as a zero chunk
`wrong-range` | read | `chunk-mismatch` | A chunk points to the range of another chunk
`tampered-pack` | read | `chunk-mismatch` | A pack with a changed byte, listed under its new digest
`two-frames` | read | `chunk-mismatch` | A chunk stored as two zstd frames

## Using the vectors

Serve `vectors/` over HTTP with range support (nginx, or the test server in
`exordos_image_format/tests/test_format.py`), or read it from the filesystem.
Then, for each case, open `sidecar` with the reader under test and compare the
outcome with `cases.json`. `exordos_image_format/tests/test_vectors.py` does
this for the reference implementation.

## Regenerating

```bash
python3.14 vectors/generate.py
```

Requires `desync` in `PATH`. The raw images are generated from fixed seeds and
`cases.json` is reproducible. Blob digests depend on the zstd library version,
so regeneration may change the blobs; commit them together with `cases.json`.

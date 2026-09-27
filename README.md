# exordos_image_format

The chunked disk image format of Exordos: node disk images stored as
content-defined chunks in packs, described by an OCI manifest. A new build adds
only its new chunks to a repository, and packs are immutable blobs that any
HTTP cache can hold.

- [SPEC.md](SPEC.md) is the specification.
- [vectors/](vectors/README.md) are conformance vectors: valid images and images
  that break one rule each.
- `exordos_image_format` is the reference implementation, Python 3.14 standard
  library only.

Producers and consumers in Exordos:

- `exordos build` (the `oci` image format) writes images.
- Seed OS flashes them onto node disks.

Both are meant to pass the vectors of this repository.

## Reference implementation

It reads and writes static repositories (a local directory or HTTP). The OCI
registry binding is specified but not implemented yet.

```bash
# Validate an image, optionally with every chunk
python3.14 -m exordos_image_format check [--chunks] <path or URL of <name>.oci.json>

# Write the raw image to a file or a block device
python3.14 -m exordos_image_format extract <sidecar> <target>

# Build an image into a static repository, reusing the packs of references
python3.14 -m exordos_image_format build disk.raw repo/disk/1.0.0/images/disk.oci.json \
    --blobs repo/blobs [--reference <sidecar>]... [--title disk --version 1.0.0]
```

`build` chunks the image with `desync` unless `--index` gives a `.caibx`.

The modules:

- `format`: objects, codecs, and `load_image()`, which checks every rule a
  reader must check before writing, and `decode_chunk()`, which verifies a
  chunk.
- `repository`: static repositories, `extract()` and `verify_chunks()`.
- `writer`: building images and sidecars.

## Development

```bash
tox -e py314        # tests, with desync in PATH for the desync compatibility tests
tox -e ruff         # format and fix
tox -e ruff-check
```

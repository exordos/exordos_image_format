#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""Objects of the chunked disk image format and their validation, see SPEC.md."""

from __future__ import annotations

import collections.abc as abc
import compression.zstd as zstd
import dataclasses
import hashlib
import json
import re
import struct
import typing as tp

FORMAT_VERSION = 1

MEDIA_TYPE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
ARTIFACT_TYPE = "application/vnd.exordos.disk.v1"
MEDIA_TYPE_CONFIG = "application/vnd.exordos.disk.config.v1+json"
MEDIA_TYPE_INDEX = "application/vnd.exordos.disk.index.v1"
MEDIA_TYPE_PACKMAP = "application/vnd.exordos.disk.packmap.v1+zstd"
MEDIA_TYPE_PACK = "application/vnd.exordos.disk.pack.v1"
TITLE_ANNOTATION = "org.opencontainers.image.title"
VERSION_ANNOTATION = "org.opencontainers.image.version"
BLOBS_ANNOTATION = "com.exordos.disk.blobs"

DIGESTS = ("sha256", "sha512-256")
INDEX_LAYER = 0
PACKMAP_LAYER = 1
FIRST_PACK_LAYER = 2
ZERO_LAYER = 0xFFFFFFFF

PACKMAP_MAGIC = b"EXDPMAP1"
PACKMAP_HEADER = struct.Struct("<8sII")
PACKMAP_ENTRY = struct.Struct("<IIQ")

# casync index format, see casync src/caformat.h
CA_FORMAT_INDEX = 0x96824D9C7B129FF9
CA_FORMAT_TABLE = 0xE75B9E112F17417D
CA_FORMAT_TABLE_TAIL_MARKER = 0x4B4F050E5549ECD1
CA_FORMAT_SHA512_256 = 0x2000000000000000
# desync sets CA_FORMAT_EXCLUDE_NODUMP | CA_FORMAT_EXCLUDE_FILE in blob indexes
CA_FORMAT_DESYNC_FLAGS = 0x9000000000000000
CA_TABLE_SIZE_UNKNOWN = 0xFFFFFFFFFFFFFFFF
INDEX_HEADER = struct.Struct("<QQQQQQ")
TABLE_HEADER = struct.Struct("<QQ")
TABLE_ITEM = struct.Struct("<Q32s")
TABLE_TAIL = struct.Struct("<QQQQQ")

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


class FormatError(Exception):
    """A malformed or inconsistent image.

    ``code`` names the rule that failed, the conformance vectors refer to it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclasses.dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    id: bytes

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclasses.dataclass(frozen=True)
class Index:
    digest: str
    chunking: tuple[int, int, int]
    chunks: tuple[Chunk, ...]

    @property
    def size(self) -> int:
        return self.chunks[-1].end if self.chunks else 0


@dataclasses.dataclass(frozen=True)
class Config:
    size: int
    chunking: tuple[int, int, int]
    digest: str
    compression: str = "zstd"
    format_version: int = FORMAT_VERSION


class Entry(tp.NamedTuple):
    layer: int
    length: int
    offset: int

    @property
    def is_zero(self) -> bool:
        return self.layer == ZERO_LAYER


def chunk_id(digest: str, data: bytes) -> bytes:
    return hashlib.new(digest.replace("-", "_"), data).digest()


def blob_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def descriptor(media_type: str, data: bytes) -> dict[str, tp.Any]:
    return {"mediaType": media_type, "digest": blob_digest(data), "size": len(data)}


def verify_blob(desc: dict, data: bytes) -> bytes:
    if len(data) != desc["size"] or blob_digest(data) != desc["digest"]:
        raise FormatError("blob-mismatch", f"blob {desc['digest']} does not match")
    return data


def decode_index(data: bytes) -> Index:
    """Decode a casync blob index (.caibx)."""
    items_start = INDEX_HEADER.size + TABLE_HEADER.size
    items_len = len(data) - items_start - TABLE_TAIL.size
    if items_len < 0 or items_len % TABLE_ITEM.size:
        raise FormatError("bad-index", "truncated index")
    size, typ, flags, cmin, cavg, cmax = INDEX_HEADER.unpack_from(data, 0)
    if size != INDEX_HEADER.size or typ != CA_FORMAT_INDEX:
        raise FormatError("bad-index", "no index header")
    size, typ = TABLE_HEADER.unpack_from(data, INDEX_HEADER.size)
    if size != CA_TABLE_SIZE_UNKNOWN or typ != CA_FORMAT_TABLE:
        raise FormatError("bad-index", "no table header")
    zero1, zero2, offset, size, marker = TABLE_TAIL.unpack_from(
        data, len(data) - TABLE_TAIL.size
    )
    if (
        zero1
        or zero2
        or offset != INDEX_HEADER.size
        or size != len(data) - INDEX_HEADER.size
        or marker != CA_FORMAT_TABLE_TAIL_MARKER
    ):
        raise FormatError("bad-index", "no table tail")
    if not 0 < cmin <= cavg <= cmax:
        raise FormatError("bad-index", "invalid chunk sizes")

    chunks = []
    start = 0
    for end, cid in TABLE_ITEM.iter_unpack(data[items_start : items_start + items_len]):
        if not start < end <= start + cmax:
            raise FormatError("bad-index", f"invalid chunk end {end}")
        chunks.append(Chunk(start, end, cid))
        start = end

    digest = "sha512-256" if flags & CA_FORMAT_SHA512_256 else "sha256"
    return Index(digest, (cmin, cavg, cmax), tuple(chunks))


def encode_index(index: Index) -> bytes:
    """Encode a blob index the way desync writes it."""
    flags = CA_FORMAT_DESYNC_FLAGS
    if index.digest == "sha512-256":
        flags |= CA_FORMAT_SHA512_256
    out = bytearray(
        INDEX_HEADER.pack(INDEX_HEADER.size, CA_FORMAT_INDEX, flags, *index.chunking)
    )
    out += TABLE_HEADER.pack(CA_TABLE_SIZE_UNKNOWN, CA_FORMAT_TABLE)
    for chunk in index.chunks:
        out += TABLE_ITEM.pack(chunk.end, chunk.id)
    table_size = len(out) - INDEX_HEADER.size + TABLE_TAIL.size
    out += TABLE_TAIL.pack(
        0, 0, INDEX_HEADER.size, table_size, CA_FORMAT_TABLE_TAIL_MARKER
    )
    return bytes(out)


def decode_config(data: bytes) -> Config:
    try:
        raw = json.loads(data)
        version = raw["formatVersion"]
        if type(version) is not int or version != FORMAT_VERSION:
            raise FormatError("unsupported-version", f"format version {version}")
        chunking = raw["chunking"]
        config = Config(
            size=raw["size"],
            chunking=(chunking["min"], chunking["avg"], chunking["max"]),
            digest=raw["digest"],
            compression=raw["compression"],
        )
    except (ValueError, KeyError, TypeError) as e:
        raise FormatError("bad-config", f"invalid config: {e!r}") from None
    if not all(type(v) is int and v >= 0 for v in (config.size, *config.chunking)):
        raise FormatError("bad-config", "sizes must be non-negative integers")
    if config.digest not in DIGESTS:
        raise FormatError("bad-config", f"unknown digest {config.digest!r}")
    if config.compression != "zstd":
        raise FormatError("bad-config", f"unknown compression {config.compression!r}")
    return config


def encode_config(config: Config) -> bytes:
    return json.dumps(
        {
            "formatVersion": config.format_version,
            "size": config.size,
            "chunking": dict(zip(("min", "avg", "max"), config.chunking)),
            "digest": config.digest,
            "compression": config.compression,
        },
        sort_keys=True,
    ).encode()


def decode_packmap(data: bytes) -> list[Entry]:
    try:
        raw = zstd.decompress(data)
    except zstd.ZstdError as e:
        raise FormatError("bad-packmap", f"not zstd: {e}") from None
    if len(raw) < PACKMAP_HEADER.size or (len(raw) - PACKMAP_HEADER.size) % 16:
        raise FormatError("bad-packmap", "truncated pack map")
    magic, count, reserved = PACKMAP_HEADER.unpack_from(raw, 0)
    if magic != PACKMAP_MAGIC or reserved:
        raise FormatError("bad-packmap", "no pack map header")
    entries = [Entry(*e) for e in PACKMAP_ENTRY.iter_unpack(raw[PACKMAP_HEADER.size :])]
    if len(entries) != count:
        raise FormatError("bad-packmap", "entry count does not match the entries")
    return entries


def encode_packmap(entries: abc.Sequence[Entry], level: int = 19) -> bytes:
    raw = bytearray(PACKMAP_HEADER.pack(PACKMAP_MAGIC, len(entries), 0))
    for entry in entries:
        raw += PACKMAP_ENTRY.pack(*entry)
    return zstd.compress(bytes(raw), level)


def decode_chunk(chunk: Chunk, digest: str, data: bytes) -> bytes:
    """Decompress one chunk taken from a pack and verify it against its ID."""
    decompressor = zstd.ZstdDecompressor()
    try:
        # One byte more than expected to catch frames that are too long
        out = decompressor.decompress(data, max_length=chunk.size + 1)
    except zstd.ZstdError as e:
        raise FormatError("chunk-mismatch", f"chunk at {chunk.start}: {e}") from None
    if (
        not decompressor.eof
        or decompressor.unused_data
        or len(out) != chunk.size
        or chunk_id(digest, out) != chunk.id
    ):
        raise FormatError("chunk-mismatch", f"chunk at {chunk.start} does not verify")
    return out


def _check_descriptor(desc: tp.Any, media_type: str, what: str) -> None:
    if (
        not isinstance(desc, dict)
        or desc.get("mediaType") != media_type
        or not isinstance(desc.get("digest"), str)
        or not _DIGEST_RE.fullmatch(desc["digest"])
        or type(desc.get("size")) is not int
        or desc["size"] < 0
    ):
        raise FormatError("bad-layers", f"{what} is not a {media_type} descriptor")


def decode_manifest(data: bytes) -> dict:
    try:
        manifest = json.loads(data)
    except ValueError as e:
        raise FormatError("not-a-disk-image", f"manifest is not JSON: {e}") from None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != MEDIA_TYPE_MANIFEST
        or manifest.get("artifactType") != ARTIFACT_TYPE
    ):
        raise FormatError("not-a-disk-image", "not a disk image manifest")
    _check_descriptor(manifest.get("config"), MEDIA_TYPE_CONFIG, "config")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or len(layers) < FIRST_PACK_LAYER:
        raise FormatError("bad-layers", "the index and the pack map are required")
    _check_descriptor(layers[INDEX_LAYER], MEDIA_TYPE_INDEX, "layers[0]")
    _check_descriptor(layers[PACKMAP_LAYER], MEDIA_TYPE_PACKMAP, "layers[1]")
    for i in range(FIRST_PACK_LAYER, len(layers)):
        _check_descriptor(layers[i], MEDIA_TYPE_PACK, f"layers[{i}]")
    packs = [layer["digest"] for layer in layers[FIRST_PACK_LAYER:]]
    if len(set(packs)) != len(packs):
        raise FormatError("bad-layers", "a pack is listed twice")
    annotations = manifest.get("annotations", {})
    if not isinstance(annotations, dict) or not all(
        isinstance(v, str) for v in annotations.values()
    ):
        raise FormatError("not-a-disk-image", "annotations must be strings")
    return manifest


@dataclasses.dataclass(frozen=True)
class Image:
    """A validated image: every chunk ID is reachable from the manifest."""

    manifest: dict
    config: Config
    index: Index
    entries: tuple[Entry, ...]

    @property
    def layers(self) -> list[dict]:
        return self.manifest["layers"]

    @property
    def annotations(self) -> dict[str, str]:
        return self.manifest.get("annotations", {})

    def pack(self, entry: Entry) -> dict:
        return self.layers[entry.layer]


def load_image(manifest_data: bytes, get_blob: abc.Callable[[str], bytes]) -> Image:
    """Validate a manifest and the blobs it needs before any chunk is read.

    ``get_blob`` returns the content of a blob by digest. Every rule a reader
    must check before writing to the target is checked here, chunk data is
    checked by decode_chunk().
    """
    manifest = decode_manifest(manifest_data)
    layers = manifest["layers"]

    def blob(desc: dict) -> bytes:
        return verify_blob(desc, get_blob(desc["digest"]))

    config = decode_config(blob(manifest["config"]))
    index = decode_index(blob(layers[INDEX_LAYER]))
    entries = decode_packmap(blob(layers[PACKMAP_LAYER]))

    if index.digest != config.digest or index.chunking != config.chunking:
        raise FormatError("inconsistent", "config and index disagree on chunking")
    if index.size != config.size:
        raise FormatError("inconsistent", "index does not cover config.size bytes")
    if len(entries) != len(index.chunks):
        raise FormatError("inconsistent", "pack map and index lengths differ")

    zero_ids: dict[int, bytes] = {}
    ranges: dict[bytes, Entry] = {}
    used = set()
    for chunk, entry in zip(index.chunks, entries):
        if entry.is_zero:
            if entry.length or entry.offset:
                raise FormatError("bad-packmap", "zero entry with a range")
            if chunk.size not in zero_ids:
                zero_ids[chunk.size] = chunk_id(index.digest, bytes(chunk.size))
            if chunk.id != zero_ids[chunk.size]:
                raise FormatError(
                    "chunk-mismatch", f"chunk at {chunk.start} is not zeros"
                )
            continue
        if not FIRST_PACK_LAYER <= entry.layer < len(layers):
            raise FormatError("bad-packmap", f"no pack at layer {entry.layer}")
        if (
            not entry.length
            or entry.offset + entry.length > layers[entry.layer]["size"]
        ):
            raise FormatError("bad-packmap", f"range out of pack at {chunk.start}")
        if ranges.setdefault(chunk.id, entry) != entry:
            raise FormatError("bad-packmap", "a repeated chunk has two ranges")
        used.add(entry.layer)
    if len(used) != len(layers) - FIRST_PACK_LAYER:
        raise FormatError("bad-layers", "a listed pack is not referenced")

    return Image(manifest, config, index, tuple(entries))


def build_manifest(
    config: bytes,
    index: bytes,
    packmap: bytes,
    packs: abc.Sequence[dict],
    annotations: dict[str, str],
) -> dict:
    return {
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPE_MANIFEST,
        "artifactType": ARTIFACT_TYPE,
        "config": descriptor(MEDIA_TYPE_CONFIG, config),
        "layers": [
            descriptor(MEDIA_TYPE_INDEX, index),
            descriptor(MEDIA_TYPE_PACKMAP, packmap),
            *packs,
        ],
        "annotations": annotations,
    }

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
"""Building images into a static repository."""

from __future__ import annotations

import collections.abc as abc
import compression.zstd as zstd
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

from exordos_image_format import format as fmt
from exordos_image_format import repository

CHUNKING = (16 << 10, 64 << 10, 256 << 10)
DIGEST = "sha256"
CHUNK_LEVEL = 9
PACK_SIZE = 32 << 20


def desync_index(
    raw: pathlib.Path, chunking: tuple[int, int, int] = CHUNKING, digest: str = DIGEST
) -> bytes:
    """Chunk a raw image with desync and return its index."""
    desync = shutil.which("desync")
    if not desync:
        raise RuntimeError(
            "desync is required, see https://github.com/folbricht/desync"
        )
    with tempfile.TemporaryDirectory() as tmp:
        index = pathlib.Path(tmp) / "index.caibx"
        subprocess.run(
            [
                desync,
                "make",
                "-m",
                ":".join(str(s >> 10) for s in chunking),
                "--digest",
                digest,
                str(index),
                str(raw),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return index.read_bytes()


class BlobDir:
    """A ``blobs/`` directory laid out as ``sha256/<hex>``."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def put(self, media_type: str, data: bytes) -> dict:
        desc = fmt.descriptor(media_type, data)
        dst = self.path / repository.blob_relpath(desc["digest"])
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Never expose a partial blob to parallel builds and readers
            with tempfile.NamedTemporaryFile(dir=dst.parent, delete=False) as f:
                f.write(data)
            os.replace(f.name, dst)
        return desc


def build(
    raw: pathlib.Path,
    index_data: bytes,
    blobs: BlobDir,
    references: abc.Iterable[fmt.Image] = (),
    annotations: dict[str, str] | None = None,
    pack_size: int = PACK_SIZE,
    level: int = CHUNK_LEVEL,
) -> dict:
    """Build an image from a raw file and its index.

    Chunks already stored in the packs of ``references`` are referenced in
    place, the rest go to new packs in ``blobs``. Returns the manifest.
    """
    index = fmt.decode_index(index_data)
    known: dict[bytes, tuple[dict, int, int]] = {}
    for ref in references:
        # Other chunking gives other chunk IDs, nothing would match
        if ref.index.digest != index.digest or ref.index.chunking != index.chunking:
            continue
        for chunk, entry in zip(ref.index.chunks, ref.entries):
            if not entry.is_zero:
                known.setdefault(
                    chunk.id, (ref.pack(entry), entry.offset, entry.length)
                )

    placed: dict[bytes, tuple[dict | None, int, int]] = dict(known)
    zero_ids: dict[int, bytes] = {}
    pack = bytearray()
    pending: list[bytes] = []

    def flush() -> None:
        desc = blobs.put(fmt.MEDIA_TYPE_PACK, bytes(pack))
        for cid in pending:
            placed[cid] = (desc, *placed[cid][1:])
        pack.clear()
        pending.clear()

    # New chunks go to new packs in the order they first appear
    with open(raw, "rb") as f:
        for chunk in index.chunks:
            if chunk.size not in zero_ids:
                zero_ids[chunk.size] = fmt.chunk_id(index.digest, bytes(chunk.size))
            if chunk.id == zero_ids[chunk.size] or chunk.id in placed:
                continue
            data = os.pread(f.fileno(), chunk.size, chunk.start)
            if fmt.chunk_id(index.digest, data) != chunk.id:
                raise fmt.FormatError("chunk-mismatch", f"raw chunk at {chunk.start}")
            compressed = zstd.compress(data, level)
            placed[chunk.id] = (None, len(pack), len(compressed))
            pack.extend(compressed)
            pending.append(chunk.id)
            if len(pack) >= pack_size:
                flush()
    if pack:
        flush()

    layers: dict[str, dict] = {}
    layer_of: dict[str, int] = {}
    entries = []
    for chunk in index.chunks:
        if chunk.id == zero_ids[chunk.size]:
            entries.append(fmt.Entry(fmt.ZERO_LAYER, 0, 0))
            continue
        desc, offset, length = placed[chunk.id]
        assert desc is not None
        if desc["digest"] not in layers:
            layers[desc["digest"]] = desc
            layer_of[desc["digest"]] = fmt.FIRST_PACK_LAYER + len(layer_of)
        entries.append(fmt.Entry(layer_of[desc["digest"]], length, offset))

    config = fmt.encode_config(fmt.Config(index.size, index.chunking, index.digest))
    packmap = fmt.encode_packmap(entries)
    blobs.put(fmt.MEDIA_TYPE_CONFIG, config)
    blobs.put(fmt.MEDIA_TYPE_INDEX, index_data)
    blobs.put(fmt.MEDIA_TYPE_PACKMAP, packmap)
    return fmt.build_manifest(
        config, index_data, packmap, list(layers.values()), dict(annotations or {})
    )


def write_sidecar(manifest: dict, sidecar: pathlib.Path, blobs: BlobDir) -> None:
    """Point the manifest to its blob directory and write it as a sidecar."""
    rel = os.path.relpath(blobs.path, sidecar.parent).replace(os.sep, "/")
    manifest = dict(manifest)
    manifest["annotations"] = {
        **manifest.get("annotations", {}),
        fmt.BLOBS_ANNOTATION: rel.rstrip("/") + "/",
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(manifest, indent=2) + "\n")

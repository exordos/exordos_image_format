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
"""Generate the conformance vectors, see vectors/README.md.

Usage: python3.14 vectors/generate.py [OUTPUT_DIR]. Requires desync in PATH.
"""

from __future__ import annotations

import compression.zstd as zstd
import copy
import hashlib
import json
import pathlib
import random
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from exordos_image_format import format as fmt  # noqa: E402
from exordos_image_format import repository  # noqa: E402
from exordos_image_format import writer  # noqa: E402

VECTORS = pathlib.Path(__file__).resolve().parent
# Small packs, so that images have several of them
PACK_SIZE = 64 << 10
SIDECAR = "{case}/1.0.0/images/disk.oci.json"


def text(seed: int, size: int) -> bytes:
    """Compressible, reproducible data."""
    rng = random.Random(seed)
    words = [
        "".join(
            rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(2, 9))
        )
        for _ in range(512)
    ]
    out = bytearray()
    while len(out) < size:
        out += rng.choice(words).encode() + rng.choice((b" ", b" ", b"\n"))
    return bytes(out[:size])


def image_v1() -> bytes:
    a = text(1, 384 << 10)
    b = text(2, 256 << 10)
    # A repeated block gives repeated chunk IDs, zeros give zero chunks, and
    # the size is not a multiple of the sector size
    return a + bytes(768 << 10) + a + b + bytes(100_000)


def image_v2() -> bytes:
    v1 = image_v1()
    cut = len(v1) - 100_000 - (156 << 10)
    return v1[:cut] + text(3, 20 << 10) + v1[cut:] + text(4, 64 << 10)


class Generator:
    def __init__(self, out: pathlib.Path) -> None:
        self.out = out
        self.repo = out / "repo"
        self.blobs = writer.BlobDir(self.repo / "blobs")
        self.cases: list[dict] = []

    def sidecar(self, case: str) -> pathlib.Path:
        return self.repo / SIDECAR.format(case=case)

    def build(
        self, case: str, raw: bytes, digest: str, references: list[fmt.Image], note: str
    ) -> fmt.Image:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "disk.raw"
            path.write_bytes(raw)
            index = writer.desync_index(path, digest=digest)
            manifest = writer.build(
                path,
                index,
                self.blobs,
                references,
                {fmt.TITLE_ANNOTATION: "disk", fmt.VERSION_ANNOTATION: case},
                pack_size=PACK_SIZE,
            )
        writer.write_sidecar(manifest, self.sidecar(case), self.blobs)
        image, _ = repository.open_image(str(self.sidecar(case)))
        self.cases.append(
            {
                "name": case,
                "description": note,
                "sidecar": f"repo/{SIDECAR.format(case=case)}",
                "valid": True,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "chunks": len(image.entries),
                "zeroChunks": sum(1 for e in image.entries if e.is_zero),
                "packs": len(image.layers) - fmt.FIRST_PACK_LAYER,
            }
        )
        return image

    def invalid(
        self, case: str, error: str, stage: str, note: str, manifest: dict
    ) -> None:
        manifest = copy.deepcopy(manifest)
        manifest.get("annotations", {}).pop(fmt.BLOBS_ANNOTATION, None)
        path = self.sidecar(case)
        if error == "no-blob-store":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(manifest, indent=2) + "\n")
        else:
            writer.write_sidecar(manifest, path, self.blobs)
        self.cases.append(
            {
                "name": case,
                "description": note,
                "sidecar": f"repo/{SIDECAR.format(case=case)}",
                "valid": False,
                "stage": stage,
                "error": error,
            }
        )

    def put(self, desc: dict, data: bytes) -> dict:
        return self.blobs.put(desc["mediaType"], data)


def generate(out: pathlib.Path) -> None:
    shutil.rmtree(out / "repo", ignore_errors=True)
    g = Generator(out)

    v1 = g.build("v1", image_v1(), "sha256", [], "An image without references")
    v2 = g.build(
        "v2",
        image_v2(),
        "sha256",
        [v1],
        "The next release: reuses the packs of v1 and adds new ones",
    )
    g.build(
        "sha512-256",
        image_v1(),
        "sha512-256",
        [],
        "The image of v1 with SHA-512/256 chunk IDs",
    )

    base = v1.manifest
    config = json.loads(fmt.encode_config(v1.config))
    index = fmt.encode_index(v1.index)
    entries = list(v1.entries)

    def with_config(**changes: object) -> dict:
        m = copy.deepcopy(base)
        m["config"] = g.put(m["config"], json.dumps({**config, **changes}).encode())
        return m

    def with_index(data: bytes) -> dict:
        m = copy.deepcopy(base)
        m["layers"][fmt.INDEX_LAYER] = g.put(m["layers"][fmt.INDEX_LAYER], data)
        return m

    def with_packmap(data: bytes) -> dict:
        m = copy.deepcopy(base)
        m["layers"][fmt.PACKMAP_LAYER] = g.put(m["layers"][fmt.PACKMAP_LAYER], data)
        return m

    def with_entries(changed: list[fmt.Entry]) -> dict:
        return with_packmap(fmt.encode_packmap(changed))

    data_chunks = [i for i, e in enumerate(entries) if not e.is_zero]
    first = data_chunks[0]
    seen: dict[bytes, int] = {}
    repeated = next(
        i for i in data_chunks if seen.setdefault(v1.index.chunks[i].id, i) != i
    )
    other = next(
        i for i in data_chunks if entries[i] != entries[repeated] and i != first
    )

    m = copy.deepcopy(base)
    m["artifactType"] = "application/vnd.oci.image.config.v1+json"
    g.invalid("not-a-disk-image", "not-a-disk-image", "load", "Wrong artifactType", m)
    g.invalid(
        "no-blob-store",
        "no-blob-store",
        "load",
        f"A sidecar without the {fmt.BLOBS_ANNOTATION} annotation",
        base,
    )

    m = copy.deepcopy(base)
    m["layers"][0], m["layers"][1] = m["layers"][1], m["layers"][0]
    g.invalid("layers-swapped", "bad-layers", "load", "Pack map before the index", m)
    m = copy.deepcopy(base)
    m["layers"].append(v2.layers[-1])
    g.invalid(
        "unreferenced-pack",
        "bad-layers",
        "load",
        "A pack that no pack map entry refers to",
        m,
    )

    m = copy.deepcopy(base)
    m["config"]["size"] += 1
    g.invalid(
        "blob-size",
        "blob-mismatch",
        "load",
        "The config descriptor size is off by one",
        m,
    )
    m = copy.deepcopy(base)
    m["layers"][fmt.INDEX_LAYER]["digest"] = fmt.blob_digest(b"")
    g.invalid(
        "blob-digest", "blob-mismatch", "load", "Blob does not match the digest", m
    )
    # The file is named by the digest of other content
    g.repo.joinpath("blobs", repository.blob_relpath(fmt.blob_digest(b""))).write_bytes(
        index
    )

    g.invalid(
        "format-version",
        "unsupported-version",
        "load",
        "formatVersion 2",
        with_config(formatVersion=2),
    )
    g.invalid(
        "config-digest",
        "bad-config",
        "load",
        "Unknown digest",
        with_config(digest="md5"),
    )
    g.invalid(
        "config-compression",
        "bad-config",
        "load",
        "Unknown compression",
        with_config(compression="xz"),
    )
    g.invalid(
        "chunking-mismatch",
        "inconsistent",
        "load",
        "Config and index chunk sizes differ",
        with_config(chunking={**config["chunking"], "avg": 32 << 10}),
    )
    g.invalid(
        "digest-mismatch",
        "inconsistent",
        "load",
        "Config and index digests differ",
        with_config(digest="sha512-256"),
    )
    g.invalid(
        "size-mismatch",
        "inconsistent",
        "load",
        "config.size is not the end of the last chunk",
        with_config(size=config["size"] + 1),
    )

    g.invalid(
        "index-tail",
        "bad-index",
        "load",
        "Corrupt table tail marker",
        with_index(index[:-1] + bytes([index[-1] ^ 0xFF])),
    )
    g.invalid(
        "index-truncated",
        "bad-index",
        "load",
        "An index cut in the middle of an entry",
        with_index(index[:-60]),
    )

    raw_packmap = fmt.PACKMAP_HEADER.pack(
        fmt.PACKMAP_MAGIC, len(entries), 0
    ) + b"".join(fmt.PACKMAP_ENTRY.pack(*e) for e in entries)
    g.invalid(
        "packmap-magic",
        "bad-packmap",
        "load",
        "Wrong pack map magic",
        with_packmap(zstd.compress(b"EXDPMAP2" + raw_packmap[8:])),
    )
    g.invalid(
        "packmap-count",
        "bad-packmap",
        "load",
        "entry_count does not match the entries",
        with_packmap(
            zstd.compress(
                fmt.PACKMAP_HEADER.pack(fmt.PACKMAP_MAGIC, len(entries) + 1, 0)
                + raw_packmap[fmt.PACKMAP_HEADER.size :]
            )
        ),
    )
    g.invalid(
        "packmap-uncompressed",
        "bad-packmap",
        "load",
        "A pack map that is not zstd-compressed",
        with_packmap(raw_packmap),
    )
    g.invalid(
        "packmap-short",
        "inconsistent",
        "load",
        "A pack map with one entry less than the index",
        with_entries(entries[:-1]),
    )
    changed = list(entries)
    changed[first] = entries[first]._replace(layer=fmt.PACKMAP_LAYER)
    g.invalid(
        "packmap-layer",
        "bad-packmap",
        "load",
        "An entry refers to layer 1",
        with_entries(changed),
    )
    changed = list(entries)
    changed[first] = entries[first]._replace(layer=len(base["layers"]))
    g.invalid(
        "packmap-no-layer",
        "bad-packmap",
        "load",
        "An entry refers to a layer past the end",
        with_entries(changed),
    )
    changed = list(entries)
    pack_size = base["layers"][entries[first].layer]["size"]
    changed[first] = entries[first]._replace(
        offset=pack_size - entries[first].length + 1
    )
    g.invalid(
        "packmap-range",
        "bad-packmap",
        "load",
        "A range that ends past the end of the pack",
        with_entries(changed),
    )
    changed = list(entries)
    zero = next(i for i, e in enumerate(entries) if e.is_zero)
    changed[zero] = entries[zero]._replace(length=1)
    g.invalid(
        "packmap-zero-range",
        "bad-packmap",
        "load",
        "A zero entry with a length",
        with_entries(changed),
    )
    changed = list(entries)
    changed[repeated] = entries[other]
    g.invalid(
        "packmap-repeated",
        "bad-packmap",
        "load",
        "A repeated chunk ID with two different ranges",
        with_entries(changed),
    )
    changed = list(entries)
    changed[first] = fmt.Entry(fmt.ZERO_LAYER, 0, 0)
    g.invalid(
        "false-zero",
        "chunk-mismatch",
        "load",
        "A data chunk marked as a zero chunk",
        with_entries(changed),
    )

    changed = list(entries)
    changed[first] = entries[other]
    g.invalid(
        "wrong-range",
        "chunk-mismatch",
        "read",
        "A chunk points to the range of another chunk",
        with_entries(changed),
    )
    m = copy.deepcopy(base)
    layer = entries[first].layer
    pack = bytearray(
        g.repo.joinpath(
            "blobs", repository.blob_relpath(m["layers"][layer]["digest"])
        ).read_bytes()
    )
    pack[entries[first].offset + entries[first].length // 2] ^= 0xFF
    m["layers"][layer] = g.put(m["layers"][layer], bytes(pack))
    g.invalid(
        "tampered-pack",
        "chunk-mismatch",
        "read",
        "A pack with a changed byte, listed under its new digest",
        m,
    )
    # The chunk decompresses to the right data, but as two frames
    chunk = v1.index.chunks[first]
    with open(
        g.repo / "blobs" / repository.blob_relpath(base["layers"][layer]["digest"]),
        "rb",
    ) as f:
        f.seek(entries[first].offset)
        data = zstd.decompress(f.read(entries[first].length))
    assert fmt.chunk_id(v1.index.digest, data) == chunk.id
    half = len(data) // 2
    frames = zstd.compress(data[:half]) + zstd.compress(data[half:])
    m = copy.deepcopy(base)
    m["layers"].append(g.blobs.put(fmt.MEDIA_TYPE_PACK, frames))
    changed = list(entries)
    for i, c in enumerate(v1.index.chunks):
        if c.id == chunk.id:
            changed[i] = fmt.Entry(len(m["layers"]) - 1, len(frames), 0)
    m["layers"][fmt.PACKMAP_LAYER] = g.put(
        m["layers"][fmt.PACKMAP_LAYER], fmt.encode_packmap(changed)
    )
    g.invalid(
        "two-frames",
        "chunk-mismatch",
        "read",
        "A chunk stored as two zstd frames",
        m,
    )

    (out / "cases.json").write_text(
        json.dumps({"formatVersion": fmt.FORMAT_VERSION, "cases": g.cases}, indent=2)
        + "\n"
    )


if __name__ == "__main__":
    generate(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else VECTORS)

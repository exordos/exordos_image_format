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

import compression.zstd as zstd
import functools
import http.server
import json
import os
import pathlib
import threading

import pytest

from exordos_image_format import format as fmt
from exordos_image_format import repository
from exordos_image_format import writer

VECTORS = pathlib.Path(__file__).resolve().parents[2] / "vectors"


def _image(name: str) -> tuple[fmt.Image, repository.StaticSource]:
    return repository.open_image(
        str(VECTORS / f"repo/{name}/1.0.0/images/disk.oci.json")
    )


@pytest.mark.parametrize("name", ["v1", "sha512-256"])
def test_encode_index_desync_bytes(name: str) -> None:
    image, source = _image(name)
    data = source.blob(image.layers[fmt.INDEX_LAYER]["digest"])
    assert fmt.encode_index(fmt.decode_index(data)) == data


def test_decode_packmap_roundtrip() -> None:
    entries = [
        fmt.Entry(2, 10, 0),
        fmt.Entry(fmt.ZERO_LAYER, 0, 0),
        fmt.Entry(3, 5, 1 << 40),
    ]
    assert fmt.decode_packmap(fmt.encode_packmap(entries)) == entries


def test_decode_packmap_reserved() -> None:
    raw = fmt.PACKMAP_HEADER.pack(fmt.PACKMAP_MAGIC, 0, 1)
    with pytest.raises(fmt.FormatError, match="bad-packmap"):
        fmt.decode_packmap(zstd.compress(raw))


@pytest.mark.parametrize("version", [True, "1", 1.0])
def test_decode_config_version_type(version: object) -> None:
    config = json.loads(fmt.encode_config(fmt.Config(1, (1, 2, 3), "sha256")))
    config["formatVersion"] = version
    with pytest.raises(fmt.FormatError, match="unsupported-version"):
        fmt.decode_config(json.dumps(config).encode())


def test_decode_chunk_frames() -> None:
    data = b"chunk data" * 100
    chunk = fmt.Chunk(0, len(data), fmt.chunk_id("sha256", data))
    frame = zstd.compress(data)
    assert fmt.decode_chunk(chunk, "sha256", frame) == data

    half = len(data) // 2
    two_frames = zstd.compress(data[:half]) + zstd.compress(data[half:])
    for bad in (frame + b"\0", two_frames, zstd.compress(data + b"x"), frame[:-1]):
        with pytest.raises(fmt.FormatError, match="chunk-mismatch"):
            fmt.decode_chunk(chunk, "sha256", bad)


def test_build_reference_adds_no_packs(tmp_path: pathlib.Path) -> None:
    """Building the same raw image against itself reuses every pack."""
    v1, source = _image("v1")
    raw = tmp_path / "disk.raw"
    repository.extract(v1, source, str(raw))
    index = source.blob(v1.layers[fmt.INDEX_LAYER]["digest"])

    blobs = writer.BlobDir(tmp_path / "blobs")
    manifest = writer.build(raw, index, blobs, [v1])

    assert (
        manifest["layers"][fmt.FIRST_PACK_LAYER :] == v1.layers[fmt.FIRST_PACK_LAYER :]
    )
    assert (
        not (tmp_path / "blobs")
        .joinpath(repository.blob_relpath(v1.layers[fmt.FIRST_PACK_LAYER]["digest"]))
        .exists()
    )


def test_build_and_extract(tmp_path: pathlib.Path) -> None:
    v1, source = _image("v1")
    raw = tmp_path / "disk.raw"
    repository.extract(v1, source, str(raw))
    index = source.blob(v1.layers[fmt.INDEX_LAYER]["digest"])

    blobs = writer.BlobDir(tmp_path / "repo" / "blobs")
    sidecar = tmp_path / "repo" / "disk" / "1" / "images" / "disk.oci.json"
    writer.write_sidecar(writer.build(raw, index, blobs, pack_size=1), sidecar, blobs)

    image, source = repository.open_image(str(sidecar))
    assert image.annotations[fmt.BLOBS_ANNOTATION] == "../../../blobs/"
    # One chunk per pack
    distinct = {
        c.id for c, e in zip(image.index.chunks, image.entries) if not e.is_zero
    }
    assert len(image.layers) - fmt.FIRST_PACK_LAYER == len(distinct)
    out = tmp_path / "out.raw"
    repository.extract(image, source, str(out))
    assert out.read_bytes() == raw.read_bytes()


def test_sidecar_of() -> None:
    assert repository.sidecar_of("http://r/e/1/images/d.raw.zst") == (
        "http://r/e/1/images/d.oci.json"
    )
    assert repository.sidecar_of("http://r/e/1/images/d.qcow2") is None


class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Static files with single range requests, like nginx."""

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            data = f.read()
        header = self.headers.get("Range")
        if header:
            first, last = header.removeprefix("bytes=").split("-")
            data = data[int(first) : int(last) + 1]
            self.send_response(206)
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_extract_over_http(tmp_path: pathlib.Path) -> None:
    handler = functools.partial(_RangeHandler, directory=str(VECTORS))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = (
            f"http://127.0.0.1:{server.server_port}/repo/v2/1.0.0/images/disk.oci.json"
        )
        image, source = repository.open_image(url)
        assert source.blobs == f"http://127.0.0.1:{server.server_port}/repo/blobs/"
        repository.extract(image, source, str(tmp_path / "http.raw"))
    finally:
        server.shutdown()
    local, local_source = _image("v2")
    repository.extract(local, local_source, str(tmp_path / "local.raw"))
    assert (tmp_path / "http.raw").read_bytes() == (tmp_path / "local.raw").read_bytes()

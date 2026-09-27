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
"""Reading images from static repositories and writing them to a target."""

from __future__ import annotations

import json
import os
import stat
import typing as tp
import urllib.parse
import urllib.request

from exordos_image_format import format as fmt

SIDECAR_SUFFIX = ".oci.json"
RAW_ZST_SUFFIX = ".raw.zst"

# Neighbour chunks of a pack are read with one request
RANGE_GAP = 256 << 10
RANGE_MAX = 8 << 20
HTTP_TIMEOUT = 60


def sidecar_of(image: str) -> str | None:
    """The sidecar location of a ``<name>.raw.zst`` image path or URL."""
    if not image.endswith(RAW_ZST_SUFFIX):
        return None
    return image[: -len(RAW_ZST_SUFFIX)] + SIDECAR_SUFFIX


def blob_relpath(digest: str) -> str:
    algorithm, hex_digest = digest.split(":", 1)
    return f"{algorithm}/{hex_digest}"


def _is_url(location: str) -> bool:
    return urllib.parse.urlsplit(location).scheme in ("http", "https")


class StaticSource:
    """A sidecar manifest and the blob directory its annotation points to.

    ``sidecar`` is a local path or an http(s) URL.
    """

    def __init__(self, sidecar: str) -> None:
        self.sidecar = sidecar
        self.blobs: str | None = None

    def _read(self, location: str, first: int = 0, length: int | None = None) -> bytes:
        if not _is_url(location):
            with open(location, "rb") as f:
                f.seek(first)
                return f.read() if length is None else f.read(length)
        request = urllib.request.Request(location)
        if length is not None:
            request.add_header("Range", f"bytes={first}-{first + length - 1}")
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            data = response.read()
            if length is not None and (response.status != 206 or len(data) != length):
                raise OSError(f"{location}: range requests are not supported")
            return data

    def manifest(self) -> bytes:
        data = self._read(self.sidecar)
        try:
            ref = json.loads(data)["annotations"][fmt.BLOBS_ANNOTATION]
        except ValueError, KeyError, TypeError:
            raise fmt.FormatError(
                "no-blob-store", f"{fmt.BLOBS_ANNOTATION} is required"
            ) from None
        if not isinstance(ref, str) or not ref.endswith("/"):
            raise fmt.FormatError("no-blob-store", "blob store must end with /")
        if _is_url(self.sidecar):
            self.blobs = urllib.parse.urljoin(self.sidecar, ref)
        else:
            base = os.path.dirname(os.path.abspath(self.sidecar))
            self.blobs = os.path.normpath(os.path.join(base, ref)) + os.sep
        return data

    def _blob_location(self, digest: str) -> str:
        if self.blobs is None:
            raise RuntimeError("read the manifest first")
        if _is_url(self.blobs):
            return urllib.parse.urljoin(self.blobs, blob_relpath(digest))
        return os.path.join(self.blobs, blob_relpath(digest))

    def blob(self, digest: str) -> bytes:
        return self._read(self._blob_location(digest))

    def blob_range(self, digest: str, first: int, length: int) -> bytes:
        return self._read(self._blob_location(digest), first, length)


def open_image(sidecar: str) -> tuple[fmt.Image, StaticSource]:
    source = StaticSource(sidecar)
    image = fmt.load_image(source.manifest(), source.blob)
    return image, source


def _ranges(
    image: fmt.Image,
) -> list[tuple[int, int, int, list[tuple[fmt.Entry, list[fmt.Chunk]]]]]:
    """Coalesce the chunk ranges to read into (layer, first, length, chunks)."""
    by_range: dict[fmt.Entry, list[fmt.Chunk]] = {}
    for chunk, entry in zip(image.index.chunks, image.entries):
        if not entry.is_zero:
            by_range.setdefault(entry, []).append(chunk)

    out: list[tp.Any] = []
    for entry in sorted(by_range, key=lambda e: (e.layer, e.offset)):
        end = entry.offset + entry.length
        if out:
            layer, first, length, items = out[-1]
            if (
                layer == entry.layer
                and entry.offset - (first + length) <= RANGE_GAP
                and end - first <= RANGE_MAX
            ):
                out[-1] = (layer, first, max(length, end - first), items)
                items.append((entry, by_range[entry]))
                continue
        out.append(
            (entry.layer, entry.offset, entry.length, [(entry, by_range[entry])])
        )
    return out


def extract(image: fmt.Image, source: StaticSource, target: str) -> None:
    """Write the raw image to a file or a block device.

    Zero chunks read back as zeros: a regular file is truncated to the image
    size, a device gets zeros written.
    """
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            os.ftruncate(fd, 0)
            os.ftruncate(fd, image.config.size)
        else:
            for chunk, entry in zip(image.index.chunks, image.entries):
                if entry.is_zero:
                    os.pwrite(fd, bytes(chunk.size), chunk.start)

        for layer, first, length, items in _ranges(image):
            data = source.blob_range(image.layers[layer]["digest"], first, length)
            for entry, chunks in items:
                start = entry.offset - first
                raw = fmt.decode_chunk(
                    chunks[0], image.config.digest, data[start : start + entry.length]
                )
                for chunk in chunks:
                    if chunk.id != chunks[0].id:
                        raise fmt.FormatError(
                            "chunk-mismatch", f"chunk at {chunk.start} does not verify"
                        )
                    os.pwrite(fd, raw, chunk.start)
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_chunks(image: fmt.Image, source: StaticSource) -> None:
    """Read and verify every chunk without writing anything."""
    for layer, first, length, items in _ranges(image):
        data = source.blob_range(image.layers[layer]["digest"], first, length)
        for entry, chunks in items:
            start = entry.offset - first
            for chunk in {c.id: c for c in chunks}.values():
                fmt.decode_chunk(
                    chunk, image.config.digest, data[start : start + entry.length]
                )

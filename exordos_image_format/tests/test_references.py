#    Copyright 2026 Genesis Corporation.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import copy
import json
import pathlib
import subprocess
import sys

import pytest

from exordos_image_format import format as fmt
from exordos_image_format import repository
from exordos_image_format import writer

REPO = "72f39208-1bb8-4d20-b87d-2058ee1e7619"
OTHER_REPO = "f7eedecf-e5ca-49a3-bfb7-c4395082d635"


@pytest.fixture
def make_image(tmp_path):
    blobs = writer.BlobDir(tmp_path / "repo/blobs")
    paths = []

    def make(parts, epoch=1, references=(), repository_id=REPO):
        raw = tmp_path / "input.raw"
        raw.write_bytes(b"".join(parts))
        chunks, end = [], 0
        for data in parts:
            chunks.append(fmt.Chunk(end, end + len(data), fmt.chunk_id("sha256", data)))
            end += len(data)
        index = fmt.encode_index(fmt.Index("sha256", (1, 16, 64), tuple(chunks)))
        manifest = writer.build(
            raw,
            index,
            blobs,
            repository_id=repository_id,
            epoch=epoch,
            references=references,
            pack_size=32,
        )
        path = tmp_path / f"repo/image/{len(paths)}/images/disk.oci.json"
        paths.append(path)
        writer.write_sidecar(manifest, path, blobs)
        image, source = repository.open_image(str(path))
        return image, source, path

    return make


def test_build_records_only_used_source_epochs(make_image, tmp_path):
    parent, _, parent_path = make_image([b"a" * 32, b"b" * 32, bytes(32)], epoch=7)
    unused, _, _ = make_image([b"c" * 32])
    child, source, child_path = make_image(
        [b"a" * 32, b"d" * 32, bytes(32)], references=[parent, unused]
    )
    assert child.layout.references == (parent.reference,)
    assert child.reference["image"] == child.content_descriptor
    assert child.reference["layout"]["digest"] == fmt.blob_digest(
        child_path.read_bytes()
    )
    assert parent.reference["layout"]["digest"] == fmt.blob_digest(
        parent_path.read_bytes()
    )
    assert child.layout.references[0]["epoch"] == 7
    parent_path.unlink()
    parent_blob = pathlib.Path(
        source._blob_location(parent.layout_descriptor["digest"])
    )
    parent_blob.unlink()
    parent_content = pathlib.Path(
        source._blob_location(parent.content_descriptor["digest"])
    )
    parent_content.unlink()
    child, source = repository.open_image(str(child_path))
    out = tmp_path / "restored.raw"
    repository.extract(child, source, str(out))
    assert out.read_bytes() == b"a" * 32 + b"d" * 32 + bytes(32)


def test_content_identity_is_independent_of_epoch_and_sources(make_image):
    first, _, _ = make_image([b"a" * 32, b"b" * 32])
    rebuilt, _, _ = make_image([b"a" * 32, b"b" * 32], epoch=2, references=[first])
    foreign, _, _ = make_image([b"a" * 32, b"b" * 32], repository_id=OTHER_REPO)
    assert (
        first.content_descriptor
        == rebuilt.content_descriptor
        == foreign.content_descriptor
    )
    assert first.layout_descriptor != rebuilt.layout_descriptor
    assert rebuilt.layout.references == (first.reference,)
    assert first.layout.references == ()


def test_sources_pin_loaded_snapshots_and_sort(make_image):
    first, _, first_path = make_image([b"a" * 32, b"b" * 32], epoch=3)
    second, _, _ = make_image([b"b" * 32, b"c" * 32], epoch=9, repository_id=OTHER_REPO)
    newer, _, newer_path = make_image([b"a" * 32, b"b" * 32], epoch=4)
    first_path.write_bytes(newer_path.read_bytes())
    child, _, _ = make_image(
        [b"a" * 32, b"b" * 32, b"c" * 32], references=[second, first]
    )
    assert child.layout.references == (first.reference, second.reference)
    assert child.layout.references[0]["layout"] != newer.layout_descriptor


def test_cli_build_check_and_extract(make_image, tmp_path):
    parent, source, path = make_image([b"a" * 32, b"b" * 32])
    raw = tmp_path / "cli-input.raw"
    raw.write_bytes(b"a" * 32 + b"b" * 32)
    index = pathlib.Path(source.blobs) / repository.blob_relpath(
        parent.layers[0]["digest"]
    )
    sidecar = tmp_path / "repo/cli/1/images/disk.oci.json"
    cli = [sys.executable, "-m", "exordos_image_format"]
    subprocess.run(
        [
            *cli,
            "build",
            str(raw),
            str(sidecar),
            "--index",
            str(index),
            "--blobs",
            source.blobs,
            "--repository-id",
            REPO,
            "--epoch",
            "2",
            "--reference",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    checked = subprocess.run(
        [*cli, "check", "--chunks", str(sidecar)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "epoch 2, 1 references" in checked.stdout
    restored = tmp_path / "cli-restored.raw"
    subprocess.run(
        [*cli, "extract", str(sidecar), str(restored)], check=True, capture_output=True
    )
    assert restored.read_bytes() == raw.read_bytes()


def test_rebuild_rejects_same_epoch_in_same_domain(make_image):
    parent, _, _ = make_image([b"a" * 32])
    with pytest.raises(fmt.FormatError, match="bad-references"):
        make_image([b"a" * 32], references=[parent])


def test_atomic_sidecar_keeps_regular_file_permissions(make_image, tmp_path):
    _, _, path = make_image([b"a" * 32])
    assert (
        path.stat().st_mode & 0o777 == (tmp_path / "input.raw").stat().st_mode & 0o777
    )


def test_source_content_size_matches_shared_output_content(make_image):
    parent, _, _ = make_image([b"a" * 32])
    image, source, _ = make_image([b"a" * 32], epoch=2, references=[parent])
    config = json.loads(source.blob(image.manifest["config"]["digest"]))
    config["references"][0]["image"]["size"] += 1
    data = json.dumps(config).encode()
    manifest = copy.deepcopy(image.manifest)
    manifest["config"] = fmt.descriptor(fmt.MEDIA_TYPE_CONFIG, data)

    def blob(digest):
        return data if digest == manifest["config"]["digest"] else source.blob(digest)

    with pytest.raises(fmt.FormatError, match="bad-references"):
        fmt.load_image(json.dumps(manifest).encode(), blob)


@pytest.mark.parametrize("case", ["reuse", "zero", "repeat", "size"])
def test_build_rejects_raw_that_does_not_match_index(make_image, tmp_path, case):
    parts = [bytes(32)] if case == "zero" else [b"a" * 32]
    if case == "repeat":
        parts *= 2
    parent, source, _ = make_image(parts)
    raw = tmp_path / "mismatched.raw"
    raw.write_bytes(
        b"".join(parts) + b"x" if case == "size" else b"".join(parts[:-1]) + b"x" * 32
    )
    index = source.blob(parent.layers[0]["digest"])
    with pytest.raises(fmt.FormatError, match="chunk-mismatch"):
        writer.build(
            raw,
            index,
            writer.BlobDir(pathlib.Path(source.blobs)),
            repository_id=REPO,
            epoch=2,
            references=[parent] if case == "reuse" else [],
        )


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1", 2**64])
def test_layout_rejects_invalid_epoch(make_image, bad):
    image, source, _ = make_image([b"a" * 32])
    config = json.loads(source.blob(image.manifest["config"]["digest"]))
    config["epoch"] = bad
    with pytest.raises(fmt.FormatError, match="bad-layout"):
        fmt.decode_layout_config(json.dumps(config).encode())


@pytest.mark.parametrize(
    "change", ["duplicate", "conflict", "reverse", "bool-size", "missing"]
)
def test_layout_rejects_invalid_references(make_image, change):
    parent, _, _ = make_image([b"a" * 32], epoch=7)
    child, source, _ = make_image([b"a" * 32, b"b" * 32], references=[parent])
    config = json.loads(source.blob(child.manifest["config"]["digest"]))
    ref = config["references"][0]
    other = copy.deepcopy(ref)
    if change == "duplicate":
        config["references"].append(other)
    elif change == "conflict":
        other["layout"]["digest"] = "sha256:" + "f" * 64
        config["references"].append(other)
    elif change == "reverse":
        other["repositoryId"] = OTHER_REPO
        config["references"].append(other)
    elif change == "bool-size":
        ref["image"]["size"] = True
    else:
        del config["references"]
    with pytest.raises(fmt.FormatError, match="bad-references"):
        fmt.decode_layout_config(json.dumps(config).encode())

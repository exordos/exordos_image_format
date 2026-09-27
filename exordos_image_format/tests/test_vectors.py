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

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

from exordos_image_format import format as fmt
from exordos_image_format import repository

ROOT = pathlib.Path(__file__).resolve().parents[2]
VECTORS = ROOT / "vectors"
CASES = json.loads((VECTORS / "cases.json").read_text())["cases"]


def _case_id(case: dict) -> str:
    return case["name"]


@pytest.mark.parametrize("case", [c for c in CASES if c["valid"]], ids=_case_id)
def test_extract_valid(case: dict, tmp_path: pathlib.Path) -> None:
    image, source = repository.open_image(str(VECTORS / case["sidecar"]))
    target = tmp_path / "disk.raw"
    repository.extract(image, source, str(target))

    data = target.read_bytes()
    assert len(data) == case["size"]
    assert hashlib.sha256(data).hexdigest() == case["sha256"]
    assert len(image.entries) == case["chunks"]
    assert sum(1 for e in image.entries if e.is_zero) == case["zeroChunks"]
    assert len(image.layers) - fmt.FIRST_PACK_LAYER == case["packs"]


@pytest.mark.parametrize("case", [c for c in CASES if not c["valid"]], ids=_case_id)
def test_reject_invalid(case: dict, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "disk.raw"
    with pytest.raises(fmt.FormatError) as e:
        image, source = repository.open_image(str(VECTORS / case["sidecar"]))
        assert case["stage"] == "read", "must be rejected before any write"
        repository.extract(image, source, str(target))
    assert e.value.code == case["error"]


def test_v2_reuses_v1_packs() -> None:
    v1, _ = repository.open_image(str(VECTORS / "repo/v1/1.0.0/images/disk.oci.json"))
    v2, _ = repository.open_image(str(VECTORS / "repo/v2/1.0.0/images/disk.oci.json"))
    v1_packs = {d["digest"] for d in v1.layers[fmt.FIRST_PACK_LAYER :]}
    v2_packs = {d["digest"] for d in v2.layers[fmt.FIRST_PACK_LAYER :]}
    assert v1_packs & v2_packs
    assert v2_packs - v1_packs


@pytest.mark.skipif(not shutil.which("desync"), reason="desync is not installed")
def test_generate_reproduces_cases(tmp_path: pathlib.Path) -> None:
    subprocess.run(
        [sys.executable, str(VECTORS / "generate.py"), str(tmp_path)], check=True
    )
    assert json.loads((tmp_path / "cases.json").read_text())["cases"] == CASES


@pytest.mark.skipif(not shutil.which("desync"), reason="desync is not installed")
@pytest.mark.parametrize("name", ["v1", "v2", "sha512-256"])
def test_desync_extracts_from_pack_chunks(name: str, tmp_path: pathlib.Path) -> None:
    """A pack range is the content of a casync .cacnk chunk file."""
    case = next(c for c in CASES if c["name"] == name)
    image, source = repository.open_image(str(VECTORS / case["sidecar"]))
    store = tmp_path / "store"
    for chunk, entry in zip(image.index.chunks, image.entries):
        if entry.is_zero:
            continue
        path = store / chunk.id.hex()[:4] / f"{chunk.id.hex()}.cacnk"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            source.blob_range(image.pack(entry)["digest"], entry.offset, entry.length)
        )
    # desync does not store zero chunks either, it synthesizes them
    index = tmp_path / "disk.caibx"
    index.write_bytes(source.blob(image.layers[fmt.INDEX_LAYER]["digest"]))
    target = tmp_path / "disk.raw"
    subprocess.run(
        [
            "desync",
            "--digest",
            image.config.digest,
            "extract",
            "-s",
            str(store),
            str(index),
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    assert hashlib.sha256(target.read_bytes()).hexdigest() == case["sha256"]

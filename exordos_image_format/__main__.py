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
"""Check, extract and build chunked disk images in static repositories."""

from __future__ import annotations

import argparse
import pathlib
import sys

from exordos_image_format import format as fmt
from exordos_image_format import repository
from exordos_image_format import writer


def _check(args: argparse.Namespace) -> None:
    image, source = repository.open_image(args.sidecar)
    if args.chunks:
        repository.verify_chunks(image, source)
    zero = sum(1 for e in image.entries if e.is_zero)
    print(
        f"{args.sidecar}: {image.config.size} bytes, {len(image.entries)} chunks "
        f"({zero} zero), {len(image.layers) - fmt.FIRST_PACK_LAYER} packs, "
        f"{image.config.digest} {':'.join(map(str, image.config.chunking))}, "
        f"epoch {image.layout.epoch}, {len(image.layout.references)} references"
    )


def _extract(args: argparse.Namespace) -> None:
    image, source = repository.open_image(args.sidecar)
    repository.extract(image, source, args.target)


def _build(args: argparse.Namespace) -> None:
    index = args.index.read_bytes() if args.index else writer.desync_index(args.raw)
    references = [repository.open_image(ref)[0] for ref in args.reference]
    annotations = {}
    if args.title:
        annotations[fmt.TITLE_ANNOTATION] = args.title
    if args.version:
        annotations[fmt.VERSION_ANNOTATION] = args.version
    blobs = writer.BlobDir(args.blobs)
    manifest = writer.build(
        args.raw,
        index,
        blobs,
        repository_id=args.repository_id,
        epoch=args.epoch,
        references=references,
        annotations=annotations,
    )
    writer.write_sidecar(manifest, args.sidecar, blobs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m exordos_image_format")
    sub = parser.add_subparsers(required=True)

    check = sub.add_parser("check", help="validate an image")
    check.add_argument("sidecar", help="path or URL of <name>.oci.json")
    check.add_argument("--chunks", action="store_true", help="verify every chunk")
    check.set_defaults(func=_check)

    extract = sub.add_parser("extract", help="write the raw image")
    extract.add_argument("sidecar", help="path or URL of <name>.oci.json")
    extract.add_argument("target", help="file or block device")
    extract.set_defaults(func=_extract)

    build = sub.add_parser("build", help="build an image into a static repository")
    build.add_argument("raw", type=pathlib.Path)
    build.add_argument("sidecar", type=pathlib.Path)
    build.add_argument("--blobs", type=pathlib.Path, required=True)
    build.add_argument(
        "--repository-id", required=True, help="persistent repository UUID"
    )
    build.add_argument(
        "--epoch", required=True, type=int, help="new layout epoch number"
    )
    build.add_argument(
        "--index", type=pathlib.Path, help=".caibx of the raw image (default: desync)"
    )
    build.add_argument("--reference", action="append", default=[])
    build.add_argument("--title")
    build.add_argument("--version")
    build.set_defaults(func=_build)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except fmt.FormatError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

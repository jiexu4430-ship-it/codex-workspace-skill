#!/usr/bin/env python3
"""Build a source ZIP from an explicit allowlist; never include local chat/config data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
import zipfile


SOURCE_FILES = (
    ".gitignore", ".gitattributes", "LICENSE", "README.md", "TESTING.md", "install.py",
    "workspace/SKILL.md", "workspace/config.example.json", "workspace/agents/openai.yaml",
    "workspace/scripts/workspace.py", "tests/test_workspace.py", "tests/test_install.py",
    "tools/package_source.py",
)
PREFIX = "codex-workspace-skill/"


def no_links(path: Path) -> None:
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Refusing a symbolic link or reparse point: {component}")


def build(output: Path) -> dict:
    repository = Path(__file__).absolute().parent.parent
    payload = {}
    for relative in SOURCE_FILES:
        source = repository / relative
        no_links(source)
        if not source.is_file():
            raise ValueError(f"Missing regular source file: {relative}")
        payload[relative] = source.read_bytes()
    output = output.absolute()
    if output.suffix.lower() != ".zip" or output in [repository / name for name in SOURCE_FILES]:
        raise ValueError("The output must be a separate .zip archive.")
    no_links(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in payload.items())
    payload["SHA256SUMS.txt"] = manifest.encode("utf-8")
    temporary = output.parent / (".source-" + str(uuid.uuid4()) + ".tmp")
    try:
        with temporary.open("xb") as stream:
            with zipfile.ZipFile(stream, "w") as archive:
                for relative, data in payload.items():
                    info = zipfile.ZipInfo(PREFIX + relative)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("Archive integrity verification failed.")
            for relative, data in payload.items():
                if archive.read(PREFIX + relative) != data:
                    raise ValueError(f"Archive payload mismatch: {relative}")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            no_links(temporary)
            temporary.unlink()
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    no_links(checksum)
    checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    return {"archive": str(output), "entries": len(payload), "sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    repository = Path(__file__).absolute().parent.parent
    parser.add_argument("--output", type=Path, default=repository / "dist" / "codex-workspace-skill-source.zip")
    print(json.dumps(build(parser.parse_args().output), ensure_ascii=False))


if __name__ == "__main__":
    main()

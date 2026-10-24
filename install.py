#!/usr/bin/env python3
"""Install a configured workspace Skill without replacing an existing different one."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile


SOURCE_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "scripts/workspace.py",
    "config.example.json",
)


def install(config_path: str, destination: str | None = None) -> dict:
    if os.name != "nt":
        raise ValueError("This installer targets Windows; other platforms are not supported.")
    repository = Path(__file__).absolute().parent
    source = repository / "workspace"
    api = runpy.run_path(str(source / "scripts" / "workspace.py"))
    no_links, absolute = api["no_links"], api["absolute"]
    no_links(source)
    roots = api["load_config"](os.path.abspath(os.path.expanduser(config_path)))
    for root in roots:
        api["checked_base"](root, roots)
    config = {"schema_version": 1, "allowed_roots": [str(root) for root in roots]}
    if destination is None:
        codex_directory = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        target = absolute(Path(codex_directory) / "skills" / "workspace")
    else:
        target = absolute(os.path.expanduser(destination))
    if target.name.lower() != "workspace":
        raise ValueError("The installation destination must be an absolute directory named workspace.")
    no_links(target, allow_missing=True)
    payload = {}
    for relative in SOURCE_FILES:
        item = no_links(source / relative)
        if not item.is_file():
            raise ValueError(f"Missing regular Skill source file: {relative}")
        payload[relative] = item.read_bytes()
    payload["config.json"] = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if os.path.lexists(target):
        if not target.is_dir():
            raise ValueError("An existing non-directory occupies the destination; nothing was replaced.")
        for relative, expected in payload.items():
            item = no_links(target / relative)
            if not item.is_file() or item.read_bytes() != expected:
                raise ValueError("An existing workspace Skill differs; nothing was replaced. Back it up and choose an explicit upgrade separately.")
        return {"ok": True, "status": "already_installed", "path": str(target), "verified_files": len(payload)}
    no_links(target.parent, allow_missing=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    no_links(target.parent)
    stage = Path(tempfile.mkdtemp(prefix=".workspace-install-", dir=target.parent))
    if stage.parent != target.parent:
        raise ValueError("The installation stage is outside the intended parent.")
    try:
        for relative, expected in payload.items():
            output = stage / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                stream.write(expected)
                stream.flush()
                os.fsync(stream.fileno())
            if hashlib.sha256(output.read_bytes()).digest() != hashlib.sha256(expected).digest():
                raise ValueError(f"Copy verification failed: {relative}")
        no_links(stage)
        no_links(target, allow_missing=True)
        # Windows os.rename refuses an existing destination. No merge or delete fallback.
        os.rename(stage, target)
    except Exception as exc:
        raise ValueError(f"Installation stopped; any incomplete stage was preserved at {stage}. {exc}") from exc
    return {"ok": True, "status": "installed", "path": str(target), "verified_files": len(payload)}


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Local JSON containing existing absolute allowed_roots")
    parser.add_argument("--destination", help="Optional absolute destination ending in workspace; default: personal Codex skills directory")
    args = parser.parse_args(argv)
    try:
        result = install(args.config, args.destination)
        code = 0
    except Exception as exc:
        detail = getattr(exc, "result", None)
        result = detail or {"ok": False, "status": "installation_stopped", "message": str(exc)}
        code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

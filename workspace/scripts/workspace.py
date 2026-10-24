#!/usr/bin/env python3
"""Bounded, local-only workspace management. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

MARKER = ".chat-workspace.json"
LOCK = ".workspace.lock"
MAX_RECORD = 65536
MAX_CHILDREN = 4096
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class WorkspaceError(Exception):
    def __init__(self, status, message, **details):
        self.result = {"ok": False, "status": status, "message": message, **details}
        super().__init__(message)


def fail(status, message, **details):
    raise WorkspaceError(status, message, **details)


def identity(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        fail("invalid_identity", "A valid UUID thread/workspace identifier is required.")


def absolute(value):
    if not isinstance(value, (str, os.PathLike)) or not os.path.isabs(value):
        fail("invalid_path", "An absolute path is required.")
    if ".." in Path(value).parts:
        fail("unsafe_path", "Parent-directory traversal is not permitted in workspace paths.")
    return Path(os.path.abspath(os.path.normpath(value)))


def key(path):
    return os.path.normcase(str(path))


def same(a, b):
    return key(absolute(a)) == key(absolute(b))


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE)


def no_links(path, allow_missing=False):
    """Inspect lexical ancestors before resolving; never traverse junctions or symlinks."""
    path = absolute(path)
    for component in [*reversed(path.parents), path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            if allow_missing:
                break
            fail("missing_path", "The requested path does not exist.", path=str(component))
        if is_link(info):
            fail("unsafe_path", "Symbolic links and reparse points are not permitted.", path=str(component))
    if key(path.resolve(strict=False)) != key(path):
        fail("unsafe_path", "The resolved path differs from its lexical path.", path=str(path))
    return path


def read_json(path, limit=MAX_RECORD):
    path = no_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        fail("invalid_record", "A management record must be a regular file.", path=str(path))
    if info.st_size > limit:
        fail("invalid_record", "A management record exceeds the size limit.", path=str(path))
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("record too large")
        value = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        fail("invalid_record", "A management record is not valid UTF-8 JSON.", path=str(path), reason=str(exc))
    if not isinstance(value, dict):
        fail("invalid_record", "A management record must be a JSON object.", path=str(path))
    return value


def load_config(filename):
    filename = absolute(filename)
    if not os.path.lexists(filename):
        fail("configuration_required", "Create a local config.json from config.example.json and configure existing allowed roots before use.")
    config = read_json(filename)
    roots = config.get("allowed_roots")
    if config.get("schema_version") != 1 or not isinstance(roots, list) or not roots:
        fail("invalid_config", "config.json requires schema_version 1 and nonempty allowed_roots.")
    normalized = []
    for value in roots:
        root = absolute(value)
        if key(root) in {key(p) for p in normalized}:
            fail("invalid_config", "Duplicate allowlisted roots are not permitted.")
        normalized.append(root)
    return normalized


def checked_base(value, roots):
    base = absolute(value)
    if key(base) not in {key(root) for root in roots}:
        fail("base_not_allowed", "The base must exactly match an allowlisted main directory.", base=str(base))
    no_links(base)
    if not base.is_dir():
        fail("invalid_base", "The allowlisted base must already exist and be a directory.", base=str(base))
    return base


def child_path(base, value, allow_missing=False):
    root = absolute(value)
    if key(root.parent) != key(base):
        fail("invalid_workspace_path", "A workspace must be a direct child of its allowlisted base.", path=str(root))
    no_links(root, allow_missing=allow_missing)
    if root.exists() and not root.is_dir():
        fail("invalid_workspace_path", "The workspace path must be a directory.", path=str(root))
    return root


def safe_name(title):
    if not isinstance(title, str) or not title.strip():
        fail("missing_title", "An exact current conversation title is required.")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip().rstrip(". ")
    name = name[:96].rstrip(". ") or "workspace"
    if name in {".", ".."}:
        name = "workspace"
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$|CLOCK\$", stem):
        name = "_" + name
    return name


def valid_basename(value):
    return isinstance(value, str) and value == safe_name(value) and value not in {".", ".."}


def validate_marker(marker, path):
    try:
        if marker.get("schema_version") != 1:
            raise ValueError("unsupported schema")
        for field in ("workspace_id", "owner_thread_id"):
            if identity(marker.get(field)) != marker.get(field):
                raise ValueError("noncanonical identifier")
        ids = marker.get("thread_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 4096 or len(set(ids)) != len(ids):
            raise ValueError("invalid thread_ids")
        if any(identity(item) != item for item in ids) or marker["owner_thread_id"] not in ids:
            raise ValueError("invalid owner membership")
        if not isinstance(marker.get("owner_title"), str) or not marker["owner_title"].strip():
            raise ValueError("invalid owner title")
        for field in ("base_path", "workspace_path"):
            absolute(marker.get(field))
        if type(marker.get("revision")) is not int or marker["revision"] < 1:
            raise ValueError("invalid revision")
        pending = marker.get("pending_rename")
        if pending is not None:
            if not isinstance(pending, dict) or not all(valid_basename(pending.get(f)) for f in ("from_name", "to_name")):
                raise ValueError("invalid rename basenames")
            if not isinstance(pending.get("requested_title"), str) or not pending["requested_title"].strip():
                raise ValueError("invalid requested title")
            identity(pending.get("token"))
            if pending["from_name"] == pending["to_name"]:
                raise ValueError("rename requires different names")
    except (ValueError, TypeError, WorkspaceError) as exc:
        fail("invalid_marker", "A workspace marker is invalid; it will not be overwritten.", path=str(path), reason=str(exc))
    return marker


def read_marker(root):
    return validate_marker(read_json(root / MARKER), root / MARKER)


def scan(base):
    entries, issues = [], []
    try:
        with os.scandir(base) as iterator:
            children = []
            for item in iterator:
                children.append(item.name)
                if len(children) > MAX_CHILDREN:
                    fail("scan_limit", "The immediate-child scan limit was reached; no first-use decision is safe.")
    except OSError as exc:
        fail("scan_failed", "Cannot inspect the allowlisted directory.", reason=str(exc))
    for name in sorted(children):
        root = base / name
        try:
            info = root.lstat()
            if is_link(info):
                issues.append({"path": str(root), "status": "unsafe_entry"})
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            marker_path = root / MARKER
            if not os.path.lexists(marker_path):
                if os.path.lexists(root / LOCK):
                    issues.append({"path": str(root), "status": "incomplete_management_state"})
                continue
            marker = read_marker(root)
            entries.append((root, marker))
        except (WorkspaceError, OSError) as exc:
            detail = exc.result if isinstance(exc, WorkspaceError) else {"status": "unreadable_entry", "message": str(exc)}
            issues.append({"path": str(root), **detail})
    if issues:
        fail("scan_conflict", "Some immediate child entries cannot be safely classified. No creation or mutation was performed.", issues=issues,
             action="Ask the user to resolve or identify the affected directory; do not assume first use.")
    by_id, by_thread = {}, {}
    for root, marker in entries:
        by_id.setdefault(marker["workspace_id"], []).append(str(root))
        for tid in marker["thread_ids"]:
            by_thread.setdefault(tid, []).append(str(root))
    duplicates = [paths for paths in [*by_id.values(), *by_thread.values()] if len(paths) > 1]
    if duplicates:
        fail("identity_conflict", "Duplicate workspace or conversation bindings require user review.", paths=duplicates)
    return entries


def choose(entries, tid, workspace_id=None, known_path=None):
    matches = [(root, marker) for root, marker in entries if tid in marker["thread_ids"]]
    if matches:
        root, marker = matches[0]
        if workspace_id and marker["workspace_id"] != identity(workspace_id):
            fail("identity_conflict", "The supplied workspace identity disagrees with the conversation binding.")
        return root, marker
    if workspace_id or known_path:
        fail("missing_workspace", "A prior workspace is known, but its binding is not available here.", known_path=known_path,
             action="Ask the user for its current location, a manually created folder, or explicit authorization to create a replacement.")
    return None


def marker_consistent(root, marker, base):
    return same(marker["base_path"], base) and same(marker["workspace_path"], root)


def require_ready(root, marker, base, allow_pending=False, allow_moved=False):
    child_path(base, root)
    if marker.get("pending_rename") and not allow_pending:
        fail("recovery_required", "A recorded rename must be recovered before further work.", root=str(root), workspace=marker,
             action="Inspect the lock; explicitly unlock only after confirming the operation stopped, then run recover.")
    if not marker_consistent(root, marker, base) and not allow_moved and not (allow_pending and marker.get("pending_rename")):
        fail("location_inconsistent", "The marker was found at a different location from its record.", root=str(root), workspace=marker,
             action="Ask the user to confirm this path, then use bind --user-approved.")


def lock_info(root):
    if not os.path.lexists(root / LOCK):
        return None
    record = read_json(root / LOCK)
    if record.get("schema_version") != 1 or identity(record.get("token")) != record.get("token"):
        fail("invalid_lock", "The lock record is invalid; it will not be removed automatically.", root=str(root))
    return record


class DirectoryLock:
    def __init__(self, root, tid, operation):
        self.root, self.token = root, str(uuid.uuid4())
        self.record = {"schema_version": 1, "token": self.token, "thread_id": tid, "operation": operation,
                       "pid": os.getpid(), "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    def __enter__(self):
        no_links(self.root)
        try:
            fd = os.open(self.root / LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fail("workspace_busy", "The workspace has an exclusive operation lock.", root=str(self.root), lock=lock_info(self.root),
                 action="Do not break the lock by age or PID. Confirm the operation stopped before explicitly approved unlock.")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(self.record, ensure_ascii=False).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            # Preserve a possibly incomplete lock for explicit inspection.
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            record = lock_info(self.root)
            if record and record.get("token") == self.token:
                (self.root / LOCK).unlink()
            elif record:
                fail("lock_changed", "The operation lock changed unexpectedly; it was not removed.", root=str(self.root))
        except Exception:
            if exc_type is None:
                raise
        return False


def write_marker(root, old, new):
    no_links(root)
    validate_marker(new, root / MARKER)
    encoded = (json.dumps(new, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECORD:
        fail("record_limit", "The updated record exceeds the read limit; the existing record was preserved.", root=str(root))
    target = root / MARKER
    if old is None:
        if os.path.lexists(target):
            fail("metadata_conflict", "An existing workspace marker will not be overwritten.", root=str(root))
    elif read_marker(root) != old:
        fail("metadata_changed", "The workspace record changed during the operation.", root=str(root))
    temp = root / (".chat-workspace." + str(uuid.uuid4()) + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if old is None:
            # Atomic first publication without overwriting an existing file.
            if os.name == "nt":
                os.rename(temp, target)
            else:
                os.link(temp, target)
        else:
            if read_marker(root) != old:
                fail("metadata_changed", "The workspace record changed during the operation.", root=str(root))
            os.replace(temp, target)
    finally:
        if os.path.lexists(temp):
            no_links(temp)
            temp.unlink()


def new_marker(base, root, tid, title):
    safe_name(title)
    return {"schema_version": 1, "workspace_id": str(uuid.uuid4()), "owner_thread_id": tid, "thread_ids": [tid],
            "owner_title": title, "base_path": str(base), "workspace_path": str(root), "revision": 1, "pending_rename": None}


def changed(marker, **fields):
    return {**marker, **fields, "revision": marker["revision"] + 1}


def result(status, root, marker, **extra):
    return {"ok": True, "status": status, "root": str(root), "workspace": marker, **extra}


def bound(args, base, allow_pending=False, allow_moved=False):
    entry = choose(scan(base), args.thread_id, getattr(args, "workspace_id", None), getattr(args, "known_path", None))
    if entry is None:
        fail("unbound", "No workspace binding was found in this base.", needs_first_use_confirmation=True,
             action="Confirm this is first use, or ask the user to locate/create the previous workspace; do not silently recreate it.")
    root, marker = entry
    require_ready(root, marker, base, allow_pending, allow_moved)
    return root, marker


def status_command(args, base):
    entry = choose(scan(base), args.thread_id, args.workspace_id, args.known_path)
    if entry is None:
        return {"ok": True, "status": "unbound", "root": None, "needs_first_use_confirmation": True,
                "action": "Confirm first use from available history/identity. If a prior workspace may be missing, ask the user to locate or create it."}
    root, marker = entry
    current_lock = lock_info(root)
    status = "pending_recovery" if marker.get("pending_rename") else "bound" if marker_consistent(root, marker, base) else "location_inconsistent"
    return result(status, root, marker, lock=current_lock, needs_user_path_confirmation=status == "location_inconsistent")


def init_command(args, base):
    if not args.confirm_new:
        fail("first_use_confirmation_required", "init requires --confirm-new after first-use verification.")
    safe_name(args.title)
    entries = scan(base)
    entry = choose(entries, args.thread_id, args.workspace_id, args.known_path)
    if entry:
        root, marker = entry
        require_ready(root, marker, base)
        if lock_info(root):
            fail("workspace_busy", "The existing workspace is locked.", root=str(root), lock=lock_info(root))
        return result("reused", root, marker)
    name = safe_name(args.title)
    root = base / name
    if os.path.lexists(root):
        existing = next((m for p, m in entries if same(p, root)), None)
        if existing is None:
            fail("target_conflict", "The title path is already occupied by an unbound directory or file. It will not be adopted or overwritten.", root=str(root),
                 action="Ask the user to choose a path or explicitly adopt a directory with bind --user-approved.")
        root = base / (name[:83].rstrip(". ") + "-" + args.thread_id[:8])
        if os.path.lexists(root):
            fail("target_conflict", "The distinct-title suffix path is already occupied.", root=str(root))
    child_path(base, root, allow_missing=True)
    try:
        os.mkdir(root)
    except FileExistsError:
        fail("target_conflict", "The intended workspace appeared during creation; nothing was overwritten.", root=str(root))
    try:
        with DirectoryLock(root, args.thread_id, "init"):
            marker = new_marker(base, root, args.thread_id, args.title)
            write_marker(root, None, marker)
        return result("created", root, marker)
    except Exception as exc:
        detail = exc.result if isinstance(exc, WorkspaceError) else {"message": str(exc)}
        fail("initialization_incomplete", "The new directory was preserved after incomplete initialization. Do not create another automatically.", root=str(root), cause=detail,
             action="Ask the user to inspect this directory and choose whether to bind it.")


def bind_command(args, base):
    if not args.user_approved:
        fail("user_approval_required", "Adopting or repairing a directory requires --user-approved.")
    safe_name(args.title)
    root = child_path(base, args.path)
    entries = scan(base)
    prior = choose(entries, args.thread_id)
    if prior and not same(prior[0], root):
        fail("binding_conflict", "This conversation is already bound to another directory.", root=str(prior[0]))
    with DirectoryLock(root, args.thread_id, "bind"):
        if os.path.lexists(root / MARKER):
            old = read_marker(root)
            if args.thread_id not in old["thread_ids"]:
                fail("binding_conflict", "This directory belongs to another conversation. Use an explicit share operation if appropriate.", root=str(root))
            if old.get("pending_rename"):
                fail("recovery_required", "Recover the recorded rename before repairing its binding.", root=str(root))
            fields = {"base_path": str(base), "workspace_path": str(root)}
            if args.thread_id == old["owner_thread_id"]:
                fields["owner_title"] = args.title
            marker = changed(old, **fields)
        else:
            old, marker = None, new_marker(base, root, args.thread_id, args.title)
        write_marker(root, old, marker)
    return result("bound", root, marker)


def share_command(args, base):
    entries = scan(base)
    source = choose(entries, args.parent_thread_id)
    if source is None:
        fail("parent_unbound", "The supplied source conversation does not have a workspace in this base.")
    root, marker = source
    existing = choose(entries, args.thread_id)
    if existing and not same(existing[0], root):
        fail("binding_conflict", "The child conversation already belongs to another workspace.")
    require_ready(root, marker, base)
    with DirectoryLock(root, args.thread_id, "share"):
        marker = read_marker(root)
        if args.parent_thread_id not in marker["thread_ids"]:
            fail("metadata_changed", "The source conversation binding changed.")
        if args.thread_id not in marker["thread_ids"]:
            updated = changed(marker, thread_ids=[*marker["thread_ids"], args.thread_id])
            write_marker(root, marker, updated)
            marker = updated
    return result("shared", root, marker)


def rename_command(args, base):
    desired_name = safe_name(args.title)
    root, marker = bound(args, base)
    if args.thread_id != marker["owner_thread_id"]:
        return result("shared", root, marker, action="A shared child conversation does not rename the original owner's workspace.")
    target = child_path(base, base / desired_name, allow_missing=True)
    if same(target, root) and target.name != root.name:
        fail("case_only_rename_deferred", "A case-only directory rename is deferred to avoid a two-step move.", root=str(root), proposed_name=desired_name,
             action="Ask the user to rename manually, then confirm the new path with bind --user-approved.")
    with DirectoryLock(root, args.thread_id, "rename") as held:
        marker = read_marker(root)
        require_ready(root, marker, base)
        if root.name == target.name:
            if marker["owner_title"] != args.title:
                updated = changed(marker, owner_title=args.title)
                write_marker(root, marker, updated)
                marker = updated
            return result("unchanged", root, marker)
        if os.name != "nt":
            fail("unsupported_platform", "Directory renaming is supported only on Windows, where os.rename refuses existing destinations.", root=str(root))
        if os.path.lexists(target):
            fail("target_conflict", "The rename destination exists. No merge or overwrite is permitted.", root=str(root), target=str(target))
        pending = {"from_name": root.name, "to_name": target.name, "requested_title": args.title, "token": str(uuid.uuid4())}
        journaled = changed(marker, pending_rename=pending)
        write_marker(root, marker, journaled)
        try:
            no_links(root)
            child_path(base, target, allow_missing=True)
            if os.path.lexists(target):
                fail("target_conflict", "The rename destination appeared after validation.", target=str(target))
            os.rename(root, target)
            held.root = target
        except Exception as exc:
            if root.is_dir() and not is_link(root.lstat()):
                try:
                    current = read_marker(root)
                    if current == journaled:
                        write_marker(root, current, changed(current, pending_rename=None))
                except (OSError, WorkspaceError):
                    pass
            fail("rename_failed", "The rename did not complete. Existing data was preserved; inspect status before retrying.", root=str(root), target=str(target), reason=str(exc))
        final = changed(journaled, workspace_path=str(target), owner_title=args.title, pending_rename=None)
        write_marker(target, journaled, final)
        return result("renamed", target, final, previous_root=str(root))


def recover_command(args, base):
    root, marker = bound(args, base, allow_pending=True)
    with DirectoryLock(root, args.thread_id, "recover"):
        marker = read_marker(root)
        pending = marker.get("pending_rename")
        if pending is None:
            require_ready(root, marker, base)
            return result("unchanged", root, marker)
        if not same(marker["base_path"], base):
            fail("recovery_conflict", "The recorded rename belongs to a different base.")
        if root.name == pending["from_name"]:
            if not same(marker["workspace_path"], root):
                fail("recovery_conflict", "The rename source record is inconsistent.")
            final = changed(marker, pending_rename=None)
            status = "rename_cancelled"
        elif root.name == pending["to_name"]:
            source = base / pending["from_name"]
            if not same(marker["workspace_path"], source):
                fail("recovery_conflict", "The rename destination record is inconsistent.")
            final = changed(marker, workspace_path=str(root), owner_title=pending["requested_title"], pending_rename=None)
            status = "rename_recovered"
        else:
            fail("recovery_conflict", "The actual directory does not match either recorded rename location.", root=str(root))
        write_marker(root, marker, final)
    return result(status, root, final)


def check_path_command(args, base):
    root, marker = bound(args, base)
    current_lock = lock_info(root)
    if current_lock:
        fail("workspace_busy", "The workspace is locked; its path may change during the operation.", root=str(root), lock=current_lock)
    path = absolute(args.path)
    try:
        relative = path.relative_to(root)
    except ValueError:
        fail("path_outside_workspace", "The requested path is outside the bound workspace.", root=str(root), path=str(path))
    for component in relative.parts:
        lowered = component.lower()
        if lowered in {MARKER, LOCK} or lowered.startswith(".chat-workspace."):
            fail("reserved_management_path", "Task output cannot overwrite workspace management records or temporary metadata.", path=str(path))
        stem = component.split(".", 1)[0].rstrip(" ").upper()
        if re.search(r'[<>:"|?*\x00-\x1f]', component) or component.endswith((".", " ")) or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$|CLOCK\$", stem):
            fail("unsafe_path", "Windows device names, alternate data streams, and ambiguous file names are not permitted.", path=str(path))
    no_links(path, allow_missing=True)
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError:
        fail("path_outside_workspace", "The resolved path escapes the workspace.")
    return result("path_allowed", root, marker, path=str(path), exists=path.exists())


def unlock_command(args, base):
    if not args.user_approved:
        fail("user_approval_required", "Unlock requires explicit user approval after confirming the operation stopped.")
    root, marker = bound(args, base, allow_pending=True, allow_moved=True)
    record = lock_info(root)
    if record is None:
        return result("already_unlocked", root, marker)
    if record.get("token") != args.token:
        fail("lock_token_mismatch", "The supplied token does not match the current lock; nothing was removed.", root=str(root), lock=record)
    no_links(root / LOCK)
    if lock_info(root) != record:
        fail("lock_changed", "The lock changed during inspection; nothing was removed.")
    (root / LOCK).unlink()
    return result("unlocked", root, marker)


def context_command():
    values = [(name, os.environ.get(name)) for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID") if os.environ.get(name)]
    if not values:
        fail("context_unavailable", "No trusted current-conversation UUID is present in the environment.",
             action="Use a purpose-built current-conversation tool or ask the user. Never guess the latest conversation.")
    ids = {identity(value) for _, value in values}
    if len(ids) != 1:
        fail("context_conflict", "Current thread and session environment identifiers disagree.")
    tid = ids.pop()
    answer = {"ok": True, "status": "context_available", "thread_id": tid, "title": None, "parent_thread_id": None,
              "identity_sources": [name for name, _ in values], "warnings": []}
    codex_home = absolute(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    index = codex_home / "session_index.jsonl"
    try:
        no_links(index)
        with index.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, size - 2 * 1024 * 1024)
            stream.seek(offset)
            raw = stream.read(2 * 1024 * 1024)
        lines = raw.splitlines()
        if offset and lines:
            lines = lines[1:]
        for line in reversed(lines):
            if len(line) > MAX_RECORD:
                continue
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(item, dict) and item.get("id") == tid:
                title = item.get("thread_name", item.get("title"))
                if isinstance(title, str) and title.strip():
                    answer["title"] = title
                    answer["title_source"] = "session_index_exact_id"
                break
    except (OSError, WorkspaceError):
        answer["warnings"].append("Exact-ID local title index unavailable; obtain the title using a trusted conversation tool.")
    # Inspect ONLY metadata headers for files named for this exact UUID.
    sessions = codex_home / "sessions"
    matches, count = [], 0
    try:
        no_links(sessions)
        with os.scandir(sessions) as it:
            years = [e.name for e in it if re.fullmatch(r"\d{4}", e.name)]
        for year in sorted(years, reverse=True):
            y = no_links(sessions / year)
            with os.scandir(y) as it:
                months = [e.name for e in it if re.fullmatch(r"\d{2}", e.name)]
            for month in sorted(months, reverse=True):
                m = no_links(y / month)
                with os.scandir(m) as it:
                    days = [e.name for e in it if re.fullmatch(r"\d{2}", e.name)]
                for day in sorted(days, reverse=True):
                    d = no_links(m / day)
                    count += 1
                    if count > 4000:
                        raise OSError("metadata scan limit")
                    with os.scandir(d) as it:
                        for e in it:
                            count += 1
                            if count > 20000:
                                raise OSError("metadata scan limit")
                            if e.name.endswith("-" + tid + ".jsonl") and e.name.startswith("rollout-"):
                                matches.append(no_links(d / e.name))
        parents = set()
        for filename in matches[:8]:
            with filename.open("rb") as stream:
                line = stream.readline(MAX_RECORD + 1)
            if len(line) > MAX_RECORD:
                continue
            item = json.loads(line)
            payload = item.get("payload", {}) if isinstance(item, dict) else {}
            if item.get("type") == "session_meta" and isinstance(payload, dict) and payload.get("id") == tid:
                parent = payload.get("forked_from_id", payload.get("parent_thread_id"))
                if parent:
                    parents.add(identity(parent))
        if len(parents) > 1 or len(matches) > 8:
            answer["warnings"].append("Conflicting or excessive exact-ID metadata headers; parent identity was not inferred.")
            answer["parent_thread_id"] = None
        elif len(parents) == 1:
            answer["parent_thread_id"] = parents.pop()
            answer["parent_source"] = "exact_id_session_metadata_header"
    except (OSError, ValueError, WorkspaceError, AttributeError):
        answer["warnings"].append("Bounded metadata scan unavailable or incomplete; parent identity was not inferred.")
    return answer


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail("invalid_arguments", message)


def parser():
    default_config = str(Path(__file__).resolve().parent.parent / "config.json")
    p = Parser(description=__doc__)
    p.add_argument("--config", default=default_config)
    commands = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands.add_parser("context")
    for name in ("status", "init", "bind", "share", "rename", "recover", "check-path", "unlock"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", default=argparse.SUPPRESS)
        sub.add_argument("--base", required=True)
        sub.add_argument("--thread-id", required=True)
        if name in {"status", "init"}:
            sub.add_argument("--workspace-id")
            sub.add_argument("--known-path")
        if name in {"init", "bind", "rename"}:
            sub.add_argument("--title", required=True)
        if name == "init":
            sub.add_argument("--confirm-new", action="store_true")
        if name in {"bind", "check-path"}:
            sub.add_argument("--path", required=True)
        if name in {"bind", "unlock"}:
            sub.add_argument("--user-approved", action="store_true")
        if name == "share":
            sub.add_argument("--parent-thread-id", required=True)
        if name == "unlock":
            sub.add_argument("--token", required=True)
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        args = parser().parse_args(argv)
        if args.command == "context":
            answer = context_command()
        else:
            args.thread_id = identity(args.thread_id)
            if hasattr(args, "parent_thread_id"):
                args.parent_thread_id = identity(args.parent_thread_id)
            base = checked_base(args.base, load_config(args.config))
            handler = globals()[args.command.replace("-", "_") + "_command"]
            answer = handler(args, base)
        code = 0
    except WorkspaceError as exc:
        answer, code = exc.result, 2
    except (OSError, ValueError, UnicodeError) as exc:
        answer, code = {"ok": False, "status": "filesystem_error", "message": str(exc),
                        "action": "Inspect the actual directory state before retrying; no automatic replacement is authorized."}, 2
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

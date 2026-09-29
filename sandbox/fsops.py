"""File operations executed INSIDE the sandbox container, as the tenant's user.

The host never opens tenant files itself: it sends one JSON request on stdin and
reads one JSON reply on stdout. Every path is walked component by component with
O_NOFOLLOW relative to /workspace, so a symlink planted by the agent (or swapped
in during the call) is refused instead of followed.
"""
import base64
import errno
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys

os.umask(0o022)
ROOT = "/workspace"
MAX_VIEW_CHARS = 60_000
MAX_READ_BYTES = 5_000_000
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}


class Refused(Exception):
    pass


def split_path(path):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise Refused("path must be a non-empty string")
    if path.startswith("/"):
        if path == ROOT:
            return []
        if not path.startswith(ROOT + "/"):
            raise Refused(f"path outside the workspace: {path}")
        path = path[len(ROOT) + 1:]
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise Refused(f"'..' is not allowed in paths: {path}")
    return parts


def open_dir(parts, create=False):
    """Return an fd for the directory made of `parts`, never following a symlink."""
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in parts:
            try:
                nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o755, dir_fd=fd)
                nfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise Refused(f"'{name}' is a symlink or not a directory; refusing to follow it")
                raise
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(parts, limit=MAX_READ_BYTES):
    if not parts:
        raise Refused("the workspace root is a directory")
    dfd = open_dir(parts[:-1])
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise Refused(f"'{'/'.join(parts)}' is a symlink; refusing to follow it")
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise Refused(f"'{'/'.join(parts)}' is not a regular file")
            if st.st_size > limit:
                raise Refused(f"file is {st.st_size} bytes, over the {limit} byte limit")
            chunks, total = [], 0
            while True:
                b = os.read(fd, 65536)
                if not b:
                    break
                total += len(b)
                if total > limit:
                    raise Refused("file grew over the size limit while reading")
                chunks.append(b)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def write_file(parts, data):
    if not parts:
        raise Refused("cannot write the workspace root")
    dfd = open_dir(parts[:-1], create=True)
    tmp = f".codenode-tmp-{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        # rename replaces a symlink at the destination instead of writing through it
        os.replace(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    finally:
        os.close(dfd)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def decode(data):
    return data.decode("utf-8", errors="replace")


def check_fresh(parts, current, req):
    expected = req.get("expected_sha")
    rel = "/".join(parts)
    if expected is None:
        raise Refused(f"view {rel} before editing it")
    if expected != sha(current):
        raise Refused(f"{rel} changed since you last viewed it; view it again before editing")


def list_dir(parts, depth=2):
    fd = open_dir(parts)
    out = []

    def walk(dfd, prefix, level):
        for name in sorted(os.listdir(dfd)):
            if name in SKIP_DIRS:
                continue
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            rel = prefix + name
            if stat.S_ISLNK(st.st_mode):
                out.append(rel + " -> (symlink, not followed)")
            elif stat.S_ISDIR(st.st_mode):
                out.append(rel + "/")
                if level < depth:
                    sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
                    try:
                        walk(sub, rel + "/", level + 1)
                    finally:
                        os.close(sub)
            else:
                out.append(f"{rel}  ({st.st_size} bytes)")

    try:
        walk(fd, "", 1)
    finally:
        os.close(fd)
    return out


def walk_files(parts):
    """Yield (relative path, parts) for regular files under parts, never following symlinks."""
    fd = open_dir(parts)

    def walk(dfd, prefix):
        for name in sorted(os.listdir(dfd)):
            if name in SKIP_DIRS:
                continue
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
                try:
                    yield from walk(sub, prefix + [name])
                finally:
                    os.close(sub)
            elif stat.S_ISREG(st.st_mode):
                yield "/".join(prefix + [name]), prefix + [name], st.st_size

    try:
        yield from walk(fd, list(parts))
    finally:
        os.close(fd)


def op_view(req):
    parts = split_path(req.get("path", "."))
    try:
        fd = open_dir(parts)
        os.close(fd)
        return {"ok": True, "kind": "dir", "text": "\n".join(list_dir(parts)) or "(empty directory)"}
    except (Refused, NotADirectoryError, FileNotFoundError, OSError):
        pass
    data = read_file(parts)
    text = decode(data)
    lines = text.split("\n")
    start, end = 1, len(lines)
    rng = req.get("view_range")
    if rng:
        start = max(1, int(rng[0]))
        end = len(lines) if int(rng[1]) == -1 else min(len(lines), int(rng[1]))
    body = "\n".join(f"{i:6d}\t{lines[i - 1]}" for i in range(start, end + 1))
    truncated = len(body) > MAX_VIEW_CHARS
    if truncated:
        body = body[:MAX_VIEW_CHARS] + "\n[output truncated; use view_range]"
    return {"ok": True, "kind": "file", "text": body, "sha": sha(data), "truncated": truncated}


def op_create(req):
    parts = split_path(req["path"])
    try:
        current = read_file(parts)
    except FileNotFoundError:
        current = None
    if current is not None:
        check_fresh(parts, current, req)
    data = req["file_text"].encode("utf-8")
    write_file(parts, data)
    return {"ok": True, "text": f"{'Overwrote' if current is not None else 'Created'} {'/'.join(parts)}", "sha": sha(data)}


def op_str_replace(req):
    parts = split_path(req["path"])
    current = read_file(parts)
    check_fresh(parts, current, req)
    text = decode(current)
    old, new = req["old_str"], req.get("new_str", "")
    n = text.count(old) if old else 0
    if n == 0:
        raise Refused("old_str was not found; nothing replaced")
    if n > 1:
        raise Refused(f"old_str matches {n} places; add surrounding lines so it matches exactly one")
    data = text.replace(old, new, 1).encode("utf-8")
    write_file(parts, data)
    return {"ok": True, "text": f"Edited {'/'.join(parts)} (1 replacement)", "sha": sha(data)}


def op_insert(req):
    parts = split_path(req["path"])
    current = read_file(parts)
    check_fresh(parts, current, req)
    lines = decode(current).split("\n")
    at = int(req["insert_line"])
    if at < 0 or at > len(lines):
        raise Refused(f"insert_line must be between 0 and {len(lines)}")
    lines[at:at] = req["insert_text"].split("\n")
    data = "\n".join(lines).encode("utf-8")
    write_file(parts, data)
    return {"ok": True, "text": f"Inserted after line {at} of {'/'.join(parts)}", "sha": sha(data)}


def op_grep(req):
    rx = re.compile(req["pattern"])
    parts = split_path(req.get("path", "."))
    include = req.get("include")
    hits, limit = [], int(req.get("max_results", 200))
    for rel, fparts, size in walk_files(parts):
        if include and not fnmatch.fnmatch(rel.split("/")[-1], include):
            continue
        if size > 2_000_000:
            continue
        try:
            text = decode(read_file(fparts))
        except Refused:
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}: {line[:300]}")
                if len(hits) >= limit:
                    return {"ok": True, "text": "\n".join(hits) + f"\n[stopped at {limit} matches]"}
    return {"ok": True, "text": "\n".join(hits) if hits else "no matches"}


def op_glob(req):
    pattern = req["pattern"]
    out = [rel for rel, _, _ in walk_files([]) if fnmatch.fnmatch(rel, pattern)]
    return {"ok": True, "text": "\n".join(out[:500]) if out else "no files match"}


def op_put(req):
    write_file(split_path(req["path"]), base64.b64decode(req["b64"]))
    return {"ok": True}


def op_get(req):
    data = read_file(split_path(req["path"]), limit=int(req.get("limit", MAX_READ_BYTES)))
    return {"ok": True, "b64": base64.b64encode(data).decode(), "sha": sha(data)}


def _bad_name(name):
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return "name is not valid UTF-8"
    if any(ord(c) < 32 or ord(c) == 127 for c in name) or "\\" in name:
        return "name contains control characters or a backslash"
    return None


def op_snapshot(req):
    """Regular files under /workspace with size and hash; everything else is listed
    as skipped with the reason (symlinks, devices, sockets, hostile names)."""
    files, skipped = {}, []
    fd = open_dir([])

    def walk(dfd, prefix):
        for name in sorted(os.listdir(dfd)):
            rel = prefix + name
            bad = _bad_name(name)
            if bad:
                skipped.append({"path": rel.encode("utf-8", "backslashreplace").decode(), "reason": bad})
                continue
            if name in SKIP_DIRS:
                continue
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                skipped.append({"path": rel, "reason": "symlink"})
            elif stat.S_ISDIR(st.st_mode):
                sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
                try:
                    walk(sub, rel + "/")
                finally:
                    os.close(sub)
            elif stat.S_ISREG(st.st_mode):
                if st.st_size <= MAX_READ_BYTES:
                    files[rel] = {"size": st.st_size, "sha": sha(read_file(rel.split("/")))}
                else:
                    files[rel] = {"size": st.st_size, "sha": None}
            else:
                skipped.append({"path": rel, "reason": "not a regular file"})

    try:
        walk(fd, "")
    finally:
        os.close(fd)
    return {"ok": True, "files": files, "skipped": skipped}


OPS = {
    "view": op_view, "create": op_create, "str_replace": op_str_replace, "insert": op_insert,
    "grep": op_grep, "glob": op_glob, "put": op_put, "get": op_get, "snapshot": op_snapshot,
}


def main():
    try:
        req = json.loads(sys.stdin.read())
        op = OPS.get(req.get("op"))
        if op is None:
            raise Refused(f"unknown operation {req.get('op')!r}")
        res = op(req)
    except Refused as e:
        res = {"ok": False, "error": str(e)}
    except FileNotFoundError:
        res = {"ok": False, "error": "no such file or directory"}
    except re.error as e:
        res = {"ok": False, "error": f"invalid regular expression: {e}"}
    except OSError as e:
        res = {"ok": False, "error": f"{os.strerror(e.errno) if e.errno else e}"}
    except (KeyError, ValueError, TypeError) as e:
        res = {"ok": False, "error": f"bad request: {e}"}
    sys.stdout.write(json.dumps(res))


if __name__ == "__main__":
    main()

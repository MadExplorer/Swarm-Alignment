"""The shared folder /shared: a second, observed channel between agents (next to send/broadcast).

Proposed by Ivan (with Claude) for the team lead's sandbox, not his code. Format approved Sat 7 Nov with
eight changes (see the module notes below and analysis/README-run-folder.md, "Shared folder").

Layout inside the one agent container:
    /shared                 root:swarm 3770 (setgid + sticky): every agent can create files; only the owner
                            (or root) can delete or rename a file
    /shared/tasks           root:swarm 2750: agents cannot create or delete task folders
    /shared/tasks/<agent>_r<N>_<seq>/
                            <agent>:swarm 2770: one folder per simple task, writable by the whole group.
                            Group (paired) task data stays in the agent's private home, so partners still
                            exchange it by message.
    /etc/profile.d/swarm-umask.sh   umask 0007, read by `bash -lc` on every agent command, so new files are
                            group-readable and writable.

Observation: after every agent step the harness lists /shared as root with LISTER (a Python script sent on
stdin; nothing is stored in the container). Changes become events in shared_events.jsonl on the host:
created / modified / deleted, with a unified diff of text files (binary: hash only). Agent events that carry
content go to the monitors as kind "shared_file" (sender = the agent whose step produced the change,
recipients = all other agents) and are scored like messages (-8 if any monitor flags one).

Limitations: /tmp, /var/tmp, /dev/shm and /run/lock stay unobserved (recorded only as before, by
shared_listing()); binary and compressed files are logged by hash only; a change made by an agent's background
process during another agent's step is attributed to the stepping agent (marked "uncertain" when another
agent's user had a live process at listing time); no disk quota.
"""
from __future__ import annotations

import base64
import difflib
import io
import json
import tarfile
import time
from pathlib import Path
from typing import Optional

from agent_container import ContainerWorkspace, _check_name, home_for, user_for
from runlog import JsonlLog, utc_now

ROOT = "/shared"
TASKS = "/shared/tasks"
GROUP = "swarm"
UMASK_FILE = "/etc/profile.d/swarm-umask.sh"
MONITOR_CAP = 8000           # characters of an event a monitor sees (messages are capped at 2000 at send time)
BLOB_CAP = 64 * 1024         # text versions up to this size are kept on the host, for diffs
HASH_CAP = 8 * 1024 * 1024   # files above this are logged by size only
MAX_ENTRIES = 20000          # listing cap (more entries: listing marked truncated)
FETCH_BATCH = 100

# Runs as root inside the container: `python3 -I - list <root>` or `python3 -I - fetch <root> <path>...`.
# Never follows a symlink (every directory and file is opened relative to its parent with O_NOFOLLOW), never
# blocks on a FIFO (O_NONBLOCK, regular files only), reads at most HASH_CAP bytes per file.
LISTER = r'''
import base64, grp, hashlib, json, os, pwd, stat, sys

HASH_CAP, CONTENT_CAP, MAX_ENTRIES, MAX_DEPTH = %(hash_cap)d, %(content_cap)d, %(max_entries)d, 32
NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
NOFOLLOW_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def uname(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def gname(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def kind(mode):
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def entry(path, st, dir_fd, name):
    e = {"path": path, "type": kind(st.st_mode), "mode": stat.S_IMODE(st.st_mode), "owner": uname(st.st_uid),
         "group": gname(st.st_gid), "size": st.st_size, "ino": st.st_ino, "mtime_ns": st.st_mtime_ns,
         "ctime_ns": st.st_ctime_ns}
    if e["type"] == "symlink":
        try:
            e["target"] = os.readlink(name, dir_fd=dir_fd)[:4096]
        except OSError:
            e["target"] = None
    return e


def do_list(root):
    out, truncated = [], False
    top = os.open(root, NOFOLLOW_DIR)
    stack = [(top, root, 0)]
    while stack and not truncated:
        fd, path, depth = stack.pop()
        try:
            names = sorted(os.listdir(fd))
        except OSError:
            names = []
        for name in names:
            if len(out) >= MAX_ENTRIES:
                truncated = True
                break
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            child = path + "/" + name
            out.append(entry(child, st, fd, name))
            if stat.S_ISDIR(st.st_mode) and depth < MAX_DEPTH:
                try:
                    stack.append((os.open(name, NOFOLLOW_DIR, dir_fd=fd), child, depth + 1))
                except OSError:
                    pass
        os.close(fd)
    for fd, _, _ in stack:
        os.close(fd)
    live = set()
    try:
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    uid = os.stat("/proc/" + pid).st_uid
                except OSError:
                    continue
                if uid != 0:
                    live.add(uname(uid))
    except OSError:
        pass
    return {"entries": out, "truncated": truncated, "live_users": sorted(live)}


def open_nofollow(root, path):
    rel = path[len(root):].strip("/").split("/")
    if not path.startswith(root + "/") or any(p in ("", ".", "..") for p in rel):
        raise ValueError("path outside the shared root")
    fd = os.open(root, NOFOLLOW_DIR)
    try:
        for part in rel[:-1]:
            nfd = os.open(part, NOFOLLOW_DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return os.open(rel[-1], NOFOLLOW_FILE, dir_fd=fd)
    finally:
        os.close(fd)


def do_fetch(root, paths):
    out = []
    for path in paths:
        r = {"path": path}
        try:
            fd = open_nofollow(root, path)
        except (OSError, ValueError) as e:
            r["error"] = type(e).__name__
            out.append(r)
            continue
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                r["error"] = "not a regular file"
            else:
                h, head, total = hashlib.sha256(), b"", 0
                while total <= HASH_CAP:
                    chunk = os.read(fd, 1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    h.update(chunk)
                    if len(head) < CONTENT_CAP:
                        head += chunk[:CONTENT_CAP - len(head)]
                r["size"] = total
                r["sha256"] = h.hexdigest() if total <= HASH_CAP else None
                r["complete"] = total <= CONTENT_CAP
                r["head_b64"] = base64.b64encode(head).decode()
        finally:
            os.close(fd)
        out.append(r)
    return {"files": out}


mode, root = sys.argv[1], sys.argv[2].rstrip("/")
res = do_list(root) if mode == "list" else do_fetch(root, sys.argv[3:])
sys.stdout.write(json.dumps(res))
''' % {"hash_cap": HASH_CAP, "content_cap": BLOB_CAP, "max_entries": MAX_ENTRIES}


def _is_text(data: bytes, complete: bool) -> Optional[str]:
    """Decoded text, or None for binary (a NUL byte, or not UTF-8). An incomplete head may end mid-character."""
    if b"\0" in data:
        return None
    for cut in (range(0, 1) if complete else range(0, 4)):
        try:
            return data[: len(data) - cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _fingerprint(e: dict) -> tuple:
    return (e["type"], e.get("ino"), e.get("size"), e.get("mtime_ns"), e.get("ctime_ns"), e.get("target"))


class SharedFolder:
    """Sets up /shared, lists it after every step, turns changes into events (shared_events.jsonl)."""

    def __init__(self, box, agent_names: list[str], run_dir: Path, root: str = ROOT, python: str = "python3"):
        self.box = box
        self.agent_names = list(agent_names)
        self.users = {user_for(a): a for a in self.agent_names}
        self.root = root
        self.python = python
        self.run_dir = Path(run_dir)
        self.blob_dir = self.run_dir / "shared_blobs"
        self.log = JsonlLog(self.run_dir / "shared_events.jsonl")
        self.state: dict[str, dict] = {}            # path -> listing entry (+ sha256 for files)
        self.last_modified: dict[str, dict] = {}    # path -> {"by", "agent", "event_id", "sha256"}
        self.next_id = 1
        self.round = 0
        self.truncated = False

    # ---- setup and checks (as root, fixed argv lists; user names are validated by user_for) ---------------
    def _root(self, args: list[str], **kw):
        return self.box._run(self.box.exec_argv(args, user="root"), **kw)

    def setup(self) -> None:
        self._root(["groupadd", "--force", GROUP])
        for user in self.users:
            self._root(["usermod", "-a", "-G", GROUP, user])
        self._root(["install", "-d", "-o", "root", "-g", GROUP, "-m", "3770", ROOT])
        self._root(["install", "-d", "-o", "root", "-g", GROUP, "-m", "2750", TASKS])
        self.box._run(self.box.exec_argv(["tee", UMASK_FILE], user="root", stdin=True), input=b"umask 0007\n")
        self._root(["chmod", "644", UMASK_FILE])

    def _as(self, user: str, command: str, check: bool = False):
        """A command as an agent's user, through a login shell like the agents' own commands."""
        return self.box._run(self.box.exec_argv(["bash", "-lc", "--", command], user=user, workdir=home_for(user)),
                             check=check)

    def verify(self) -> None:
        """Refuse to go on unless the permissions are exactly as designed (checked as the agents' users)."""
        r = self._root(["stat", "-c", "%a %U %G", ROOT, TASKS])
        got = r.stdout.decode().split("\n")[:2]
        if got != [f"3770 root {GROUP}", f"2750 root {GROUP}"]:
            raise RuntimeError(f"shared folder permissions wrong: {got}")
        users = list(self.users)
        for u in users:
            if self._as(u, "umask").stdout.decode().strip() != "0007":
                raise RuntimeError(f"umask 0007 not in effect for {u} (does the image's /etc/profile read "
                                   f"/etc/profile.d?)")
            if self._as(u, f"touch {TASKS}/.probe_{u}").returncode == 0:
                raise RuntimeError(f"{u} can create folders in {TASKS}")
        if len(users) >= 2:
            a, b = users[0], users[1]
            probe = f"{ROOT}/.swarm_probe_{a}"
            try:
                if self._as(a, f"echo a > {probe}").returncode != 0:
                    raise RuntimeError(f"{a} cannot write {ROOT}")
                if self._as(b, f"cat {probe} && echo b >> {probe}").returncode != 0:
                    raise RuntimeError(f"{b} cannot read and write a file of {a} in {ROOT}")
                if self._as(b, f"rm -f {probe}; test -e {probe}").returncode != 0:
                    raise RuntimeError(f"{b} could delete a file of {a} (sticky bit missing)")
            finally:
                self._root(["rm", "-f", "--", probe])
        for a in users:
            for b in users:
                if a != b and self._as(a, f"ls {home_for(b)}").returncode == 0:
                    raise RuntimeError(f"{a} can list {home_for(b)}")

    # ---- listing -----------------------------------------------------------------------------------------
    def _lister(self, args: list[str], timeout: float = 60) -> dict:
        r = self.box._run(self.box.exec_argv([self.python, "-I", "-", *args], user="root", workdir="/", stdin=True),
                          input=LISTER.encode(), timeout=timeout, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"shared folder lister failed: {r.stderr.decode('utf-8', 'replace')[-300:]}")
        return json.loads(r.stdout)

    def _list(self) -> dict:
        return self._lister(["list", self.root])

    def _fetch(self, paths: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(paths), FETCH_BATCH):
            for f in self._lister(["fetch", self.root, *paths[i:i + FETCH_BATCH]])["files"]:
                out[f["path"]] = f
        return out

    # ---- text versions on the host ---------------------------------------------------------------------
    def _save_blob(self, sha: str, data: bytes) -> None:
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        p = self.blob_dir / sha
        if not p.exists():
            p.write_bytes(data)

    def _blob_text(self, sha: Optional[str]) -> Optional[str]:
        if not sha:
            return None
        p = self.blob_dir / sha
        if not p.exists():
            return None
        return _is_text(p.read_bytes(), True)

    # ---- events ------------------------------------------------------------------------------------------
    def observe(self, step_agent: Optional[str] = None, by: str = "agent") -> list[dict]:
        """List /shared, log every change since the last listing, return the events (agent events with
        "monitored": true go to the monitors). by="harness" for the harness's own writes (never monitored)."""
        listing = self._list()
        self.truncated = listing.get("truncated", False)
        cur = {e["path"]: e for e in listing["entries"]}
        live = sorted(self.users.get(u, u) for u in listing.get("live_users", [])
                      if u in self.users and self.users[u] != step_agent)
        changed = []
        for path, e in cur.items():
            old = self.state.get(path)
            if old is None:
                changed.append(("created", path))
            elif _fingerprint(old) != _fingerprint(e):
                changed.append(("modified", path))
            else:
                e["sha256"] = old.get("sha256")
        deleted = [p for p in self.state if p not in cur]
        files = self._fetch([p for _, p in changed if cur[p]["type"] == "file"]) if changed else {}
        events = []
        now = time.time()
        for change, path in changed:
            ev = self._event(change, path, cur[path], self.state.get(path), files.get(path), step_agent, by,
                             live, now)
            if ev is not None:
                events.append(ev)
        for path in deleted:
            old = self.state[path]
            events.append(self._base("deleted", path, old, step_agent, by, live, now,
                                     content_changed=True, monitored=False))
        self.state = cur
        for ev in events:
            self.log.write(ev)
        return events

    def _base(self, change, path, e, step_agent, by, live, now, **fields) -> dict:
        ev = {"id": self.next_id, "round": self.round,
              "time": utc_now(),
              "by": by, "agent": step_agent if by == "agent" else None,
              "attribution": "uncertain" if (by == "agent" and live) else "exact", "other_live_agents": live,
              "change": change, "path": path, "type": e["type"], "owner": self.users.get(e["owner"], e["owner"]),
              "group": e["group"], "mode": oct(e["mode"]), "size": e.get("size"), **fields}
        self.next_id += 1
        return ev

    def _event(self, change, path, e, old, f, step_agent, by, live, now) -> Optional[dict]:
        header = f"{change} {path}"
        if e["type"] == "symlink":
            header += f" -> {e.get('target')}"
        if e["type"] != "file":
            content_changed = change == "created" or (old or {}).get("target") != e.get("target") \
                or (old or {}).get("type") != e["type"]
            ev = self._base(change, path, e, step_agent, by, live, now, content_changed=content_changed,
                            target=e.get("target"), diff=header, truncated=False, added_text=header,
                            monitored=by == "agent" and content_changed)
            return ev
        if f is None or f.get("error"):
            return self._base(change, path, e, step_agent, by, live, now, content_changed=None,
                              error=(f or {}).get("error", "not fetched"), diff=header, truncated=False,
                              added_text=header, monitored=by == "agent")
        sha = f.get("sha256")
        e["sha256"] = sha
        old_sha = (old or {}).get("sha256")
        content_changed = change == "created" or sha != old_sha or sha is None
        head = base64.b64decode(f.get("head_b64", ""))
        complete = bool(f.get("complete"))
        text = _is_text(head, complete)
        fields = {"sha256": sha, "content_changed": content_changed, "binary": text is None, "complete": complete}
        if content_changed:
            self.last_modified[path] = {"by": by, "agent": step_agent if by == "agent" else None,
                                        "event_id": self.next_id, "sha256": sha}
        if not content_changed:
            return self._base(change, path, e, step_agent, by, live, now, **fields, diff=header, truncated=False,
                              added_text="", monitored=False)
        if text is None:
            body = f"(binary file, {f.get('size')} bytes, sha256 {sha})"
            fields["binary_text"] = head.decode("latin-1")       # for labels: words in the raw bytes
            diff, added = body, ""
        else:
            if complete and sha:
                self._save_blob(sha, head)
            old_text = self._blob_text(old_sha) if change == "modified" else ""
            if complete and old_text is not None:
                lines = list(difflib.unified_diff(old_text.splitlines(keepends=True), text.splitlines(keepends=True),
                                                  fromfile=path, tofile=path, n=1))
                diff = "".join(ln if ln.endswith("\n") else ln + "\n" for ln in lines)
                added = "".join(ln[1:] for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
            else:
                note = "" if complete else f"(first {len(head)} bytes of {f.get('size')})\n"
                diff, added = note + text, text
        monitor_text = header + "\n" + diff
        truncated = len(monitor_text) > MONITOR_CAP
        return self._base(change, path, e, step_agent, by, live, now, **fields, diff=monitor_text[:MONITOR_CAP],
                          truncated=truncated, added_text=path + "\n" + added, monitored=by == "agent")

    def file_info(self, path: str) -> dict:
        """sha256 and last modifier (from the event log) of a file in /shared."""
        lm = self.last_modified.get(path, {})
        return {"sha256": (self.state.get(path) or {}).get("sha256"), "last_modified_by": lm.get("agent"),
                "last_modified_by_harness": lm.get("by") == "harness", "last_event_id": lm.get("event_id")}

    def remove(self, folders: list[str]) -> None:
        """Remove task folders (harness housekeeping at round start). rm never follows symlinks."""
        for d in folders:
            if not d.startswith(TASKS + "/") or ".." in d:
                raise ValueError(f"refusing to remove {d!r}")
            self._root(["rm", "-rf", "--one-file-system", "--", d], check=False)


class SharedWorkspace(ContainerWorkspace):
    """Task folders: simple tasks in /shared/tasks/<agent>_r<N>_<seq>/ (place="shared"), group tasks in the
    agent's home as before (place="home"). Every harness write is logged as a harness event (not monitored)."""

    def __init__(self, box, shared: SharedFolder):
        super().__init__(box)
        self.shared = shared
        self.created: list[tuple[int, str]] = []    # (round, folder) for the round-start cleanup

    @staticmethod
    def _shared_name(agent: str, dirname: str) -> str:
        _check_name(dirname, "folder name")
        return f"{user_for(agent)}_{dirname[5:] if dirname.startswith('task_') else dirname}"

    def task_dir(self, agent: str, dirname: str, place: str = "home") -> str:
        if place == "shared":
            return f"{TASKS}/{self._shared_name(agent, dirname)}"
        return super().task_dir(agent, dirname)

    def put_files(self, agent: str, dirname: str, files: dict[str, bytes], place: str = "home") -> str:
        if place != "shared":
            return super().put_files(agent, dirname, files)
        user = user_for(agent)
        d = self.task_dir(agent, dirname, place)
        self.box._run(self.box.exec_argv(["install", "-d", "-o", user, "-g", GROUP, "-m", "2770", d], user="root"))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for fname, data in files.items():
                ti = tarfile.TarInfo(_check_name(fname, "file name"))
                ti.size, ti.mode, ti.mtime = len(data), 0o660, int(time.time())
                tar.addfile(ti, io.BytesIO(data))
        # Extracted and made group-writable by the agent's own user (not root), inside the fresh folder.
        self.box._run(self.box.exec_argv(["tar", "-x", "-f", "-", "--no-same-owner"], user=user, workdir=d,
                                         stdin=True), input=buf.getvalue(), timeout=120)
        self.box._run(self.box.exec_argv(["chmod", "-R", "g+rwX", "--", d], user=user))
        self.created.append((self.shared.round, d))
        self.shared.observe(by="harness")
        return d

    def read_file(self, agent: str, dirname: str, fname: str, max_bytes: int, place: str = "home") -> bytes:
        if place != "shared":
            return super().read_file(agent, dirname, fname, max_bytes)
        path = f"{self.task_dir(agent, dirname, place)}/{_check_name(fname, 'file name')}"
        # As the agent's user, a regular file only, never through a symlink (another agent could plant one).
        code = ("import os,stat,sys\n"
                "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)\n"
                "assert stat.S_ISREG(os.fstat(fd).st_mode)\n"
                "sys.stdout.buffer.write(os.read(fd,int(sys.argv[2])))\n")
        r = self.box._run(self.box.exec_argv([self.shared.python, "-I", "-c", code, path, str(max_bytes)],
                                             user=user_for(agent)), check=False)
        if r.returncode != 0:
            raise FileNotFoundError(path)
        return r.stdout

    def file_info(self, agent: str, dirname: str, fname: str, place: str = "home") -> dict:
        if place != "shared":
            return {}
        return self.shared.file_info(f"{self.task_dir(agent, dirname, place)}/{fname}")

    def begin_round(self, round_no: int) -> None:
        """Remove the earlier rounds' task folders (logged as harness events), then start the new round."""
        old = [d for r, d in self.created if r < round_no]
        self.shared.remove(old)
        self.created = [(r, d) for r, d in self.created if r >= round_no]
        self.shared.observe(by="harness")
        self.shared.round = round_no


"""Test helpers: a fake UnleashX FTP server, and builders for fake Xbox programs and disc images.

Nothing here touches a real Xbox or needs real game files.
"""
from __future__ import annotations

import ftplib
import struct
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SECTOR = 2048
MAGIC = b"MICROSOFT*XBOX*MEDIA"


# ---------------------------------------------------------------- fake Xbox program


def make_xbe(title: str = "Test Game", title_id: int = 0x54450001, region: int = 0x1,
             size: int = 8192) -> bytes:
    """A minimal default.xbe with just the header fields the scripts read."""
    base, cert = 0x10000, 0x200
    b = bytearray(max(size, cert + 0xB0))
    b[0:4] = b"XBEH"
    struct.pack_into("<I", b, 0x104, base)          # base address
    struct.pack_into("<I", b, 0x118, base + cert)   # certificate address
    struct.pack_into("<I", b, cert + 0x08, title_id)
    name = title.encode("utf-16le")[:80]
    b[cert + 0x0C:cert + 0x0C + len(name)] = name
    struct.pack_into("<I", b, cert + 0xA0, region)
    struct.pack_into("<I", b, cert + 0xA8, 0)       # disc number
    struct.pack_into("<I", b, cert + 0xAC, 1)       # version
    return bytes(b)


# ---------------------------------------------------------------- fake Xbox disc image


def build_xiso(path: Path, files: dict[str, bytes], base: int = 0) -> Path:
    """Write an XDVDFS image containing `files` ("dir/sub/name" -> bytes).

    base=0 makes a trimmed image; 0x2080000 etc. puts it where full dumps keep the game data.
    Directory tables are built as a simple right-linked chain, which the format allows.
    """
    tree: dict = {}
    for rel, data in files.items():
        node = tree
        parts = rel.split("/")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = data

    out = bytearray()
    next_sector = 33  # sectors 0-31 unused, 32 = volume descriptor

    def alloc(nbytes: int) -> int:
        nonlocal next_sector
        s = next_sector
        next_sector += max(1, -(-nbytes // SECTOR))
        return s

    chunks: list[tuple[int, bytes]] = []

    def write_dir(node: dict) -> tuple[int, int]:
        if not node:
            return 0, 0
        names = sorted(node, key=str.lower)
        # entry sizes first, so right-pointers can be filled in
        sizes = [(14 + len(n.encode("latin-1")) + 3) // 4 * 4 for n in names]
        offsets = [sum(sizes[:i]) for i in range(len(names))]
        table = bytearray(sum(sizes))
        for i, n in enumerate(names):
            child = node[n]
            if isinstance(child, dict):
                sec, size = write_dir(child)
                attr = 0x10
            else:
                sec, size, attr = alloc(len(child)), len(child), 0x20
                chunks.append((sec, child))
            right = offsets[i + 1] // 4 if i + 1 < len(names) else 0
            nb = n.encode("latin-1")
            struct.pack_into("<HHIIBB", table, offsets[i], 0, right, sec, size, attr, len(nb))
            table[offsets[i] + 14: offsets[i] + 14 + len(nb)] = nb
        sec = alloc(len(table))
        chunks.append((sec, bytes(table)))
        return sec, len(table)

    root_sec, root_size = write_dir(tree)
    vd = bytearray(SECTOR)
    vd[0:20] = MAGIC
    struct.pack_into("<IIQ", vd, 20, root_sec, root_size, 0x01C2950000000000)
    vd[SECTOR - 20:] = MAGIC
    chunks.append((32, bytes(vd)))
    out = bytearray(next_sector * SECTOR)
    for sec, data in chunks:
        out[sec * SECTOR: sec * SECTOR + len(data)] = data
    with open(path, "wb") as fh:
        if base:
            fh.seek(base)
        fh.write(out)
    return path


# ---------------------------------------------------------------- fake UnleashX FTP server


class FakeFS:
    """Shared state for every connection to the fake Xbox."""

    def __init__(self, free_mb: float = 1000.0):
        self.files: dict[str, bytes] = {}
        self.dirs = {"/", "/E"}
        self.fail_stor_once: set[str] = set()   # names whose first STOR drops the connection
        self.stor_calls = 0
        self.free_mb = free_mb
        self.users = {("anonymous", "guest@"): "ro", ("xbox", "xbox"): "rw"}
        self.lock = threading.Lock()

    def add_dir(self, path: str) -> None:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            self.dirs.add("/" + "/".join(parts[:i]))

    def add_file(self, path: str, data: bytes) -> None:
        self.add_dir(path.rsplit("/", 1)[0])
        self.files[path] = data


class FakeFTP:
    """Behaves like UnleashX for what the scripts use - including ignoring LIST paths."""

    def __init__(self, fs: FakeFS):
        self.fs, self.cwdp, self.mode = fs, "/", None

    # session
    def login(self, user, pw):
        mode = self.fs.users.get((user, pw))
        if not mode:
            raise ftplib.error_perm("530 Login incorrect.")
        self.mode = mode

    def set_pasv(self, v): pass
    def quit(self): pass
    def close(self): pass

    def getwelcome(self):
        return (f"220-Drive C\t Used: 3.00MB\tFree: 481.00MB\tTotal: 484.92MB\n"
                f"220-Drive E\t Used: 100.00MB\tFree: {self.fs.free_mb:,.2f}MB\tTotal: 4,881.63MB\n"
                f"220 UnleashX FTP Server ready.")

    # paths (case-insensitive, like FATX)
    def _abs(self, p):
        p = p if p.startswith("/") else self.cwdp.rstrip("/") + "/" + p
        parts = []
        for x in p.split("/"):
            if x in ("", "."):
                continue
            if x == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(x)
        return "/" + "/".join(parts)

    def _find_dir(self, p):
        return next((d for d in self.fs.dirs if d.lower() == p.lower()), None)

    def _need_rw(self):
        if self.mode != "rw":
            raise ftplib.error_perm("550 Permission denied.")

    def cwd(self, p):
        d = self._find_dir(self._abs(p))
        if d is None:
            raise ftplib.error_perm("550 No such directory.")
        self.cwdp = d

    def pwd(self):
        return self.cwdp

    def mkd(self, name):
        self._need_rw()
        with self.fs.lock:
            p = self._abs(name)
            if self._find_dir(p) is not None:
                raise ftplib.error_perm("550 Directory already exists.")
            time.sleep(0.002)  # widen the window for two connections racing
            self.fs.dirs.add(p)

    def rmd(self, name):
        self._need_rw()
        p = self._find_dir(self._abs(name))
        if p is None:
            raise ftplib.error_perm("550 No such directory.")
        pre = p.lower() + "/"
        if any(f.lower().startswith(pre) for f in self.fs.files) or \
           any(d.lower().startswith(pre) for d in self.fs.dirs):
            raise ftplib.error_perm("550 Directory not empty.")
        self.fs.dirs.discard(p)

    def delete(self, name):
        self._need_rw()
        p = self._abs(name)
        for f in list(self.fs.files):
            if f.lower() == p.lower():
                del self.fs.files[f]
                return
        raise ftplib.error_perm("550 No such file.")

    def retrlines(self, cmd, cb):
        if cmd != "LIST":
            raise AssertionError("UnleashX ignores LIST paths - scripts must CWD first, then plain LIST")
        cb("drw-r--r--   1 XBOX      XBOX        0 Nov 15 08:00 ..")
        base = self.cwdp.rstrip("/") + "/"
        for d in sorted(self.fs.dirs):
            rest = d[len(base):]
            if d.lower().startswith(base.lower()) and rest and "/" not in rest:
                cb(f"drw-r--r--   1 XBOX      XBOX        0 Jan 01 00:00 {rest}")
        for f, b in sorted(self.fs.files.items()):
            rest = f[len(base):]
            if f.lower().startswith(base.lower()) and "/" not in rest:
                cb(f"-rw-r--r--   1 XBOX      XBOX        {len(b)} Jan 01 00:00 {rest}")

    def storbinary(self, cmd, fh, blocksize=8192, callback=None):
        self._need_rw()
        name = cmd[5:]
        with self.fs.lock:
            self.fs.stor_calls += 1
        data = b""
        while True:
            blk = fh.read(blocksize)
            if not blk:
                break
            data += blk
            time.sleep(0.0005)
            if callback:
                callback(blk)
            if name in self.fs.fail_stor_once and len(data) >= blocksize:
                with self.fs.lock:
                    self.fs.fail_stor_once.discard(name)
                raise ConnectionResetError("simulated dropped connection")
        self.fs.files[self._abs(name)] = data


def fake_connect(fs: FakeFS):
    """A drop-in for push_stage.connect that talks to `fs` instead of the network."""
    def connect(ip, user, password):
        f = FakeFTP(fs)
        f.login(user, password)
        return f
    return connect

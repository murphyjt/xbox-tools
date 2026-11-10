#!/usr/bin/env python3
"""Read Xbox disc images (XDVDFS) directly - trimmed .xiso files and full-size dumps.

Run it to see what's in an image:
    xbox-info "Panzer Dragoon Orta (USA).xiso.iso"          # summary
    xbox-info "Panzer Dragoon Orta (USA).xiso.iso" --list   # plus every file

Or use it from other scripts:
    img = XisoImage("Panzer Dragoon Orta (USA).xiso.iso")
    for f in img.files():            # XisoFile(path="st06/map/x.mdb", size=..., offset=...)
        with img.open(f) as fh: ...  # a read-only file-like object for that one file

Also has the shared helpers for naming a game from its default.xbe.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

SECTOR = 2048
MAGIC = b"MICROSOFT*XBOX*MEDIA"
# Where the Xbox file system starts inside the image: trimmed xiso, then full dumps.
PARTITIONS = {0: "trimmed image", 0x18300000: "full image (XGD1)",
              0xFD90000: "full image (XGD2)", 0x2080000: "full image (XGD3)"}
ATTR_DIR = 0x10
FATX_MAX = 42
FATX_BAD = set('"*+,/:;<=>?[\\]|') | {chr(c) for c in range(32)}
REGIONS = {0x1: "North America", 0x2: "Japan", 0x4: "Europe/Australia"}


class NotXboxImage(ValueError):
    pass


@dataclass(frozen=True)
class XisoFile:
    path: str        # inside the image, "/" separated, e.g. "st06/map/st06_zone01_03b.mdb"
    size: int
    offset: int      # absolute byte offset of the file's data in the image


class _Slice(io.RawIOBase):
    """Read-only view of one file's bytes inside the image."""

    def __init__(self, image: Path, offset: int, size: int):
        self._fh = open(image, "rb")
        self._fh.seek(offset)
        self._left = size

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self._left <= 0:
            return 0
        n = self._fh.readinto(memoryview(b)[: min(len(b), self._left)])
        self._left -= n or 0
        return n or 0

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()


class XisoImage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with open(self.path, "rb") as fh:
            for base, kind in PARTITIONS.items():
                fh.seek(base + 32 * SECTOR)
                vd = fh.read(SECTOR)
                if vd[:20] == MAGIC and vd[-20:] == MAGIC:
                    self.base, self.kind = base, kind
                    self._root_sector, self._root_size = struct.unpack_from("<II", vd, 20)
                    ft = struct.unpack_from("<Q", vd, 28)[0]   # Windows FILETIME of the image
                    try:
                        self.created = (_dt.datetime(1601, 1, 1, tzinfo=_dt.timezone.utc)
                                        + _dt.timedelta(microseconds=ft // 10)) if ft else None
                    except OverflowError:
                        self.created = None
                    break
            else:
                raise NotXboxImage(f"{self.path.name} isn't an Xbox disc image (no XDVDFS volume found)")
        self._files: list[XisoFile] | None = None
        self._dirs: list[str] | None = None

    # -- directory tables are binary trees of entries; walk them all
    def _walk(self) -> None:
        files: list[XisoFile] = []
        dirs: list[str] = []
        with open(self.path, "rb") as fh:
            stack = [("", self._root_sector, self._root_size)]
            while stack:
                prefix, sector, size = stack.pop()
                if size == 0:
                    continue
                fh.seek(self.base + sector * SECTOR)
                table = fh.read(size)
                todo, seen = [0], set()
                while todo:
                    off = todo.pop()
                    if off in seen or off + 14 > len(table):
                        continue
                    seen.add(off)
                    left, right, esec, esize, attr, nlen = struct.unpack_from("<HHIIBB", table, off)
                    if left == 0xFFFF:  # padding up to the next sector
                        continue
                    name = table[off + 14: off + 14 + nlen].decode("latin-1")
                    if left:
                        todo.append(left * 4)
                    if right:
                        todo.append(right * 4)
                    path = f"{prefix}{name}"
                    if attr & ATTR_DIR:
                        dirs.append(path)
                        stack.append((path + "/", esec, esize))
                    else:
                        files.append(XisoFile(path, esize, self.base + esec * SECTOR))
        self._files = sorted(files, key=lambda f: f.path.lower())
        self._dirs = sorted(dirs, key=str.lower)

    def files(self) -> list[XisoFile]:
        if self._files is None:
            self._walk()
        return list(self._files)  # type: ignore[arg-type]

    def dirs(self) -> list[str]:
        if self._dirs is None:
            self._walk()
        return list(self._dirs)  # type: ignore[arg-type]

    def open(self, f: XisoFile) -> io.BufferedReader:
        return io.BufferedReader(_Slice(self.path, f.offset, f.size), buffer_size=256 * 1024)

    def read(self, f: XisoFile) -> bytes:
        with self.open(f) as fh:
            return fh.read()

    def find(self, path: str) -> XisoFile | None:
        p = path.lower()
        return next((f for f in self.files() if f.path.lower() == p), None)


# ---------------------------------------------------------------- shared naming helpers


def xbe_info(data: bytes) -> tuple[str, int, int]:
    """(title, title ID, region bits) from the start of a default.xbe."""
    if data[:4] != b"XBEH":
        raise ValueError("default.xbe isn't a valid Xbox program")
    base = struct.unpack_from("<I", data, 0x104)[0]
    cert = struct.unpack_from("<I", data, 0x118)[0] - base
    title_id = struct.unpack_from("<I", data, cert + 8)[0]
    title = data[cert + 0xC:cert + 0xC + 80].decode("utf-16le", "ignore").split("\0")[0].strip()
    region = struct.unpack_from("<I", data, cert + 0xA0)[0]
    return title, title_id, region


def xbe_details(data: bytes) -> dict:
    """More from the XBE certificate: publisher code, disc number and version."""
    base = struct.unpack_from("<I", data, 0x104)[0]
    cert = struct.unpack_from("<I", data, 0x118)[0] - base
    title_id = struct.unpack_from("<I", data, cert + 8)[0]
    pub = bytes([title_id >> 24 & 0xFF, title_id >> 16 & 0xFF])
    return {"publisher": pub.decode("latin-1") if pub.isalnum() else "?",
            "game_number": title_id & 0xFFFF,
            "disc": struct.unpack_from("<I", data, cert + 0xA8)[0],
            "version": struct.unpack_from("<I", data, cert + 0xAC)[0]}


def region_names(region: int) -> str:
    return ", ".join(v for k, v in REGIONS.items() if region & k) or f"0x{region:X}"


def fatx_name(s: str) -> str:
    """Make a folder name the Xbox's file system accepts (42 chars, no special characters)."""
    s = "".join("-" if c in FATX_BAD else c for c in s).strip(" .")
    s = re.sub(r"\s+", " ", s)
    return s[:FATX_MAX].rstrip(" .") or "Game"


def too_long_names(paths: list[str]) -> list[str]:
    return sorted({part for p in paths for part in p.split("/") if len(part) > FATX_MAX})


# ---------------------------------------------------------------- command line: show info


XBOX_CLUSTER = 16 * 1024  # the Xbox hard drive stores files in 16 KB clusters


def _h(n: int) -> str:
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return f"{n / size:,.2f} {unit}"
    return f"{n:,} bytes"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Show what's inside an Xbox disc image (.iso / .xiso).")
    ap.add_argument("image", help="path to the image")
    ap.add_argument("--list", action="store_true", help="also list every file with its size")
    args = ap.parse_args(argv)
    path = Path(args.image).expanduser()
    if not path.is_file():
        print(f"File not found: {path}")
        return 1
    try:
        img = XisoImage(path)
    except NotXboxImage as e:
        print(e)
        return 1

    files, dirs = img.files(), img.dirs()
    upd = [f for f in files if f.path.split("/")[0].lower() == "$systemupdate"]
    game = [f for f in files if f not in upd]
    game_dirs = [d for d in dirs if d.split("/")[0].lower() != "$systemupdate"]
    data = sum(f.size for f in game)
    on_xbox = sum(-(-f.size // XBOX_CLUSTER) * XBOX_CLUSTER for f in game) + (len(game_dirs) + 1) * XBOX_CLUSTER
    image_size = path.stat().st_size

    print(f"{path.name}")
    print(f"  Image type    Xbox {img.kind}" + (f" (game data starts at offset 0x{img.base:X})" if img.base else ""))
    print(f"  Image size    {_h(image_size)}  ({image_size:,} bytes)")
    if img.created:
        print(f"  Image made    {img.created:%Y-%m-%d %H:%M} UTC")

    xbe = img.find("default.xbe")
    if xbe:
        with img.open(xbe) as fh:
            head = fh.read(65536)
        try:
            title, tid, region = xbe_info(head)
            d = xbe_details(head)
            print(f"  Game          {title}")
            print(f"  Title ID      {tid:08X}  (publisher code {d['publisher']}, game #{d['game_number']})")
            print(f"  Region        {region_names(region)}")
            print(f"  Disc / ver.   disc {d['disc'] + 1}, version {d['version']}")
            print(f"  Folder name   {fatx_name(title)}  (what xbox-stage / xbox-push would use)")
        except ValueError as e:
            print(f"  default.xbe   {e}")
    else:
        print("  default.xbe   missing - the Xbox couldn't launch this")

    print(f"  Contents      {len(game):,} files in {len(game_dirs):,} folders")
    print(f"  Game data     {_h(data)}  ({data:,} bytes)  <- size once unpacked on a PC")
    print(f"  On the Xbox   about {_h(on_xbox)}  (files rounded up to 16 KB clusters)")
    overhead = image_size - data - sum(f.size for f in upd)
    note = "video section + padding of a full dump" if img.base else "sector alignment + directory tables"
    print(f"  Image extra   {_h(max(overhead, 0))}  ({note})")
    if upd:
        print(f"  $SystemUpdate {len(upd)} files, {_h(sum(f.size for f in upd))}  (skipped when staging/uploading)")
    long = too_long_names([f.path for f in game] + game_dirs)
    if long:
        print(f"  Name problem  {len(long)} name(s) longer than {FATX_MAX} characters: {', '.join(long[:3])}")
    else:
        print(f"  Names         all fit the Xbox's {FATX_MAX}-character limit")
    print("  Biggest files " + ", ".join(f"{f.path} ({_h(f.size)})" for f in sorted(game, key=lambda f: -f.size)[:3]))

    if args.list:
        print()
        for f in game + upd:
            print(f"  {f.size:>14,}  {f.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

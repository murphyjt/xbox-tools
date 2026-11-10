"""Upload a staging folder, or a game straight from its disc image, to the Xbox over FTP.

    <stage>/E/Games/X/...            ->  E:\\Games\\X\\...
    "My Game (USA).iso" / .xiso      ->  E:\\Games\\<game's title>\\...   (no extracting needed)

- Skips files already on the Xbox with the same size, so a rerun resumes.
- Stops if it can't check a folder (e.g. UnleashX isn't on screen) instead of guessing.
- Checks the Xbox has room before asking for the login, so it never fails half-way
  through for lack of space (--ignore-space to override).
- Uploads over 2 FTP connections at once by default (--connections 1-4), with a live
  progress line that updates during each file, and automatic reconnect-and-retry.

Usage (from PowerShell or any terminal, in the xbox folder):
    xbox-push <stage-folder>              # upload
    xbox-push "C:\\...\\My Game.iso"        # upload a game straight from its image
    xbox-push "C:\\...\\My Game.iso" --name "My Game"   # choose the folder name
    xbox-push <stage-folder or image> --dry-run         # check only, no login needed
    xbox-push <stage-folder> --ip 192.168.137.2 --verify
    xbox-push xbmc-stage --exclude "*FileZilla Server.xml"   # skip files the app rewrites

Only UnleashX runs an FTP server - games, XBMC and the MS dashboard don't.
"""
from __future__ import annotations

import argparse
import fnmatch
import ftplib
import getpass
import os
import queue
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

import xiso

DEFAULT_IP = "192.168.137.2"
RETRIES = 3
BLOCK = 64 * 1024
CLUSTER = 16 * 1024          # FATX allocates space in 16 KB clusters on the Xbox's drives
SPACE_MARGIN = 1024 * 1024   # keep at least 1 MB spare
NET_ERRORS = (OSError, EOFError, ftplib.error_temp, ftplib.error_reply)

# ---------------------------------------------------------------- display


class Display:
    """One self-updating status line when writing to a terminal; plain lines otherwise."""

    def __init__(self) -> None:
        self.tty = sys.stdout.isatty()
        if self.tty and os.name == "nt":
            os.system("")  # turns on ANSI colour handling in the Windows console
        c = (lambda code: f"\x1b[{code}m") if self.tty else (lambda code: "")
        self.green, self.red, self.cyan, self.dim, self.bold, self.reset = (
            c(32), c(31), c(36), c(2), c(1), c(0))
        self._last = 0.0

    def status(self, text: str, force: bool = False) -> None:
        if not self.tty:
            return
        now = time.monotonic()
        if not force and now - self._last < 0.1:  # redraw at most 10x a second
            return
        self._last = now
        width = shutil.get_terminal_size((100, 20)).columns - 1
        visible = _strip_ansi(text)
        if len(visible) > width:  # trim from the end, keeping colour codes intact
            text = _truncate_ansi(text, width - 1) + "…" + self.reset
        sys.stdout.write("\r\x1b[2K" + text)
        sys.stdout.flush()

    def line(self, text: str = "") -> None:
        if self.tty:
            sys.stdout.write("\r\x1b[2K")
        print(text, flush=True)


def _strip_ansi(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "\x1b":
            i = s.index("m", i) + 1
        else:
            out.append(s[i]); i += 1
    return "".join(out)


def _truncate_ansi(s: str, n: int) -> str:
    out, i, seen = [], 0, 0
    while i < len(s) and seen < n:
        if s[i] == "\x1b":
            j = s.index("m", i) + 1; out.append(s[i:j]); i = j
        else:
            out.append(s[i]); i += 1; seen += 1
    return "".join(out)


def mb(n: int) -> str:
    return f"{n / 1048576:.1f}"


def human(n: int) -> str:
    return f"{n / 1073741824:.2f} GB" if abs(n) >= 1073741824 else f"{n / 1048576:.0f} MB"


def bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    return "#" * filled + "." * (width - filled)


# ---------------------------------------------------------------- FTP helpers


def connect(ip: str, user: str, password: str) -> ftplib.FTP:
    ftp = ftplib.FTP(ip, timeout=60)
    ftp.login(user, password)
    ftp.set_pasv(True)
    return ftp


def list_folder(ftp: ftplib.FTP, remote_dir: str) -> dict[str, int] | None:
    """Return {lowercase name: size} for files in remote_dir, or None if it doesn't exist.

    UnleashX ignores a path given to LIST, so change into the folder first.
    """
    try:
        ftp.cwd("/" + remote_dir)
    except ftplib.error_perm:
        return None  # 550: folder isn't on the Xbox yet
    if ftp.pwd().strip("/").lower() != remote_dir.strip("/").lower():
        raise RuntimeError(f"Xbox put us in {ftp.pwd()!r} instead of {remote_dir!r}")
    lines: list[str] = []
    ftp.retrlines("LIST", lines.append)
    files: dict[str, int] = {}
    for ln in lines:
        parts = ln.split(None, 8)
        if len(parts) == 9 and ln.startswith("-"):
            files[parts[8].lower()] = int(parts[4])
    return files


def ensure_dir(ftp: ftplib.FTP, remote_dir: str) -> None:
    """Change into remote_dir, creating any missing folders on the way.

    With several connections, another one may create the same folder first,
    so a failed MKD is fine as long as we can then change into the folder.
    """
    ftp.cwd("/")
    for part in PurePosixPath(remote_dir).parts:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            try:
                ftp.mkd(part)
            except ftplib.error_perm:
                pass
            ftp.cwd(part)


# ---------------------------------------------------------------- main steps


@dataclass
class Item:
    rel: str                          # Xbox path, e.g. "E/Games/Panzer Dragoon Orta/st06/map/x.mdb"
    size: int
    opener: Callable[[], BinaryIO]    # opens this file's bytes (from a staging folder or an image)


def scan_stage(stage: Path) -> tuple[list[str], list[Item]]:
    dirs = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_dir())
    files = sorted(
        (Item(p.relative_to(stage).as_posix(), p.stat().st_size, lambda p=p: open(p, "rb"))
         for p in stage.rglob("*") if p.is_file()),
        key=lambda it: it.rel)
    return dirs, files


def scan_image(img: xiso.XisoImage, folder: str, name: str) -> tuple[list[str], list[Item]]:
    """Map an image's files to <folder>/<name>/... on the Xbox, leaving out $SystemUpdate."""
    root = f"{folder}/{name}"
    parts = root.split("/")
    dirs = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    dirs += [f"{root}/{d}" for d in img.dirs() if d.split("/")[0].lower() != "$systemupdate"]
    files = [Item(f"{root}/{f.path}", f.size, lambda f=f: img.open(f))
             for f in img.files() if f.path.split("/")[0].lower() != "$systemupdate"]
    return sorted(dirs, key=str.lower), sorted(files, key=lambda it: it.rel)


@dataclass
class XboxState:
    missing: list[Item]          # files that still need sending
    replaced: dict[str, int]     # rel path -> size of the old copy being overwritten
    new_dirs: list[str]          # folders that don't exist on the Xbox yet
    free: dict[str, int]         # drive letter -> free bytes, from the FTP welcome banner


def parse_free(welcome: str) -> dict[str, int]:
    """UnleashX's banner has lines like 'Drive E  Used: 3,672.88MB  Free: 1,208.75MB'."""
    return {m.group(1).upper(): int(float(m.group(2).replace(",", "")) * 1048576)
            for m in re.finditer(r"Drive ([A-Z])\s+Used:\s*[\d,.]+MB\s+Free:\s*([\d,.]+)MB", welcome or "")}


def find_missing(ip: str, dirs: list[str], files: list[Item], ui: Display) -> XboxState:
    ftp = connect(ip, "anonymous", "guest@")  # read-only check, no password needed
    free = parse_free(ftp.getwelcome())
    remote: dict[str, dict[str, int] | None] = {}
    try:
        for i, d in enumerate(dirs, 1):
            ui.status(f"{ui.cyan}Checking Xbox folders{ui.reset} {i}/{len(dirs)}  {ui.dim}{d}{ui.reset}", force=True)
            remote[d.lower()] = list_folder(ftp, d)
    finally:
        try:
            ftp.quit()
        except NET_ERRORS:
            pass
    missing, replaced = [], {}
    for it in files:
        folder, name = it.rel.rsplit("/", 1) if "/" in it.rel else ("", it.rel)
        have = remote.get(folder.lower()) or {}
        old = have.get(name.lower())
        if old != it.size:
            missing.append(it)
            if old is not None:
                replaced[it.rel] = old
    new_dirs = [d for d in dirs if remote.get(d.lower()) is None]
    return XboxState(missing, replaced, new_dirs, free)


def space_needed(state: XboxState) -> dict[str, int]:
    """Extra bytes each drive needs, counting whole 16 KB clusters like FATX does."""
    def on_disk(n: int) -> int:
        return -(-n // CLUSTER) * CLUSTER
    need: dict[str, int] = {}
    for it in state.missing:
        drive = it.rel.split("/", 1)[0].upper()
        need[drive] = need.get(drive, 0) + on_disk(it.size) - on_disk(state.replaced.get(it.rel, 0))
    for d in state.new_dirs:
        drive = d.split("/", 1)[0].upper()
        if "/" in d:  # the drive's root folder always exists
            need[drive] = need.get(drive, 0) + CLUSTER
    return need


def check_space(state: XboxState, ui: Display) -> bool:
    """Print whether the upload fits; returns False if any drive is too full."""
    ok = True
    for drive, need in sorted(space_needed(state).items()):
        free = state.free.get(drive)
        if free is None:
            ui.line(f"{ui.dim}Couldn't read the free space on {drive}: - uploading without a space check.{ui.reset}")
            continue
        if need <= 0:
            continue
        if need > free - SPACE_MARGIN:
            short = need - (free - SPACE_MARGIN)
            ui.line(f"{ui.red}Won't fit on {drive}:{ui.reset} needs about {human(need)}, the Xbox has {human(free)} free.")
            print(f"Free up about {human(short)} first (xbox-remove), then run this again.")
            ok = False
        else:
            ui.line(f"{ui.green}Fits on {drive}:{ui.reset} needs about {human(need)} of {human(free)} free.")
    return ok


def upload(ip: str, user: str, password: str, todo: list[Item], ui: Display, conns: int = 2) -> bool:
    """Upload todo over `conns` parallel FTP connections. Returns True if every file made it."""
    total = sum(it.size for it in todo)
    conns = max(1, min(conns, len(todo)))
    # Log in on every connection first, so a wrong password fails before anything is sent.
    ftps = [connect(ip, user, password) for _ in range(conns)]

    work: queue.Queue[Item] = queue.Queue()
    for it in todo:
        work.put(it)
    lock = threading.Lock()
    state = {"sent": 0, "done": 0}
    current: dict[int, str] = {}        # worker -> file name being sent
    notices: list[str] = []             # messages for the main thread to print
    errors: list[str] = []
    stop = threading.Event()

    def worker(w: int) -> None:
        ftp = ftps[w]
        try:
            while not stop.is_set():
                try:
                    it = work.get_nowait()
                except queue.Empty:
                    return
                folder, name = it.rel.rsplit("/", 1) if "/" in it.rel else ("", it.rel)
                for attempt in range(1, RETRIES + 1):
                    cur = 0

                    def progress(block: bytes) -> None:
                        nonlocal cur
                        cur += len(block)
                        with lock:
                            state["sent"] += len(block)

                    with lock:
                        current[w] = name
                    try:
                        ensure_dir(ftp, folder)
                        with it.opener() as fh:
                            ftp.storbinary(f"STOR {name}", fh, BLOCK, callback=progress)
                        break
                    except ftplib.error_perm as e:
                        with lock:
                            errors.append(f"The Xbox refused {it.rel}: {e}")
                        stop.set()
                        return
                    except NET_ERRORS as e:
                        with lock:
                            state["sent"] -= cur        # this attempt's bytes will be resent
                        if attempt == RETRIES or stop.is_set():
                            with lock:
                                errors.append(f"Lost the connection on {it.rel} ({e}) - gave up after {attempt} tries.")
                            stop.set()
                            return
                        with lock:
                            notices.append(f"Connection problem on {name} ({e}); reconnecting (try {attempt + 1}/{RETRIES})...")
                        time.sleep(2)
                        try:
                            ftp.close()
                        except NET_ERRORS:
                            pass
                        try:
                            ftp = connect(ip, user, password)
                        except (*NET_ERRORS, ftplib.Error):
                            pass  # next attempt fails fast and is counted
                with lock:
                    state["done"] += 1
                    current.pop(w, None)
        finally:
            try:
                ftp.quit()
            except (*NET_ERRORS, ftplib.Error):
                pass

    def show(force: bool = False) -> None:
        with lock:
            sent, done, names = state["sent"], state["done"], sorted(current.values())
            msgs = notices[:]; notices.clear()
        for m in msgs:
            ui.line(f"{ui.dim}{m}{ui.reset}")
        pct = sent * 100 / total if total else 100
        secs = max(time.monotonic() - start, 0.001)
        rate = sent / secs
        eta = (total - sent) / rate if rate > 0 else 0
        ui.status(
            f"{ui.cyan}[{bar(pct)}]{ui.reset} {pct:3.0f}%  {done}/{len(todo)} files  "
            f"{mb(sent)}/{mb(total)} MB  {rate / 1048576:.1f} MB/s  ETA {int(eta // 60)}m{int(eta % 60):02d}s  "
            f"{ui.dim}{', '.join(names)}{ui.reset}", force=force)

    start = time.monotonic()
    threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(conns)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            show()
            time.sleep(0.1)
    except KeyboardInterrupt:
        stop.set()
        raise
    show(force=True)

    if errors:
        for e in errors:
            ui.line(f"{ui.red}{e}{ui.reset}")
        return False
    secs = time.monotonic() - start
    ui.line(f"{ui.green}Upload finished:{ui.reset} {len(todo)} files, {mb(total)} MB in "
            f"{int(secs // 60)}m {int(secs % 60)}s "
            f"({total / 1048576 / max(secs, 0.001):.1f} MB/s over {conns} connection{'s' if conns > 1 else ''}).")
    return True


def load_image(path: Path, folder: str, name: str | None,
               ui: Display) -> tuple[str, str, list[str], list[Item]] | None:
    """Open an Xbox disc image and map it to <folder>/<game name> on the Xbox.

    Returns (label, root, dirs, files), or None after printing why it can't be used.
    """
    try:
        img = xiso.XisoImage(path)
    except xiso.NotXboxImage as e:
        print(f"{ui.red}{e}{ui.reset}")
        return None
    xbe = img.find("default.xbe")
    if xbe is None:
        print(f"{ui.red}There's no default.xbe at the top of {path.name}, so the Xbox couldn't launch it.{ui.reset}")
        return None
    with img.open(xbe) as fh:
        title, title_id, region = xiso.xbe_info(fh.read(65536))
    folder = folder.replace("\\", "/").replace(":", "").strip("/")
    game = xiso.fatx_name(name or title or path.stem)
    root = f"{folder}/{game}"
    dirs, files = scan_image(img, folder, game)
    too_long = xiso.too_long_names([it.rel for it in files] + dirs)
    if too_long:
        print(f"{ui.red}These names are longer than the Xbox allows ({xiso.FATX_MAX} characters):{ui.reset}")
        for n in too_long[:10]:
            print(f"  {n}")
        return None
    shown = root.split("/")[0] + ":\\" + "\\".join(root.split("/")[1:])
    print(f"{path.name}: Xbox {img.kind}. Game: {title!r} (title ID {title_id:08X}, {xiso.region_names(region)}).")
    print(f"Uploading straight from the image into {ui.bold}{shown}{ui.reset}.")
    if region and not region & 0x1:
        print(f"{ui.red}Note: this game isn't marked for North America; it may refuse to start on your NTSC console.{ui.reset}")
    return path.name, root, dirs, files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Upload a staging folder or an Xbox disc image to the Xbox over FTP.")
    ap.add_argument("source", help="staging folder laid out like the Xbox (e.g. orta-stage), "
                                   "or an Xbox disc image (.iso / .xiso) to upload straight into E:\\Games")
    ap.add_argument("--name", help="image only: folder name on the Xbox (default: the game's title)")
    ap.add_argument("--folder", default="E/Games", help="image only: Xbox folder to put the game in (default E/Games)")
    ap.add_argument("--ip", default=DEFAULT_IP, help=f"Xbox address (default {DEFAULT_IP})")
    ap.add_argument("--dry-run", action="store_true", help="only check what's missing; no login")
    ap.add_argument("--verify", action="store_true", help="re-check every file's size after uploading")
    ap.add_argument("--connections", type=int, default=2, choices=range(1, 5), metavar="1-4",
                    help="parallel uploads (default 2; UnleashX was fastest with 2 in testing)")
    ap.add_argument("--ignore-space", action="store_true",
                    help="upload even if the free-space check says it won't fit")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help='skip files matching a wildcard (case-insensitive), e.g. --exclude "*.xml"; repeatable')
    args = ap.parse_args(argv)

    src = Path(args.source).expanduser()
    ui = Display()
    root = None  # game folder on the Xbox, when uploading from an image
    if src.is_dir():
        label = src.name
        dirs, files = scan_stage(src)
    elif src.is_file():
        loaded = load_image(src, args.folder, args.name, ui)
        if loaded is None:
            return 1
        label, root, dirs, files = loaded
    else:
        print(f"Not found: {args.source} (expected a staging folder or an Xbox disc image)")
        return 1
    if args.exclude:
        kept = [it for it in files
                if not any(fnmatch.fnmatch(it.rel.lower(), p.lower()) for p in args.exclude)]
        if len(kept) < len(files):
            print(f"Excluding {len(files) - len(kept)} file(s) matching {', '.join(args.exclude)}.")
        files = kept
    if not files:
        print(f"Nothing to upload in {label}.")
        return 1

    try:
        state = find_missing(args.ip, dirs, files, ui)
    except (*NET_ERRORS, RuntimeError) as e:
        ui.line(f"{ui.red}Couldn't check the Xbox at {args.ip}: {e}{ui.reset}")
        print("Is UnleashX on screen? Games, XBMC and the MS dashboard don't run its FTP server.")
        return 1
    todo = state.missing
    if root and f"{root}/default.xbe" in state.replaced:
        shown = root.split("/")[0] + ":\\" + "\\".join(root.split("/")[1:])
        ui.line(f"{ui.red}{shown} on the Xbox already holds a different game{ui.reset} (its default.xbe doesn't match).")
        print('Use --name "Another Name" to upload this one alongside it, or remove that one first (xbox-remove).')
        print("Nothing was uploaded.")
        return 1

    need = sum(it.size for it in todo)
    ui.line(f"{len(files)} files in {label}: {ui.green}{len(files) - len(todo)} already on the Xbox{ui.reset}, "
            f"{ui.bold}{len(todo)} to upload{ui.reset} ({mb(need)} MB).")
    if not todo:
        print(f"{ui.green}Nothing to do - everything is already there.{ui.reset}")
        return 0
    if not check_space(state, ui):
        if not args.ignore_space:
            print("Nothing was uploaded.")
            return 1
        print(f"{ui.dim}--ignore-space given - uploading anyway.{ui.reset}")
    if args.dry_run:
        print("Dry run - first files that would be sent:")
        for it in todo[:5]:
            print(f"  {it.size:>10}  {it.rel}")
        return 0

    user = input("FTP user: ")
    password = getpass.getpass("FTP password: ")
    try:
        ok = upload(args.ip, user, password, todo, ui, args.connections)
    except KeyboardInterrupt:
        ui.line(f"{ui.red}Stopped.{ui.reset} Run the same command again to resume - finished files are skipped.")
        return 130
    except ftplib.error_perm as e:
        if str(e).startswith("530"):
            ui.line(f"{ui.red}The Xbox rejected the username/password.{ui.reset}")
        else:
            ui.line(f"{ui.red}The Xbox refused the login: {e}{ui.reset}")
        return 1
    except (*NET_ERRORS, ftplib.Error) as e:
        ui.line(f"{ui.red}Couldn't connect to the Xbox: {e}{ui.reset}")
        print("Is UnleashX on screen?")
        return 1
    if not ok:
        print("Run the same command again to resume - finished files are skipped.")
        return 1

    if args.verify:
        left = find_missing(args.ip, dirs, files, ui).missing
        if left:
            ui.line(f"{ui.red}Verify: {len(left)} files still missing or the wrong size.{ui.reset}")
            return 1
        ui.line(f"{ui.green}Verified: all {len(files)} files are on the Xbox at the right size.{ui.reset}")
    else:
        print("Run with --dry-run (or --verify next time) to double-check every file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

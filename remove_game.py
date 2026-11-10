"""Pick a game on the Xbox with the arrow keys and remove it to free up space.

- Lists the folders in E:\\Games with their sizes and the Xbox's free space (read-only).
- Shows whether each game also has a copy on this PC (in a *-stage folder here),
  so you know you can put it back later with push_stage.py.
- Asks y/N before deleting, then deletes with a live progress line.
- Game saves live in E:\\UDATA, not in the game folder, so they are kept.

Usage (from PowerShell, in the xbox folder):
    remove-game                       # pick from E:\\Games
    remove-game --dry-run             # pick and show what would go; no login
    remove-game --folder E/Applications --ip 192.168.137.2

Keys: Up/Down (or W/S) to move, Enter to choose, Esc or Q to quit.
Only UnleashX runs an FTP server - games, XBMC and the MS dashboard don't.
"""
from __future__ import annotations

import argparse
import ftplib
import getpass
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from push_stage import DEFAULT_IP, NET_ERRORS, Display, connect



def human(n: int) -> str:
    return f"{n / 1073741824:.2f} GB" if n >= 1073741824 else f"{n / 1048576:.0f} MB"


# ---------------------------------------------------------------- Xbox listing


def list_entries(ftp: ftplib.FTP, remote_dir: str) -> tuple[list[str], list[tuple[str, int]]]:
    """(subfolders, [(file, size)]) of remote_dir. UnleashX needs CWD then a plain LIST."""
    ftp.cwd("/" + remote_dir)
    lines: list[str] = []
    ftp.retrlines("LIST", lines.append)
    dirs, files = [], []
    for ln in lines:
        parts = ln.split(None, 8)
        if len(parts) < 9 or parts[8] in (".", ".."):
            continue
        if ln.startswith("d"):
            dirs.append(parts[8])
        elif ln.startswith("-"):
            files.append((parts[8], int(parts[4])))
    return dirs, files


@dataclass
class Tree:
    files: list[str] = field(default_factory=list)   # full remote paths
    dirs: list[str] = field(default_factory=list)    # full remote paths, parents first
    size: int = 0


def walk(ftp: ftplib.FTP, root: str) -> Tree:
    t = Tree(dirs=[root])
    stack = [root]
    while stack:
        d = stack.pop()
        subdirs, files = list_entries(ftp, d)
        for name, size in files:
            t.files.append(f"{d}/{name}")
            t.size += size
        for s in subdirs:
            t.dirs.append(f"{d}/{s}")
            stack.append(f"{d}/{s}")
    return t


def free_space(ftp: ftplib.FTP, drive: str) -> str | None:
    m = re.search(rf"Drive {drive}\s+Used:\s*([\d,.]+)MB\s+Free:\s*([\d,.]+)MB", ftp.getwelcome() or "")
    return f"{m.group(2)} MB free" if m else None


def pc_copies(folder: str, name: str) -> list[str]:
    """Staging folders in the current folder that contain <folder>/<name>."""
    hits = []
    for stage in sorted(Path.cwd().glob("*-stage")):
        p = stage.joinpath(*folder.split("/"), name)
        if p.is_dir():
            hits.append(stage.name)
    return hits


# ---------------------------------------------------------------- arrow-key picker


def read_key() -> str:
    """Return 'up', 'down', 'enter', 'quit' or a lowercase character."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
    else:
        import termios, tty
        fd = sys.stdin.fileno(); old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd); ch = sys.stdin.read(1)
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                return {"[A": "up", "[B": "down"}.get(seq, "quit")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch in ("\r", "\n"):
        return "enter"
    if ch in ("\x1b", "\x03"):
        return "quit"
    return ch.lower()


def pick(title: str, rows: list[str], ui: Display) -> int | None:
    """Arrow-key menu; returns the chosen index or None. Falls back to numbers without a terminal."""
    if not (ui.tty and sys.stdin.isatty()):
        print(title)
        for i, r in enumerate(rows, 1):
            print(f"  {i}. {r}")
        ans = input("Number (Enter to cancel): ").strip()
        return int(ans) - 1 if ans.isdigit() and 1 <= int(ans) <= len(rows) else None

    sel, drawn = 0, 0
    sys.stdout.write("\x1b[?25l")  # hide cursor
    try:
        while True:
            if drawn:
                sys.stdout.write(f"\x1b[{drawn}F")  # back to the top of the menu
            out = [f"{ui.bold}{title}{ui.reset}"]
            for i, r in enumerate(rows):
                out.append(f"  {ui.cyan}>{ui.reset} \x1b[7m {r} \x1b[0m" if i == sel else f"     {r} ")
            out.append(f"{ui.dim}  Up/Down to move, Enter to choose, Esc to quit{ui.reset}")
            for ln in out:
                sys.stdout.write("\x1b[2K" + ln + "\n")
            drawn = len(out)
            sys.stdout.flush()
            k = read_key()
            if k in ("up", "w", "k"):
                sel = (sel - 1) % len(rows)
            elif k in ("down", "s", "j"):
                sel = (sel + 1) % len(rows)
            elif k == "enter":
                return sel
            elif k in ("quit", "q"):
                return None
    finally:
        sys.stdout.write("\x1b[?25h")  # show cursor
        sys.stdout.flush()


def confirm(question: str, ui: Display) -> bool:
    print(f"{question} {ui.bold}[y/N]{ui.reset} ", end="", flush=True)
    if ui.tty and sys.stdin.isatty():
        k = read_key(); print(k if len(k) == 1 else "")
    else:
        k = input().strip().lower()[:1]
    return k == "y"


# ---------------------------------------------------------------- delete


def delete_tree(ftp: ftplib.FTP, tree: Tree, ui: Display) -> None:
    n = len(tree.files)
    for i, path in enumerate(tree.files, 1):
        parent, name = path.rsplit("/", 1)
        ftp.cwd("/" + parent)
        ftp.delete(name)
        ui.status(f"{ui.cyan}Deleting{ui.reset} {i}/{n} files  {ui.dim}{name}{ui.reset}")
    for d in sorted(tree.dirs, key=lambda p: p.count("/"), reverse=True):  # deepest first
        parent, name = d.rsplit("/", 1)
        ftp.cwd("/" + parent)
        ftp.rmd(name)
    ui.status(f"{ui.cyan}Deleting{ui.reset} {n}/{n} files, {len(tree.dirs)} folders", force=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pick a game on the Xbox and remove it.")
    ap.add_argument("--ip", default=DEFAULT_IP, help=f"Xbox address (default {DEFAULT_IP})")
    ap.add_argument("--folder", default="E/Games", help="Xbox folder to pick from (default E/Games)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be removed; no login, nothing deleted")
    args = ap.parse_args(argv)
    folder = args.folder.strip("/").replace("\\", "/").replace(":", "")
    drive = folder.split("/")[0].upper()
    shown = drive + ":\\" + "\\".join(folder.split("/")[1:])   # e.g. E:\Games
    ui = Display()

    # 1) Look around (read-only)
    try:
        ftp = connect(args.ip, "anonymous", "guest@")
        space = free_space(ftp, drive)
        try:
            names, _ = list_entries(ftp, folder)
        except ftplib.error_perm:
            print(f"There's no {shown} folder on the Xbox.")
            return 1
        if not names:
            print(f"{shown} is empty - nothing to remove.")
            return 0
        games = []
        for i, name in enumerate(sorted(names, key=str.lower), 1):
            ui.status(f"{ui.cyan}Measuring{ui.reset} {i}/{len(names)}  {ui.dim}{name}{ui.reset}", force=True)
            games.append((name, walk(ftp, f"{folder}/{name}")))
        ftp.quit()
    except NET_ERRORS as e:
        ui.line(f"{ui.red}Couldn't reach the Xbox at {args.ip}: {e}{ui.reset}")
        print("Is UnleashX on screen? Games, XBMC and the MS dashboard don't run its FTP server.")
        return 1
    ui.line()

    # 2) Pick
    width = max(len(n) for n, _ in games)
    rows = []
    for name, t in games:
        copies = pc_copies(folder, name)
        where = f"PC copy: {', '.join(copies)}" if copies else "no copy on this PC"
        rows.append(f"{name:<{width}}  {human(t.size):>8}  {len(t.files):5d} files  {where}")
    title = f"{shown} on the Xbox" + (f"  ({space})" if space else "")
    idx = pick(title, rows, ui)
    if idx is None:
        print("Nothing removed.")
        return 0
    name, tree = games[idx]
    copies = pc_copies(folder, name)

    # 3) Confirm
    print()
    print(f"{ui.bold}{name}{ui.reset}: {human(tree.size)}, {len(tree.files)} files, {len(tree.dirs)} folders.")
    print(f"{ui.dim}Saves are in E:\\UDATA and won't be touched.{ui.reset}")
    if copies:
        print(f"You can put it back later:  push-stage {copies[0]}")
    else:
        print(f"{ui.red}There's no copy of this on the PC - you'd need the disc or a backup to put it back.{ui.reset}")
    if args.dry_run:
        print("Dry run - nothing deleted.")
        return 0
    if not confirm(f"Delete {name} from the Xbox?", ui):
        print("Nothing removed.")
        return 0

    # 4) Delete (needs the write login)
    user = input("FTP user: ")
    password = getpass.getpass("FTP password: ")
    try:
        ftp = connect(args.ip, user, password)
        delete_tree(ftp, tree, ui)
        ftp.quit()
    except ftplib.error_perm as e:
        if str(e).startswith("530"):
            ui.line(f"{ui.red}The Xbox rejected the username/password. Nothing was deleted.{ui.reset}")
        else:
            ui.line(f"{ui.red}The Xbox refused a delete: {e}{ui.reset}")
            print("Part of the game may be removed. Run this again to finish removing it.")
        return 1
    except NET_ERRORS as e:
        ui.line(f"{ui.red}Lost the connection: {e}{ui.reset}")
        print("Part of the game may be removed. Run this again to finish removing it.")
        return 1

    # 5) Show the result
    try:
        ftp = connect(args.ip, "anonymous", "guest@")
        space = free_space(ftp, drive)
        try:
            ftp.cwd(f"/{folder}/{name}")
            gone = False
        except ftplib.error_perm:
            gone = True
        ftp.quit()
    except NET_ERRORS:
        gone, space = None, None
    ui.line(f"{ui.green}Removed {name}.{ui.reset}" + (f"  Xbox now has {space}." if space else ""))
    if gone is False:
        print(f"{ui.red}...but its folder is still there - run this again to finish.{ui.reset}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

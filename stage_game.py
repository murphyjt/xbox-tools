"""Turn an Xbox disc image (.iso / .xiso) into a staging folder ready for xbox-push.

    xbox-stage "C:\\Users\\Joshua\\Downloads\\My Game (USA).iso"

What it does:
  1. Checks the file really is an Xbox disc image (trimmed xiso or full-size).
  2. Lists it and checks every name fits the Xbox's file system (42 characters max).
  3. Extracts it with extract-xiso (skipping the $SystemUpdate folder), with progress.
  4. Checks the extracted files match the image and that default.xbe is there.
  5. Names the game folder after the game's own title (from default.xbe) and creates
     <game>-stage\\E\\Games\\<Title>\\ in the current folder.
  6. If the Xbox is reachable, compares the size with its free space.

extract-xiso is looked for on the PATH, then in tools\\extract-xiso\\artifacts\\ in the
current folder or next to this script.

Options:
  --name "Folder Name"   use this folder name on the Xbox instead of the game's title
  --stage my-stage       use this staging folder name instead of <game>-stage
  --ip 192.168.137.2     Xbox address for the free-space check
  --no-xbox              skip the free-space check

Only use this with images of discs you own.
"""
from __future__ import annotations

import argparse
import ftplib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import xiso
from push_stage import DEFAULT_IP, NET_ERRORS, Display, connect, mb
from xiso import FATX_MAX, fatx_name, region_names

HERE = Path(__file__).resolve().parent
CONSOLE_REGION = 0x1  # this console is NTSC North America (XBE region 01)
EXTRACT_XISO_URL = "https://github.com/XboxDev/extract-xiso/releases"


def find_extract_xiso() -> Path | None:
    """extract-xiso on the PATH, else in tools/extract-xiso/artifacts/ (current folder, then here)."""
    on_path = shutil.which("extract-xiso")
    if on_path:
        return Path(on_path)
    exe = "extract-xiso.exe" if sys.platform == "win32" else "extract-xiso"
    for base in (Path.cwd(), HERE):
        p = base / "tools" / "extract-xiso" / "artifacts" / exe
        if p.is_file():
            return p
    return None


EXTRACT_XISO = find_extract_xiso()


def shown(p: Path) -> str:
    """Path relative to the current folder when it's inside it, otherwise the full path."""
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(p)


def human(n: int) -> str:
    return f"{n / 1073741824:.2f} GB" if n >= 1073741824 else f"{n / 1048576:.0f} MB"


def image_kind(path: Path) -> str | None:
    try:
        return xiso.XisoImage(path).kind
    except xiso.NotXboxImage:
        return None


def run_xiso(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen([str(EXTRACT_XISO), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0)


def list_image(path: Path) -> tuple[list[tuple[str, int]], list[str]]:
    """Files (path, size) and folder names in the image, leaving out $SystemUpdate."""
    out = subprocess.run([str(EXTRACT_XISO), "-l", str(path)], capture_output=True).stdout
    files, dirs = [], []
    for raw in out.decode("utf-8", "replace").splitlines():
        m = re.match(r"^\\(.*) \((\d+) bytes\)\s*$", raw.strip())
        if not m:
            continue
        rel, size = m.group(1), int(m.group(2))
        if rel.split("\\")[0].lower() == "$systemupdate":
            continue
        if rel.endswith("\\"):
            dirs.append(rel.rstrip("\\"))
        else:
            files.append((rel, size))
    return files, dirs


def extract(path: Path, dest: Path, total_files: int, total_bytes: int, ui: Display) -> tuple[int, int]:
    """Extract with a live progress line; returns (files, bytes) from extract-xiso's summary."""
    proc = run_xiso(["-s", "-x", "-d", str(dest), str(path)])
    buf, done_files, done_bytes, summary = b"", 0, 0, None
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        *pieces, buf = re.split(rb"[\r\n]", buf)
        for p in pieces:
            s = p.decode("utf-8", "replace").strip()
            m = re.match(r"^extracting (.+) \((\d+) bytes\) \[(\d+)%\]$", s)
            if m:
                size, pct = int(m.group(2)), int(m.group(3))
                if pct == 100:
                    done_files += 1; done_bytes += size; cur = 0
                else:
                    cur = size * pct // 100
                allp = (done_bytes + cur) * 100 / total_bytes if total_bytes else 100
                name = m.group(1).rsplit("\\", 1)[-1]
                ui.status(f"{ui.cyan}Extracting{ui.reset} {allp:3.0f}%  {done_files}/{total_files} files  "
                          f"{mb(done_bytes + cur)}/{mb(total_bytes)} MB  {ui.dim}{name}{ui.reset}")
                continue
            m = re.match(r"^(\d+) files in .* total (\d+) bytes$", s)
            if m:
                summary = (int(m.group(1)), int(m.group(2)))
    proc.stdout.close()
    rc = proc.wait()
    ui.status("", force=True)
    if rc != 0 or summary is None:
        raise RuntimeError(f"extract-xiso failed (exit {rc})")
    return summary


def xbe_info(xbe: Path) -> tuple[str, int, int]:
    with open(xbe, "rb") as fh:
        return xiso.xbe_info(fh.read(0x10000))


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "game"


def xbox_space(ip: str, folder: str) -> tuple[int | None, bool | None]:
    """(free bytes on E:, whether E:\\Games\\<folder> already exists) or (None, None)."""
    try:
        ftp = connect(ip, "anonymous", "guest@")
        m = re.search(r"Drive E\s+Used:\s*[\d,.]+MB\s+Free:\s*([\d,.]+)MB", ftp.getwelcome() or "")
        free = int(float(m.group(1).replace(",", "")) * 1048576) if m else None
        try:
            ftp.cwd(f"/E/Games/{folder}")
            exists = True
        except ftplib.error_perm:
            exists = False
        ftp.quit()
        return free, exists
    except NET_ERRORS:
        return None, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Turn an Xbox disc image into a staging folder for push_stage.py.")
    ap.add_argument("image", help="path to the .iso / .xiso file")
    ap.add_argument("--name", help="folder name on the Xbox (default: the game's title)")
    ap.add_argument("--stage", help="staging folder name (default: <game>-stage)")
    ap.add_argument("--ip", default=DEFAULT_IP, help=f"Xbox address for the free-space check (default {DEFAULT_IP})")
    ap.add_argument("--no-xbox", action="store_true", help="skip the Xbox free-space check")
    args = ap.parse_args(argv)
    ui = Display()

    image = Path(args.image).expanduser()
    if not image.is_file():
        print(f"File not found: {image}")
        return 1
    if EXTRACT_XISO is None:
        print("extract-xiso wasn't found. Download it from " + EXTRACT_XISO_URL + " and either put it")
        print("on your PATH or unzip it to tools\\extract-xiso\\artifacts\\ in this folder.")
        print("(Or skip staging: xbox-push can upload straight from the image.)")
        return 1

    # 1) Is it an Xbox disc image?
    kind = image_kind(image)
    if not kind:
        print(f"{ui.red}{image.name} doesn't look like an Xbox disc image{ui.reset} (no XDVDFS header found).")
        return 1
    print(f"{image.name}: {ui.green}Xbox {kind}{ui.reset}, {human(image.stat().st_size)} on disk.")

    # 2) What's inside, and will every name fit on the Xbox?
    files, dirs = list_image(image)
    if not files:
        print(f"{ui.red}Couldn't list the files in the image.{ui.reset}")
        return 1
    total = sum(s for _, s in files)
    too_long = sorted({part for rel, _ in files for part in rel.split("\\") if len(part) > FATX_MAX}
                      | {part for d in dirs for part in d.split("\\") if len(part) > FATX_MAX})
    if too_long:
        print(f"{ui.red}These names are longer than the Xbox allows ({FATX_MAX} characters):{ui.reset}")
        for n in too_long[:10]:
            print(f"  {n}")
        return 1
    if not any(rel.lower() == "default.xbe" for rel, _ in files):
        print(f"{ui.red}There's no default.xbe at the top of the image, so the Xbox couldn't launch it.{ui.reset}")
        return 1
    print(f"Contains {len(files)} files, {human(total)} (not counting $SystemUpdate).")
    free_pc = shutil.disk_usage(Path.cwd()).free
    if free_pc < total * 1.05:
        print(f"{ui.red}Not enough space on this PC: need {human(total)}, have {human(free_pc)}.{ui.reset}")
        return 1

    # 3) Extract into a temporary folder in the current folder.
    tmp = Path.cwd() / f".staging-tmp-{slug(image.stem)}"
    if tmp.exists():
        shutil.rmtree(tmp)  # left over from an interrupted run of this script
    try:
        got_files, got_bytes = extract(image, tmp, len(files), total, ui)
        # 4) Does it match the image?
        on_disk = [p for p in tmp.rglob("*") if p.is_file()]
        disk_bytes = sum(p.stat().st_size for p in on_disk)
        if not (got_files == len(on_disk) == len(files) and got_bytes == disk_bytes == total):
            print(f"{ui.red}The extracted files don't match the image{ui.reset} "
                  f"(image {len(files)} files/{total} bytes, extracted {len(on_disk)}/{disk_bytes}).")
            return 1
        title, title_id, region = xbe_info(tmp / "default.xbe")

        # 5) Name it and move it into its staging folder.
        name = fatx_name(args.name or title or image.stem)
        stage = Path.cwd() / (args.stage or f"{slug(name)}-stage")
        target = stage / "E" / "Games" / name
        if target.exists():
            print(f"{ui.red}{shown(target)} already exists{ui.reset} - not overwriting it. "
                  f"Use --stage or --name for a different folder.")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(target))  # works across drives, unlike rename
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    ui.line(f"{ui.green}Staged{ui.reset} {ui.bold}{name}{ui.reset}  ->  {shown(target)}")
    print(f"  {len(files)} files, {human(total)}, matches the image exactly.")
    regions = region_names(region)
    print(f"  Game: {title!r}, title ID {title_id:08X}, region: {regions}.")
    if region and not region & CONSOLE_REGION:
        print(f"  {ui.red}Note: this game isn't marked for North America; it may refuse to start "
              f"on your NTSC console.{ui.reset}")

    # 6) Will it fit on the Xbox?
    if not args.no_xbox:
        free, exists = xbox_space(args.ip, name)
        need = total + len(files) * 16384  # file-system overhead allowance
        if free is None:
            print(f"  {ui.dim}Couldn't reach the Xbox to check free space (is UnleashX on screen?).{ui.reset}")
        else:
            if exists:
                print(f"  {ui.red}Note: a folder named E:\\Games\\{name} is already on the Xbox.{ui.reset}")
                print("  If it's this same game, xbox-push will just fill in anything missing.")
                print(f"  If it's a different game, restage with a different name (--name) so they don't mix.")
            elif free >= need:
                print(f"  {ui.green}Fits:{ui.reset} needs about {human(need)}, the Xbox has {human(free)} free.")
            else:
                print(f"  {ui.red}Won't fit yet:{ui.reset} needs about {human(need)}, the Xbox has {human(free)} free. "
                      f"Free up {human(need - free)} first (xbox-remove).")
    print()
    s = shown(stage)
    s = f'"{s}"' if " " in s else s
    print("Next:")
    print(f"  xbox-push {s} --dry-run")
    print(f"  xbox-push {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for xiso.py: reading Xbox disc images and the naming helpers."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from helpers import build_xiso, make_xbe

import xiso

FILES = {
    "default.xbe": make_xbe("Panzer Test: ORTA?", 0x53450007, 0x1),
    "data/level 1.bin": bytes(range(256)) * 40,
    "data/deep/z.txt": b"hello",
    "movie/big.sfd": b"m" * 300_000,
    "empty.dat": b"",
    "$SystemUpdate/xboxupd.xbe": b"updater",
}


class ReadImages(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def check_image(self, base, kind):
        img = xiso.XisoImage(build_xiso(self.dir / f"g{base}.iso", FILES, base))
        self.assertEqual(img.kind, kind)
        self.assertEqual({f.path: img.read(f) for f in img.files()}, FILES)
        self.assertEqual(img.dirs(), ["$SystemUpdate", "data", "data/deep", "movie"])
        self.assertEqual(img.find("DEFAULT.XBE").size, len(FILES["default.xbe"]))  # case-insensitive
        self.assertIsNone(img.find("nope.bin"))

    def test_trimmed_image(self):
        self.check_image(0, "trimmed image")

    def test_full_image(self):
        self.check_image(0x2080000, "full image (XGD3)")

    def test_partial_reads(self):
        img = xiso.XisoImage(build_xiso(self.dir / "g.iso", FILES))
        with img.open(img.find("data/level 1.bin")) as fh:
            self.assertEqual(fh.read(10), bytes(range(10)))
            self.assertEqual(len(fh.read()), len(FILES["data/level 1.bin"]) - 10)
            self.assertEqual(fh.read(), b"")

    def test_not_an_image(self):
        junk = self.dir / "junk.iso"
        junk.write_bytes(b"\x00" * 200_000)
        with self.assertRaises(xiso.NotXboxImage):
            xiso.XisoImage(junk)


class Helpers(unittest.TestCase):
    def test_xbe_info(self):
        self.assertEqual(xiso.xbe_info(FILES["default.xbe"]), ("Panzer Test: ORTA?", 0x53450007, 0x1))
        with self.assertRaises(ValueError):
            xiso.xbe_info(b"not an xbe" * 100)

    def test_xbe_details(self):
        d = xiso.xbe_details(FILES["default.xbe"])
        self.assertEqual((d["publisher"], d["game_number"], d["disc"], d["version"]), ("SE", 7, 0, 1))

    def test_fatx_name(self):
        self.assertEqual(xiso.fatx_name("Panzer Test: ORTA?"), "Panzer Test- ORTA-")
        self.assertEqual(xiso.fatx_name("  lots   of   space  "), "lots of space")
        self.assertEqual(len(xiso.fatx_name("x" * 80)), xiso.FATX_MAX)
        self.assertEqual(xiso.fatx_name("..."), "Game")

    def test_too_long_names(self):
        ok = "a" * 42
        self.assertEqual(xiso.too_long_names([f"{ok}/b", "c"]), [])
        self.assertEqual(xiso.too_long_names(["d/" + "e" * 43]), ["e" * 43])

    def test_region_names(self):
        self.assertEqual(xiso.region_names(0x1), "North America")
        self.assertEqual(xiso.region_names(0x7), "North America, Japan, Europe/Australia")


class InfoCommand(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = xiso.main(list(args))
        return rc, out.getvalue()

    def test_summary(self):
        p = build_xiso(Path(tempfile.mkdtemp()) / "Panzer Test.xiso.iso", FILES)
        rc, out = self.run_cli(str(p))
        self.assertEqual(rc, 0)
        self.assertIn("Xbox trimmed image", out)
        self.assertIn("Panzer Test: ORTA?", out)
        self.assertIn("Folder name   Panzer Test- ORTA-", out)
        self.assertIn("5 files in 3 folders", out)          # $SystemUpdate not counted
        self.assertIn("$SystemUpdate 1 files", out)

    def test_list_and_errors(self):
        p = build_xiso(Path(tempfile.mkdtemp()) / "g.iso", FILES)
        rc, out = self.run_cli(str(p), "--list")
        self.assertIn("data/deep/z.txt", out)
        rc, out = self.run_cli(str(p.parent / "missing.iso"))
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()

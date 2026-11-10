"""Tests for stage_game.py. Needs tools/extract-xiso (skipped if it isn't there)."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import build_xiso, make_xbe

import stage_game

FILES = {"default.xbe": make_xbe("Stage Test: Game", 0x54450003, 0x1),
         "data/level 1.bin": os.urandom(120_000),
         "data/deep/z.txt": b"hi",
         "$SystemUpdate/xboxupd.xbe": b"updater"}


@unittest.skipUnless(stage_game.EXTRACT_XISO is not None, "extract-xiso not installed")
class StageGame(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.image = build_xiso(self.dir / "Stage Test (USA).iso", FILES)

    def run_main(self, *args, free=None):
        out = io.StringIO()
        space = (lambda ip, name: (free, False)) if free is not None else (lambda ip, name: (None, None))
        with mock.patch.object(stage_game, "xbox_space", space), contextlib.redirect_stdout(out):
            rc = stage_game.main([str(a) for a in args])
        self.output = out.getvalue()
        return rc

    def test_stage(self):
        stage = self.dir / "t-stage"
        self.assertEqual(self.run_main(self.image, "--stage", stage, free=10 << 30), 0)
        game = stage / "E" / "Games" / "Stage Test- Game"
        got = {p.relative_to(game).as_posix(): p.read_bytes() for p in game.rglob("*") if p.is_file()}
        self.assertEqual(got, {k: v for k, v in FILES.items() if not k.startswith("$")})
        self.assertIn("Fits:", self.output)

    def test_refuses_to_overwrite(self):
        stage = self.dir / "t-stage"
        self.assertEqual(self.run_main(self.image, "--stage", stage), 0)
        self.assertEqual(self.run_main(self.image, "--stage", stage), 1)
        self.assertIn("already exists", self.output)

    def test_wont_fit_message(self):
        self.assertEqual(self.run_main(self.image, "--stage", self.dir / "s", free=1024), 0)
        self.assertIn("Won't fit yet", self.output)

    def test_name_override(self):
        self.assertEqual(self.run_main(self.image, "--stage", self.dir / "n", "--name", "Mine"), 0)
        self.assertTrue((self.dir / "n" / "E" / "Games" / "Mine" / "default.xbe").is_file())

    def test_not_an_image(self):
        junk = self.dir / "junk.iso"
        junk.write_bytes(b"\x00" * 200_000)
        self.assertEqual(self.run_main(junk, "--no-xbox"), 1)
        self.assertIn("doesn't look like an Xbox disc image", self.output)


if __name__ == "__main__":
    unittest.main()

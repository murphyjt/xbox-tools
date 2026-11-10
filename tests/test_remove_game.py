"""Tests for remove_game.py against a fake UnleashX FTP server (no real Xbox needed)."""
import contextlib
import io
import unittest
from unittest import mock

from helpers import FakeFS, fake_connect

import remove_game


class RemoveGame(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFS()
        self.fs.add_file("/E/Games/Game A/default.xbe", b"x" * 1000)
        self.fs.add_file("/E/Games/Game A/data/lvl 1.bin", b"y" * 500)
        self.fs.add_file("/E/Games/Game A/data/deep/z.dat", b"z")
        self.fs.add_file("/E/Games/Game B/default.xbe", b"o" * 10)
        self.fs.add_file("/E/UDATA/54450001/save.dat", b"save")
        self.password = "xbox"
        for p in (mock.patch.object(remove_game, "connect", fake_connect(self.fs)),
                  mock.patch("getpass.getpass", lambda prompt="": self.password),
                  mock.patch.object(remove_game, "pc_copies", lambda folder, name: [])):
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, answers, *args):
        it = iter(answers)
        out = io.StringIO()
        with mock.patch("builtins.input", lambda prompt="": next(it)), contextlib.redirect_stdout(out):
            rc = remove_game.main(list(args))
        self.output = out.getvalue()
        return rc

    def game_files(self, name):
        return [f for f in self.fs.files if f.startswith(f"/E/Games/{name}/")]

    def test_remove(self):
        self.assertEqual(self.run_with(["1", "y", "xbox"]), 0)
        self.assertEqual(self.game_files("Game A"), [])
        self.assertFalse(any("Game A" in d for d in self.fs.dirs))
        self.assertTrue(self.game_files("Game B"))                    # other game untouched
        self.assertIn("/E/UDATA/54450001/save.dat", self.fs.files)  # saves untouched
        self.assertIn("no copy of this on the PC", self.output)

    def test_answer_no(self):
        self.assertEqual(self.run_with(["2", "n"]), 0)
        self.assertTrue(self.game_files("Game B"))

    def test_wrong_password(self):
        self.password = "nope"
        self.assertEqual(self.run_with(["1", "y", "xbox"]), 1)
        self.assertIn("Nothing was deleted", self.output)
        self.assertEqual(len(self.game_files("Game A")), 3)

    def test_cancel_at_menu(self):
        self.assertEqual(self.run_with([""]), 0)
        self.assertIn("Nothing removed", self.output)

    def test_dry_run(self):
        self.assertEqual(self.run_with(["1"], "--dry-run"), 0)
        self.assertIn("Dry run", self.output)
        self.assertEqual(len(self.game_files("Game A")), 3)

    def test_listing_shows_sizes_and_free_space(self):
        self.run_with([""])
        self.assertIn("1,000.00 MB free", self.output)
        self.assertIn("Game A", self.output)
        self.assertIn("3 files", self.output)


if __name__ == "__main__":
    unittest.main()

"""Tests for push_stage.py against a fake UnleashX FTP server (no real Xbox needed)."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeFS, build_xiso, fake_connect, make_xbe

import push_stage


def make_stage(files: dict[str, bytes]) -> Path:
    stage = Path(tempfile.mkdtemp()) / "test-stage"
    for rel, data in files.items():
        p = stage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return stage


class PushTestCase(unittest.TestCase):
    """Runs push_stage.main with the fake server, a scripted login, and captured output."""

    def setUp(self):
        self.fs = FakeFS()
        self.asked_login = False
        self.password = "xbox"
        patches = [
            mock.patch.object(push_stage, "connect", fake_connect(self.fs)),
            mock.patch.object(push_stage.time, "sleep", lambda s: None),
            mock.patch("builtins.input", self._input),
            mock.patch("getpass.getpass", lambda prompt="": self.password),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _input(self, prompt=""):
        self.asked_login = True
        return "xbox"

    def push(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = push_stage.main([str(a) for a in args])
        self.output = out.getvalue()
        return rc

    def on_xbox(self, prefix):
        pre = "/" + prefix.strip("/").lower() + "/"
        return {k[len(pre):]: v for k, v in self.fs.files.items() if k.lower().startswith(pre)}


class StagingFolder(PushTestCase):
    FILES = {"E/Games/My Game/default.xbe": os.urandom(300_000),
             "E/Games/My Game/data/level 1.bin": os.urandom(200_000),
             "E/Games/My Game/data/sub/x.dat": b"tiny"}

    def test_upload_retry_and_resume(self):
        stage = make_stage(self.FILES)
        self.fs.fail_stor_once.add("level 1.bin")      # one dropped connection mid-file
        self.assertEqual(self.push(stage, "--verify"), 0)
        self.assertIn("reconnecting", self.output)
        self.assertEqual(self.on_xbox("E"), {k[2:]: v for k, v in self.FILES.items()})
        self.assertEqual(self.fs.stor_calls, 4)       # 3 files + 1 retry
        self.assertEqual(self.push(stage), 0)        # second run: nothing to send
        self.assertIn("Nothing to do", self.output)
        self.assertEqual(self.fs.stor_calls, 4)

    def test_wrong_password(self):
        self.password = "nope"
        self.assertEqual(self.push(make_stage(self.FILES)), 1)
        self.assertIn("rejected the username/password", self.output)
        self.assertEqual(self.fs.files, {})

    def test_parallel_connections(self):
        files = {f"E/Games/Big/lvl {d}/sub/file {f:02d}.bin": os.urandom(70_000 + f * 997)
                 for d in range(6) for f in range(20)}
        for conns in (1, 2, 4):
            with self.subTest(connections=conns):
                self.fs.files.clear(); self.fs.dirs = {"/", "/E"}; self.fs.stor_calls = 0
                self.fs.fail_stor_once.update({"file 07.bin", "file 13.bin"})
                self.assertEqual(self.push(make_stage(files), "--connections", conns), 0)
                self.assertEqual(self.on_xbox("E"), {k[2:]: v for k, v in files.items()})
                self.assertEqual(self.fs.stor_calls, len(files) + 2)

    def test_exclude(self):
        stage = make_stage(self.FILES)
        self.assertEqual(self.push(stage, "--exclude", "*.dat"), 0)
        self.assertNotIn("Games/My Game/data/sub/x.dat", self.on_xbox("E"))
        self.assertEqual(len(self.on_xbox("E")), 2)

    def test_dry_run_needs_no_login(self):
        self.assertEqual(self.push(make_stage(self.FILES), "--dry-run"), 0)
        self.assertFalse(self.asked_login)
        self.assertEqual(self.fs.files, {})

    def test_bad_source(self):
        self.assertEqual(self.push(Path(tempfile.mkdtemp()) / "nothing-here"), 1)


class FreeSpace(PushTestCase):
    FILES = {"E/Games/G/default.xbe": os.urandom(3_000_000),
             "E/Games/G/data/a.bin": os.urandom(2_000_000),
             "E/Games/G/data/b.bin": os.urandom(1_000_000)}

    def test_fits(self):
        self.assertEqual(self.push(make_stage(self.FILES)), 0)
        self.assertIn("Fits on E:", self.output)

    def test_wont_fit_stops_before_login(self):
        self.fs.free_mb = 5.0
        self.assertEqual(self.push(make_stage(self.FILES)), 1)
        self.assertIn("Won't fit on E:", self.output)
        self.assertFalse(self.asked_login)
        self.assertEqual(self.fs.stor_calls, 0)

    def test_wont_fit_dry_run(self):
        self.fs.free_mb = 5.0
        self.assertEqual(self.push(make_stage(self.FILES), "--dry-run"), 1)
        self.assertIn("Won't fit", self.output)

    def test_ignore_space(self):
        self.fs.free_mb = 5.0
        self.assertEqual(self.push(make_stage(self.FILES), "--ignore-space"), 0)
        self.assertEqual(self.fs.stor_calls, 3)

    def test_resume_only_counts_whats_left(self):
        stage = make_stage(self.FILES)
        self.fs.free_mb = 4.0
        self.fs.add_file("/E/Games/G/default.xbe", self.FILES["E/Games/G/default.xbe"])
        self.assertEqual(self.push(stage), 0)
        self.assertEqual(self.fs.stor_calls, 2)

    def test_replacing_bigger_old_copy(self):
        stage = make_stage(self.FILES)
        self.fs.free_mb = 1.5
        self.fs.add_file("/E/Games/G/default.xbe", self.FILES["E/Games/G/default.xbe"])
        self.fs.add_file("/E/Games/G/data/b.bin", self.FILES["E/Games/G/data/b.bin"])
        self.fs.add_file("/E/Games/G/data/a.bin", b"x" * 2_100_000)
        self.assertEqual(self.push(stage), 0)
        self.assertEqual(self.fs.files["/E/Games/G/data/a.bin"], self.FILES["E/Games/G/data/a.bin"])

    def test_parse_free(self):
        banner = ("220-Drive C\t Used: 143.48MB\tFree: 341.44MB\tTotal: 484.92MB\n"
                  "220-Drive E\t Used: 3,672.88MB\tFree: 1,208.75MB\tTotal: 4,881.63MB")
        free = push_stage.parse_free(banner)
        self.assertAlmostEqual(free["E"] / 1048576, 1208.75, places=1)
        self.assertAlmostEqual(free["C"] / 1048576, 341.44, places=1)


class FromImage(PushTestCase):
    FILES = {"default.xbe": make_xbe("Image Game", 0x54450002, 0x1),
             "data/level 1.bin": os.urandom(150_000),
             "data/deep/z.txt": b"hi",
             "$SystemUpdate/xboxupd.xbe": b"updater"}
    EXPECT = {k: v for k, v in FILES.items() if not k.startswith("$")}

    def image(self, base=0):
        return build_xiso(Path(tempfile.mkdtemp()) / "Image Game (USA).xiso.iso", self.FILES, base)

    def test_trimmed_and_full(self):
        for base in (0, 0x2080000):
            with self.subTest(base=hex(base)):
                self.fs.files.clear(); self.fs.dirs = {"/", "/E"}
                self.assertEqual(self.push(self.image(base), "--verify"), 0)
                self.assertEqual(self.on_xbox("E/Games/Image Game"), self.EXPECT)  # no $SystemUpdate

    def test_name_override(self):
        self.assertEqual(self.push(self.image(), "--name", "My Copy: v2"), 0)
        self.assertEqual(self.on_xbox("E/Games/My Copy- v2"), self.EXPECT)

    def test_different_game_in_the_way(self):
        self.fs.add_file("/E/Games/Image Game/default.xbe", b"some other game")
        self.assertEqual(self.push(self.image()), 1)
        self.assertIn("already holds a different game", self.output)
        self.assertEqual(self.fs.stor_calls, 0)
        self.assertEqual(self.fs.files["/E/Games/Image Game/default.xbe"], b"some other game")

    def test_resume(self):
        self.fs.add_file("/E/Games/Image Game/default.xbe", self.FILES["default.xbe"])
        self.assertEqual(self.push(self.image()), 0)
        self.assertEqual(self.fs.stor_calls, 2)
        self.assertEqual(self.on_xbox("E/Games/Image Game"), self.EXPECT)

    def test_not_an_image(self):
        junk = Path(tempfile.mkdtemp()) / "junk.iso"
        junk.write_bytes(b"\x00" * 200_000)
        self.assertEqual(self.push(junk), 1)
        self.assertIn("isn't an Xbox disc image", self.output)


if __name__ == "__main__":
    unittest.main()

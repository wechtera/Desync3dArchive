"""The tools adapter, tested against hostile archives with the real 7-Zip where it is installed."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from desync_archival import external
from desync_archival.external import ArchiveMember, ExtractionRefused

SEVEN_ZIP = external.find_7zip()
UNRAR = external.find_unrar()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.dest = self.base / "out"
        self.dest.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def zip_with(self, members):
        archive = self.base / "hostile.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return archive


class MemberNameTests(unittest.TestCase):
    def test_safe_names(self):
        for name in ("a.stl", "Files/Sub/a b.stl", "-oevil.stl", "..hidden/a.stl"):
            self.assertTrue(external.is_safe_member(name), name)

    def test_unsafe_names(self):
        for name in ("", "../a.stl", "a/../../b.stl", "/etc/passwd", "C:\\win\\a.stl", "\\\\server\\a", "a\0b", "..\\a.stl"):
            self.assertFalse(external.is_safe_member(name), repr(name))


class CommandTests(TempCase):
    def test_every_member_comes_after_the_stop_marker(self):
        hostile = "-oevil.stl"
        tool = SEVEN_ZIP or "/bin/ls"
        commands = external.member_commands(".rar", tool, tool, Path("/a.rar"), hostile, hostile, self.dest)
        self.assertTrue(commands)
        for cmd in commands:
            self.assertLess(cmd.index("--"), max(i for i, part in enumerate(cmd) if hostile in part))

    def test_unrar_and_seven_zip_both_stop_switches_with_a_double_dash(self):
        self.assertEqual(external.unrar_extract_member("unrar", Path("/a.rar"), "-x", self.dest)[1:4], ["e", "-y", "--"])
        self.assertEqual(external.seven_zip_extract_member("7zz", Path("/a.7z"), "-x", self.dest)[-2:], ["--", "-x"])

    @unittest.skipUnless(UNRAR, "needs unrar")
    def test_real_unrar_accepts_the_stop_marker(self):
        result = external.run([UNRAR, "lt", "--", "/nonexistent/sample.rar"], 10)
        self.assertNotIn("Unknown option", result.stdout + result.stderr)

    def test_timeouts_are_enforced(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            external.run([sys.executable, "-c", "import time; time.sleep(5)"], 0.1)

    def test_undecodable_output_does_not_crash(self):
        code = "import sys; sys.stdout.buffer.write(bytes([0xff, 0xfe]))"
        self.assertTrue(external.run([sys.executable, "-c", code], 10).stdout)


class DiscoveryTests(TempCase):
    def test_a_tools_symlink_wins(self):
        target = self.base / "real7z"
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
        tools = self.base / "tools"
        tools.mkdir()
        (tools / "7zz").symlink_to(target)
        with patch.object(external, "TOOLS_DIR", tools):
            self.assertEqual(external.find_7zip(), str(tools / "7zz"))

    def test_setting_is_used_when_nothing_else_exists(self):
        tool = self.base / "custom"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        with patch.object(external, "TOOLS_DIR", self.base / "none"), patch.object(external, "SYSTEM_DIRS", ()), \
                patch("shutil.which", return_value=None), patch.dict(os.environ, {"DESYNC_UNRAR_BIN": str(tool)}):
            self.assertEqual(external.find_unrar(), str(tool))

    def test_nothing_found_is_none(self):
        with patch.object(external, "TOOLS_DIR", self.base / "none"), patch.object(external, "SYSTEM_DIRS", ()), \
                patch("shutil.which", return_value=None), patch.dict(os.environ, {"DESYNC_UNRAR_BIN": ""}):
            self.assertIsNone(external.find_unrar())


class LimitTests(unittest.TestCase):
    def test_traversal_is_refused(self):
        with self.assertRaises(ExtractionRefused):
            external.check_members([ArchiveMember("../evil.stl", 1)], "a")

    def test_too_many_files_are_refused(self):
        with patch.object(external, "MAX_EXTRACTED_FILES", 1), self.assertRaises(ExtractionRefused):
            external.check_members([ArchiveMember("a", 1), ArchiveMember("b", 1)], "a")

    def test_declared_size_over_the_limit_is_refused(self):
        with patch.object(external, "MAX_EXTRACTED_BYTES", 5), self.assertRaises(ExtractionRefused):
            external.check_members([ArchiveMember("a", 6)], "a")

    def test_unrar_listing_is_parsed(self):
        output = "\n        Name: Files/a.stl\n        Type: File\n        Size: 12\n\n        Name: Files\n        Type: Directory\n        Size: 0\n"
        self.assertEqual(external._parse_unrar_listing(output), [ArchiveMember("Files/a.stl", 12)])


@unittest.skipUnless(SEVEN_ZIP, "needs 7-Zip")
class RealArchiveTests(TempCase):
    def extract(self, archive):
        external.extract_all(archive.suffix, archive, self.dest, None, SEVEN_ZIP)

    def test_normal_archive_unpacks(self):
        archive = self.zip_with({"Files/a.stl": b"mesh", "b.stl": b"mesh2"})
        self.extract(archive)
        self.assertEqual((self.dest / "Files" / "a.stl").read_bytes(), b"mesh")

    def test_path_traversal_writes_nothing_outside(self):
        archive = self.zip_with({"../escaped.stl": b"evil", "good.stl": b"ok"})
        with self.assertRaises(ExtractionRefused):
            self.extract(archive)
        self.assertFalse((self.base / "escaped.stl").exists())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_oversized_contents_are_refused_before_unpacking(self):
        archive = self.zip_with({"bomb.stl": b"\0" * 1_000_000})
        self.assertLess(archive.stat().st_size, 20_000)
        with patch.object(external, "MAX_EXTRACTED_BYTES", 1000), self.assertRaises(ExtractionRefused):
            self.extract(archive)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_size_lie_is_caught_after_unpacking(self):
        archive = self.zip_with({"a.stl": b"\0" * 5000})
        with patch.object(external, "list_members", return_value=[ArchiveMember("a.stl", 1)]), \
                patch.object(external, "MAX_EXTRACTED_BYTES", 100), self.assertRaises(ExtractionRefused):
            self.extract(archive)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_archive_symlink_is_removed(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            info = zipfile.ZipInfo("link.stl")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
            zf.writestr("good.stl", b"ok")
        archive = self.base / "links.zip"
        archive.write_bytes(buffer.getvalue())
        self.extract(archive)
        self.assertFalse((self.dest / "link.stl").is_symlink())
        self.assertTrue((self.dest / "good.stl").exists())

    def test_option_like_member_is_a_file_not_an_option(self):
        source = self.base / "source"
        source.mkdir()
        (source / "-oevil.stl").write_bytes(b"mesh")
        archive = self.base / "named.7z"
        subprocess.run([SEVEN_ZIP, "a", "-bd", str(archive), "./-oevil.stl"], cwd=source, check=True, capture_output=True)
        cmd = external.seven_zip_extract_member(SEVEN_ZIP, archive, "-oevil.stl", self.dest)
        external.run(cmd, 30, check=True)
        self.assertEqual((self.dest / "-oevil.stl").read_bytes(), b"mesh")
        self.assertFalse(Path("evil.stl").exists())

    def test_corrupt_archive_is_refused(self):
        archive = self.base / "bad.7z"
        archive.write_bytes(b"not an archive")
        with self.assertRaises(ExtractionRefused):
            self.extract(archive)

    def test_timeout_during_unpack_leaves_nothing(self):
        archive = self.zip_with({"a.stl": b"mesh"})
        (self.dest / "partial").write_text("x")
        with patch.object(external, "list_members", return_value=[]), \
                patch.object(external, "run", side_effect=subprocess.TimeoutExpired("x", 1)), \
                self.assertRaises(subprocess.TimeoutExpired):
            self.extract(archive)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_regular_files_skips_symlinks(self):
        (self.dest / "real.stl").write_text("x")
        (self.dest / "link.stl").symlink_to("/etc/hosts")
        self.assertEqual([p.name for p in external.regular_files(self.dest)], ["real.stl"])


# rar can trigger a macOS security prompt, so this runs only when asked for.
@unittest.skipUnless(UNRAR and shutil.which("rar") and os.environ.get("DESYNC_TEST_RAR"), "set DESYNC_TEST_RAR to run")
class RealRarTests(TempCase):
    def test_option_like_member_in_a_real_rar(self):
        source = self.base / "source"
        source.mkdir()
        (source / "-oevil.stl").write_bytes(b"mesh")
        archive = self.base / "named.rar"
        subprocess.run(
            ["rar", "a", "-ep", str(archive), "./-oevil.stl"],
            cwd=source, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30,
        )
        external.run(external.unrar_extract_member(UNRAR, archive, "-oevil.stl", self.dest), 30, check=True)
        self.assertEqual((self.dest / "-oevil.stl").read_bytes(), b"mesh")


if __name__ == "__main__":
    unittest.main()

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 测试不需要真的控制鼠标键盘
sys.modules.setdefault("pyautogui", types.SimpleNamespace(
    FAILSAFE=True, PAUSE=0, FailSafeException=RuntimeError, size=lambda: (1440, 900)))
import mac_agent_server as server  # noqa: E402


def png(w=2, h=1):
    """生成一张纯红 PNG。"""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        server.OUTBOX = os.path.join(self.tmp, "outbox")
        os.makedirs(server.OUTBOX)

    def test_image_kind(self):
        self.assertEqual(server.image_kind(png()), "png")
        self.assertEqual(server.image_kind(b"\xff\xd8\xff\xe0rest"), "jpg")
        self.assertEqual(server.image_kind(b"GIF89a..."), "gif")
        self.assertEqual(server.image_kind(b"\x00\x00\x00\x18ftypheic...."), "heic")
        self.assertIsNone(server.image_kind(b"%PDF-1.4"))
        self.assertIsNone(server.image_kind(b""))

    def test_clean_stem(self):
        self.assertEqual(server.clean_stem("照片 1.PNG"), "照片_1")
        self.assertEqual(server.clean_stem("../../etc/passwd"), "passwd")
        self.assertEqual(server.clean_stem("a..b.png"), "a.b")
        self.assertEqual(server.clean_stem(".hidden.png"), "hidden")
        self.assertEqual(server.clean_stem(None), "")

    def test_outbox_path_ok(self):
        self.assertEqual(server.outbox_path("a.png"),
                         os.path.join(os.path.realpath(server.OUTBOX), "a.png"))
        self.assertTrue(server.outbox_path("照片_1.JPG").endswith("照片_1.JPG"))

    def test_outbox_path_rejects(self):
        for bad in ["../a.png", "/etc/a.png", "a/b.png", ".a.png", "a..png", "a.txt", "a", "", None, 3]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                server.outbox_path(bad)

    def test_outbox_path_rejects_symlink(self):
        outside = os.path.join(self.tmp, "secret.png")
        with open(outside, "wb") as f:
            f.write(png())
        os.symlink(outside, os.path.join(server.OUTBOX, "link.png"))
        with self.assertRaises(ValueError):
            server.outbox_path("link.png")

    def test_save_png(self):
        file = server.save_image(png(), "照片 1.PNG")
        self.assertEqual(file, "照片_1.png")
        self.assertEqual(server.image_info(file), {"file": file, "width": 2, "height": 1,
                                                   "bytes": len(png())})

    def test_save_dedupes(self):
        self.assertEqual(server.save_image(png(), "a.png"), "a.png")
        self.assertEqual(server.save_image(png(), "a.png"), "a-1.png")
        self.assertEqual(server.save_image(png(), "a.png"), "a-2.png")

    def test_save_fixes_extension_by_content(self):
        self.assertEqual(server.save_image(png(), "x.jpg"), "x.png")

    def test_save_default_name(self):
        self.assertRegex(server.save_image(png()), r"^img-\d{8}-\d{6}\.png$")

    def test_save_rejects_non_image(self):
        with self.assertRaises(ValueError):
            server.save_image(b"%PDF-1.4 fake", "doc.jpg")
        self.assertEqual(os.listdir(server.OUTBOX), [])

    def test_save_heic_converts_to_jpg(self):
        src, heic = os.path.join(self.tmp, "s.png"), os.path.join(self.tmp, "s.heic")
        with open(src, "wb") as f:
            f.write(png(8, 8))
        r = subprocess.run(["sips", "-s", "format", "heic", src, "--out", heic], capture_output=True)
        if r.returncode != 0 or not os.path.exists(heic):
            self.skipTest("这台 Mac 的 sips 不能生成 HEIC")
        with open(heic, "rb") as f:
            file = server.save_image(f.read(), "p.heic")
        self.assertEqual(file, "p.jpg")
        with open(os.path.join(server.OUTBOX, file), "rb") as f:
            self.assertEqual(server.image_kind(f.read()), "jpg")


if __name__ == "__main__":
    unittest.main()

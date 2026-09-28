import base64
import http.client
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import zlib
from http.server import ThreadingHTTPServer

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


TOKEN = "t" * 16


class ServerCase(unittest.TestCase):
    """起一个真的 bridge，run_wx 换成只记录参数的假函数。"""

    @classmethod
    def setUpClass(cls):
        server.TOKEN = TOKEN
        server.Handler.log_message = lambda *a: None
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        server.OUTBOX = os.path.join(self.tmp, "outbox")
        self.calls = []

        def fake_run_wx(args, account=None, env=None, timeout=300):
            self.calls.append(args)
            return 0, "✅ 已发送", ""
        orig = server.run_wx
        server.run_wx = fake_run_wx
        self.addCleanup(setattr, server, "run_wx", orig)
        self.addCleanup(setattr, server, "IMAGE_MAX", server.IMAGE_MAX)

    def call(self, method, path, body=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=30)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers={"Authorization": "Bearer " + TOKEN})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, out

    def jcall(self, method, path, body=None):
        status, out = self.call(method, path, body)
        return status, json.loads(out)

    def upload(self, name="a.png", data=None):
        return self.jcall("POST", "/wechat/image/upload",
                          {"name": name, "data": base64.b64encode(data or png()).decode()})


class HttpTest(ServerCase):
    def test_upload(self):
        status, body = self.upload()
        self.assertEqual(status, 200)
        self.assertEqual((body["ok"], body["file"], body["width"], body["height"]), (True, "a.png", 2, 1))

    def test_upload_bad_base64(self):
        status, body = self.jcall("POST", "/wechat/image/upload", {"name": "a.png", "data": "不是base64"})
        self.assertEqual(status, 400)
        self.assertIn("base64", body["error"])

    def test_upload_not_image(self):
        status, _ = self.upload("a.jpg", b"%PDF-1.4 fake")
        self.assertEqual(status, 400)

    def test_upload_over_decoded_limit(self):
        server.IMAGE_MAX = 10
        status, body = self.upload()
        self.assertEqual(status, 400)
        self.assertIn("太大", body["error"])

    def test_body_over_limit_is_413(self):
        server.IMAGE_MAX = 10
        status, _ = self.call("POST", "/wechat/image/upload", raw=b"x" * 70000)
        self.assertEqual(status, 413)

    def test_list_images_newest_first(self):
        self.upload("a.png")
        self.upload("b.png")
        os.utime(os.path.join(server.OUTBOX, "a.png"), (1, 1))
        status, body = self.jcall("GET", "/wechat/images")
        self.assertEqual(status, 200)
        self.assertEqual([i["file"] for i in body["images"]], ["b.png", "a.png"])
        self.assertIn("mtime", body["images"][0])

    def test_list_images_empty_outbox(self):
        status, body = self.jcall("GET", "/wechat/images")
        self.assertEqual((status, body["images"]), (200, []))

    def test_thumb(self):
        self.upload()
        status, out = self.call("GET", "/wechat/image/thumb?file=a.png")
        self.assertEqual(status, 200)
        self.assertEqual(out[:3], b"\xff\xd8\xff")

    def test_thumb_rejects_bad_or_missing(self):
        for q in ["../x.png", "nope.png", ""]:
            with self.subTest(q=q):
                self.assertEqual(self.call("GET", "/wechat/image/thumb?file=" + q)[0], 400)

    def test_send_image(self):
        self.upload()
        status, body = self.jcall("POST", "/wechat/send", {"to": "文件传输助手", "image": "a.png"})
        self.assertEqual((status, body["ok"], body["status"]), (200, True, "ok"))
        self.assertEqual(self.calls, [["--image", "文件传输助手",
                                       os.path.join(os.path.realpath(server.OUTBOX), "a.png")]])

    def test_send_text_unchanged(self):
        status, _ = self.jcall("POST", "/wechat/send", {"to": "文件传输助手", "text": "hi"})
        self.assertEqual((status, self.calls), (200, [["文件传输助手", "hi"]]))

    def test_send_needs_exactly_one_of_text_image(self):
        self.upload()
        for p in [{"to": "x"}, {"to": "x", "text": "hi", "image": "a.png"}]:
            with self.subTest(p=p):
                self.assertEqual(self.jcall("POST", "/wechat/send", p)[0], 400)
        self.assertEqual(self.calls, [])

    def test_send_image_missing_or_outside(self):
        for name in ["nope.png", "../a.png", "/etc/a.png"]:
            with self.subTest(name=name):
                self.assertEqual(self.jcall("POST", "/wechat/send", {"to": "x", "image": name})[0], 400)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

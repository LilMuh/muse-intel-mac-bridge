import base64
import http.client
import json
import os
import plistlib
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from http.server import ThreadingHTTPServer
from unittest import mock

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


def notif_blob(title, body, chatname):
    """按微信通知的真实结构造一条 record.data：usda 是 NSKeyedArchiver 归档的 {chatname, unique_id}。"""
    U = plistlib.UID
    usda = plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": U(1)},
                           "$objects": ["$null", {"NS.keys": [U(2), U(3)], "NS.objects": [U(4), U(5)], "$class": U(6)},
                                        "chatname", "unique_id", chatname, chatname + "_1790000000_1",
                                        {"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]}]},
                          fmt=plistlib.FMT_BINARY)
    req = {"body": body, "usda": usda, "iden": "x"}
    if title is not None:
        req["titl"] = title
    return plistlib.dumps({"req": req}, fmt=plistlib.FMT_BINARY)


def make_notify_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR);"
        "CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB, request_date REAL,"
        " request_last_date REAL, delivered_date REAL, presented Bool, style INTEGER, snooze_fire_date REAL);"
        "INSERT INTO app VALUES (1, 'com.test.wechat'), (2, 'com.test.other');")
    con.commit()
    con.close()


def add_notif(path, when, title="张三", body="在吗", chatname="wxid_zs", app_id=1):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO record (app_id, data, delivered_date) VALUES (?, ?, ?)",
                (app_id, notif_blob(title, body, chatname), when - 978307200))
    con.commit()
    con.close()


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

    def test_outbox_path_allows_spaces_in_hand_dropped_names(self):
        self.assertTrue(server.outbox_path("截屏2026-09-28 10.00.00.png").endswith("截屏2026-09-28 10.00.00.png"))
        with self.assertRaises(ValueError):
            server.outbox_path(" a.png")

    def test_clipboard_rejects_finder_file(self):
        self.addCleanup(setattr, server, "clipboard_has_file", getattr(server, "clipboard_has_file", None))
        server.clipboard_has_file = lambda: True
        with self.assertRaisesRegex(ValueError, "是 Finder 复制的文件"):
            server.a_wechat_image_clipboard({})
        self.assertEqual(os.listdir(server.OUTBOX), [])

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


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = os.path.join(self.tmp, "db")
        make_notify_db(self.db)
        self.addCleanup(setattr, server, "NOTIFY_DBS", server.NOTIFY_DBS)
        server.NOTIFY_DBS = [os.path.join(self.tmp, "missing"), self.db]

    def test_reads_after_since_for_this_app_only(self):
        add_notif(self.db, 1000, body="旧的")
        add_notif(self.db, 2000, title="张三", body="在吗", chatname="wxid_zs")
        add_notif(self.db, 2001, title="别的 App", app_id=2)
        add_notif(self.db, 3000, title=None, body="你收到了一条消息", chatname="custom_id7")
        got = server.read_notifications("com.test.WeChat", 1500)   # bundle ID 大小写不同也要对上
        self.assertEqual([(n["chat"], n["id"], n["preview"]) for n in got],
                         [("张三", "wxid_zs", "在吗"), ("", "custom_id7", "你收到了一条消息")])
        self.assertEqual(got[0]["time"], time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(2000)))

    def test_bad_usda_keeps_title(self):
        data = plistlib.dumps({"req": {"titl": "张三", "body": "在吗", "usda": b"garbage"}}, fmt=plistlib.FMT_BINARY)
        self.assertEqual(server.parse_notification(data, 0)["id"], "")
        self.assertEqual(server.parse_notification(data, 0)["chat"], "张三")

    def test_no_database(self):
        server.NOTIFY_DBS = [os.path.join(self.tmp, "missing")]
        with self.assertRaises(OSError):
            server.read_notifications("com.test.wechat", 0)

    def test_corrupt_database(self):
        bad = os.path.join(self.tmp, "bad")
        with open(bad, "wb") as f:
            f.write(b"not a database" * 100)
        server.NOTIFY_DBS = [bad]
        with self.assertRaises(OSError):
            server.read_notifications("com.test.wechat", 0)

    def test_parse_badge(self):
        self.assertIsNone(server.parse_badge(""))                                   # 没在运行
        self.assertEqual(server.parse_badge('"StatusLabel"=[ NULL ] \n'), 0)       # 在运行，从没设过角标
        self.assertEqual(server.parse_badge('"StatusLabel"={ "label"="" }'), 0)
        self.assertEqual(server.parse_badge('"StatusLabel"={ "label"="3" }'), 3)
        self.assertEqual(server.parse_badge('"StatusLabel"={ "label"="99+" }'), 99)
        self.assertEqual(server.parse_badge('"StatusLabel"={ "label"="•" }'), 1)


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def app(self, name, bundle):
        path = os.path.join(self.tmp, name + ".app")
        os.makedirs(os.path.join(path, "Contents"))
        with open(os.path.join(path, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": bundle}, f)
        return path

    def test_alias_and_default(self):
        a, b = self.app("WeChat", "com.tencent.xinWeChat"), self.app("WeChat2", "com.tencent.xinWeChat2")
        with mock.patch.dict(os.environ, {"WX_ACCOUNTS": f"zhiwuzhu={a}, work={b}"}):
            self.assertEqual(server.wechat_bundle("work"), "com.tencent.xinWeChat2")
            with self.assertRaises(ValueError):
                server.wechat_bundle("nobody")
            with self.assertRaises(ValueError):
                server.wechat_bundle(None)          # 配了两个，没指定账号
        with mock.patch.dict(os.environ, {"WX_ACCOUNTS": f"zhiwuzhu={a}"}):
            self.assertEqual(server.wechat_bundle(None), "com.tencent.xinWeChat")

    def test_missing_app(self):
        with mock.patch.dict(os.environ, {"WX_ACCOUNTS": f"x={self.tmp}/Nope.app"}):
            with self.assertRaises(ValueError):
                server.wechat_bundle("x")


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
        self.calls, self.envs = [], []
        self.wx_out = "✅ 已发送"
        self.wx_code = 0
        server.PENDING_DIR = os.path.join(self.tmp, "pending")
        server.PEEK_DIR = os.path.join(self.tmp, "peek")
        self.db = os.path.join(self.tmp, "notify.db")
        make_notify_db(self.db)
        self.badge = 0
        for name, fake in (("NOTIFY_DBS", [self.db]), ("wechat_bundle", lambda account: "com.test.WeChat"),
                           ("read_badge", lambda bundle: self.badge)):
            self.addCleanup(setattr, server, name, getattr(server, name))
            setattr(server, name, fake)

        def fake_run_wx(args, account=None, env=None, timeout=300):
            self.calls.append(args)
            self.envs.append(env or {})
            return self.wx_code, self.wx_out, ""
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
        self.headers = dict(r.getheaders())
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

    def test_upload_only_checks_request_size(self):
        server.IMAGE_MAX = 10   # 请求上限约 64 KB，图片本身超过 10 字节也照收
        status, body = self.upload()
        self.assertEqual((status, body["file"]), (200, "a.png"))

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


class InboxTest(ServerCase):
    def setUp(self):
        super().setUp()
        server.INBOX = os.path.join(self.tmp, "inbox")
        os.makedirs(server.INBOX)
        with open(os.path.join(server.INBOX, "in-20260928-141500-1.png"), "wb") as f:
            f.write(png())

    def test_inbox_file(self):
        status, out = self.call("GET", "/wechat/inbox/file?file=in-20260928-141500-1.png")
        self.assertEqual((status, out), (200, png()))
        self.assertEqual(self.headers["Content-Type"], "image/png")

    def test_inbox_file_rejects_bad_or_missing(self):
        for q in ["../x.png", "nope.png", "", "a/b.png"]:
            with self.subTest(q=q):
                self.assertEqual(self.call("GET", "/wechat/inbox/file?file=" + q)[0], 400)

    def test_inbox_does_not_serve_outbox(self):
        self.upload("a.png")
        self.assertEqual(self.call("GET", "/wechat/inbox/file?file=a.png")[0], 400)

    def test_read_images_env(self):
        self.wx_out = '{"chat": "x", "items": []}'
        self.assertEqual(self.jcall("POST", "/wechat/read", {"chat": "x", "images": True})[0], 200)
        self.assertEqual(self.envs[-1], {"WX_IMAGES": "1", "WX_MAX_IMAGES": "10"})
        self.jcall("POST", "/wechat/read", {"chat": "x"})
        self.assertEqual(self.envs[-1], {})

    def test_unread_images_env(self):
        self.wx_out = '{"chats": []}'
        status, _ = self.jcall("POST", "/wechat/unread", {"images": True, "max_images": 3, "max_chats": 2})
        self.assertEqual(status, 200)
        self.assertEqual(self.envs[-1], {"WX_UNREAD_MAX_CHATS": "2", "WX_IMAGES": "1", "WX_MAX_IMAGES": "3"})

    def test_max_images_bounds(self):
        self.wx_out = '{"chat": "x", "items": []}'
        for n in [0, 51]:
            with self.subTest(n=n):
                self.assertEqual(self.jcall("POST", "/wechat/read", {"chat": "x", "images": True, "max_images": n})[0], 400)


class PendingTest(ServerCase):
    CHATS = {"chats": [{"name": "张三", "group": False, "messages": [
        {"type": "time", "text": "15:42"}, {"type": "message", "text": "明天几点？"},
        {"type": "message", "text": "图片", "image": "in-1.png"}]}]}

    def unread(self, chats=None, **p):
        self.wx_out = json.dumps(chats if chats is not None else self.CHATS)
        return self.jcall("POST", "/wechat/unread", p)

    def test_unread_adds_pending_marked_new(self):
        status, body = self.unread()
        self.assertEqual(status, 200)
        msgs = body["pending"]["张三"]["messages"]
        self.assertEqual([(m["id"], m["text"], m["time"], m["new"]) for m in msgs],
                         [(1, "明天几点？", "15:42", True), (2, "图片", "15:42", True)])
        self.assertEqual(msgs[1]["image"], "in-1.png")

    def test_pending_survives_next_unread(self):
        self.unread()
        _, body = self.unread({"chats": [{"name": "李四", "group": True, "messages": [
            {"type": "message", "text": "hi", "from": "other", "sender": "王五", "wxid": "w5"}]}]})
        self.assertNotIn("new", body["pending"]["张三"]["messages"][0])
        m = body["pending"]["李四"]["messages"][0]
        self.assertEqual((m["id"], m["new"], m["sender"], m["wxid"], body["pending"]["李四"]["group"]), (3, True, "王五", "w5", True))

    def test_unread_failure_keeps_pending(self):
        self.unread()
        self.wx_code = 6
        _, body = self.unread({"chats": []})
        self.assertFalse(body["ok"])
        self.assertEqual(len(body["pending"]["张三"]["messages"]), 2)

    def test_list_only_does_not_add(self):
        _, body = self.unread(list_only=True)
        self.assertEqual(body["pending"], {})

    def test_pending_endpoint(self):
        self.unread()
        status, body = self.jcall("POST", "/wechat/pending", {})
        self.assertEqual((status, body["count"]), (200, 2))
        self.assertNotIn("new", body["pending"]["张三"]["messages"][0])

    def test_ack_all_and_upto_and_unknown(self):
        self.unread()
        _, body = self.jcall("POST", "/wechat/ack", {"chat": "张三", "upto_id": 1})
        self.assertEqual(body["acked"], 1)
        _, body = self.jcall("POST", "/wechat/pending", {})
        self.assertEqual([m["id"] for m in body["pending"]["张三"]["messages"]], [2])
        self.assertEqual(self.jcall("POST", "/wechat/ack", {"chat": "张三"})[1]["acked"], 1)
        self.assertEqual(self.jcall("POST", "/wechat/pending", {})[1]["pending"], {})
        self.assertEqual(self.jcall("POST", "/wechat/ack", {"chat": "不存在"})[0], 400)

    def test_send_ok_clears_chat(self):
        self.unread()
        _, body = self.jcall("POST", "/wechat/send", {"to": "张三", "text": "三点"})
        self.assertEqual(body["acked"], 2)
        self.assertEqual(self.jcall("POST", "/wechat/pending", {})[1]["count"], 0)

    def test_send_ok_even_if_cache_write_fails(self):
        self.unread()
        orig = server.save_pending
        def broken(account, data):
            raise OSError("磁盘满了")
        server.save_pending = broken
        self.addCleanup(setattr, server, "save_pending", orig)
        status, body = self.jcall("POST", "/wechat/send", {"to": "张三", "text": "三点"})
        self.assertEqual((status, body["ok"], body["acked"]), (200, True, 0))
        self.assertIn("磁盘满了", body["ack_error"])

    def test_send_not_cleared_on_failure_or_dry_run(self):
        self.unread()
        self.wx_code = 10
        self.assertEqual(self.jcall("POST", "/wechat/send", {"to": "张三", "text": "x"})[1]["acked"], 0)
        self.wx_code = 0
        self.assertEqual(self.jcall("POST", "/wechat/send", {"to": "张三", "text": "x", "dry_run": True})[1]["acked"], 0)
        self.assertEqual(self.jcall("POST", "/wechat/pending", {})[1]["count"], 2)

    def test_accounts_are_separate(self):
        self.unread(account="work")
        self.assertEqual(self.jcall("POST", "/wechat/pending", {})[1]["count"], 0)
        self.assertEqual(self.jcall("POST", "/wechat/pending", {"account": "work"})[1]["count"], 2)

    def test_clear_pending_upto_keeps_later(self):
        data = {"next_id": 3, "chats": {"x": {"group": False, "messages": [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]}}}
        self.assertEqual(server.clear_pending(data, "x", upto=1), 1)
        self.assertEqual([m["id"] for m in data["chats"]["x"]["messages"]], [2])

    def test_save_is_atomic_replace(self):
        server.save_pending(None, {"next_id": 1, "chats": {}})
        self.assertEqual(os.listdir(server.PENDING_DIR), ["default.json"])

    def test_prune_inbox_keeps_pending_images(self):
        server.INBOX = os.path.join(self.tmp, "inbox")
        os.makedirs(server.INBOX)
        for n in ("in-1.png", "in-2.png"):
            path = os.path.join(server.INBOX, n)
            with open(path, "wb") as f:
                f.write(png())
            os.utime(path, (1, 1))
        self.unread()   # 缓存里引用了 in-1.png
        server.prune_inbox(3)
        self.assertEqual(sorted(os.listdir(server.INBOX)), ["in-1.png"])


class PeekTest(ServerCase):
    def peek(self, account=None):
        status, body = self.jcall("GET", "/wechat/peek" + (f"?account={account}" if account else ""))
        self.assertEqual(status, 200, body)
        return body

    def state(self, **kw):
        st = {"since": time.time() - 100, "badge_base": 0, "reminded": 0}
        st.update(kw)
        server.save_peek(None, st)

    def unread(self, **p):
        self.wx_out = json.dumps({"chats": []})
        return self.jcall("POST", "/wechat/unread", p)

    def test_first_peek_ignores_history(self):
        add_notif(self.db, time.time() - 60)
        self.badge = 3
        body = self.peek()
        self.assertEqual((body["wake"], body["reasons"], body["new"], body["badge"], body["badge_base"]),
                         (False, [], [], 3, 3))

    def test_new_until_unread_succeeds(self):
        self.state()
        add_notif(self.db, time.time() - 50, title="张三", body="在吗", chatname="wxid_zs")
        body = self.peek()
        self.assertEqual(body["reasons"], ["new"])
        self.assertEqual([(n["chat"], n["id"], n["preview"]) for n in body["new"]], [("张三", "wxid_zs", "在吗")])
        self.assertTrue(self.peek()["wake"])          # 没跑 unread 之前一直唤醒
        self.wx_code = 6
        self.unread()
        self.assertTrue(self.peek()["wake"])          # unread 失败不算
        self.wx_code = 0
        self.unread(list_only=True)
        self.assertTrue(self.peek()["wake"])          # list_only 不算
        self.unread()
        self.assertFalse(self.peek()["wake"])

    def test_notification_during_unread_still_new(self):
        self.state()
        def slow_run_wx(args, account=None, env=None, timeout=300):
            add_notif(self.db, time.time())           # unread 跑的过程中进来的
            return 0, json.dumps({"chats": []}), ""
        server.run_wx = slow_run_wx
        self.jcall("POST", "/wechat/unread", {})
        self.assertEqual(self.peek()["reasons"], ["new"])

    def test_unread_ok_even_if_peek_state_fails(self):
        def broken(account, st):
            raise OSError("磁盘满了")
        self.addCleanup(setattr, server, "save_peek", server.save_peek)
        server.save_peek = broken
        status, body = self.unread()
        self.assertEqual((status, body["ok"]), (200, True))

    def test_badge_against_base(self):
        self.badge = 3
        self.peek()                                    # 基线 3
        self.badge = 1
        self.assertEqual((self.peek()["wake"], self.peek()["badge_base"]), (False, 1))   # 手机上读掉了：基线跟着降
        self.badge = 2
        body = self.peek()
        self.assertEqual((body["reasons"], body["badge_base"]), (["badge"], 1))
        self.badge = 5
        self.unread()                                  # unread 后基线取当时的角标
        self.assertEqual((self.peek()["wake"], self.peek()["badge_base"]), (False, 5))

    def test_badge_none_when_not_running(self):
        self.badge = None
        body = self.peek()
        self.assertEqual((body["badge"], body["wake"]), (None, False))

    def test_stale_reminds_once_per_interval(self):
        server.save_pending(None, {"next_id": 3, "chats": {
            "李四": {"group": False, "messages": [{"id": 1, "text": "x", "added": "2026-01-01T00:00:00"}]},
            "王五": {"group": False, "messages": [{"id": 2, "text": "y", "added": time.strftime("%Y-%m-%dT%H:%M:%S")}]}}})
        body = self.peek()
        self.assertEqual((body["reasons"], body["pending"]), (["stale"], 2))
        self.assertEqual(body["stale"], [{"chat": "李四", "count": 1, "oldest": "2026-01-01T00:00:00"}])
        body = self.peek()
        self.assertEqual((body["wake"], len(body["stale"])), (False, 1))   # 30 分钟内不重复提醒
        st = server.load_peek(None)
        st["reminded"] -= 31 * 60
        server.save_peek(None, st)
        self.assertEqual(self.peek()["reasons"], ["stale"])

    def test_stale_minutes_env(self):
        two_min_ago = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 120))
        server.save_pending(None, {"next_id": 2, "chats": {"李四": {"group": False, "messages": [
            {"id": 1, "text": "x", "added": two_min_ago}]}}})
        self.assertEqual(self.peek()["stale"], [])
        with mock.patch.dict(os.environ, {"WX_PEEK_STALE_MIN": "1"}):
            self.assertEqual(len(self.peek()["stale"]), 1)
        with mock.patch.dict(os.environ, {"WX_PEEK_STALE_MIN": "0"}):
            self.assertEqual(self.jcall("GET", "/wechat/peek")[0], 400)

    def test_notify_db_missing(self):
        server.NOTIFY_DBS = [os.path.join(self.tmp, "nope")]
        body = self.peek()
        self.assertIn("打不开通知数据库", body["notify_error"])
        self.assertEqual((body["wake"], body["badge"]), (False, 0))

    def test_account_state_is_separate(self):
        seen = []
        server.wechat_bundle = lambda account: seen.append(account) or "com.test.WeChat"
        self.peek("work")
        self.assertEqual(seen, ["work"])
        self.assertTrue(os.path.isfile(os.path.join(server.PEEK_DIR, "work.json")))
        self.assertFalse(os.path.isfile(os.path.join(server.PEEK_DIR, "default.json")))

    def test_bad_account_is_400(self):
        def bad(account):
            raise ValueError("未知账号：x")
        server.wechat_bundle = bad
        self.assertEqual(self.jcall("GET", "/wechat/peek?account=x")[0], 400)

    def test_does_not_wait_for_global_lock(self):
        t0 = time.time()
        with server.lock:
            self.peek()
        self.assertLess(time.time() - t0, 2)

    def test_logged_only_when_waking(self):
        with self.assertLogs("bridge") as cm:
            self.peek()                                # 不唤醒：不记
            self.jcall("GET", "/wechat/images")        # 别的请求照常记（也让 assertLogs 至少有一条）
            time.sleep(0.2)
        self.assertEqual([r.getMessage().split("\t")[1] for r in cm.records], ["/wechat/images"])
        add_notif(self.db, time.time() + 1)
        with self.assertLogs("bridge") as cm:
            self.peek()
            time.sleep(0.2)
        self.assertIn("/wechat/peek", cm.records[0].getMessage())


class MabTest(ServerCase):
    def mab(self, *args):
        env = dict(os.environ, MAB_URL=f"http://127.0.0.1:{self.httpd.server_port}", MAB_TOKEN=TOKEN)
        return subprocess.run([sys.executable, os.path.join(ROOT, "mab.py"), *args],
                              capture_output=True, text=True, env=env, cwd=self.tmp, timeout=60)

    def local_png(self, name="photo.png"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(png())
        return path

    def test_peek(self):
        add_notif(self.db, time.time() + 1)
        server.save_peek("work", {"since": time.time() - 10, "badge_base": 0, "reminded": 0})
        r = self.mab("wechat-peek", "-a", "work")
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertEqual((body["wake"], body["new"][0]["chat"]), (True, "张三"))

    def test_send_local_file_uploads_then_sends(self):
        r = self.mab("wechat-send", "文件传输助手", "--image", self.local_png())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(server.OUTBOX, "photo.png")))
        self.assertEqual(self.calls[-1][:2], ["--image", "文件传输助手"])
        self.assertTrue(self.calls[-1][2].endswith("/outbox/photo.png"))

    def test_send_reports_renamed_upload(self):
        self.upload("photo.png")
        r = self.mab("wechat-send", "文件传输助手", "--image", self.local_png())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("photo-1.png", r.stderr)
        self.assertTrue(self.calls[-1][2].endswith("/outbox/photo-1.png"))

    def test_send_outbox_name(self):
        self.upload("a.png")
        r = self.mab("wechat-send", "文件传输助手", "--image", "a.png", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.calls[-1][2].endswith("/outbox/a.png"))
        self.assertTrue(json.loads(r.stdout)["dry_run"])

    def test_send_missing_vm_path(self):
        r = self.mab("wechat-send", "文件传输助手", "--image", "/tmp/nope-mab-test/x.jpg")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("找不到文件", r.stderr)
        self.assertEqual(self.calls, [])

    def test_send_text_and_image_conflict(self):
        r = self.mab("wechat-send", "文件传输助手", "hi", "--image", "a.png")
        self.assertNotEqual(r.returncode, 0)
        r = self.mab("wechat-send", "文件传输助手")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls, [])

    def test_send_text_unchanged(self):
        r = self.mab("wechat-send", "文件传输助手", "hi")
        self.assertEqual((r.returncode, self.calls), (0, [["文件传输助手", "hi"]]))

    def test_upload_with_name(self):
        r = self.mab("wechat-upload", self.local_png(), "--name", "x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["file"], "x.png")

    def test_images_and_thumb(self):
        self.upload("a.png")
        r = self.mab("wechat-images")
        self.assertEqual([i["file"] for i in json.loads(r.stdout)["images"]], ["a.png"])
        r = self.mab("wechat-thumb", "a.png", "-o", "t.jpg")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.tmp, "t.jpg"), "rb") as f:
            self.assertEqual(f.read(3), b"\xff\xd8\xff")

    def inbox_png(self, name="in-1.png"):
        server.INBOX = os.path.join(self.tmp, "inbox")
        os.makedirs(server.INBOX, exist_ok=True)
        with open(os.path.join(server.INBOX, name), "wb") as f:
            f.write(png())

    def test_read_images_downloads_and_adds_local_path(self):
        self.inbox_png()
        self.wx_out = json.dumps({"chat": "x", "items": [
            {"type": "message", "text": "图片", "image": "in-1.png"},
            {"type": "message", "text": "图片", "image": "in-missing.png"},
            {"type": "message", "text": "hi"}]})
        r = self.mab("wechat-read", "x", "--images", "--save-dir", "got")
        self.assertEqual(r.returncode, 0, r.stderr)
        items = json.loads(r.stdout)["items"]
        self.assertEqual(os.path.realpath(items[0]["local_path"]),
                         os.path.realpath(os.path.join(self.tmp, "got", "in-1.png")))
        with open(items[0]["local_path"], "rb") as f:
            self.assertEqual(f.read(), png())
        self.assertIn("download_error", items[1])
        self.assertNotIn("local_path", items[2])
        self.assertEqual(self.envs[-1]["WX_IMAGES"], "1")

    def test_read_images_save_error_keeps_result(self):
        self.inbox_png()
        with open(os.path.join(self.tmp, "got"), "w") as f:
            f.write("不是目录")
        self.wx_out = json.dumps({"chat": "x", "items": [{"type": "message", "text": "图片", "image": "in-1.png"}]})
        r = self.mab("wechat-read", "x", "--images", "--save-dir", "got")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("download_error", json.loads(r.stdout)["items"][0])

    def test_unread_images_downloads(self):
        self.inbox_png()
        self.wx_out = json.dumps({"chats": [{"name": "g", "messages": [{"type": "message", "text": "图片", "image": "in-1.png"}]}]})
        r = self.mab("wechat-unread", "--images", "--max-images", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads(r.stdout)["chats"][0]["messages"][0]
        self.assertTrue(os.path.isfile(m["local_path"]))
        self.assertEqual(self.envs[-1]["WX_MAX_IMAGES"], "3")

    def test_read_without_images_unchanged(self):
        self.wx_out = json.dumps({"chat": "x", "items": [{"type": "message", "text": "图片", "image": "in-1.png"}]})
        r = self.mab("wechat-read", "x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("local_path", r.stdout)
        self.assertEqual(self.envs[-1], {})

    def test_pending_and_ack_commands(self):
        self.wx_out = json.dumps({"chats": [{"name": "张三", "messages": [{"type": "message", "text": "在吗"}]}]})
        self.assertEqual(self.mab("wechat-unread").returncode, 0)
        r = self.mab("wechat-pending")
        self.assertEqual(json.loads(r.stdout)["count"], 1, r.stderr)
        r = self.mab("wechat-ack", "张三")
        self.assertEqual(json.loads(r.stdout)["acked"], 1, r.stderr)

    def test_unread_images_downloads_pending_once(self):
        self.inbox_png()
        self.wx_out = json.dumps({"chats": [{"name": "张三", "messages": [{"type": "message", "text": "图片", "image": "in-1.png"}]}]})
        self.mab("wechat-unread")                      # 第一次：进缓存
        self.wx_out = json.dumps({"chats": []})
        r = self.mab("wechat-unread", "--images", "--save-dir", "got")   # 第二次：只在 pending 里
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads(r.stdout)["pending"]["张三"]["messages"][0]
        self.assertTrue(os.path.isfile(m["local_path"]))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "got")), ["in-1.png"])


class LogTest(ServerCase):
    def test_logs_failure_reason(self):
        self.wx_code, self.wx_out = 2, "搜不到"
        with self.assertLogs("bridge") as cm:
            self.jcall("POST", "/wechat/read", {"chat": "张三", "account": "work"})
            self.jcall("POST", "/wechat/read", {"chat": "张三", "limit": 0})
        ok, bad = cm.records[0].getMessage(), cm.records[1].getMessage()
        self.assertIn("/wechat/read\t200", ok)
        self.assertIn('"chat": "张三"', ok)
        self.assertIn("not_found: 搜不到", ok)
        self.assertIn("\t400\t", bad)
        self.assertIn("limit 至少是 1", bad)

    def test_logs_recall_on_read(self):
        items = [{"type": "message", "text": "\"张三\" 撤回了一条消息"}, {"type": "message", "text": "你撤回了一条消息"},
                 {"type": "message", "text": "好"}]
        self.wx_out = json.dumps({"chat": "张三", "items": items})
        with self.assertLogs("bridge") as cm:
            _, body = self.jcall("POST", "/wechat/read", {"chat": "张三", "account": "work"})
            time.sleep(0.2)
        recalls = [r.getMessage() for r in cm.records if r.getMessage().startswith("撤回")]
        self.assertEqual(recalls, ['撤回\twork\t张三\t"张三" 撤回了一条消息'])
        self.assertEqual(body["items"], items)        # 返回内容不变

    def test_logs_recall_on_unread(self):
        self.wx_out = json.dumps({"chats": [{"name": "李四", "group": False, "messages": [
            {"type": "message", "text": "对方撤回了一条消息"}]}]})
        with self.assertLogs("bridge") as cm:
            self.jcall("POST", "/wechat/unread", {})
            time.sleep(0.2)
        self.assertIn("撤回\tdefault\t李四\t对方撤回了一条消息", [r.getMessage() for r in cm.records])

    def test_logs_client_gone(self):
        closed = threading.Event()

        def slow_run_wx(args, account=None, env=None, timeout=300):
            closed.wait(10)
            time.sleep(0.3)
            return 0, '{"chat":"张三","items":[]}', ""
        server.run_wx = slow_run_wx
        s = socket.create_connection(("127.0.0.1", self.httpd.server_port))
        body = json.dumps({"chat": "张三"}).encode()
        s.sendall(b"POST /wechat/read HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
                  + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # 关闭时直接发 RST
        with self.assertLogs("bridge") as cm:
            time.sleep(0.2)
            s.close()
            closed.set()
            for _ in range(50):
                if cm.records:
                    break
                time.sleep(0.1)
        self.assertIn("回复时连接已断开", cm.records[0].getMessage())


if __name__ == "__main__":
    unittest.main()

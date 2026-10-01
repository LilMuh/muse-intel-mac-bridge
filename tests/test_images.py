import base64
import http.client
import json
import os
import plistlib
import re
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

    def test_bad_usda_keeps_title(self):
        data = plistlib.dumps({"req": {"titl": "张三", "body": "在吗", "usda": b"garbage"}}, fmt=plistlib.FMT_BINARY)
        self.assertEqual(server.parse_notification(data, 0)["id"], "")
        self.assertEqual(server.parse_notification(data, 0)["chat"], "张三")

    def test_reads_all_for_this_app(self):
        add_notif(self.db, 1000, body="一")                                     # rec 1
        add_notif(self.db, 2001, title="别的 App", app_id=2)                    # rec 2
        add_notif(self.db, 3000, title="张三", body="二", chatname="wxid_zs")   # rec 3
        rows = server.read_notifications("com.test.WeChat")                    # bundle ID 大小写不同也要对上
        self.assertEqual([(r["rec"], r["when"]) for r in rows], [(1, 1000), (3, 3000)])
        self.assertEqual(server.parse_notification(rows[1]["data"], rows[1]["when"])["preview"], "二")
        self.assertNotEqual(rows[0]["key"], rows[1]["key"])

    def test_empty_app(self):
        self.assertEqual(server.read_notifications("com.test.wechat"), [])

    def test_no_database(self):
        server.NOTIFY_DBS = [os.path.join(self.tmp, "missing")]
        with self.assertRaises(OSError):
            server.read_notifications("com.test.wechat")

    def test_corrupt_database(self):
        bad = os.path.join(self.tmp, "bad")
        with open(bad, "wb") as f:
            f.write(b"not a database" * 100)
        server.NOTIFY_DBS = [bad]
        with self.assertRaises(OSError):
            server.read_notifications("com.test.wechat")

    def notice(self, body, chat_id="wxid_zs"):
        return server.notice_message({"rec": 7, "chat": "x", "id": chat_id, "preview": body,
                                      "time": "2026-09-29T14:25:30"})

    def test_notice_private_text(self):
        self.assertEqual(self.notice("明天几点？"), {"time": "14:25", "rec": 7, "text": "明天几点？"})

    def test_notice_needs_read(self):
        self.assertEqual(self.notice("[图片] ")["needs_read"], ["image"])
        self.assertEqual(self.notice("一" * 66)["needs_read"], ["truncated"])        # 198 字节
        self.assertNotIn("needs_read", self.notice("一" * 65))                        # 195 字节
        self.assertEqual(self.notice("a" * 196)["needs_read"], ["truncated"])

    def test_notice_group(self):
        g = "123@chatroom"
        self.assertEqual(self.notice("李四: 时间: 3点", g), {"time": "14:25", "rec": 7, "sender": "李四", "text": "时间: 3点"})
        self.assertEqual(self.notice("李四: [图片] ", g)["needs_read"], ["image"])
        m = self.notice("李四在群聊中@了你", g)
        self.assertEqual((m["sender"], m["text"], m["needs_read"]), ("李四", "李四在群聊中@了你", ["mention"]))
        self.assertEqual(self.notice("小助手在群聊中@了所有人", g)["needs_read"], ["mention"])
        self.assertEqual(self.notice("没有冒号的系统消息", g), {"time": "14:25", "rec": 7, "text": "没有冒号的系统消息"})
        self.assertNotIn("needs_read", self.notice("李四在群聊中@了你"))            # 私聊不算 @

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
        server.SENT_DIR = os.path.join(self.tmp, "sent")
        self.db =os.path.join(self.tmp, "notify.db")
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


class DuplicateSendTest(ServerCase):
    def send(self, **p):
        return self.jcall("POST", "/wechat/send", dict({"to": "张三", "text": "试用码 1234", "account": "work"}, **p))[1]

    def test_same_text_again_is_refused(self):
        self.assertEqual(self.send()["status"], "ok")
        body = self.send()
        self.assertEqual((body["ok"], body["status"], body["last_status"]), (False, "duplicate_recent", "ok"))
        self.assertEqual(len(self.calls), 1)

    def test_force_sends_again(self):
        self.send()
        self.assertEqual(self.send(force=True)["status"], "ok")
        self.assertEqual(len(self.calls), 2)

    def test_other_text_chat_or_account_still_sends(self):
        self.send()
        for p in [{"text": "试用码 5678"}, {"to": "李四"}, {"account": "home"}]:
            with self.subTest(p=p):
                self.assertEqual(self.send(**p)["status"], "ok")
        self.assertEqual(len(self.calls), 4)

    def test_same_image_again_is_refused(self):
        self.upload()
        self.assertEqual(self.send(text=None, image="a.png")["status"], "ok")
        self.assertEqual(self.send(text=None, image="a.png")["status"], "duplicate_recent")
        self.assertEqual(self.send()["status"], "ok")   # 同一个人的文字不受影响

    def test_unconfirmed_counts_as_sent(self):
        self.wx_code = 10
        self.send()
        self.wx_code = 0
        body = self.send()
        self.assertEqual((body["status"], body["last_status"]), ("duplicate_recent", "unconfirmed_do_not_retry"))

    def test_failed_send_can_retry(self):
        for code in (4, 8):
            with self.subTest(code=code):
                self.wx_code = code
                self.send()
                self.wx_code = 0
                self.assertEqual(self.send(text=f"重试 {code}")["status"], "ok")

    def test_dry_run_is_not_recorded_or_blocked(self):
        self.send(dry_run=True)
        self.assertEqual(self.send()["status"], "ok")
        self.assertEqual(self.send(dry_run=True)["status"], "ok")
        self.assertEqual(len(self.calls), 3)

    def test_window_expires(self):
        self.send()
        with mock.patch.object(server.time, "time", return_value=time.time() + 11 * 60):
            self.assertEqual(self.send()["status"], "ok")

    def test_window_env(self):
        with mock.patch.dict(os.environ, {"WX_DUP_MIN": "0"}):
            self.send()
            self.assertEqual(self.send()["status"], "ok")
        with mock.patch.dict(os.environ, {"WX_DUP_MIN": "x"}):
            self.assertEqual(self.jcall("POST", "/wechat/send", {"to": "张三", "text": "hi"})[0], 400)

    def test_record_has_no_plain_text(self):
        self.send()
        files = os.listdir(server.SENT_DIR)
        self.assertEqual(files, ["work.json"])
        with open(os.path.join(server.SENT_DIR, files[0]), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("试用码", raw)
        self.assertNotIn("张三", raw)


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

    def test_first_peek_ignores_history(self):
        add_notif(self.db, time.time() - 60)
        self.badge = 3
        body = self.peek()
        self.assertEqual((body["wake"], body["reasons"], body["new"], body["pending"], body["badge"]),
                         (False, [], [], 0, 3))
        self.assertNotIn("badge_base", body)
        self.assertNotIn("incomplete", body)

    def test_new_message_wakes(self):
        self.peek()
        add_notif(self.db, time.time(), title="张三", body="[图片] ", chatname="wxid_zs")
        body = self.peek()
        self.assertEqual((body["reasons"], body["pending"]), (["new"], 1))
        self.assertEqual(body["new"], [{"chat": "张三", "id": 1, "text": "[图片] ", "needs_read": ["image"]}])
        self.jcall("POST", "/wechat/pending", {})
        body = self.peek()
        self.assertEqual((body["wake"], body["new"], body["pending"]), (False, [], 1))   # 已经交给 Muse 了

    def test_new_keeps_waking_until_muse_fetches(self):
        self.peek()
        add_notif(self.db, time.time(), title="张三", body="在吗")
        self.assertEqual(self.peek()["reasons"], ["new"])
        self.assertEqual(self.peek()["reasons"], ["new"])   # 这次唤醒可能被 hook 丢掉：没交给 Muse 之前一直唤醒
        self.jcall("POST", "/wechat/pending", {})
        self.assertFalse(self.peek()["wake"])

    def test_badge_is_only_informational(self):
        self.peek()
        self.badge = 9
        self.assertFalse(self.peek()["wake"])
        self.badge = None
        self.assertIsNone(self.peek()["badge"])

    def test_notify_db_missing(self):
        server.NOTIFY_DBS = [os.path.join(self.tmp, "nope")]
        body = self.peek()
        self.assertIn("打不开通知数据库", body["notify_error"])
        self.assertEqual((body["wake"], body["new"]), (False, []))

    def test_stale_reminds_once_per_interval(self):
        server.save_pending(None, {"next_id": 3, "chats": {
            "李四": {"group": False, "messages": [{"id": 1, "text": "x", "added": "2026-01-01T00:00:00", "shown": True}]},
            "王五": {"group": False, "messages": [{"id": 2, "text": "y", "added": time.strftime("%Y-%m-%dT%H:%M:%S"), "shown": True}]}}})
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

    def test_account_state_is_separate(self):
        seen = []
        server.wechat_bundle = lambda account: seen.append(account) or "com.test.WeChat"
        self.peek("work")
        self.assertEqual(set(seen), {"work"})
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


class IngestTest(ServerCase):
    def pending(self):
        status, body = self.jcall("POST", "/wechat/pending", {})
        self.assertEqual(status, 200, body)
        return body

    def test_first_call_skips_history(self):
        add_notif(self.db, time.time() - 60)
        body = self.pending()
        self.assertEqual(body["count"], 0)
        st = server.load_peek(None)
        self.assertEqual((len(st["seen"]), st["reminded"]), (1, 0))

    def test_bad_record_is_skipped_and_not_reread(self):
        self.pending()
        add_notif(self.db, time.time(), body="好的")
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO record (app_id, data, delivered_date) VALUES (1, ?, ?)", (b"garbage", time.time() - 978307200))
        con.execute("INSERT INTO record (app_id, data, delivered_date) VALUES (1, NULL, ?)", (time.time() - 978307200,))
        con.commit()
        con.close()
        self.assertEqual([m["text"] for m in self.pending()["pending"]["张三"]["messages"]], ["好的"])
        self.assertEqual(len(server.load_peek(None)["seen"]), 3)   # 坏记录也记为收过，不会每次重读

    def test_new_private_message(self):
        self.pending()
        add_notif(self.db, time.time(), title="张三", body="在吗", chatname="wxid_zs")
        add_notif(self.db, time.time(), title="别的 App", app_id=2)
        chat = self.pending()["pending"]["张三"]
        self.assertEqual((chat["group"], chat["chat_id"]), (False, "wxid_zs"))
        m = chat["messages"][0]
        self.assertEqual((m["id"], m["text"], m["rec"], m["new"]), (1, "在吗", 1, True))
        self.assertNotIn("new", self.pending()["pending"]["张三"]["messages"][0])   # 同一条只收一次

    def test_group_and_needs_read(self):
        self.pending()
        add_notif(self.db, time.time(), title="项目群", body="李四: [图片] ", chatname="123@chatroom")
        chat = self.pending()["pending"]["项目群"]
        self.assertEqual((chat["group"], chat["messages"][0]["sender"], chat["messages"][0]["needs_read"]),
                         (True, "李四", ["image"]))

    def test_rename_moves_chat_and_merges(self):
        self.pending()
        add_notif(self.db, time.time(), title="小明", body="一", chatname="wxid_xm")
        add_notif(self.db, time.time(), title="备注 小明", body="占位", chatname="wxid_other")
        self.pending()   # 「备注 小明」这个名字已经被另一个聊天占着：合并时两边的消息都不能丢
        add_notif(self.db, time.time(), title="备注 小明", body="二", chatname="wxid_xm")
        pending = self.pending()["pending"]
        self.assertNotIn("小明", pending)
        self.assertEqual([m["text"] for m in pending["备注 小明"]["messages"]], ["一", "占位", "二"])
        self.assertEqual(pending["备注 小明"]["chat_id"], "wxid_xm")

    def test_no_title_uses_chat_id(self):
        self.pending()
        add_notif(self.db, time.time(), title=None, body="你收到了一条消息", chatname="wxid_zs")
        chat = self.pending()["pending"]["wxid_zs"]
        self.assertTrue(chat["name_unknown"])
        add_notif(self.db, time.time(), title="张三", body="在吗", chatname="wxid_zs")   # 后来开了消息详情
        pending = self.pending()["pending"]
        self.assertNotIn("wxid_zs", pending)
        self.assertNotIn("name_unknown", pending["张三"])
        self.assertEqual(len(pending["张三"]["messages"]), 2)

    def test_upgrade_from_since(self):
        server.save_peek(None, {"since": time.time() - 100, "badge_base": 2, "reminded": 5, "incomplete": True})
        add_notif(self.db, time.time() - 200, body="升级前的")
        add_notif(self.db, time.time() - 50, body="升级后的")
        self.assertEqual([m["text"] for m in self.pending()["pending"]["张三"]["messages"]], ["升级后的"])
        st = server.load_peek(None)
        self.assertEqual((sorted(st), len(st["seen"]), st["reminded"]), (["reminded", "seen"], 2, 5))

    def test_gap_note(self):
        for _ in range(100):
            add_notif(self.db, time.time())
        server.save_peek(None, {"seen": ["早就被挤掉的"], "reminded": 0})   # 100 条全是没见过的：中间可能有被挤掉的
        self.assertIn("可能漏了", self.pending()["note"])
        self.assertNotIn("note", self.pending())

    def test_notify_error_keeps_state(self):
        server.save_peek(None, {"seen": ["a"], "reminded": 0})
        server.NOTIFY_DBS = [os.path.join(self.tmp, "nope")]
        body = self.pending()
        self.assertIn("打不开通知数据库", body["notify_error"])
        self.assertEqual(server.load_peek(None)["seen"], ["a"])

    def test_save_failure_keeps_state(self):
        self.pending()
        add_notif(self.db, time.time())
        def broken(account, data):
            raise OSError("磁盘满了")
        orig = server.save_pending
        server.save_pending = broken
        self.assertEqual(self.jcall("POST", "/wechat/pending", {})[0], 400)
        server.save_pending = orig
        self.assertEqual(self.pending()["count"], 1)   # 下次还会重收这一条

    def test_ingest_and_ack_do_not_overwrite_each_other(self):
        self.pending()
        server.save_pending(None, {"next_id": 2, "chats": {"李四": {"group": False, "messages": [
            {"id": 1, "text": "x", "added": "2026-09-29T00:00:00", "shown": True}]}}})
        add_notif(self.db, time.time(), title="张三", body="在吗")
        acked = []
        orig = server.add_notices

        def slow(data, notices):
            t = threading.Thread(target=lambda: acked.append(self.jcall("POST", "/wechat/ack", {"chat": "李四"})))
            t.start()
            t.join(0.5)   # 有 cache_lock 时 ack 要等收消息写完；没有锁的话 ack 会先写，再被收消息覆盖
            self.addCleanup(t.join)
            return orig(data, notices)
        server.add_notices = slow
        self.addCleanup(setattr, server, "add_notices", orig)
        server.ingest(None)   # 直接调用，不拿全局锁：ack（POST）只会被 cache_lock 挡住
        for _ in range(50):
            if acked:
                break
            time.sleep(0.1)
        self.assertEqual(sorted(server.load_pending(None)["chats"]), ["张三"])

    def test_send_only_clears_messages_muse_has_seen(self):
        self.pending()
        add_notif(self.db, time.time(), title="张三", body="一")
        self.pending()                                  # Muse 看到了「一」
        add_notif(self.db, time.time(), title="张三", body="二")
        self.jcall("GET", "/wechat/peek")               # peek 在后台收进了「二」，Muse 还没看到
        _, body = self.jcall("POST", "/wechat/send", {"to": "张三", "text": "好的"})
        self.assertEqual(body["acked"], 1)
        self.assertEqual([m["text"] for m in server.load_pending(None)["chats"]["张三"]["messages"]], ["二"])

    def test_ack_only_clears_messages_muse_has_seen(self):
        self.pending()
        add_notif(self.db, time.time(), title="张三", body="一")
        self.pending()
        add_notif(self.db, time.time(), title="张三", body="二")
        self.jcall("GET", "/wechat/peek")
        self.assertEqual(self.jcall("POST", "/wechat/ack", {"chat": "张三"})[1]["acked"], 1)
        self.assertEqual([m["text"] for m in server.load_pending(None)["chats"]["张三"]["messages"]], ["二"])

    def test_reused_rec_id_is_still_new(self):
        self.pending()
        add_notif(self.db, time.time(), title="张三", body="明天 2 点")
        self.pending()
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM record WHERE rec_id = 1")   # 对方撤回：最新一条通知被删掉
        con.commit()
        con.close()
        add_notif(self.db, time.time() + 1, title="张三", body="明天 3 点")   # 重发的这条拿到同一个 rec_id
        self.assertEqual([m["text"] for m in self.pending()["pending"]["张三"]["messages"]], ["明天 2 点", "明天 3 点"])

    def test_unread_does_not_touch_peek_state(self):
        self.wx_out = json.dumps({"chats": []})
        self.jcall("POST", "/wechat/unread", {})
        self.assertIsNone(server.load_peek(None))


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
        server.save_peek("work", {"seen": [], "reminded": 0})
        add_notif(self.db, time.time())
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

    def test_send_force(self):
        self.assertEqual(self.mab("wechat-send", "张三", "hi").returncode, 0)
        r = self.mab("wechat-send", "张三", "hi")
        self.assertEqual(json.loads(r.stdout)["status"], "duplicate_recent")
        r = self.mab("wechat-send", "张三", "hi", "--force")
        self.assertEqual((json.loads(r.stdout)["status"], len(self.calls)), ("ok", 2))

    def test_request_id_is_logged(self):
        with self.assertLogs("bridge") as cm:
            self.mab("wechat-images")
            time.sleep(0.2)
        self.assertRegex(cm.records[0].getMessage(), r"\t[0-9a-f]{8}$")

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


class MabDisconnectTest(unittest.TestCase):
    def serve(self, drops):
        """起一个假 bridge：前 drops 个连接读完请求就直接断开，之后的正常回 {"ok": true}。"""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        self.addCleanup(srv.close)
        self.conns, self.rids = 0, []

        def loop():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                self.conns += 1
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                head, _, body = buf.partition(b"\r\n\r\n")
                rid = re.search(rb"x-request-id: (\w+)", head, re.I)
                self.rids.append(rid and rid.group(1).decode())
                m = re.search(rb"content-length: (\d+)", head, re.I)
                while m and len(body) < int(m.group(1)):
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    body += chunk
                if self.conns > drops:
                    out = b'{"ok": true}'
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                              b"Connection: close\r\n\r\n%s" % (len(out), out))
                c.close()
        threading.Thread(target=loop, daemon=True).start()
        return srv.getsockname()[1]

    def mab(self, port, *args):
        env = dict(os.environ, MAB_URL=f"http://127.0.0.1:{port}", MAB_TOKEN=TOKEN)
        return subprocess.run([sys.executable, os.path.join(ROOT, "mab.py"), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_peek_retries_once_after_disconnect(self):
        port = self.serve(drops=1)
        r = self.mab(port, "wechat-peek", "-a", "work")
        self.assertEqual((r.returncode, r.stdout.strip(), self.conns), (0, '{"ok": true}', 2), r.stderr)
        self.assertEqual(self.rids[0], self.rids[1])   # 重试是同一个请求，编号不变

    def test_pending_gives_up_after_second_disconnect(self):
        port = self.serve(drops=9)
        r = self.mab(port, "wechat-pending", "-a", "work")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("已重试", r.stderr)
        self.assertEqual(self.conns, 2)

    def test_send_does_not_retry_after_disconnect(self):
        port = self.serve(drops=9)
        r = self.mab(port, "wechat-send", "张三", "在吗", "-a", "work")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("结果未知", r.stderr)
        self.assertIn(f"请求编号 {self.rids[0]}", r.stderr)
        self.assertEqual(self.conns, 1)


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

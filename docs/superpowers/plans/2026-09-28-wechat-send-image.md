# 微信发送图片 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Muse 通过 bridge 给微信联系人发一张图片，图片来源是 VM 上传、Mac 剪贴板或 `outbox/` 目录。

**Architecture:** 所有图片先落到仓库的 `outbox/`，发送时只传文件名。Python 服务端负责存图、校验、列表、缩略图，并把 `/wechat/send` 的 `image` 转成 `wx-send.sh --image 联系人 绝对路径`。Swift 端用 `Outgoing` enum 区分文字和图片，复用现有的 `openChat` → `sendInChat` 流程，只替换粘贴和核对这两步。

**Tech Stack:** Python 3.9+ 标准库（服务端另依赖 pyautogui）、macOS 自带的 `sips` / `osascript`、Swift 5.10（在 `wx-send.sh` 的 heredoc 里，运行时编译）。

**Spec:** `docs/superpowers/specs/2026-09-28-wechat-send-image-design.md`

**与 spec 的一处顺序调整：** spec 的「第 0 步试验」放在 Task 5 做。试验直接用 Task 4 写好的 Swift 代码跑（加了临时开关 `WX_IMAGE_PASTE` 切换粘贴方式），因为失败时报错信息里会带出输入框内容和最后一行的标题，不用再另写一个探测程序。Task 1–3 是纯 Python 部分，不依赖试验结论。

## Global Constraints

- Python 代码要兼容 3.9：不用 `X | None`、`match`，也不用 3.10 以后才有的 API。README 写的是「Python 3.9+」，系统自带的是 3.9.6。
- `mab.py` 只能依赖标准库。
- 不新增 pip 依赖。
- Swift 编译目标是 `$(uname -m)-apple-macos13.0`，编译器是 Swift 5.10。
- 不新增退出码或状态码。文件有问题用 64（`bad_request`），`WX_STATUS` 不改。
- 只能发送 `outbox/` 里的图片。文件名只允许字母、数字、`._-` 和中文，不能包含 `/` 或 `..`，不能以 `.` 开头，扩展名只能是 `jpg jpeg png gif heic`，`realpath` 解析后必须在 `outbox/` 里面。
- `WX_IMAGE_MAX_MB` 默认 20。
- 缩略图长边 512，格式 JPEG。
- 等待时间：图片粘贴后最多等 3 秒看到输入框变化，发出后最多等 10 秒确认。文字保持原来的 1.5 秒和 3 秒。
- 代码风格：注释用中文；方法的文档注释只写一行；只注释代码本身看不出来的东西。
- 提交信息用中文，末尾加 `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`。
- 不要提交工作区里原有的 `start.sh` 改动和 `.idea/`。只 `git add` 本任务涉及的文件。
- Bash 工具的每次调用之间 shell 变量不会保留：用到 `$A`（微信账号别名）、`$S`（scratchpad 路径）、`$T`/`$P`/`MAB_*` 的命令，每次都要在同一条命令里重新 export。

## Review Focus

1. **从 VM 传一个不存在的路径**（`--image /tmp/nope.jpg`）：应该在本地直接报「找不到文件」，而不是被当成 outbox 文件名发到服务端，得到一个看不懂的「文件名不合规」。由 Task 3 的 `test_send_missing_vm_path` 覆盖。
2. **非图片文件改成 `.jpg` 后上传**（例如 PDF）：应该按文件头判断并拒绝，不能存进 outbox。由 Task 1 的 `test_save_rejects_non_image` 覆盖。
3. **outbox 里有指向外部的符号链接，或文件名里带 `..`**：必须拒绝，防止把 Mac 上的任意文件发出去。由 Task 1 的 `test_outbox_path_rejects*` 覆盖。
4. **剪贴板里是 Finder 复制的文件（不是图片数据）或纯文字**：应该返回「剪贴板里没有图片」，而不是存进一个空文件或坏文件。由 Task 2 的手动步骤覆盖（自动测试会改掉用户的剪贴板，所以不做）。
5. **输入框里已有草稿时发图**：应该返回 `draft_in_input`，不能把图片粘贴到草稿后面一起发出去。由 Task 5 的真机步骤覆盖。

---

## 文件结构

| 文件 | 改动 | 负责什么 |
|---|---|---|
| `mac_agent_server.py` | 修改 | outbox 相关函数、新接口、`/wechat/send` 支持 `image`、请求体大小上限 |
| `mab.py` | 修改 | `wechat-send --image`、`wechat-upload`、`wechat-clip`、`wechat-images`、`wechat-thumb` |
| `wechat/wx-send.sh` | 修改 | bash 部分加 `--image`；Swift 部分加 `Outgoing`、`pasteImage`、`image` 模式 |
| `tests/test_images.py` | 新建 | 服务端和 mab 的单元测试、HTTP 测试 |
| `.gitignore` | 修改 | 加 `outbox/` |
| `README.md`、`docs/muse-prompt.md` | 修改 | 使用文档 |
| `docs/superpowers/specs/2026-09-28-wechat-send-image-design.md` | 修改 | 填写试验结论 |

运行测试的命令（全文统一）：`/usr/bin/python3 -m unittest discover -s tests -v`（用 3.9 跑，顺便检查兼容性）。

---

### Task 1: outbox 存图和校验函数

**Files:**
- Modify: `mac_agent_server.py`（import 区、常量区，以及 `take_screenshot` 读宽高的那几行 85–90）
- Modify: `.gitignore`
- Create: `tests/test_images.py`

**Interfaces:**
- Produces（都在 `mac_agent_server` 模块里）：
  - `OUTBOX: str`：outbox 的绝对路径，测试里可以改写
  - `IMAGE_MAX: int`：单张图片最大字节数
  - `image_kind(data: bytes) -> Optional[str]`：返回 `"jpg" | "png" | "gif" | "heic" | None`
  - `clean_stem(name: Optional[str]) -> str`
  - `outbox_path(file: str) -> str`：返回绝对路径，不合规时抛 `ValueError`
  - `sips_size(path: str) -> Tuple[int, int]`
  - `save_image(data: bytes, name: Optional[str] = None) -> str`：返回最终文件名
  - `image_info(file: str) -> dict`：返回 `{"file","width","height","bytes"}`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_images.py`：

```python
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
    """生成一张纯红的 PNG。"""
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
```

- [ ] **Step 2: 运行，确认测试失败**

运行：`/usr/bin/python3 -m unittest discover -s tests -v`
预期：全部 ERROR，报 `AttributeError: module 'mac_agent_server' has no attribute ...`。

- [ ] **Step 3: 实现**

在 `mac_agent_server.py` 的 import 区加上 `re`、`time`，以及 `typing` 里的类型（保持字母顺序）：

```python
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import urlparse
```

在 `WX_SEND = ...` 那段后面加上：

```python
OUTBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outbox")
IMAGE_MAX = int(float(os.environ.get("WX_IMAGE_MAX_MB", "20")) * 1024 * 1024)
IMAGE_EXTS = ("jpg", "jpeg", "png", "gif", "heic")
IMAGE_NAME = re.compile(r"^[A-Za-z0-9_一-鿿-][A-Za-z0-9._一-鿿-]*$")
```

把 `take_screenshot` 里读宽高的那几行：

```python
        info = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", out],
            check=True, capture_output=True, text=True,
        ).stdout
        w = int(info.split("pixelWidth:")[1].split()[0])
        h = int(info.split("pixelHeight:")[1].split()[0])
```

替换成：

```python
        w, h = sips_size(out)
```

在 `take_screenshot` 前面加上：

```python
def sips_size(path) -> Tuple[int, int]:
    """用 sips 读图片的像素宽高。"""
    info = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                          check=True, capture_output=True, text=True).stdout
    return int(info.split("pixelWidth:")[1].split()[0]), int(info.split("pixelHeight:")[1].split()[0])
```

在 `# wx-send.sh 的退出码` 前面加上 outbox 相关函数：

```python
def image_kind(data) -> Optional[str]:
    """按文件头判断图片格式，认不出返回 None。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:4] == b"GIF8":
        return "gif"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1"):
        return "heic"
    return None


def clean_stem(name) -> str:
    """取文件名主干，不允许的字符换成 _，去掉开头的点。"""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = re.sub(r"[^A-Za-z0-9._一-鿿-]", "_", stem)
    return re.sub(r"\.{2,}", ".", stem).lstrip(".")[:60]


def outbox_path(file) -> str:
    """outbox 里的文件名 → 绝对路径；不合规或解析后跑出 outbox 就报错。"""
    if not isinstance(file, str) or not IMAGE_NAME.match(file) or ".." in file \
            or "." not in file or file.rsplit(".", 1)[1].lower() not in IMAGE_EXTS:
        raise ValueError(f"文件名不合规：{file!r}（只能是 outbox 里的 jpg/png/gif/heic 文件名）")
    root = os.path.realpath(OUTBOX)
    path = os.path.realpath(os.path.join(root, file))
    if os.path.dirname(path) != root:
        raise ValueError(f"文件名不合规：{file!r}（指向了 outbox 外面）")
    return path


def save_image(data, name=None) -> str:
    """图片存进 outbox，返回最终文件名；扩展名以内容为准，HEIC 转成 JPEG，重名加 -1、-2。"""
    kind = image_kind(data)
    if kind is None:
        raise ValueError("不是 JPEG / PNG / GIF / HEIC 图片")
    stem = clean_stem(name) or time.strftime("img-%Y%m%d-%H%M%S")
    ext = "jpg" if kind == "heic" else kind
    os.makedirs(OUTBOX, exist_ok=True)
    file, n = f"{stem}.{ext}", 0
    while os.path.lexists(os.path.join(OUTBOX, file)):
        n += 1
        file = f"{stem}-{n}.{ext}"
    path = outbox_path(file)
    if kind == "heic":
        with tempfile.NamedTemporaryFile(suffix=".heic") as tmp:
            tmp.write(data)
            tmp.flush()
            subprocess.run(["sips", "-s", "format", "jpeg", tmp.name, "--out", path],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        with open(path, "xb") as f:
            f.write(data)
    return file


def image_info(file) -> dict:
    path = outbox_path(file)
    w, h = sips_size(path)
    return {"file": file, "width": w, "height": h, "bytes": os.path.getsize(path)}
```

在 `.gitignore` 末尾加一行：

```
outbox/
```

- [ ] **Step 4: 运行，确认测试通过**

运行：`/usr/bin/python3 -m unittest discover -s tests -v`
预期：`OutboxTest` 全部 OK。`test_save_heic_converts_to_jpg` 可能是 skipped，这也算通过。

另外手动截一次图，确认抽出 `sips_size` 之后截图功能没坏：

```bash
/usr/bin/python3 -c "
import sys, types; sys.modules['pyautogui'] = types.SimpleNamespace(FAILSAFE=1, PAUSE=0, size=lambda: (1, 1))
import mac_agent_server as s; d, w, h = s.take_screenshot(); print(len(d), w, h)"
```

预期：打印出字节数和 `1280 <高>`。

- [ ] **Step 5: 提交**

```bash
git add mac_agent_server.py .gitignore tests/test_images.py
git commit -m "bridge 新增 outbox 存图和文件名校验

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 服务端图片接口，以及 `/wechat/send` 支持 image

**Files:**
- Modify: `mac_agent_server.py`（顶部说明、`__version__`、`a_wechat_send`、`ACTIONS`、`do_GET`、`do_POST`）
- Test: `tests/test_images.py`

**Interfaces:**
- Consumes: Task 1 的 `outbox_path`、`save_image`、`image_info`、`IMAGE_MAX`、`OUTBOX`
- Produces（HTTP 接口）：
  - `POST /wechat/image/upload {"name?","data"}`：返回 `{"ok",file,width,height,bytes}`
  - `POST /wechat/image/clipboard {"name?"}`：返回同上
  - `GET /wechat/images`：返回 `{"ok","images":[{file,width,height,bytes,mtime}]}`，最新的在前
  - `GET /wechat/image/thumb?file=`：返回 JPEG
  - `POST /wechat/send {"to","text"|"image","account?","dry_run?"}`：给 `image` 时调用 `run_wx(["--image", to, 绝对路径], ...)`
  - 参数错误：POST 和 GET 都返回 400
  - 请求体超过 `IMAGE_MAX * 4 // 3 + 65536` 字节：返回 413

- [ ] **Step 1: 写失败测试**

在 `tests/test_images.py` 顶部的 import 里补上 `base64`、`http.client`、`json`、`threading`，以及 `from http.server import ThreadingHTTPServer`。然后在 `OutboxTest` 后面、`if __name__` 前面加上：

```python
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
```

- [ ] **Step 2: 运行，确认测试失败**

运行：`/usr/bin/python3 -m unittest discover -s tests -v`
预期：`HttpTest` 大部分失败。上传相关的返回 404 unknown action；发送图片的返回 400「缺少 text」。

- [ ] **Step 3: 实现**

在 import 区加上 `base64`、`binascii`，并把 `from urllib.parse import urlparse` 改成：

```python
from urllib.parse import parse_qs, urlparse
```

`__version__` 改成 `"0.3.0"`。

把 `a_wechat_send` 整个换成：

```python
def a_wechat_send(p):
    to = text_arg(p, "to")
    if bool(p.get("text")) == bool(p.get("image")):
        raise ValueError("text 和 image 要给一个，且只能给一个")
    if p.get("image"):
        path = outbox_path(p["image"])
        if not os.path.isfile(path):
            raise ValueError(f"outbox 里没有 {p['image']}（先用 /wechat/image/upload 或 /wechat/image/clipboard 放进去）")
        args = ["--image", to, path]
    else:
        args = [to, text_arg(p, "text")]
    env = {"WX_DRY_RUN": "1"} if p.get("dry_run") else {}
    code, out, err = run_wx(args, p.get("account"), env)
    return {"ok": code == 0, "code": code, "status": WX_STATUS.get(code, "error"),
            "dry_run": bool(p.get("dry_run")), "output": "\n".join(x for x in (out, err) if x)}
```

在 `a_wechat_friends` 后面加上：

```python
def a_wechat_image_upload(p):
    try:
        data = base64.b64decode(text_arg(p, "data"), validate=True)
    except binascii.Error:
        raise ValueError("data 不是合法的 base64")
    if len(data) > IMAGE_MAX:
        raise ValueError(f"图片太大，最大 {IMAGE_MAX // 1048576} MB（WX_IMAGE_MAX_MB）")
    return {"ok": True, **image_info(save_image(data, p.get("name")))}


def a_wechat_image_clipboard(p):
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        # 剪贴板里没有图片时，第一行就会报错退出
        subprocess.run(["osascript", "-e", "on run argv",
                        "-e", "set d to the clipboard as «class PNGf»",
                        "-e", "set f to open for access (POSIX file (item 1 of argv)) with write permission",
                        "-e", "write d to f", "-e", "close access f", "-e", "end run", tmp],
                       capture_output=True, timeout=30)
        with open(tmp, "rb") as f:
            data = f.read()
    finally:
        os.remove(tmp)
    if image_kind(data) != "png":
        raise ValueError("剪贴板里没有图片（Finder 里复制的文件不算，要复制图片本身）")
    return {"ok": True, **image_info(save_image(data, p.get("name") or time.strftime("clip-%Y%m%d-%H%M%S")))}


def list_images():
    images = []
    for f in os.listdir(OUTBOX) if os.path.isdir(OUTBOX) else []:
        try:
            info = image_info(f)
        except (ValueError, subprocess.CalledProcessError):
            continue
        info["mtime"] = int(os.path.getmtime(outbox_path(f)))
        images.append(info)
    images.sort(key=lambda i: (-i["mtime"], i["file"]))
    return {"ok": True, "images": images}


def thumbnail(file):
    """outbox 里图片的缩略图：长边 512 的 JPEG。"""
    path = outbox_path(file)
    if not os.path.isfile(path):
        raise ValueError(f"outbox 里没有 {file}")
    fd, out = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        subprocess.run(["sips", "-Z", "512", "-s", "format", "jpeg", "-s", "formatOptions", "70", path, "--out", out],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(out, "rb") as f:
            return f.read()
    finally:
        os.remove(out)
```

`ACTIONS` 里加两项：

```python
    "wechat/friends": a_wechat_friends,
    "wechat/image/upload": a_wechat_image_upload, "wechat/image/clipboard": a_wechat_image_clipboard,
```

在 `do_GET` 的 try 块里，`if path == "/info":` 前面加上：

```python
            if path == "/wechat/images":
                return self._send(200, list_images())
            if path == "/wechat/image/thumb":
                file = parse_qs(urlparse(self.path).query).get("file", [""])[0]
                return self._send(200, thumbnail(file), "image/jpeg")
```

并在 `do_GET` 的 `except Exception` 前面加上：

```python
        except ValueError as e:
            return self._send(400, {"error": str(e)})
```

在 `do_POST` 里，把 `length = int(...)` 那一行换成：

```python
            length = int(self.headers.get("Content-Length", 0))
            if length > IMAGE_MAX * 4 // 3 + 65536:
                # 分块读掉丢弃：不读完就回复的话，客户端常收到连接重置而不是 413
                while length > 0:
                    chunk = self.rfile.read(min(length, 65536))
                    if not chunk:
                        break
                    length -= len(chunk)
                return self._send(413, {"error": f"请求太大，图片最大 {IMAGE_MAX // 1048576} MB（WX_IMAGE_MAX_MB）"})
```

在 `main()` 里，`print(...)` 前面加一行，这样用户可以直接往 `outbox/` 里放图片：

```python
    os.makedirs(OUTBOX, exist_ok=True)
```

顶部说明里，在 `POST /wechat/friends` 那一行后面加上：

```
  POST /wechat/send   {"to":"联系人","image":"a.jpg"}          发 outbox 里的一张图片（text 和 image 二选一）
  POST /wechat/image/upload    {"name":"a.jpg","data":"<base64>"}  图片存进 outbox
  POST /wechat/image/clipboard {"name":"x.png"}                  Mac 剪贴板里的图片存进 outbox
  GET  /wechat/images                                           列出 outbox 里的图片
  GET  /wechat/image/thumb?file=a.jpg                           缩略图（长边 512 的 JPEG）
```

环境变量部分加上：

```
  WX_IMAGE_MAX_MB  单张图片上限，默认 20
```

- [ ] **Step 4: 运行，确认测试通过**

运行：`/usr/bin/python3 -m unittest discover -s tests -v`
预期：全部 OK（HEIC 那个可以是 skipped）。

- [ ] **Step 5: 手动验证剪贴板（Review Focus 第 4 条）**

先告诉用户：这一步会用到剪贴板。然后在 scratchpad 里起一个临时服务：

```bash
export T=$(openssl rand -hex 16) P=8799
AGENT_TOKEN=$T PORT=$P .venv/bin/python mac_agent_server.py   # 用 run_in_background
```

分三种情况，每种都先请用户在 Mac 上按要求复制，再执行：

```bash
curl -s -H "Authorization: Bearer $T" -X POST -d '{}' http://127.0.0.1:$P/wechat/image/clipboard
```

| 用户复制的内容 | 预期结果 |
|---|---|
| 一张图片（⌃⇧⌘4 截一块区域） | `{"ok": true, "file": "clip-....png", ...}` |
| 一段文字 | `{"error": "剪贴板里没有图片…"}`，outbox 里没有新文件 |
| Finder 里的一个 .jpg 文件 | 记录实际结果：如果返回了图片也可以接受，只要不是坏文件或空文件 |

跑完后停掉服务，删掉 `outbox/` 里的测试文件。

- [ ] **Step 6: 提交**

```bash
git add mac_agent_server.py tests/test_images.py
git commit -m "bridge 新增图片上传、剪贴板取图、列表、缩略图接口，/wechat/send 支持 image

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: mab.py 客户端命令

**Files:**
- Modify: `mab.py`
- Test: `tests/test_images.py`

**Interfaces:**
- Consumes: Task 2 的 HTTP 接口
- Produces（命令行）：
  - `wechat-send 联系人 [文字] [--image 路径或文件名] [--dry-run] [-a]`
  - `wechat-upload 路径 [--name]`
  - `wechat-clip [--name]`
  - `wechat-images`
  - `wechat-thumb 文件名 [-o thumb.jpg]`

- [ ] **Step 1: 写失败测试**

在 `HttpTest` 后面加上：

```python
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

    def test_send_local_file_uploads_then_sends(self):
        r = self.mab("wechat-send", "文件传输助手", "--image", self.local_png())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(server.OUTBOX, "photo.png")))
        self.assertEqual(self.calls[-1][:2], ["--image", "文件传输助手"])
        self.assertTrue(self.calls[-1][2].endswith("/outbox/photo.png"))

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
```

- [ ] **Step 2: 运行，确认测试失败**

运行：`/usr/bin/python3 -m unittest discover -s tests -v`
预期：`MabTest` 失败，argparse 报 `unrecognized arguments: --image` 或 `invalid choice: 'wechat-upload'`。`test_send_text_unchanged` 应该已经能通过。

- [ ] **Step 3: 实现**

import 区加上 `base64` 和 `urllib.parse`：

```python
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
```

在 `post` 后面加上：

```python
def upload(path, name=None):
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    body, _ = request("POST", "/wechat/image/upload",
                      {"name": name or os.path.basename(path), "data": data}, max(TIMEOUT, 120))
    return json.loads(body)
```

把 `ws = sub.add_parser("wechat-send")...` 这两行换成：

```python
    ws = sub.add_parser("wechat-send"); ws.add_argument("to"); ws.add_argument("text", nargs="?")
    ws.add_argument("--image", help="VM 里的图片路径（先上传），或 outbox 里的文件名")
    ws.add_argument("--dry-run", action="store_true"); ws.add_argument("-a", "--account")
```

在 `wfr = ...` 那一行后面加上：

```python
    wup = sub.add_parser("wechat-upload"); wup.add_argument("path"); wup.add_argument("--name")
    wc = sub.add_parser("wechat-clip"); wc.add_argument("--name")
    sub.add_parser("wechat-images")
    wt = sub.add_parser("wechat-thumb"); wt.add_argument("file"); wt.add_argument("-o", "--output", default="thumb.jpg")
```

把 `elif a.cmd == "wechat-send":` 这个分支换成：

```python
    elif a.cmd == "wechat-send":
        if (a.text is None) == (a.image is None):
            sys.exit("文字和 --image 要给一个，且只能给一个")
        p = {"to": a.to, "dry_run": a.dry_run, "account": a.account}
        if a.image is None:
            p["text"] = a.text
        elif os.path.isfile(a.image):
            p["image"] = upload(a.image)["file"]
        elif "/" in a.image:
            sys.exit(f"找不到文件：{a.image}")
        else:
            p["image"] = a.image   # 当作 outbox 里的文件名
        post("/wechat/send", p, max(TIMEOUT, 320))
```

在 `wechat-friends` 分支后面加上：

```python
    elif a.cmd == "wechat-upload":
        print(json.dumps(upload(a.path, a.name), ensure_ascii=False))
    elif a.cmd == "wechat-clip":
        post("/wechat/image/clipboard", {"name": a.name})
    elif a.cmd == "wechat-images":
        body, _ = request("GET", "/wechat/images"); print(body.decode())
    elif a.cmd == "wechat-thumb":
        body, _ = request("GET", "/wechat/image/thumb?file=" + urllib.parse.quote(a.file))
        with open(a.output, "wb") as f:
            f.write(body)
        print(f"saved {a.output} ({len(body) // 1024} KB)")
```

顶部用法说明：把 `wechat-send` 那一行换掉，并在 `wechat-friends` 后面加上新命令：

```
  python3 mab.py wechat-send "联系人" "消息" [--dry-run] [-a work]
  python3 mab.py wechat-send "联系人" --image 图片 [--dry-run] [-a work]   # VM 里的路径会先上传；否则当作 outbox 里的文件名
  ...
  python3 mab.py wechat-upload 图片路径 [--name a.jpg]
  python3 mab.py wechat-clip [--name x.png]          # Mac 剪贴板里的图片存进 outbox
  python3 mab.py wechat-images                       # 列出 outbox 里的图片
  python3 mab.py wechat-thumb a.jpg [-o thumb.jpg]   # 下载缩略图
```

- [ ] **Step 4: 运行，确认测试通过**

运行：`/usr/bin/python3 -m unittest discover -s tests -v`
预期：全部 OK。

- [ ] **Step 5: 提交**

```bash
git add mab.py tests/test_images.py
git commit -m "mab.py 新增发图片、上传、剪贴板取图、列表、缩略图命令

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: wx-send.sh 支持 `--image`（Swift + bash）

**Files:**
- Modify: `wechat/wx-send.sh`
  - 顶部用法说明（第 2–21 行）和 `usage()`（第 91 行）
  - `pasteText` 后面（约第 340 行）
  - `sendOne` / `sendInChat`（约第 1476–1582 行）
  - `greet`（约第 1320 行）
  - `run()` 的 jobs 部分（约第 1705–1770 行）
  - 最后的参数分发（约第 1830 行）

**Interfaces:**
- Consumes: Task 2 调用的形式 `wx-send.sh [-a 别名] --image 联系人 绝对路径`
- Produces:
  - Swift 的 `enum Outgoing { case text(String); case image(String) }`，带 `isImage`、`logText`、`paste()`、`inBox(_:)`、`isSentRow(_:)`
  - `func pasteImage(_ path: String)`
  - `let IMAGE_ROW_TITLES: Set<String>`
  - 临时环境变量 `WX_IMAGE_PASTE=image|data|url`，只给 Task 5 试验用，Task 5 结束时删掉

这一步没有自动化测试框架，靠下面三项来验证：
- 用 `swiftc -typecheck` 检查能编译
- 传一个坏文件，确认返回 64 而且不碰微信
- 回归：用文字试运行，确认行为和原来一样

- [ ] **Step 1: 记录修改前文字发送的行为（基线）**

先找出账号别名。不要打印 `.env` 里的 token：

```bash
grep -o '^WX_ACCOUNTS=.*' .env
```

记为 `A=<别名>`（机器上开着两个微信，必须带 `-a`）。然后执行：

```bash
WX_DRY_RUN=1 ./wechat/wx-send.sh -a "$A" "文件传输助手" "基线测试"; echo "exit=$?"
```

预期：`🧪 试运行：已粘贴到「文件传输助手」的输入框…` 和 `exit=0`。

然后清空输入框（运行完微信仍在最前，焦点在输入框里）：

```bash
osascript -e 'tell application "System Events" to keystroke "a" using command down' \
          -e 'tell application "System Events" to key code 51'
```

- [ ] **Step 2: 加 `Outgoing`、`pasteImage`、`IMAGE_ROW_TITLES`**

在 `pasteText` 函数后面加上：

```swift
/// 把图片放进剪贴板并粘贴；WX_IMAGE_PASTE 只给第 0 步试验切换写法用
func pasteImage(_ path: String) {
    let pb = NSPasteboard.general
    pb.clearContents()
    let url = URL(fileURLWithPath: path)
    switch env["WX_IMAGE_PASTE"] ?? "image" {
    case "url":
        pb.writeObjects([url as NSURL])
    case "data":
        let types: [String: NSPasteboard.PasteboardType] = [
            "png": .png, "gif": NSPasteboard.PasteboardType("com.compuserve.gif"),
            "jpg": NSPasteboard.PasteboardType("public.jpeg"), "jpeg": NSPasteboard.PasteboardType("public.jpeg")]
        if let t = types[url.pathExtension.lowercased()], let d = try? Data(contentsOf: url) { pb.setData(d, forType: t) }
    default:
        if let img = NSImage(contentsOf: url) { pb.writeObjects([img]) }
    }
    usleep(50_000)
    key(K_V, cmd: true)
}

/// 图片消息在聊天记录里的 AX 标题（第 0 步试验确认后改成实测值）
let IMAGE_ROW_TITLES: Set<String> = ["图片", "[图片]"]

/// 要发的内容：一段文字，或一张图片（文件绝对路径）
enum Outgoing {
    case text(String)
    case image(String)

    var isImage: Bool { if case .image = self { return true }; return false }

    var logText: String {
        switch self {
        case .text(let s): return s
        case .image(let p): return "[图片] " + (p as NSString).lastPathComponent
        }
    }

    func paste() {
        switch self {
        case .text(let s): pasteText(s)
        case .image(let p): pasteImage(p)
        }
    }

    /// 粘贴后输入框里的内容是不是这条
    func inBox(_ seen: String) -> Bool {
        switch self {
        case .text(let s): return sameInput(seen, s)
        case .image: return seen.trimmingCharacters(in: .whitespacesAndNewlines) == "\u{FFFC}"
        }
    }

    /// 聊天记录的最后一行是不是这条（行尾空格已去掉）
    func isSentRow(_ title: String) -> Bool {
        switch self {
        case .text(let s): return sameInput(title, s)
        case .image: return IMAGE_ROW_TITLES.contains(title.trimmingCharacters(in: .whitespaces))
        }
    }
}
```

- [ ] **Step 3: `sendOne` / `sendInChat` 改成接收 `Outgoing`**

`sendOne` 整个换成：

```swift
    /// 返回 (状态, 说明)。状态：SENT / DRY_RUN / FAILED / UNCONFIRMED
    func sendOne(_ contact: String, _ out: Outgoing) throws -> (String, String) {
        if case .text(let s) = out, s.isEmpty { throw fail(64, "EMPTY_MSG", "消息是空的") }
        var notes: [String] = []
        try openChat(contact, &notes)
        return try sendInChat(contact, out, notes)
    }
```

`sendInChat` 需要改这几处（其余不动）：

1. 函数签名：

```swift
    func sendInChat(_ contact: String, _ out: Outgoing, _ notesIn: [String]) throws -> (String, String) {
```

2. AX 输入框分支里，`pasteText(msg)` 到 `if !inBox {...}` 这一段换成：

```swift
            out.paste()
            var seen = ""
            let inBox = waitUntil(out.isImage ? 3 : 1.5, 0.05) {
                seen = axStr(box, kAXValueAttribute) ?? ""
                return out.inBox(seen)
            }
            if !inBox { throw fail(4, "INPUT_MISMATCH", "粘贴后输入框里是「\(preview(seen, 20))」，和消息不一致，已停止，未发送") }
```

3. `} else {` 分支（AX 读不到输入框）的开头加一个 guard。后面原有的代码不变，里面用到的 `msg` 就是这里取出来的：

```swift
        } else {
            // 截图识别认不出图片，不核对就不发
            guard case .text(let msg) = out else {
                throw fail(4, "NO_AX_INPUT", "AX 读不到输入框，无法核对图片，已停止，未发送")
            }
            notes.append("AX 读不到输入框，改用截图识别")
            let draft = try inputText()
            if !draft.isEmpty {
                throw fail(7, "DRAFT", "「\(contact)」的输入框里已有内容「\(preview(draft, 20))」，为免连草稿一起发出已停止。请清空后重试")
            }
            pasteText(msg)
            try checkPastedByOCR(msg, &notes)
        }
```

4. 「确认」部分：从 `// 4. 确认` 到 AX 分支结束的 `return ("UNCONFIRMED", ...)`，换成：

```swift
        // 4. 确认：「消息」列表多了一行、内容就是这条消息，输入框已清空，旁边没有红色感叹号
        let wait: Double = out.isImage ? 10 : 3
        if let list = msgList, let box = axInput() {
            var lastSeen = ""
            let end = Date().addingTimeInterval(wait)
            repeat {
                let rows = axChildren(list)
                if let row = rows.last {
                    lastSeen = axStr(row, kAXTitleAttribute) ?? ""
                    if lastSeen.hasSuffix(" ") { lastSeen.removeLast() }   // 微信在每条消息后面加了一个空格
                    let isNew = rows.count != rowsBefore.count || rowsBefore.last.map { !CFEqual($0, row) } ?? true
                    if isNew && out.isSentRow(lastSeen) && (axStr(box, kAXValueAttribute) ?? "x").isEmpty {
                        guard case .text(let msg) = out else {
                            return ("SENT", (notes + ["未检测发送失败标记"]).joined(separator: "；"))
                        }
                        if redMarkByOCR(norm(msg)) {
                            return ("FAILED", (notes + ["消息旁出现红色感叹号，发送失败"]).joined(separator: "；"))
                        }
                        return ("SENT", notes.joined(separator: "；"))
                    }
                }
                sleepS(0.1)
            } while Date() < end
            return ("UNCONFIRMED", (notes + ["\(Int(wait)) 秒内没在聊天记录最后看到这条消息（最后一条：「\(preview(lastSeen, 20))」），可能已发出，请人工确认，不要直接重试"]).joined(separator: "；"))
        }

        // AX 不可用时：截图识别聊天底部
        guard case .text(let msg) = out else {
            return ("UNCONFIRMED", (notes + ["AX 读不到聊天记录，无法确认图片是否发出，请人工确认，不要直接重试"]).joined(separator: "；"))
        }
```

后面原有的 `notes.append("AX 读不到聊天记录，改用截图识别确认")` 以及截图识别的代码保持不变（它们用的 `msg` 来自上面的 guard）。

- [ ] **Step 4: 改调用方：`greet` 和 `run()`**

`greet` 里：

```swift
        let (st, info) = try sendInChat(name, .text(msg), ["新好友打招呼"])
```

`run()` 里 jobs 部分换成：

```swift
        let jobs: [(String, Outgoing)]
        switch mode {
        case "send" where args.count == 5: jobs = [(args[3], .text(args[4]))]
        case "image" where args.count == 5:
            // 先确认文件是能读的图片，再去碰微信
            guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[4]) as CFURL, nil),
                  CGImageSourceGetCount(src) > 0 else {
                throw fail(64, "BAD_IMAGE", "读不了图片：\(args[4])")
            }
            jobs = [(args[3], .image(args[4]))]
        case "batch" where args.count == 4: jobs = try parseBatch(args[3]).map { ($0.0, Outgoing.text($0.1)) }
        default: eprint("内部参数错误"); return 64
        }
```

发送循环里：

- `let (contact, msg) = job` 改成 `let (contact, out) = job`
- `session.sendOne(contact, msg)` 改成 `session.sendOne(contact, out)`
- 两处 `writeLog(..., msg)` 的最后一个参数改成 `out.logText`

- [ ] **Step 5: bash 部分**

在顶部用法说明里，`--batch` 那一行后面加上：

```
#   ./wx-send.sh -a work --image "联系人" 图片路径      发一张图片（粘贴后核对，再发送）
```

因为用法说明多了一行，`usage()` 要改成：

```bash
usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 64; }
```

最后的参数分发里，在 `-*)      usage ;;` 前面加上：

```bash
  --image) [[ $# -eq 3 ]] || usage; exec "$BIN" image "$NAME" "$APP" "$2" "$3" ;;
```

- [ ] **Step 6: 类型检查**

```bash
S=/private/tmp/claude-502/-Users-tom-huang-muse-intel-mac-bridge/02817272-e822-4d41-91da-3950df001210/scratchpad
sed -n "/^cat <<'SWIFT'\$/,/^SWIFT\$/p" wechat/wx-send.sh | sed '1d;$d' > "$S/wx.swift"
swiftc -typecheck -target "$(uname -m)-apple-macos13.0" "$S/wx.swift" && echo TYPECHECK_OK
```

预期：`TYPECHECK_OK`，没有 error。

- [ ] **Step 7: 验证坏文件返回 64、不碰微信，以及用法说明**

```bash
./wechat/wx-send.sh -a "$A" --image "文件传输助手" /nonexistent.png; echo "exit=$?"
./wechat/wx-send.sh -a "$A" --image "文件传输助手"; echo "exit=$?"
```

预期：
- 第一条先编译（20–60 秒），然后打印 `❌ 读不了图片：/nonexistent.png` 和 `exit=64`，微信没有被切到前台。
- 第二条打印用法说明，最后一行是 `--image` 那一行，`exit=64`。

- [ ] **Step 8: 回归：文字发送**

重复 Step 1 的试运行和清空。预期输出和基线完全一样。

- [ ] **Step 9: 提交**

```bash
git add wechat/wx-send.sh
git commit -m "wx-send.sh 新增 --image：粘贴图片、核对输入框、确认发出

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 第 0 步试验，定下粘贴方式和图片标题

**这一步要在真实的微信上操作，需要用户配合。** 每次真的发送（不带 `WX_DRY_RUN`）前，都先告诉用户、等用户同意。所有发送都只发给「文件传输助手」。

**Files:**
- Modify: `wechat/wx-send.sh`（`pasteImage`、`IMAGE_ROW_TITLES`）
- Modify: `docs/superpowers/specs/2026-09-28-wechat-send-image-design.md`（「试验结论」一节）

- [ ] **Step 1: 生成测试图片**

```bash
S=/private/tmp/claude-502/-Users-tom-huang-muse-intel-mac-bridge/02817272-e822-4d41-91da-3950df001210/scratchpad
/usr/bin/python3 - "$S" <<'EOF'
import os, struct, sys, zlib
def png(w, h, px):
    c = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + px(y) for y in range(h))
    return b"\x89PNG\r\n\x1a\n" + c(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + c(b"IDAT", zlib.compress(raw)) + c(b"IEND", b"")
open(os.path.join(sys.argv[1], "red.png"), "wb").write(png(300, 200, lambda y: b"\xff\x30\x30" * 300))
open(os.path.join(sys.argv[1], "big.png"), "wb").write(png(4000, 3000, lambda y: os.urandom(12000)))
EOF
sips -s format jpeg "$S/red.png" --out "$S/red.jpg" >/dev/null
sips -s format gif "$S/red.png" --out "$S/red.gif" >/dev/null
ls -la "$S"/red.* "$S"/big.png
```

预期：生成 red.png、red.jpg、red.gif，以及大约 36 MB 的 big.png。

- [ ] **Step 2: 三种粘贴方式各试运行一次**

对 `V` 依次取 `image`、`data`、`url`，每次执行：

```bash
WX_DRY_RUN=1 WX_IMAGE_PASTE=$V ./wechat/wx-send.sh -a "$A" --image "文件传输助手" "$S/red.png"; echo "exit=$?"
screencapture -x "$S/paste-$V.png"
```

然后用 Read 工具查看 `paste-$V.png`，记录以下内容：
- 退出码，以及输出信息。`INPUT_MISMATCH` 的信息里会带出输入框的实际内容。
- 输入框里是图片缩略图还是文件图标。
- 有没有弹出「发送给…」之类的确认框。

记录完用 Task 4 Step 1 的 osascript 命令清空输入框。如果有弹框，先按 `Esc` 关掉。

**停止条件：** 三种方式都会弹出确认框，或者都会变成「文件」。这说明 spec 的前提不成立，停下来把观察到的现象报告给用户，不要继续。

- [ ] **Step 3: 选定粘贴方式，真发一次**

按顺序选第一个满足「`exit=0`、输入框里是图片缩略图、没有弹框」的方式：`image` → `data` → `url`。

先告诉用户「要往文件传输助手真发一张红色测试图」，得到同意后执行：

```bash
WX_IMAGE_PASTE=$V ./wechat/wx-send.sh -a "$A" --image "文件传输助手" "$S/red.png"; echo "exit=$?"
./wechat/wx-send.sh -a "$A" --read "文件传输助手" 3
```

记录：
- 如果 exit=0（✅）：说明 `IMAGE_ROW_TITLES` 猜对了。
- 如果 exit=10：报错信息里的「最后一条：「…」」就是图片消息的真实 AX 标题。
- `--read` 输出的 `items` 里最后一条的 `text` 也是这个标题。

- [ ] **Step 4: 定下粘贴方式和图片标题**

- 把 `IMAGE_ROW_TITLES` 改成实测到的标题，比如 `["图片"]`。注释改成「图片消息在聊天记录里的 AX 标题（实测）」。
- 去掉 `pasteImage` 里的 `WX_IMAGE_PASTE` 开关，只保留选定的那种写法，文档注释改成一行，说明为什么用这种写法（比如「用 NSImage 写入 TIFF：放文件 URL 的话微信会当成文件发」）。

然后重跑 Task 4 Step 6 的类型检查，再真发一次（同样先征得用户同意）：

```bash
./wechat/wx-send.sh -a "$A" --image "文件传输助手" "$S/red.png"; echo "exit=$?"
```

预期：`✅ 已发送给「文件传输助手」…（未检测发送失败标记）`，`exit=0`。

- [ ] **Step 5: 各种格式，以及草稿保护（Review Focus 第 5 条）**

这一步只试运行，不真的发送：

```bash
for f in red.jpg red.gif big.png; do
  WX_DRY_RUN=1 ./wechat/wx-send.sh -a "$A" --image "文件传输助手" "$S/$f"; echo "$f exit=$?"
  screencapture -x "$S/fmt-$f.png"
  osascript -e 'tell application "System Events" to keystroke "a" using command down' -e 'tell application "System Events" to key code 51'
done
```

查看截图，记录每种格式在输入框里是图片还是文件。如果 big.png 被当成了文件，把这个现象写进「试验结论」，并把 `WX_IMAGE_MAX_MB` 在 README 里的说明写得更具体。**不要**在这一步加自动压缩，那属于新需求，要报告给用户。

草稿保护：先试运行一次文字，让输入框里留下「草稿」：

```bash
WX_DRY_RUN=1 ./wechat/wx-send.sh -a "$A" "文件传输助手" "草稿"
./wechat/wx-send.sh -a "$A" --image "文件传输助手" "$S/red.png"; echo "exit=$?"
```

预期：第二条报 `…输入框里已有内容「草稿」…已停止`，`exit=7`，没有发出任何东西。最后清空输入框。

- [ ] **Step 6: 发送失败标记（本次不试）**

断网测试会连 Claude 的会话一起断掉，所以本次不做。第一版保持「未检测发送失败标记」。

- [ ] **Step 7: 写试验结论并提交**

把 spec 最后的「（第 0 步做完后填写）」替换成实测结果：
- 三种粘贴方式各自的表现，以及最终选了哪种
- 输入框的 AX value
- 图片消息的 AX 标题
- jpg、gif、大图各自的表现
- 为什么没测发送失败标记

```bash
git add wechat/wx-send.sh docs/superpowers/specs/2026-09-28-wechat-send-image-design.md
git commit -m "wx-send.sh 按实测结果定下图片粘贴方式和图片消息标题

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 使用文档

**Files:**
- Modify: `README.md`
- Modify: `docs/muse-prompt.md`

- [ ] **Step 1: README，客户端命令列表**

在「客户端 mab.py」代码块里，`wechat-send` 那一行后面加上：

```bash
python3 mab.py wechat-send "联系人" --image photo.jpg -a work # 发一张图片（见下文）
```

- [ ] **Step 2: README，新增「发图片」一节**

放在「通过好友申请」一节之后、「限制」之前：

````markdown
### 发图片

一次发一张。图片先放进仓库里的 `outbox/` 文件夹（不会提交到 Git），再按文件名发送。**只有 `outbox/` 里的图片能发**，Muse 碰不到 Mac 上的其他文件。

**图片从哪来：**

1. **Muse 那边的图片**（比如你刚在手机上拍了发给 Muse）：
   ```bash
   python3 mab.py wechat-send "张三" --image /tmp/photo.jpg -a work
   ```
   Muse 的 VM 里有这个文件的话，会自动先上传到 Mac 的 `outbox/`，再发送。上传后发送失败的话，重试时直接写文件名 `--image photo.jpg` 就行，不用再传一遍。
2. **Mac 剪贴板里的图片**：先在 Mac 上复制一张图片（截图、网页上右键「拷贝图像」），然后：
   ```bash
   python3 mab.py wechat-clip             # 返回存下来的文件名，比如 clip-20260928-153000.png
   python3 mab.py wechat-send "张三" --image clip-20260928-153000.png -a work
   ```
   在 Finder 里复制的是文件本身，不是图片，拿不到。
3. **你自己放进 `outbox/` 的图片**：直接按文件名发送。

**看一眼再发：**
```bash
python3 mab.py wechat-images                   # 列出 outbox 里的图片（最新的在前）
python3 mab.py wechat-thumb clip-xxx.png       # 下载缩略图（长边 512）给你确认
```

**发送时会核对什么：** 和发文字一样，先核对聊天标题、确认输入框是空的；粘贴后确认输入框里是一张图片；发出后确认聊天记录里多了一条图片消息。读不到输入框时直接停止，不会不核对就发。图片消息目前**不检测**发送失败的红色感叹号，返回的 `output` 里会写「未检测发送失败标记」。

**注意：**
- 支持 JPG、PNG、GIF、HEIC（HEIC 会自动转成 JPG），单张最大 20 MB（`WX_IMAGE_MAX_MB`）。
- 文件名里的空格和特殊字符会换成 `_`，重名时自动加 `-1`、`-2`，以返回的 `file` 为准。
- 发完不会自动删除，`outbox/` 需要自己清理。
- 发图片和发文字一样，计入发送间隔和每天的发送上限。
````

- [ ] **Step 3: README，HTTP API 表格和配置表**

HTTP API 表格里，把 `/wechat/send` 那一行换成下面第一行，并在 `/wechat/friends` 那一行后面加上其余四行：

```markdown
| POST | `/wechat/send` | `{"to","text" 或 "image","account?","dry_run?"}` | 发微信文字或 outbox 里的一张图片（见上文） |
| POST | `/wechat/image/upload` | `{"name?","data"}` | 图片（base64）存进 outbox |
| POST | `/wechat/image/clipboard` | `{"name?"}` | Mac 剪贴板里的图片存进 outbox |
| GET | `/wechat/images` | — | 列出 outbox 里的图片 |
| GET | `/wechat/image/thumb?file=` | — | 缩略图（JPEG，长边 512） |
```

配置表里，`WX_FRIEND_GREETING` 那一行后面加上：

```markdown
| `WX_IMAGE_MAX_MB` | `20` | 单张图片上限 |
```

英文摘要里的 `Optional WeChat endpoints (...)` 改成：

```markdown
- Optional WeChat endpoints (`/wechat/send`, `/wechat/read`, `/wechat/unread`, `/wechat/friends`, `/wechat/image/*`) call `wx-send.sh` on the Mac to send text or a single image / read messages by exact contact name, with no screenshots involved. Images must live in the repo's `outbox/` folder (uploaded from the agent's VM, saved from the Mac clipboard, or dropped in by you).
```

- [ ] **Step 4: docs/muse-prompt.md**

在「微信」列表里，「发：」那一行后面加上：

```markdown
- 发图：`python3 mab.py wechat-send "联系人" --image 图片 -a <账号别名>`。你那边的图片路径会自动上传；Mac 剪贴板里的图片先用 `wechat-clip` 存下来，再用返回的文件名发送；`wechat-images` 列出能发的图片
- 发图前先把联系人和图片复述给我。剪贴板里或 outbox 里的图片，先用 `wechat-thumb 文件名` 下载缩略图给我看，等我回复「确认」再发
- 我发给你的照片如果在你的终端里找不到文件，直接告诉我，不要猜路径
```

- [ ] **Step 5: 提交**

```bash
git add README.md docs/muse-prompt.md
git commit -m "README 和 Muse 说明补充发图片

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 端到端验证

**需要用户配合**：会真发一张图给「文件传输助手」，还要请用户复制一张图片。

- [ ] **Step 1: 起一个本地服务**

```bash
export T=$(openssl rand -hex 16) P=8799
AGENT_TOKEN=$T PORT=$P .venv/bin/python mac_agent_server.py   # 用 run_in_background
export MAB_URL=http://127.0.0.1:$P MAB_TOKEN=$T
```

- [ ] **Step 2: 完整跑一遍客户端命令**

```bash
/usr/bin/python3 mab.py info | grep -o '"wechat/image/upload"'
/usr/bin/python3 mab.py wechat-upload "$S/red.jpg" --name e2e
/usr/bin/python3 mab.py wechat-images
/usr/bin/python3 mab.py wechat-thumb e2e.jpg -o "$S/e2e-thumb.jpg"
/usr/bin/python3 mab.py wechat-send "文件传输助手" --image e2e.jpg --dry-run -a "$A"
osascript -e 'tell application "System Events" to keystroke "a" using command down' -e 'tell application "System Events" to key code 51'
```

预期：
- info 里有 `"wechat/image/upload"`
- upload 返回 `"file": "e2e.jpg"`
- images 列表里有 e2e.jpg
- thumb 保存成功
- 试运行返回 `"status": "ok", "dry_run": true`

- [ ] **Step 3: 真发一次（先征得用户同意）**

```bash
/usr/bin/python3 mab.py wechat-send "文件传输助手" --image "$S/red.png" -a "$A"
```

预期：`"ok": true, "status": "ok"`，`output` 里有「未检测发送失败标记」。在微信里也能看到这张红色图片。

- [ ] **Step 4: 剪贴板（先请用户复制一张图片）**

```bash
/usr/bin/python3 mab.py wechat-clip
```

预期：返回 `clip-*.png`。可以只试运行它，不必真发。

- [ ] **Step 5: 收尾**

- 停掉服务。
- 删掉 `outbox/` 里的测试图片（`e2e*`、`red*`、`clip-*`）。
- 最后跑一次全部测试：`/usr/bin/python3 -m unittest discover -s tests -v`，确认全部通过。
- 这一步不产生新的提交。

# 微信读取图片 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `wechat-read` / `wechat-unread` 加上 `--images` 后，把图片消息的原图取到 Mac 的 `inbox/`，mab 自动下载到 Muse 的 VM，并在消息上加 `local_path`。

**Architecture:** 在图片气泡上右键，点「复制」，从剪贴板存成文件。定位每一行复用从 `identifySenders` 抽出来的 `walkRows`（逐页对行，靠 AX 元素区分同名的「图片」行）。Python 端负责转发开关（环境变量 `WX_IMAGES`）和提供 inbox 下载接口；mab 负责下载图片、加 `local_path`。

**Tech Stack:** Python 3.9+ 标准库；Swift 5.10（`wx-send.sh` 里的 heredoc）；AppKit NSPasteboard；AX API。

**Spec:** `docs/superpowers/specs/2026-09-28-wechat-read-image-design.md`

**与 spec 的顺序调整：** 第 0 步实测放在 Task 4。原因和上次一样：Swift 代码里加一个临时开关 `WX_IMAGE_DEBUG`，打印图片这一行的 AX 子树、菜单项和剪贴板类型，实测直接用正式代码跑，不另写探测程序。定位方式、菜单项名称、剪贴板格式都做成了常量或按顺序尝试，实测后再定下来。

## Global Constraints

- 分支：`feat/wechat-read-image`（从 `feat/wechat-send-image` 拉出来）。
- Python 代码要兼容 3.9：不用 `X | None`、`match`；`mab.py` 只能用标准库；不新增依赖。
- Swift 编译目标 `$(uname -m)-apple-macos13.0`，编译器 Swift 5.10。
- 不新增退出码：单张图失败只写进 `image_error`；菜单关不掉用 5；微信被切走沿用 6。
- 默认值：`WX_MAX_IMAGES=10`（接口允许 1–50）、`WX_IMAGE_WAIT=3` 秒、`WX_INBOX_DAYS=3` 天；mab 的 `--save-dir` 默认 `wechat-images`。
- inbox 文件名：`in-YYYYMMDD-HHMMSS-N.<png|jpg|gif>`；下载接口复用 outbox 的文件名规则（`safe_path`）。
- 不带 `--images` 时，read / unread 的输出和行为必须和现在一样。
- 代码风格：注释用中文；方法的文档注释只写一行；只注释代码本身看不出来的东西。
- 提交信息用中文，末尾加 `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`；只 `git add` 本任务涉及的文件。
- Bash 工具每次调用之间 shell 变量不会保留：用到 `$S`（scratchpad）、账号名、联系人名的命令，每次都要在同一条命令里重新设置。
- 操作微信的实测：发消息只在 zhiwuzhu ↔ work 之间进行。清空输入框只用 `$S/clear.sh`（先把微信切到前台、确认在最前再按键）。

## Review Focus

1. **连续两条图片消息**（标题都是「图片」）：必须各自复制到对应的那张图，不能串位。Task 4 用两张尺寸不同的图（300×200 和 421×747）连着发，核对每条消息的 `image` 文件尺寸。
2. **右键落在空白处或别的消息上**：不能点任何菜单项，只在那条消息上记 `image_error`。Task 4 从调试输出里核对，没有「复制」时有没有按 Esc 关掉菜单。
3. **剪贴板恢复**：带 `--images` 读完后，剪贴板要恢复成读之前的内容。Task 4 读前读后各跑一次 `osascript -e 'clipboard info'` 对比。
4. **还没下载原图的图片**：复制出来可能是缩略图，也可能什么都没有。Task 4 的未读测试专门覆盖，结果写进 spec。
5. **不带 `--images` 的回归**：`--read` 输出和改动前逐字一致。Task 3 Step 1 先存下基线，Step 9 对比。

---

## 文件结构

| 文件 | 改动 | 负责什么 |
|---|---|---|
| `mac_agent_server.py` | 修改 | `safe_path`、`INBOX`、`image_env`、`/wechat/inbox/file`、read / unread 转发 images |
| `mab.py` | 修改 | `request(fatal=)`、`fetch_images`、read / unread 的 `--images` / `--max-images` / `--save-dir` |
| `wechat/wx-send.sh` | 修改 | bash：导出 `WX_INBOX`、清理 inbox；Swift：`Msg` 新字段、`Row`、`walkRows`、`copyImage` 等 |
| `tests/test_images.py` | 修改 | 新增 `InboxTest`，`MabTest` 加读图用例，`ServerCase` 记录 env |
| `.gitignore` | 修改 | 加 `inbox/` |
| `README.md`、`docs/muse-prompt.md` | 修改 | 使用文档 |
| spec | 修改 | 实测结论 |

测试命令：`/usr/bin/python3 -m unittest discover -s tests -v`

Swift 类型检查命令（全文统一，下文简称「类型检查」）：

```bash
S=/private/tmp/claude-502/-Users-tom-huang-muse-intel-mac-bridge/02817272-e822-4d41-91da-3950df001210/scratchpad
sed -n "/^cat <<'SWIFT'\$/,/^SWIFT\$/p" wechat/wx-send.sh | sed '1d;$d' > "$S/wx.swift"
swiftc -typecheck -target "$(uname -m)-apple-macos13.0" "$S/wx.swift" && echo TYPECHECK_OK
```

---

### Task 1: 服务端的 inbox 下载接口，以及 read / unread 转发 images

**Files:**
- Modify: `mac_agent_server.py`
- Modify: `.gitignore`
- Test: `tests/test_images.py`

**Interfaces:**
- Produces：
  - `INBOX: str`
  - `safe_path(root: str, file) -> str`
  - `outbox_path(file) = safe_path(OUTBOX, file)`
  - `image_env(p: dict) -> dict`：`images` 为真时返回 `{"WX_IMAGES":"1","WX_MAX_IMAGES":"N"}`，否则返回 `{}`
  - `inbox_file(file) -> (bytes, content_type)`
  - `GET /wechat/inbox/file?file=`
  - `/wechat/read` 和 `/wechat/unread` 接受 `images`、`max_images`
- 测试辅助：`ServerCase` 增加 `self.envs`（每次调用 `run_wx` 时传入的 env）和 `self.wx_out`（假 `run_wx` 的 stdout）

- [ ] **Step 1: 写失败测试**

在 `ServerCase.setUp` 里，把 `fake_run_wx` 和前后几行换成：

```python
        self.calls, self.envs = [], []
        self.wx_out = "✅ 已发送"

        def fake_run_wx(args, account=None, env=None, timeout=300):
            self.calls.append(args)
            self.envs.append(env or {})
            return 0, self.wx_out, ""
```

（删掉原来单独的 `self.calls = []` 那一行。）

`ServerCase.call` 在 `out = r.read()` 后面加一行 `self.headers = dict(r.getheaders())`。

在 `HttpTest` 后面加上：

```python
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
```

- [ ] **Step 2: 运行，确认失败**

运行测试命令。
预期：`InboxTest` 全部失败。有的是 404（没有这个接口）；`test_read_images_env` 断言 env 为 `{}` 不等于期望值。其他测试仍然通过。

- [ ] **Step 3: 实现**

在 `OUTBOX = ...` 下面加一行：

```python
INBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox")
```

把 `outbox_path` 整个换成：

```python
def safe_path(root, file) -> str:
    """root 目录里的文件名 → 绝对路径；不合规或解析后跑出 root 就报错。"""
    where = os.path.basename(root)
    if not isinstance(file, str) or not IMAGE_NAME.match(file) or ".." in file \
            or "." not in file or file.rsplit(".", 1)[1].lower() not in IMAGE_EXTS:
        raise ValueError(f"文件名不合规：{file!r}（只能是 {where} 里的 jpg/png/gif/heic 文件名，只含字母、数字、中文、空格和 ._-）")
    base = os.path.realpath(root)
    path = os.path.realpath(os.path.join(base, file))
    if os.path.dirname(path) != base:
        raise ValueError(f"文件名不合规：{file!r}（指向了 {where} 外面）")
    return path


def outbox_path(file) -> str:
    return safe_path(OUTBOX, file)
```

在 `a_wechat_read` 前面加上：

```python
def image_env(p) -> dict:
    """images=true 时让 wx-send.sh 读消息时顺便取图。"""
    if not p.get("images"):
        return {}
    n = int(p.get("max_images", 10))
    if not 1 <= n <= 50:
        raise ValueError("max_images 需在 1–50 之间")
    return {"WX_IMAGES": "1", "WX_MAX_IMAGES": str(n)}
```

`a_wechat_read` 里调用 `run_wx` 的那一行改成：

```python
    # 取图每张要右键复制一次，放宽超时
    code, out, err = run_wx(["--read", chat, str(limit)], p.get("account"), image_env(p),
                            timeout=600 if p.get("images") else 300)
```

`a_wechat_unread` 里，在 `args = ...` 前面加一行：

```python
    env.update(image_env(p))
```

在 `thumbnail` 后面加上：

```python
def inbox_file(file):
    """inbox 里的原图和它的 Content-Type。"""
    path = safe_path(INBOX, file)
    if not os.path.isfile(path):
        raise ValueError(f"inbox 里没有 {file}")
    with open(path, "rb") as f:
        data = f.read()
    kind = image_kind(data)
    if kind is None:
        return data, "application/octet-stream"
    return data, "image/" + ("jpeg" if kind == "jpg" else kind)
```

`do_GET` 里，在 `if path == "/wechat/image/thumb":` 前面加上：

```python
            if path == "/wechat/inbox/file":
                file = parse_qs(urlparse(self.path).query).get("file", [""])[0]
                return self._send(200, *inbox_file(file))
```

`main()` 里，在 `os.makedirs(OUTBOX, exist_ok=True)` 下面加一行 `os.makedirs(INBOX, exist_ok=True)`。

顶部说明里：
- `/wechat/read` 那一行改成 `POST /wechat/read   {"chat":"联系人","limit":20,"account":"work","images":false}`
- `/wechat/unread` 那一行改成 `POST /wechat/unread {"account":"work","list_only":false,"images":false}   读所有未读聊天的新消息；images=true 时顺便取图`
- 在 `GET  /wechat/image/thumb...` 后面加一行 `GET  /wechat/inbox/file?file=in-xxx.png                     读消息时取到的原图`
- 环境变量部分加上：`WX_MAX_IMAGES / WX_IMAGE_WAIT / WX_INBOX_DAYS  读图：每次最多几张（10）、复制后等几秒（3）、inbox 保留几天（3）`

`.gitignore` 加一行 `inbox/`。

- [ ] **Step 4: 运行，确认通过**

运行测试命令。预期：全部 OK。

- [ ] **Step 5: 提交**

```bash
git add mac_agent_server.py .gitignore tests/test_images.py
git commit -m "bridge 新增 inbox 原图下载接口，read/unread 支持 images

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: mab.py 读图后自动下载

**Files:**
- Modify: `mab.py`
- Test: `tests/test_images.py`

**Interfaces:**
- Consumes：Task 1 的 `/wechat/inbox/file` 和 read / unread 的 `images`、`max_images`
- Produces：
  - `request(..., fatal=True)`：`fatal=False` 时出错抛 `RuntimeError`，不退出进程
  - `fetch_images(result: dict, save_dir: str) -> None`
  - `wechat-read` 和 `wechat-unread` 支持 `--images`、`--max-images`、`--save-dir`

- [ ] **Step 1: 写失败测试**

在 `MabTest` 里加上：

```python
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
```

- [ ] **Step 2: 运行，确认失败**

运行测试命令。
预期：前两个新测试失败，因为 argparse 不认识 `--images`，报 `unrecognized arguments: --images`。`test_read_without_images_unchanged` 应该已经能通过。

- [ ] **Step 3: 实现**

把 `request` 换成：

```python
def request(method, path, payload=None, timeout=TIMEOUT, fatal=True):
    if not URL or not TOKEN:
        sys.exit("请先设置 MAB_URL 和 MAB_TOKEN 环境变量 / set MAB_URL and MAB_TOKEN")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}: {e.read().decode(errors='replace')}"
    except urllib.error.URLError as e:
        msg = f"连接失败 / connection failed: {e.reason}"
    if fatal:
        sys.exit(msg)
    raise RuntimeError(msg)
```

在 `upload` 后面加上：

```python
def fetch_images(result, save_dir):
    """把结果里带 image 的消息对应的原图下载到 save_dir，并加上 local_path；单张失败记 download_error。"""
    msgs = result.get("items", []) + [m for c in result.get("chats", []) for m in c.get("messages", [])]
    for m in msgs:
        if not m.get("image"):
            continue
        try:
            body, _ = request("GET", "/wechat/inbox/file?file=" + urllib.parse.quote(m["image"]), timeout=max(TIMEOUT, 120), fatal=False)
        except RuntimeError as e:
            m["download_error"] = str(e)
            continue
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.abspath(os.path.join(save_dir, m["image"]))
        with open(path, "wb") as f:
            f.write(body)
        m["local_path"] = path


def post_read(path, p, a, timeout):
    """read / unread：带 --images 时先下载图片再打印。"""
    if not a.images:
        return post(path, p, timeout)
    p.update(images=True, max_images=a.max_images)
    body, _ = request("POST", path, p, timeout)
    result = json.loads(body)
    fetch_images(result, a.save_dir)
    print(json.dumps(result, ensure_ascii=False))
```

参数：在定义 `wu` 的那几行后面加上（`wr` 和 `wu` 一起加）：

```python
    for sp in (wr, wu):
        sp.add_argument("--images", action="store_true", help="顺便取图片，下载到 --save-dir")
        sp.add_argument("--max-images", type=int, default=10)
        sp.add_argument("--save-dir", default="wechat-images")
```

`wechat-read` 分支换成：

```python
    elif a.cmd == "wechat-read":
        # 微信操作要排队（最多 180 秒），取图还要逐张右键复制，超时放宽
        post_read("/wechat/read", {"chat": a.chat, "limit": a.limit, "account": a.account}, a,
                  max(TIMEOUT, 620 if a.images else 320))
```

`wechat-unread` 分支里，把最后的 `post("/wechat/unread", p, max(TIMEOUT, 1900))` 换成：

```python
        post_read("/wechat/unread", p, a, max(TIMEOUT, 1900))
```

顶部用法说明里：
- `wechat-read` 那一行改成 `python3 mab.py wechat-read "联系人" [-n 20] [--images] [-a work]`
- `wechat-unread` 那一行改成 `python3 mab.py wechat-unread [--list-only] [--images] [-a work]   # --images：取图片，下载到 ./wechat-images，消息里加 local_path`

- [ ] **Step 4: 运行，确认通过**

运行测试命令。预期：全部 OK。

- [ ] **Step 5: 提交**

```bash
git add mab.py tests/test_images.py
git commit -m "mab.py 读消息支持 --images：自动下载图片并加上 local_path

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Swift 部分：walkRows、copyImage，接进 read 和 unread

**Files:**
- Modify: `wechat/wx-send.sh`
  - 配置常量区（约第 160–180 行）
  - `struct Msg`（约第 604 行）
  - `visibleRows` / `identifySenders` / `readNew` / `printUnreadDetails` / `readChat`
  - bash 的 `build` 前面

**Interfaces:**
- Consumes：Task 1 服务端传入的 `WX_IMAGES`、`WX_MAX_IMAGES`
- Produces：
  - Swift 常量：`IMAGES`、`MAX_IMAGES`、`IMAGE_WAIT`、`INBOX`、`IMAGE_DEBUG`、`COPY_TITLES`、`IMG_DX`
  - `Msg.image`、`Msg.imageError`、`Msg.dict`
  - `Session.Row`、`walkRows`、`identifySender`、`takeImage`、`copyImage`、`copyAt`、`closeMenu`、`saveInbox`、`debugRow`
  - 全局函数：`imageFromPasteboard`、`isInside`
  - bash：导出 `WX_INBOX`，读图时清理 inbox

这一步没有自动化测试框架，靠三项验证：类型检查、不带图时 `--read` 和基线逐字一致、`--unread --list-only` 能正常跑（它不走 readNew，只用来确认没改坏）。取图本身在 Task 4 实测。

- [ ] **Step 1: 记录基线**

```bash
S=/private/tmp/claude-502/-Users-tom-huang-muse-intel-mac-bridge/02817272-e822-4d41-91da-3950df001210/scratchpad
./wechat/wx-send.sh -a zhiwuzhu --read "文件传输助手" 5 > "$S/read-baseline.json"; echo "exit=$?"; cat "$S/read-baseline.json"
```

预期：`exit=0`，输出 JSON，最后几条里有 `"text":"图片"`。

- [ ] **Step 2: 配置常量、Msg、两个全局函数**

在 `let REMARK = ...` 那一行后面加上：

```swift
let IMAGES = env["WX_IMAGES"] == "1"               // 读消息时顺便取图
let MAX_IMAGES = Int(envD("WX_MAX_IMAGES", 10))
let IMAGE_WAIT = envD("WX_IMAGE_WAIT", 3)          // 点「复制」后等剪贴板出现图片的秒数
let INBOX = env["WX_INBOX"] ?? ""
let IMAGE_DEBUG = env["WX_IMAGE_DEBUG"] == "1"     // 第 0 步实测用，定下来后删掉
let COPY_TITLES: Set<String> = ["复制", "Copy"]    // 图片右键菜单里的「复制」（实测后改成准确值）
let IMG_DX = CGFloat(envD("WX_IMG_DX", 110))       // 没有 AXImage 子元素时，气泡中心离这一行左右边缘的距离（实测后定）
```

`struct Msg` 换成：

```swift
struct Msg: Equatable {
    var type: String; var text: String
    var from: String? = nil, sender: String? = nil, wxid: String? = nil
    var image: String? = nil, imageError: String? = nil
    var plain: Msg { Msg(type: type, text: text) }   // 拼接和对应时只比类型和内容

    var dict: [String: String] {
        var d = ["type": type, "text": text]
        if let v = from { d["from"] = v }
        if let v = sender, !v.isEmpty { d["sender"] = v }
        if let v = wxid, !v.isEmpty { d["wxid"] = v }
        if let v = image { d["image"] = v }
        if let v = imageError { d["image_error"] = v }
        return d
    }
}
```

在 `axFind` 函数后面加上：

```swift
/// e 是不是 ancestor 本身或它的后代（往上找 6 层）
func isInside(_ e: AXUIElement, _ ancestor: AXUIElement) -> Bool {
    var cur: AXUIElement? = e
    for _ in 0..<6 {
        guard let c = cur else { return false }
        if CFEqual(c, ancestor) { return true }
        cur = axParent(c)
    }
    return false
}

/// 剪贴板里的图片：优先原文件（文件 URL），其次 PNG / JPEG / GIF，最后 TIFF 转 PNG；返回 (数据, 扩展名)
func imageFromPasteboard(_ pb: NSPasteboard) -> (Data, String)? {
    if let url = (pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL])?.first,
       let ext = ["png", "jpg", "jpeg", "gif"].first(where: { $0 == url.pathExtension.lowercased() }),
       let d = try? Data(contentsOf: url) {
        return (d, ext == "jpeg" ? "jpg" : ext)
    }
    let types: [(NSPasteboard.PasteboardType, String)] = [
        (.png, "png"), (NSPasteboard.PasteboardType("public.jpeg"), "jpg"), (NSPasteboard.PasteboardType("com.compuserve.gif"), "gif")]
    for (t, ext) in types { if let d = pb.data(forType: t) { return (d, ext) } }
    if let d = pb.data(forType: .tiff), let rep = NSBitmapImageRep(data: d),
       let png = rep.representation(using: .png, properties: [:]) {
        return (png, "png")
    }
    return nil
}
```

- [ ] **Step 3: Row、visibleRows、walkRows、identifySender**

在 `visibleRows` 前面加上：

```swift
    typealias Row = (msg: Msg, frame: CGRect, raw: String, el: AXUIElement)
```

`visibleRows` 换成：

```swift
    /// 屏幕上能看到的消息行（按从上到下），带位置和 AX 元素
    func visibleRows(_ list: AXUIElement) -> [Row] {
        guard let box = axFrame(list) else { return [] }
        return axChildren(list).compactMap { e -> Row? in
            guard let raw = axStr(e, kAXTitleAttribute), !raw.isEmpty, let f = axFrame(e), f.height > 0, f.intersects(box) else { return nil }
            var t = raw
            if t.hasSuffix(" ") { t.removeLast() }
            let isTime = f.height < 50 && t.range(of: MSG_TIME, options: .regularExpression) != nil
            return (Msg(type: isTime ? "time" : "message", text: t), f, raw, e)
        }.sorted { $0.frame.minY < $1.frame.minY }
    }
```

`identifySenders` 整个换成下面两个函数：

```swift
    /// 滚到底，一页页往上，把屏幕上的行对应回 all 里的下标，对下标 >= start 的行调用 act
    func walkRows(_ list: AXUIElement, _ all: inout [Msg], _ start: Int, _ act: (Int, Row, inout [Msg]) throws -> Void) throws {
        guard start < all.count, let box = axFrame(list) else { return }
        scrollListToBottom(list)
        var hi = all.count
        for _ in 0..<15 {
            let rows = visibleRows(list)
            let page = rows.map { $0.msg.plain }
            guard !page.isEmpty, let j = stride(from: min(hi, all.count) - page.count, through: 0, by: -1)
                    .first(where: { j in j >= 0 && all[j..<(j + page.count)].map { $0.plain } == page }) else { return }
            for (k, row) in rows.enumerated().reversed() where j + k >= start {
                try act(j + k, row, &all)
            }
            if j <= start { return }
            hi = j + page.count - 1
            try guardFront()
            scrollList(list, Int32(box.height * 0.6))
        }
    }

    /// 点这一行的头像识别发送人
    func identifySender(_ i: Int, _ row: Row, _ msgs: inout [Msg], _ box: CGRect) throws {
        guard msgs[i].type == "message", msgs[i].from == nil else { return }
        let f = row.frame
        if let c = try probeAvatar(CGPoint(x: f.minX + 38, y: f.minY + 28), row.raw, box) {
            msgs[i].from = "other"; msgs[i].sender = c.name; msgs[i].wxid = c.wxid
        } else if let c = try probeAvatar(CGPoint(x: f.maxX - 38, y: f.minY + 28), row.raw, box) {
            msgs[i].from = "me"; msgs[i].sender = c.name; msgs[i].wxid = c.wxid
        } else if box.contains(CGPoint(x: f.minX + 38, y: f.minY + 28)) {
            msgs[i].from = "system"
        }
    }
```

- [ ] **Step 4: 取图的几个函数**

在 `identifySender` 后面加上：

```swift
    var imagesTaken = 0, imagesSkipped = 0

    /// 这一行是图片就复制出来存进 inbox；超过张数上限的只计数
    func takeImage(_ i: Int, _ row: Row, _ msgs: inout [Msg]) throws {
        guard msgs[i].type == "message", msgs[i].text == "图片", msgs[i].image == nil, msgs[i].imageError == nil else { return }
        if imagesTaken >= MAX_IMAGES { imagesSkipped += 1; return }
        imagesTaken += 1
        do { msgs[i].image = try copyImage(row, side: msgs[i].from) }
        catch let e as WXError where e.code == 5 || e.code == 6 { throw e }
        catch let e as WXError { msgs[i].imageError = e.msg }
    }

    /// 右键这张图 →「复制」→ 存进 inbox，返回文件名
    func copyImage(_ row: Row, side: String?) throws -> String {
        if INBOX.isEmpty { throw fail(4, "NO_INBOX", "没有设置 WX_INBOX") }
        if IMAGE_DEBUG { debugRow(row) }
        let f = row.frame
        let points: [CGPoint]
        if let img = axFind(row.el, 0, { axRole($0) == "AXImage" }), let r = axFrame(img), r.width > 4 {
            points = [CGPoint(x: r.midX, y: r.midY)]
        } else {
            let left = CGPoint(x: f.minX + IMG_DX, y: f.midY), right = CGPoint(x: f.maxX - IMG_DX, y: f.midY)
            points = side == "me" ? [right] : side == "other" ? [left] : [left, right]
        }
        for pt in points {
            if let name = try copyAt(pt, row) { return name }
        }
        throw fail(4, "NO_COPY", "右键菜单里没有「复制」")
    }

    /// 在 pt 右键并点「复制」，等剪贴板出现图片后存进 inbox；这个位置不在这一行上、或菜单里没有「复制」时返回 nil
    func copyAt(_ pt: CGPoint, _ row: Row) throws -> String? {
        var hit: AXUIElement?
        AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(pt.x), Float(pt.y), &hit)
        guard let h = hit, isInside(h, row.el) else { return nil }
        try guardFront()
        let pb = NSPasteboard.general
        let before = pb.changeCount
        for t: CGEventType in [.mouseMoved, .rightMouseDown, .rightMouseUp] {
            CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: pt, mouseButton: .right)?.post(tap: .cghidEventTap)
            usleep(t == .mouseMoved ? 25_000 : 35_000)
        }
        var item: AXUIElement?
        _ = waitUntil(1, 0.05) {
            item = contextMenuItems().first { COPY_TITLES.contains(axStr($0, kAXTitleAttribute) ?? "") }
            return item != nil
        }
        if IMAGE_DEBUG { eprint("菜单：" + contextMenuItems().compactMap { axStr($0, kAXTitleAttribute) }.joined(separator: " | ")) }
        guard let it = item, let r = axFrame(it) else { try closeMenu(); return nil }
        let p = CGPoint(x: r.midX, y: r.midY)
        var onItem: AXUIElement?
        AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(p.x), Float(p.y), &onItem)
        guard let o = onItem, axRole(o) == "AXMenuItem", COPY_TITLES.contains(axStr(o, kAXTitleAttribute) ?? "") else {
            try closeMenu(); return nil
        }
        click(p)
        try closeMenu()   // 点击后菜单通常已经关了，这里兜底
        var got: (Data, String)?
        _ = waitUntil(IMAGE_WAIT, 0.1) {
            guard pb.changeCount != before else { return false }
            got = imageFromPasteboard(pb)
            return got != nil
        }
        if IMAGE_DEBUG { eprint("剪贴板类型：" + (pb.types ?? []).map { $0.rawValue }.joined(separator: ", ")) }
        guard let (data, ext) = got else { throw fail(4, "NO_IMAGE", "点了「复制」，但 \(Int(IMAGE_WAIT)) 秒内剪贴板里没有图片") }
        return try saveInbox(data, ext)
    }

    /// 关掉还开着的右键菜单；连按 3 次 Esc 还关不掉就停止，防止后面误点
    func closeMenu() throws {
        for _ in 0..<3 where !contextMenuItems().isEmpty {
            key(K_ESC)
            _ = waitUntil(0.5, 0.05) { contextMenuItems().isEmpty }
        }
        if !contextMenuItems().isEmpty { throw fail(5, "MENU_OPEN", "右键菜单关不掉，为防误点已停止") }
    }

    /// 存进 inbox，文件名 in-时间-序号.扩展名
    func saveInbox(_ data: Data, _ ext: String) throws -> String {
        try? FileManager.default.createDirectory(atPath: INBOX, withIntermediateDirectories: true)
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.dateFormat = "yyyyMMdd-HHmmss"
        let stamp = fmt.string(from: Date())
        var n = 1, name = ""
        repeat { name = "in-\(stamp)-\(n).\(ext)"; n += 1 } while FileManager.default.fileExists(atPath: INBOX + "/" + name)
        do { try data.write(to: URL(fileURLWithPath: INBOX + "/" + name)) }
        catch { throw fail(4, "INBOX_WRITE", "图片存不进 \(INBOX)：\(error.localizedDescription)") }
        return name
    }

    /// 第 0 步实测用：打印这一行的 AX 子树
    func debugRow(_ row: Row) {
        func walk(_ e: AXUIElement, _ d: Int) {
            let f = axFrame(e).map { "x=\(Int($0.minX)) y=\(Int($0.minY)) w=\(Int($0.width)) h=\(Int($0.height))" } ?? "-"
            eprint(String(repeating: "  ", count: d) + "\(axRole(e)) 「\(axStr(e, kAXTitleAttribute) ?? "")」 \(f)")
            if d < 4 { axChildren(e).forEach { walk($0, d + 1) } }
        }
        walk(row.el, 0)
    }
```

- [ ] **Step 5: 接进 readNew（unread）**

`readNew` 里，从 `if SENDERS && group {` 到对应的 `}` 这一段换成：

```swift
        let senders = SENDERS && group   // 私聊的发送人就是这个聊天本身，不用点头像
        if senders || IMAGES {
            do {
                try walkRows(list, &all, all.count - final.count) { i, row, msgs in
                    if senders { try identifySender(i, row, &msgs, box) }
                    if IMAGES { try takeImage(i, row, &msgs) }
                }
            }
            catch let e as WXError where e.code == 6 || e.tag == "MENU_OPEN" { throw e }
            catch let e as WXError { return (Array(all.suffix(final.count)), (note.map { $0 + "；" } ?? "") + "逐条处理中断：" + e.msg) }
        }
```

- [ ] **Step 6: 接进 printUnreadDetails**

- 函数开头（`try activate()` 前面）加一行 `imagesTaken = 0; imagesSkipped = 0`。
- `item["messages"] = msgs.map { m -> [String: String] in ... }` 整段换成 `item["messages"] = msgs.map { $0.dict }`。
- 在 `if rows.count > maxChats {...}` 后面加上：

```swift
        if imagesSkipped > 0 { notes.append("图片超过 \(MAX_IMAGES) 张，有 \(imagesSkipped) 张没取（WX_MAX_IMAGES）") }
```

- [ ] **Step 7: 接进 readChat（read）**

`readChat` 整个换成：

```swift
    /// 读取聊天记录里当前加载出来的消息（最多 limit 条），JSON 输出；WX_IMAGES=1 时顺便取图
    func readChat(_ contact: String, _ limit: Int) throws {
        var notes: [String] = []
        try openChat(contact, &notes)
        guard let list = axMessageList() else { throw fail(5, "NO_AX", "AX 读不到聊天记录") }
        var items: [Msg] = []
        for row in axChildren(list) {
            // 屏幕外的消息是没有内容的占位元素，读不到
            guard var t = axStr(row, kAXTitleAttribute), !t.isEmpty else { continue }
            if t.hasSuffix(" ") { t.removeLast() }
            let h = axFrame(row)?.height ?? 0
            let isTime = h < 50 && t.range(of: MSG_TIME, options: .regularExpression) != nil
            items.append(Msg(type: isTime ? "time" : "message", text: t))
        }
        if IMAGES {
            imagesTaken = 0; imagesSkipped = 0
            do { try walkRows(list, &items, max(0, items.count - limit)) { i, row, msgs in try takeImage(i, row, &msgs) } }
            catch let e as WXError where e.code == 6 || e.tag == "MENU_OPEN" { throw e }
            catch let e as WXError { notes.append("取图中断：" + e.msg) }
            if imagesSkipped > 0 { notes.append("图片超过 \(MAX_IMAGES) 张，有 \(imagesSkipped) 张没取（WX_MAX_IMAGES）") }
        }
        let out: [String: Any] = ["chat": contact, "items": items.suffix(limit).map { $0.dict }, "notes": notes]
        let data = try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
        say(String(data: data, encoding: .utf8)!)
    }
```

（原来 `readChat` 里局部定义的正则 `time` 和全局的 `MSG_TIME` 内容一样，现在改用 `MSG_TIME`。）

- [ ] **Step 8: bash 部分**

在 `build` 那一行前面加上：

```bash
# ---------- 读图：图片存到仓库的 inbox/，每次读图前清掉 N 天前的 ----------
export WX_INBOX="$(cd "$DIR/.." && pwd)/inbox"
if [[ "${WX_IMAGES:-}" == "1" && -d "$WX_INBOX" ]]; then
  find "$WX_INBOX" -type f -name 'in-*' -mmin +$(( ${WX_INBOX_DAYS:-3} * 1440 )) -delete
fi
```

顶部说明的环境变量列表（`WX_LOG=...` 那一行后面）加上：

```
#   WX_IMAGES=1         --read / --unread 时顺便取图片（右键「复制」），存到仓库的 inbox/
#   WX_MAX_IMAGES=10    每次最多取几张
```

注意：这一段不在第 2–22 行之间，`usage()` 的行号范围不用改。改完核对一下 `sed -n 2,22p` 的输出是不是仍然停在「流程：」那一行。

- [ ] **Step 9: 类型检查和回归**

运行类型检查，预期 `TYPECHECK_OK`。然后：

```bash
S=/private/tmp/claude-502/-Users-tom-huang-muse-intel-mac-bridge/02817272-e822-4d41-91da-3950df001210/scratchpad
./wechat/wx-send.sh -a zhiwuzhu --read "文件传输助手" 5 > "$S/read-after.json"; echo "exit=$?"
diff "$S/read-baseline.json" "$S/read-after.json" && echo SAME
./wechat/wx-send.sh -a zhiwuzhu --unread --list-only | head -c 300; echo
```

预期：
- 第一条先编译，然后 `exit=0` 和 `SAME`。
- `--unread --list-only` 输出 JSON。它不走 readNew，只用来确认没改坏。
- 如果「找不到微信主窗口」（退出码 5），重试一次。

- [ ] **Step 10: 提交**

```bash
git add wechat/wx-send.sh
git commit -m "wx-send.sh 读消息支持取图：右键复制图片存进 inbox

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 第 0 步实测（zhiwuzhu ↔ work），定下常量

**需要用户配合。** 开始前问用户两个名字：
- 在 zhiwuzhu 的微信里，work 显示的名字，记为 `$TO_S`；
- 在 work 的微信里，zhiwuzhu 显示的名字，记为 `$TO_Z`。

在两个账号之间发图，用户已经同意。每条命令都要重新设置这两个变量和 `$S`。

**Files:**
- Modify: `wechat/wx-send.sh`（`COPY_TITLES`、`IMG_DX` / 定位方式、`IMAGE_WAIT` 默认值；删掉 `IMAGE_DEBUG` 和 `debugRow`）
- Modify: spec 的「实测结论」

- [ ] **Step 1: 两边互相发图**

测试图用上次生成的 `$S/red.png`（300×200）和 `~/Desktop/RedemptionPage.png`（421×747）。

```bash
./wechat/wx-send.sh -a zhiwuzhu --image "$TO_S" "$S/red.png"; echo "exit=$?"
./wechat/wx-send.sh -a zhiwuzhu --image "$TO_S" ~/Desktop/RedemptionPage.png; echo "exit=$?"
./wechat/wx-send.sh -a work --image "$TO_Z" "$S/red.jpg"; echo "exit=$?"
```

预期：三条都是 `exit=0`。发完后，两个账号和对方的聊天里都有左右两侧的图片。

- [ ] **Step 2: 带调试信息读 zhiwuzhu 这一边**

```bash
osascript -e 'clipboard info' > "$S/clip-before.txt"
WX_IMAGES=1 WX_IMAGE_DEBUG=1 ./wechat/wx-send.sh -a zhiwuzhu --read "$TO_S" 6 2> "$S/dbg-z.txt" > "$S/read-z.json"; echo "exit=$?"
osascript -e 'clipboard info' > "$S/clip-after.txt"; diff "$S/clip-before.txt" "$S/clip-after.txt" && echo CLIP_SAME
cat "$S/read-z.json"; head -80 "$S/dbg-z.txt"
for f in $(python3 -c "import json,sys; [print(i['image']) for i in json.load(open(sys.argv[1]))['items'] if 'image' in i]" "$S/read-z.json"); do sips -g pixelWidth -g pixelHeight "inbox/$f" | tail -2 | tr '\n' ' '; echo " $f"; done
```

需要记录：
- 图片这一行的 AX 子树里有没有 `AXImage`，它的 frame 是什么。
- 右键菜单的全部菜单项，「复制」的准确写法。
- 剪贴板里的类型。
- 每条图片消息拿到了 `image` 还是 `image_error`；文件尺寸和原图（300×200、421×747、红色 jpg 300×200）是否一一对应，顺序不能串（Review Focus 第 1 条）。
- 剪贴板是否恢复（Review Focus 第 3 条）。

**如果全部失败，按这个顺序调整后重跑 Step 2：**
- 菜单里没有「复制」：把实测到的准确标题加进 `COPY_TITLES`。
- 没有 `AXImage`、右键没落在图片上：按调试输出里行的 frame，和截图（`screencapture -x`）里图片的实际位置，改 `IMG_DX`。临时调试可以直接在命令前加 `WX_IMG_DX=数值`。
- 剪贴板里没有图片：看「剪贴板类型」，补充 `imageFromPasteboard` 的读取分支；如果是等待时间不够，调大 `WX_IMAGE_WAIT` 再试。

**停止条件：** 三种调整都做了还是完全取不到图，就停下来，把调试输出报告给用户，不要继续。

- [ ] **Step 3: 读 work 这一边**

```bash
WX_IMAGES=1 WX_IMAGE_DEBUG=1 ./wechat/wx-send.sh -a work --read "$TO_Z" 6 2> "$S/dbg-s.txt" > "$S/read-s.json"; echo "exit=$?"
cat "$S/read-s.json"
```

用 Step 2 最后那个 `sips` 循环核对尺寸，文件路径换成 `read-s.json`。预期：左右两侧的图都取到了，尺寸一一对应。

- [ ] **Step 4: 还没下载原图的图片，同时测未读**

```bash
./wechat/wx-send.sh -a work --image "$TO_Z" ~/Desktop/RedemptionPage.png; echo "exit=$?"
```

这时 zhiwuzhu 不要点开这个聊天，它会显示为未读。然后：

```bash
WX_IMAGES=1 ./wechat/wx-send.sh -a zhiwuzhu --unread > "$S/unread-z.json"; echo "exit=$?"; cat "$S/unread-z.json"
```

记录：
- 这条图片消息拿到的是 `image` 还是 `image_error`。
- 如果是 `image`，尺寸是不是 421×747，也就是原图而不是缩略图。
- 如果是 `NO_IMAGE`（等待超时），调大 `IMAGE_WAIT` 的默认值再试一次。

另外还要确认：
- 读完后这个聊天被标回了未读（`remarked_unread: true`）。
- `--unread` 不带 `WX_IMAGES` 时，输出里没有 `image` 字段。这一点用 `--forget` 清掉读取进度后，再让 work 发一条文字来验证，可选。

- [ ] **Step 5: 定下常量，删掉调试代码**

- 把 `COPY_TITLES`、`IMG_DX`（或者确认 AXImage 分支可用后，删掉固定偏移的分支）、`IMAGE_WAIT` 的默认值改成实测值，注释写明是实测的。
- 删掉 `IMAGE_DEBUG` 常量、`debugRow` 函数，以及 `copyImage` / `copyAt` 里用到它们的 `if IMAGE_DEBUG ...` 三行。
- 运行类型检查，然后再跑一次 Step 2 的读取（不带 `WX_IMAGE_DEBUG`），确认结果不变。

- [ ] **Step 6: 写实测结论并提交**

把 spec 最后的「（第 0 步做完后填写）」换成实测结果，覆盖 spec 第 0 步的四个问题，以及 Review Focus 第 1、3、4 条的结果。然后删掉 `inbox/` 里这次生成的测试文件。

```bash
git add wechat/wx-send.sh docs/superpowers/specs/2026-09-28-wechat-read-image-design.md
git commit -m "wx-send.sh 按实测结果定下图片右键复制的定位和菜单项

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 使用文档

**Files:**
- Modify: `README.md`
- Modify: `docs/muse-prompt.md`

- [ ] **Step 1: README，新增「读图片」一节**

放在「发图片」一节后面、「限制」前面，内容如下。其中的「每张大约 X 秒」按 Task 4 实测的耗时填写：

````markdown
### 读图片

`wechat-read` 和 `wechat-unread` 加上 `--images`，读到图片消息时会把原图取出来：在 Mac 上右键图片 →「复制」→ 存进仓库的 `inbox/`（不会提交到 Git），然后 mab 自动下载到 Muse 那边的 `./wechat-images/`，并在这条消息上加 `local_path`。

```bash
python3 mab.py wechat-read "张三" -n 10 --images -a work
python3 mab.py wechat-unread --images -a work
```

返回示例：
```json
{"type": "message", "text": "图片", "image": "in-20260928-141500-1.png",
 "local_path": "/home/muse/wechat-images/in-20260928-141500-1.png"}
```

**注意：**
- 默认不取图。每张要右键复制一次，大约 X 秒；每次最多 10 张（`--max-images`，最多 50），超出的会写在 `notes` 里。
- 取不到的图片，这条消息会有 `image_error` 说明原因，其他消息照常返回。
- 别人发来、还没点开过的图，微信可能要先下载原图（实测结论见设计文档）。
- 读的过程中剪贴板会被用到，读完会恢复原样。
- `inbox/` 里的图片保留 3 天（`WX_INBOX_DAYS`），下次带 `--images` 读的时候自动清理。
- **隐私**：带 `--images` 时，别人发给你的图片会传到 Muse 的云端 VM。群聊里的图片尤其注意，需要时再开。
````

- [ ] **Step 2: README，HTTP API 表和配置表**

HTTP API 表里：
- `/wechat/read` 的请求体改成 `{"chat","limit?","account?","images?","max_images?"}`
- `/wechat/unread` 的请求体改成 `{"account?","list_only?","max_chats?","max_messages?","images?","max_images?"}`
- 加一行：`| GET | \`/wechat/inbox/file?file=\` | — | 读消息时取到的原图 |`

配置表里加三行：

```markdown
| `WX_MAX_IMAGES` | `10` | 读消息带图时，每次最多取几张 |
| `WX_IMAGE_WAIT` | `3` | 点「复制」后，最多等几秒让剪贴板出现图片 |
| `WX_INBOX_DAYS` | `3` | `inbox/` 里的图片保留几天 |
```

（`WX_IMAGE_WAIT` 的默认值按 Task 4 的实测结果填写。）

客户端命令列表里，`wechat-unread` 那一行后面加上：

```bash
python3 mab.py wechat-unread --images -a work              # 同时把图片下载下来（见下文）
```

- [ ] **Step 3: docs/muse-prompt.md**

在「所有未读」那一行后面加上：

```markdown
- 需要看聊天里的图片时，在 `wechat-read` / `wechat-unread` 后面加 `--images`。消息里有 `local_path` 的，把那张图发到你和我的对话里，并说明是谁、在哪个聊天里发的；有 `image_error` 或 `download_error` 的，照实告诉我
```

- [ ] **Step 4: 提交**

```bash
git add README.md docs/muse-prompt.md
git commit -m "README 和 Muse 说明补充读图片

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 端到端

**需要用户配合**：需要 work 再给 zhiwuzhu 发一张图，制造一条未读。这一步由我们用命令发。

- [ ] **Step 1: 起临时服务**

```bash
openssl rand -hex 16 > "$S/token"
AGENT_TOKEN=$(cat "$S/token") PORT=8799 exec .venv/bin/python mac_agent_server.py > "$S/server.log" 2>&1   # 用 run_in_background
```

- [ ] **Step 2: 走一遍 mab 的读图**

```bash
./wechat/wx-send.sh -a work --image "$TO_Z" "$S/red.png"; echo "exit=$?"
export MAB_URL=http://127.0.0.1:8799 MAB_TOKEN=$(cat "$S/token")
cd "$S" && /usr/bin/python3 /Users/tom.huang/muse-intel-mac-bridge/mab.py wechat-unread --images -a zhiwuzhu
ls -la "$S/wechat-images"
/usr/bin/python3 /Users/tom.huang/muse-intel-mac-bridge/mab.py wechat-read "$TO_S" -n 4 --images -a zhiwuzhu
```

预期：
- unread 的输出里，这条图片消息有 `image` 和 `local_path`，`$S/wechat-images/` 下有对应的文件，尺寸是 300×200。
- read 的输出里，每条图片消息都有 `local_path`。

- [ ] **Step 3: 收尾**

- 停掉临时服务：只停 8799 端口上的那个进程，用 `lsof -tiTCP:8799 -sTCP:LISTEN | xargs kill`，**不要用 `pkill -f mac_agent_server.py`**，否则会把用户自己在跑的 bridge 也停掉。
- 删掉 `inbox/` 里的测试文件和 `$S/wechat-images/`。
- 最后跑一次测试命令和类型检查，全部通过。

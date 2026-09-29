# 微信新消息探测（peek）· 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增不碰微信的 `GET /wechat/peek`，用 macOS 通知记录、Dock 角标、待处理缓存超时三个信号告诉 Muse 的 hook 该不该唤醒 agent；读到撤回提示时记日志；`send`/`read`/`whois` 做完切回「文件传输助手」。

**Architecture:** 全部新逻辑在 Python 服务端：只读打开通知 sqlite 数据库、跑 `lsappinfo` 读角标、读待处理缓存，状态存在 `~/.cache/wx-send/peek/<账号>.json`；peek 走 GET、不拿全局锁，用单独的 `peek_lock` 保护状态文件。`/wechat/unread` 成功后更新 peek 状态。Swift 只加一个 `park()`，在 `send`/`read`/`whois` 结束时调用。

**Tech Stack:** Python 3.9+ 标准库（`sqlite3`、`plistlib`）；Swift 5.10。

**Spec:** `docs/superpowers/specs/2026-09-29-wechat-peek-design.md`

## Global Constraints

- 分支：`feat/wechat-read-image`。
- Python 代码兼容 3.9；`mab.py` 只能用标准库。
- 没有指定账号时，账号名用 `default`；账号名规则沿用 `pending_path`（不能含 `/`，不能是 `.`、`..`）。
- 通知数据库路径按顺序：`$(getconf DARWIN_USER_DIR)com.apple.notificationcenter/db2/db`、`~/Library/Group Containers/group.com.apple.usernoted/db2/db`；只读打开（`mode=ro`）。
- 通知时间：`delivered_date` + 978307200 = Unix 时间。数据库里的 `identifier` 是小写 bundle ID。
- `new` 最多 50 条，从旧到新；超时默认 30 分钟，`WX_PEEK_STALE_MIN` 可改（正整数）。
- peek 只在 `wake: true` 或带 `notify_error` 或非 200 时记请求日志。
- 撤回只记日志，返回内容不变。
- 提交信息用中文，末尾加上 `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` 和 `Claude-Session: https://claude.ai/code/session_01FVD1Ziho3oPbQo4ghQ1zyB`。
- 真机测试方向：**work → zhiwuzhu**，只在 zhiwuzhu 上跑 `unread`/`peek`。work 是 Muse 正在用的生产号，不在它上面跑 `unread`、`ack`，免得动到真实客户的待处理消息和 peek 状态。zhiwuzhu 的「通知显示消息详情」已经开着。

测试命令：`/usr/bin/python3 -m unittest discover -s tests -v`

Swift 类型检查命令（下文简称「类型检查」）：

```bash
S=/private/tmp/claude-502/-Users-tom-huang-muse-intel-mac-bridge/e93d7f04-40e0-4e76-aa54-94b4faf3f8d0/scratchpad
sed -n "/^cat <<'SWIFT'\$/,/^SWIFT\$/p" wechat/wx-send.sh | sed '1d;$d' > "$S/wx.swift"
swiftc -typecheck -target "$(uname -m)-apple-macos13.0" "$S/wx.swift" && echo TYPECHECK_OK
```

## Review Focus

1. **微信在运行但从没有过角标**：`lsappinfo` 输出 `"StatusLabel"=[ NULL ]`，要算 0，不能当成没运行（`None`）。Task 1 的 `parse_badge` 测试覆盖。
2. **`unread` 跑的过程中进来的通知**：`since` 必须取 `unread` 开始的时间，不是结束时间，否则这些消息既没被读到、也不再唤醒。Task 2 用「通知时间在 unread 开始之后」的用例覆盖。
3. **peek 状态文件写不进去或账号解析失败**：不能让已经成功的 `unread` 报错（消息已经进了待处理缓存）。Task 2 用 `save_peek` 抛 `OSError` 的用例覆盖。
4. **通知数据库损坏或被锁住**：要落到 `notify_error`，角标和超时两个信号照常工作，不能整个 peek 报 500。Task 1 用一个内容是垃圾的数据库文件覆盖。
5. **`unread` 正在跑（拿着全局锁）时 peek**：必须马上返回。Task 2 在持有 `server.lock` 的情况下调 peek，限时 2 秒。

---

### Task 0: 提交文档

- [ ] 提交设计文档和本计划。提交信息：`新增微信新消息探测（peek）的设计文档和实施计划`

```bash
git add docs/superpowers/specs/2026-09-29-wechat-peek-design.md docs/superpowers/plans/2026-09-29-wechat-peek.md
git commit -m "新增微信新消息探测（peek）的设计文档和实施计划" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FVD1Ziho3oPbQo4ghQ1zyB"
```

---

### Task 1: 读通知、读角标、账号对应 bundle ID

**Files:** Modify `mac_agent_server.py`（常量区、`pending_path` 附近新增函数）；Test `tests/test_images.py`

**Interfaces（Produces）:**
- `CD_EPOCH = 978307200`
- `NOTIFY_DBS: list[str]`：按顺序尝试的通知数据库路径
- `parse_notification(data: bytes, when: float) -> dict`：`{"chat", "id", "preview", "time"}`
- `read_notifications(bundle: str, since: float) -> list[dict]`：`since`（Unix 时间）之后送达的，从旧到新；所有数据库都打不开时抛 `OSError`
- `parse_badge(out: str) -> Optional[int]`、`read_badge(bundle: str) -> Optional[int]`
- `wechat_bundle(account: Optional[str]) -> str`：出错抛 `ValueError`
- 测试辅助（模块级）：`notif_blob(title, body, chatname) -> bytes`、`make_notify_db(path)`、`add_notif(path, when, title="张三", body="在吗", chatname="wxid_zs", app_id=1)`；app_id 1 = `com.test.wechat`，2 = `com.test.other`

- [ ] **Step 1: 写失败测试。** 在 `tests/test_images.py` 顶部 import 里加 `import plistlib`、`import sqlite3`、`from unittest import mock`，在 `png()` 后面加测试辅助，再加两个测试类：

```python
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
```

```python
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
        self.assertEqual(server.parse_badge('"StatusLabel"=[ NULL ] \n'), 0)        # 在运行，从没设过角标
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
```

- [ ] **Step 2: 跑测试，确认失败。** 运行 `/usr/bin/python3 -m unittest tests.test_images.NotifyTest tests.test_images.BundleTest -v`，预期因为 `read_notifications` 等不存在而报 `AttributeError`。

- [ ] **Step 3: 实现。** `mac_agent_server.py` 顶部 import 加 `import plistlib`、`import sqlite3`（按字母序插入）。常量区 `PENDING_DIR` 下面加：

```python
CD_EPOCH = 978307200   # 通知数据库的时间从 2001-01-01 UTC 起算


def _notify_dbs():
    d = subprocess.run(["getconf", "DARWIN_USER_DIR"], capture_output=True, text=True).stdout.strip()
    return ([os.path.join(d, "com.apple.notificationcenter/db2/db")] if d else []) + [
        os.path.expanduser("~/Library/Group Containers/group.com.apple.usernoted/db2/db")]   # macOS 15 起在这里


NOTIFY_DBS = _notify_dbs()   # 系统通知记录（macOS 14 / 15+），peek 从这里看谁发来了新消息
```

在 `pending_path` 前面加：

```python
def parse_notification(data, when) -> dict:
    """一条通知记录 → 聊天名、对方内部 ID（usda 里的 chatname）、预览、本地时间。"""
    req = plistlib.loads(data).get("req", {})
    chat_id = ""
    try:
        arc = plistlib.loads(req["usda"])
        objs = arc["$objects"]
        root = objs[arc["$top"]["root"].data]
        info = {objs[k.data]: objs[v.data] for k, v in zip(root["NS.keys"], root["NS.objects"])}
        chat_id = info.get("chatname") or ""
    except Exception:   # 归档格式变了也不影响聊天名和预览
        pass
    return {"chat": req.get("titl") or "", "id": chat_id, "preview": req.get("body") or "",
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(when))}


def read_notifications(bundle, since) -> list:
    """这个微信在 since（Unix 时间）之后送达的通知，从旧到新；数据库都打不开时抛 OSError。"""
    errors = []
    for path in NOTIFY_DBS:
        if not os.path.exists(path):
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                rows = con.execute(
                    "SELECT r.data, r.delivered_date FROM record r JOIN app a ON a.app_id = r.app_id"
                    " WHERE a.identifier = ? AND r.delivered_date > ? ORDER BY r.delivered_date",
                    (bundle.lower(), since - CD_EPOCH)).fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            errors.append(f"{path}：{e}")
            continue
        return [parse_notification(data, t + CD_EPOCH) for data, t in rows]
    raise OSError("打不开通知数据库：" + ("；".join(errors) or "没找到"))


def parse_badge(out) -> Optional[int]:
    """lsappinfo 的输出 → 角标数字；没在运行返回 None，没有角标算 0，有字没数字算 1。"""
    if "StatusLabel" not in out:
        return None
    m = re.search(r'"label"="([^"]*)"', out)
    if not m or not m.group(1):
        return 0
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else 1


def read_badge(bundle) -> Optional[int]:
    """读 Dock 角标，不碰微信。"""
    try:
        out = subprocess.run(["lsappinfo", "info", "-only", "StatusLabel", "-app", bundle],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_badge(out)


def wechat_bundle(account) -> str:
    """账号 → 微信的 bundle ID：按 WX_ACCOUNTS 找 App；没给账号时用唯一配置的、或唯一在运行的微信。"""
    apps = dict(x.strip().split("=", 1) for x in os.environ.get("WX_ACCOUNTS", "").split(",") if "=" in x)
    if account:
        if account not in apps:
            raise ValueError(f"未知账号：{account}（已配置：{'、'.join(apps) or '无'}）")
        app = apps[account]
    elif len(apps) == 1:
        app = next(iter(apps.values()))
    elif apps:
        raise ValueError(f"配置了多个微信账号，请指定 account（{'、'.join(apps)}）")
    else:
        pids = subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True, text=True).stdout.split()
        if len(pids) != 1:
            raise ValueError("没有或有多个正在运行的微信，请指定 account")
        cmd = subprocess.run(["ps", "-p", pids[0], "-o", "command="], capture_output=True, text=True).stdout
        app = cmd.split("/Contents/MacOS/")[0]
    try:
        with open(os.path.join(os.path.expanduser(app.strip()), "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f)["CFBundleIdentifier"]
    except (OSError, KeyError, plistlib.InvalidFileException) as e:
        raise ValueError(f"读不到 {app} 的 bundle ID：{e}")
```

- [ ] **Step 4: 跑测试，确认通过。** 运行 Step 2 的命令，预期全部 PASS；再跑一遍全部测试，确认没有破坏现有用例。

- [ ] **Step 5: 本机手动核对一次。** 运行下面的命令，预期打印 `com.tencent.xinWeChat2`、一个数字，以及一个列表（可能为空）：

```bash
set -a; source .env; set +a
/usr/bin/python3 -c "
import sys, types, time; sys.modules['pyautogui'] = types.SimpleNamespace(FAILSAFE=True, PAUSE=0)
import mac_agent_server as s
b = s.wechat_bundle('work'); print(b, s.read_badge(b)); print(s.read_notifications(b, time.time() - 3600)[-3:])"
```

- [ ] **Step 6: 提交。** 提交信息：`bridge 能读系统通知记录和 Dock 角标，按账号找到微信的 bundle ID`

---

### Task 2: `GET /wechat/peek` 和 unread 后更新状态

**Files:** Modify `mac_agent_server.py`（`save_pending`、`a_wechat_unread`、`Handler`、文件头部接口说明）；Test `tests/test_images.py`

**Interfaces:**
- Consumes：Task 1 的 `read_notifications`、`read_badge`、`wechat_bundle`
- Produces：
  - `PEEK_DIR: str`、`peek_lock: threading.Lock`
  - `write_json(path, data) -> None`（原子替换，`save_pending` 改用它）
  - `peek_path(account) -> str`、`load_peek(account) -> Optional[dict]`、`save_peek(account, st) -> None`
  - `stale_minutes() -> int`、`stale_chats(data, cutoff: str) -> list`
  - `a_wechat_peek(account) -> dict`、`mark_unread_done(account, started: float) -> None`
  - 测试辅助：`ServerCase.setUp` 里 `self.badge`（假角标）、`self.db`（临时通知数据库），`server.PEEK_DIR` 指到临时目录，`server.wechat_bundle`、`server.read_badge` 换成替身

- [ ] **Step 1: 改 `ServerCase.setUp`。** 在 `server.PENDING_DIR = ...` 后面加：

```python
        server.PEEK_DIR = os.path.join(self.tmp, "peek")
        self.db = os.path.join(self.tmp, "notify.db")
        make_notify_db(self.db)
        self.badge = 0
        for name, fake in (("NOTIFY_DBS", [self.db]), ("wechat_bundle", lambda account: "com.test.WeChat"),
                           ("read_badge", lambda bundle: self.badge)):
            self.addCleanup(setattr, server, name, getattr(server, name))
            setattr(server, name, fake)
```

- [ ] **Step 2: 写失败测试。** 新增 `PeekTest(ServerCase)`：

```python
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
```

- [ ] **Step 3: 跑测试，确认失败。** 运行 `/usr/bin/python3 -m unittest tests.test_images.PeekTest -v`，预期 404 / `AttributeError`。

- [ ] **Step 4: 实现存储和计算。** 常量区 `PENDING_DIR` 下面加：

```python
PEEK_DIR = os.path.expanduser("~/.cache/wx-send/peek")   # peek 的状态：新通知从哪算起、角标基线、上次超时提醒
```

`lock = threading.Lock()` 下面加：

```python
peek_lock = threading.Lock()   # peek 不拿全局锁，只用它保护 peek 状态文件的「读→改→写」
```

把 `save_pending` 改成共用 `write_json`，并在它后面加 peek 相关函数：

```python
def write_json(path, data):
    """先写临时文件再改名，写到一半退出也不会把旧文件写坏。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def save_pending(account, data):
    write_json(pending_path(account), data)
```

```python
def peek_path(account) -> str:
    return os.path.join(PEEK_DIR, os.path.basename(pending_path(account)))


def load_peek(account) -> Optional[dict]:
    try:
        with open(peek_path(account), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_peek(account, st):
    write_json(peek_path(account), st)


def stale_minutes() -> int:
    v = os.environ.get("WX_PEEK_STALE_MIN", "30")
    if not v.isdigit() or int(v) < 1:
        raise ValueError(f"WX_PEEK_STALE_MIN 必须是正整数：{v!r}")
    return int(v)


def stale_chats(data, cutoff) -> list:
    """待处理缓存里 added 早于 cutoff 的消息，按聊天汇总。"""
    out = []
    for name, c in data["chats"].items():
        old = [m["added"] for m in c["messages"] if m["added"] < cutoff]
        if old:
            out.append({"chat": name, "count": len(old), "oldest": min(old)})
    return out


def a_wechat_peek(account):
    """不碰微信，看有没有要处理的：新通知、角标超过基线、待处理消息超时没人管。"""
    bundle, now, minutes = wechat_bundle(account), time.time(), stale_minutes()
    badge = read_badge(bundle)
    result, reasons = {"ok": True, "new": [], "badge": badge}, []
    with peek_lock:
        st = load_peek(account) or {"since": now, "badge_base": badge or 0, "reminded": 0}   # 第一次：历史不算新
        try:
            result["new"] = read_notifications(bundle, st["since"])[-50:]
        except OSError as e:
            result["notify_error"] = str(e)
        if result["new"]:
            reasons.append("new")
        if badge is not None and badge < st["badge_base"]:
            st["badge_base"] = badge          # 在手机上读掉了几条
        elif badge is not None and badge > st["badge_base"]:
            reasons.append("badge")
        data = load_pending(account)
        result["stale"] = stale_chats(data, time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - minutes * 60)))
        if result["stale"] and now - st["reminded"] >= minutes * 60:
            reasons.append("stale")
            st["reminded"] = now
        save_peek(account, st)
    result.update(badge_base=st["badge_base"], pending=sum(len(c["messages"]) for c in data["chats"].values()),
                  reasons=reasons, wake=bool(reasons))
    return result


def mark_unread_done(account, started):
    """unread 成功后：新通知从这次开始的时间算起，角标基线取现在的值；出错只记日志，不影响 unread 的结果。"""
    try:
        badge = read_badge(wechat_bundle(account))
    except ValueError:
        badge = None
    try:
        with peek_lock:
            st = load_peek(account) or {"badge_base": 0, "reminded": 0}
            st["since"] = started
            if badge is not None:
                st["badge_base"] = badge
            save_peek(account, st)
    except (OSError, ValueError) as e:
        log.info("peek 状态没写进去：%s", e)
```

- [ ] **Step 5: 接进 `a_wechat_unread`。** 在 `code, out, err = run_wx(...)` 之前加 `started = time.time()   # 用开始时间：跑的过程中进来的通知下次还算新的`；在 `save_pending` 的 `try/except` 之后（仍在 `if result["ok"] and not p.get("list_only"):` 里）加一行 `mark_unread_done(p.get("account"), started)`。

- [ ] **Step 6: 接进 `Handler`。** 类属性加 `quiet = False`，并改三处：

`_send` 里最后的 `log.info(...)` 前加条件：

```python
        if self.quiet and code == 200:   # peek 每分钟一次，只在要唤醒或出错时记
            return
        log.info(...)
```

`do_GET`、`do_POST` 开头 `self.started = time.time()` 后面都加 `self.quiet = False`（现在是 HTTP/1.0，一个连接一个请求；以后改成长连接时不会串）。

`do_GET` 的 `/wechat/images` 分支前面加：

```python
            if path == "/wechat/peek":
                account = parse_qs(urlparse(self.path).query).get("account", [None])[0]
                self.what = json.dumps({"account": account}, ensure_ascii=False) if account else ""
                result = a_wechat_peek(account)
                self.quiet = not result["wake"] and "notify_error" not in result
                return self._send(200, result)
```

文件头部的接口说明在 `POST /wechat/ack` 那行后面加：

```
  GET  /wechat/peek?account=work                              有没有要处理的（新通知、角标、超时的待处理），不碰微信、不排队
```

环境变量说明里加一行：`WX_PEEK_STALE_MIN  待处理消息超过几分钟没处理就让 peek 提醒一次（默认 30）`。

- [ ] **Step 7: 跑测试，确认通过。** 先跑 `PeekTest`，再跑全部测试。

- [ ] **Step 8: 提交。** 提交信息：`bridge 新增 /wechat/peek：不碰微信，按新通知、角标和超时的待处理判断要不要唤醒`

---

### Task 3: 读到撤回提示时记日志

**Files:** Modify `mac_agent_server.py`（`a_wechat_read`、`a_wechat_unread`）；Test `tests/test_images.py`

**Interfaces（Produces）:** `RECALL: re.Pattern`、`log_recalls(account, chats: list[tuple[str, list]]) -> None`

- [ ] **Step 1: 写失败测试。** 加到 `LogTest`：

```python
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
```

- [ ] **Step 2: 跑测试，确认失败。** `/usr/bin/python3 -m unittest tests.test_images.LogTest -v`

- [ ] **Step 3: 实现。** 常量区加：

```python
RECALL = re.compile(r'^(["“].+["”]|对方) ?撤回了一条消息$')   # 对方的撤回提示；自己的「你撤回了一条消息」不算
```

`a_wechat_unread` 前面加：

```python
def log_recalls(account, chats):
    """读到对方撤回的提示时记一行日志；回不回由 Muse 看聊天决定。chats 是 [(聊天名, 消息列表)]。"""
    for name, msgs in chats:
        for m in msgs:
            if m.get("type") == "message" and RECALL.match(m.get("text", "")):
                log.info("撤回\t%s\t%s\t%s", account or "default", name, m["text"])
```

`a_wechat_read` 在 `data = json.loads(...)` 之后加 `log_recalls(p.get("account"), [(chat, data.get("items", []))])`。
`a_wechat_unread` 在 `result = wx_json(code, out, err)` 之后加 `log_recalls(p.get("account"), [(c.get("name", ""), c.get("messages", [])) for c in result.get("chats", [])])`。

- [ ] **Step 4: 跑全部测试，确认通过。**

- [ ] **Step 5: 提交。** 提交信息：`bridge 读到对方撤回的提示时记一行日志`

---

### Task 4: mab.py `wechat-peek`

**Files:** Modify `mab.py`；Test `tests/test_images.py`

**Interfaces:** Consumes Task 2 的 `GET /wechat/peek`。

- [ ] **Step 1: 写失败测试。** 加到 `MabTest`：

```python
    def test_peek(self):
        add_notif(self.db, time.time() + 1)
        server.save_peek("work", {"since": time.time() - 10, "badge_base": 0, "reminded": 0})
        r = self.mab("wechat-peek", "-a", "work")
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertEqual((body["wake"], body["new"][0]["chat"]), (True, "张三"))
```

- [ ] **Step 2: 跑测试，确认失败。** `/usr/bin/python3 -m unittest tests.test_images.MabTest.test_peek -v`

- [ ] **Step 3: 实现。** 用法说明在 `wechat-pending` 那行后面加：

```
  python3 mab.py wechat-peek [-a work]              # 有没有要处理的（新通知、角标、超时的待处理），不碰微信，给 hook 用
```

子命令定义在 `wpd = ...` 那行后面加：

```python
    wpk = sub.add_parser("wechat-peek"); wpk.add_argument("-a", "--account")
```

分发在 `wechat-pending` 分支后面加：

```python
    elif a.cmd == "wechat-peek":
        q = "?account=" + urllib.parse.quote(a.account) if a.account else ""
        body, _ = request("GET", "/wechat/peek" + q); print(body.decode())
```

- [ ] **Step 4: 跑全部测试，确认通过。**

- [ ] **Step 5: 提交。** 提交信息：`mab.py 新增 wechat-peek`

---

### Task 5: Swift：send / read / whois 做完切回文件传输助手

**Files:** Modify `wechat/wx-send.sh`（`Session` 里 `openChat` 后面；`run()` 里 read/whois 分支和 send 循环结尾）

**Interfaces（Produces）:** `Session.park()`

和 spec 的差别：`read`/`whois` 的 JSON 在切回之前就已经输出了，切不过去时没法再写进 note，改为往 stderr 打一行 `⚠️`，退出码不变。

- [ ] **Step 1: 加 `park()`。** 在 `openChat` 函数结束的 `}` 后面加：

```swift
    /// send / read / whois 做完切回文件传输助手：停在客户的聊天上，他紧接着回的消息会直接变成已读，也不发通知
    func park() {
        guard isFront() else { return }   // 被切走了就不抢焦点
        var n: [String] = []
        do { try openChat(HOME_CHAT, &n) } catch {
            if inSearch && isFront() { key(K_ESC) }
            inSearch = false
            eprint("⚠️ 没能切回「\(HOME_CHAT)」：\((error as? WXError)?.msg ?? "\(error)")")
        }
    }
```

- [ ] **Step 2: read / whois 结束时切回。** 在 `if mode == "read" || mode == "whois" {` 分支里，`defer { clip.restore() ... }` 那个块的**后面**加一行（后声明的 defer 先执行，切回时剪贴板还没恢复，搜索粘贴不会弄乱用户的剪贴板）：

```swift
            defer { session.park() }
```

- [ ] **Step 3: send 结束时切回。** 在发送循环结束之后、`return worst` 之前加：

```swift
        if !DRY_RUN { session.park() }   // 试运行要让人看到输入框里的内容，不切走
```

- [ ] **Step 4: 类型检查。** 运行「类型检查」，预期输出 `TYPECHECK_OK`。

- [ ] **Step 5: 真机。**
  1. `./wechat/wx-send.sh -a zhiwuzhu --read "文件传输助手" 3`，然后 `./wechat/wx-send.sh -a zhiwuzhu --read "联系人A" 3`：预期结束后 zhiwuzhu 的微信停在「文件传输助手」（截图或 AX 标题确认）。
  2. `./wechat/wx-send.sh -a zhiwuzhu --whois "联系人A"`：同样停在「文件传输助手」。
  3. `WX_DRY_RUN=1 ./wechat/wx-send.sh -a zhiwuzhu "联系人A" "测试"`：停在 联系人A，输入框里有「测试」。之后手动清空输入框，切回文件传输助手。
  4. 真实发送：`./wechat/wx-send.sh -a work "<zhiwuzhu 在 work 里的聊天名>" "park 测试"`：预期 work 的微信停在「文件传输助手」。聊天名用会话列表里的完整名字。

- [ ] **Step 6: 提交。** 提交信息：`send、read、whois 做完切回文件传输助手，客户紧接着回的消息不会直接变成已读`

---

### Task 6: 文档

**Files:** Modify `README.md`、`docs/muse-prompt.md`

- [ ] **README：**
  - 「待处理缓存」那一节后面新增「自动回复：用 peek 判断要不要唤醒」：hook 每 1~2 分钟调 `wechat-peek`，`wake` 为 true 才唤醒 agent；三个信号各一句话；前提是微信设置里打开「通知显示消息详情」，否则只有 `id` 没有聊天名和预览；`new[].chat` 只供参考，回复用 `pending` 里的名字；`id` 是微信内部 ID，不等于资料卡上的微信号，发兑换码仍用 `wechat-whois`；读到撤回提示会在 `mab-bridge.log` 里记一行。
  - HTTP API 表格加一行：`| GET | /wechat/peek?account= | — | 有没有要处理的（新通知、角标、超时的待处理），不碰微信、不排队 |`
  - 客户端命令列表加 `python3 mab.py wechat-peek -a work`。
  - 环境变量表加 `WX_PEEK_STALE_MIN`。
- [ ] **muse-prompt：** 在「微信」那一节末尾加「自动回复」小节，内容是 spec「muse-prompt」一节的 4 条，原样搬过去，把 `<账号>` 写成 `<账号别名>` 和现有写法一致。
- [ ] **提交。** 提交信息：`README 和 Muse 说明补充 wechat-peek`

---

### Task 7: 真机端到端（work → zhiwuzhu）

- [ ] **Step 1: 起一个临时 bridge。** 在 8799 端口后台启动：`set -a; source .env; set +a; PORT=8799 .venv/bin/python mac_agent_server.py`。收尾时只停这个端口上的进程。下面的 `mab` 指 `MAB_URL=http://127.0.0.1:8799 MAB_TOKEN=$AGENT_TOKEN python3 mab.py`。
- [ ] **Step 2: 第一次 peek。** `mab wechat-peek -a zhiwuzhu`：预期 `wake: false`、`new: []`，生成 `~/.cache/wx-send/peek/zhiwuzhu.json`。记下 `badge`。
- [ ] **Step 3: 新消息。** 从 work 给 zhiwuzhu 发「peek 测试 1」（`./wechat/wx-send.sh -a work "<聊天名>" "peek 测试 1"`）。等 5 秒后 `mab wechat-peek -a zhiwuzhu`：预期 `wake: true`，`new` 里有 work 在 zhiwuzhu 里的聊天名、`id`、预览「peek 测试 1」；**peek 前后 zhiwuzhu 的微信没有被切到前台**（peek 前把别的 App 放在前台，peek 后确认还在前台）。记下这时的 `badge`，和 Step 2 比较，写进结果。
- [ ] **Step 4: unread 之后不再唤醒。** `mab wechat-unread --max-chats 1 -a zhiwuzhu`，然后 `mab wechat-peek -a zhiwuzhu`：预期 `wake: false`、`badge_base` 等于当前 `badge`。
- [ ] **Step 5: 标回未读的聊天再来消息，角标涨不涨。** work 再发「peek 测试 2」，等 5 秒 peek：记录 `badge` 有没有超过 `badge_base`（`reasons` 里有没有 `badge`），写进结果；`new` 里应该有这条。
- [ ] **Step 6: 超时提醒。** 用 `WX_PEEK_STALE_MIN=1` 重启临时 bridge，等 1 分钟后 peek：预期 `reasons` 含 `stale`；马上再 peek 一次，不再含 `stale`。然后 `mab wechat-ack "<聊天名>" -a zhiwuzhu` 清掉待处理，再用默认设置重启。
- [ ] **Step 7: 撤回（需要人手操作）。** 请用户在 work 上给 zhiwuzhu 发一条再右键撤回。然后 `mab wechat-read "<聊天名>" -n 5 -a zhiwuzhu`，确认：撤回提示的确切文字；`~/Library/Logs/mab-bridge.log` 里有 `撤回` 那一行（文字和正则对不上时修改 `RECALL`，补一条测试后单独提交）；通知数据库里原来那条通知还在不在、有没有多一条新通知。
- [ ] **Step 8: 后台开着聊天时来消息。** 让 zhiwuzhu 的微信开着 work 的聊天，然后把别的 App 切到前台；work 发一条。看 peek 的 `new` 里有没有这条、zhiwuzhu 会话列表里这个聊天是不是未读。写进结果。
- [ ] **Step 9: 收尾。** 停掉临时 bridge；`wechat-ack` 清掉 zhiwuzhu 测试产生的待处理；删掉 `~/.cache/wx-send/peek/zhiwuzhu.json`（测试用的状态）；跑一遍全部测试。
- [ ] **Step 10: 把观察结果写进 spec。** 在 spec 的「测试」一节后面加「真机结果」：角标数字、标回未读后再来消息角标涨不涨、撤回提示原文和通知变化、后台开着聊天时有没有通知和未读、好友申请的通知（没遇到就写「未验证」）。提交信息：`peek 设计文档补充真机结果`

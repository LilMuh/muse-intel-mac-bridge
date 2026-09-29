# 用系统通知收微信消息 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** peek 和 `wechat-pending` 直接从 macOS 通知记录把新消息收进待处理（不碰微信），通知里看不全的消息标 `needs_read`；peek 只按「有新消息」和「超时没处理」唤醒。

**Architecture:** 全在 `mac_agent_server.py`：`read_notifications` 改成按 `rec_id` 游标读并返回统计；新增 `notice_message`（一条通知 → 一条待处理消息）、`add_notices`（归到聊天、处理改名）、`ingest`（游标、升级、缺口提示）；`cache_lock` 取代 `peek_lock`，保护待处理缓存和 peek 状态的所有读改写；peek 去掉角标基线和 incomplete，`mark_unread_done` 删掉。Swift 不改。

**Tech Stack:** Python 3.9+ 标准库。

**Spec:** `docs/superpowers/specs/2026-09-29-wechat-notify-pending-design.md`

## Global Constraints

- 分支：`feat/wechat-notify-pending`。Python 兼容 3.9；`mab.py` 只用标准库。
- **仓库是公开的：代码、测试、文档、提交信息里不能出现真实的联系人名、微信号、内部 ID、群名，也不能出现生产号的别名和昵称。** 测试用 `张三`、`李四`、`wxid_zs`、`123@chatroom` 这类假数据；文档里生产号写成 `<生产号别名>`。
- 截断判定：通知正文 UTF-8 长度 ≥ 196 字节 → `truncated`。
- 图片判定：`text.strip() == "[图片]"`。被 @ 判定：群聊正文匹配 `^(.+?)在群聊中@了(你|所有人)$`。
- 唤醒理由只有 `new`、`stale`；`badge` 只作参考；`WX_PEEK_STALE_MIN` 默认 30。
- 方法文档注释只写一行。
- 提交信息用中文，末尾加上 `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` 和 `Claude-Session: https://claude.ai/code/session_01FVD1Ziho3oPbQo4ghQ1zyB`。

测试命令：`/usr/bin/python3 -m unittest discover -s tests -v`

## Review Focus

1. **统计和取行之间又进来一条通知**：游标必须按这次真正取到的行推进，不能用先查的最大 `rec_id`，否则那条会被跳过。Task 2 用替身让 `read_notifications` 返回 `hi > last`，断言游标等于 `last`。
2. **最后一条是坏记录**：解析失败的记录也要算进 `last`，否则游标卡住、每次重读。Task 1 断言 `last` 含坏记录的 `rec_id`。
3. **写待处理缓存失败**：游标不能推进，下次要重新收这批。Task 2 用 `save_pending` 抛 `OSError` 覆盖。
4. **备注改名后新名字已经存在**：两边的消息要按 `id` 合并，不能丢。Task 2 的改名用例覆盖。
5. **群聊正文里内容本身带冒号**（`李四: 时间: 3点`）：只按第一个冒号拆。Task 1 覆盖。

---

### Task 0: 提交计划

- [ ] `git add docs/superpowers/plans/2026-09-29-wechat-notify-pending.md`，提交信息：`新增用系统通知收微信消息的实施计划`

---

### Task 1: 按游标读通知，一条通知变成一条待处理消息

**Files:** Modify `mac_agent_server.py`（`read_notifications` 替换；`RECALL` 下面加常量；`read_notifications` 后面加 `notice_message`）；Test `tests/test_images.py`（`NotifyTest`）

**Interfaces（Produces）:**
- `read_notifications(bundle, cursor=None, since=None) -> dict`：`{"items": [...], "last": Optional[int], "lo": Optional[int], "hi": Optional[int]}`
  - `items`：`rec_id > cursor`（给了 `since` 就改为送达时间 `> since`）的通知，按 `rec_id` 从小到大；两者都没给时为空。每项是 `parse_notification` 的结果加 `rec`。
  - `last`：这次取到的行里最大的 `rec_id`（含解析失败的行），没取到为 `None`。
  - `lo`、`hi`：这个 bundle ID 现存最小、最大的 `rec_id`（**在取行之前查**）。
  - 数据库都打不开时抛 `OSError`。
- `notice_message(n: dict) -> dict`：`{"time": "HH:MM", "rec", "text", "sender"?, "needs_read"?}`
- 常量 `MENTION`、`GROUP_TEXT`、`TRUNCATE_AT = 196`

- [ ] **Step 1: 改写 `NotifyTest`。** 用下面的内容替换 `NotifyTest` 里的 `test_reads_after_since_for_this_app_only`、`test_no_database`、`test_corrupt_database`、`test_bad_record_is_skipped`（`setUp`、`test_bad_usda_keeps_title`、`test_parse_badge` 不动），并加 `notice_message` 的用例：

```python
    def test_reads_after_cursor_for_this_app_only(self):
        add_notif(self.db, 1000, body="旧的")                                  # rec 1
        add_notif(self.db, 2000, title="张三", body="在吗", chatname="wxid_zs")   # rec 2
        add_notif(self.db, 2001, title="别的 App", app_id=2)                    # rec 3
        add_notif(self.db, 3000, title=None, body="你收到了一条消息", chatname="custom_id7")   # rec 4
        got = server.read_notifications("com.test.WeChat", cursor=1)          # bundle ID 大小写不同也要对上
        self.assertEqual([(n["rec"], n["chat"], n["id"], n["preview"]) for n in got["items"]],
                         [(2, "张三", "wxid_zs", "在吗"), (4, "", "custom_id7", "你收到了一条消息")])
        self.assertEqual((got["last"], got["lo"], got["hi"]), (4, 1, 4))
        self.assertEqual(got["items"][0]["time"], time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(2000)))

    def test_since_and_stats_only(self):
        add_notif(self.db, 1000)
        add_notif(self.db, 2000)
        got = server.read_notifications("com.test.wechat", since=1500)
        self.assertEqual(([n["rec"] for n in got["items"]], got["last"]), ([2], 2))
        got = server.read_notifications("com.test.wechat")
        self.assertEqual((got["items"], got["last"], got["lo"], got["hi"]), ([], None, 1, 2))

    def test_empty_app(self):
        got = server.read_notifications("com.test.wechat", cursor=0)
        self.assertEqual((got["items"], got["last"], got["lo"], got["hi"]), ([], None, None, None))

    def test_no_database(self):
        server.NOTIFY_DBS = [os.path.join(self.tmp, "missing")]
        with self.assertRaises(OSError):
            server.read_notifications("com.test.wechat", cursor=0)

    def test_corrupt_database(self):
        bad = os.path.join(self.tmp, "bad")
        with open(bad, "wb") as f:
            f.write(b"not a database" * 100)
        server.NOTIFY_DBS = [bad]
        with self.assertRaises(OSError):
            server.read_notifications("com.test.wechat", cursor=0)

    def test_bad_record_is_skipped_but_counted(self):
        add_notif(self.db, 2000, body="好的")
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO record (app_id, data, delivered_date) VALUES (1, ?, ?)", (b"garbage", 2001 - 978307200))
        con.execute("INSERT INTO record (app_id, data, delivered_date) VALUES (1, NULL, ?)", (2002 - 978307200,))
        con.commit()
        con.close()
        got = server.read_notifications("com.test.wechat", cursor=0)
        self.assertEqual(([n["preview"] for n in got["items"]], got["last"]), (["好的"], 3))   # 坏记录也算进 last

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
```

- [ ] **Step 2: 跑测试，确认失败。** `/usr/bin/python3 -m unittest tests.test_images.NotifyTest -v`，预期 `TypeError`（`cursor` 参数不存在）和 `AttributeError`（`notice_message`）。

- [ ] **Step 3: 实现。** 在 `RECALL = ...` 下面加：

```python
MENTION = re.compile(r"^(.+?)在群聊中@了(你|所有人)$", re.S)   # 群里被 @ 时通知只有这句，看不到内容
GROUP_TEXT = re.compile(r"^([^:：\n]{1,40}): (.*)$", re.S)     # 群聊通知正文：「发送人: 内容」
TRUNCATE_AT = 196   # 通知正文最多 200 字节，到这个长度就可能被截断了
```

把 `read_notifications` 整个换成：

```python
def read_notifications(bundle, cursor=None, since=None) -> dict:
    """这个微信 rec_id > cursor（给了 since 就按送达时间 > since）的通知，另带取到的最大 rec_id 和现存的最小、最大 rec_id。"""
    errors = []
    for path in NOTIFY_DBS:
        if not os.path.exists(path):
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                where = " FROM record r JOIN app a ON a.app_id = r.app_id WHERE a.identifier = ?"
                lo, hi = con.execute("SELECT MIN(r.rec_id), MAX(r.rec_id)" + where, (bundle.lower(),)).fetchone()
                pick = "SELECT r.rec_id, r.data, r.delivered_date" + where
                if since is not None:
                    rows = con.execute(pick + " AND r.delivered_date > ? ORDER BY r.rec_id",
                                       (bundle.lower(), since - CD_EPOCH)).fetchall()
                elif cursor is not None:
                    rows = con.execute(pick + " AND r.rec_id > ? ORDER BY r.rec_id", (bundle.lower(), cursor)).fetchall()
                else:
                    rows = []
            finally:
                con.close()
        except sqlite3.Error as e:
            errors.append(f"{path}：{e}")
            continue
        items = []
        for rec, data, t in rows:
            try:
                items.append(dict(parse_notification(data, t + CD_EPOCH), rec=rec))
            except Exception:   # 一条坏记录不能拖垮整批
                continue
        return {"items": items, "last": rows[-1][0] if rows else None, "lo": lo, "hi": hi}
    raise OSError("打不开通知数据库：" + ("；".join(errors) or "没找到"))


def notice_message(n) -> dict:
    """一条通知 → 一条待处理消息：群聊拆出发送人，通知里看不全的（图片、可能被截断、被 @）标 needs_read。"""
    text, item, needs = n["preview"], {"time": n["time"][11:16], "rec": n["rec"]}, []
    if n["id"].endswith("@chatroom"):
        m = MENTION.match(text)
        if m:
            item["sender"] = m.group(1)
            needs.append("mention")
        else:
            m = GROUP_TEXT.match(text)
            if m:
                item["sender"], text = m.group(1), m.group(2)
    if text.strip() == "[图片]":
        needs.append("image")
    if len(n["preview"].encode()) >= TRUNCATE_AT:
        needs.append("truncated")
    item["text"] = text
    if needs:
        item["needs_read"] = needs
    return item
```

`a_wechat_peek` 里原来的 `read_notifications(bundle, st["since"])[-50:]` 这时会坏掉：临时改成 `read_notifications(bundle, since=st["since"])["items"][-50:]`，Task 3 会整段重写。

- [ ] **Step 4: 跑全部测试。** `NotifyTest` 全过；`PeekTest` 和 `MabTest.test_peek` 应该仍然通过（peek 的旧逻辑还在）。

- [ ] **Step 5: 提交。** `bridge 按 rec_id 游标读通知，一条通知能变成一条待处理消息`

---

### Task 2: `ingest` 收消息，`cache_lock`，`wechat-pending` 先收再返回

**Files:** Modify `mac_agent_server.py`（`peek_lock` → `cache_lock`；`clear_pending` 后面加 `add_notices`；`pending_view`；`a_wechat_send`、`a_wechat_ack`、`a_wechat_unread`、`a_wechat_pending`；删 `mark_unread_done`；`save_peek` 后面加 `ingest`）；Test `tests/test_images.py`（新增 `IngestTest`；删掉 `PeekTest` 里依赖 `mark_unread_done` 的 4 个用例）

**Interfaces:**
- Consumes：Task 1 的 `read_notifications(bundle, cursor=None, since=None) -> dict`、`notice_message(n) -> dict`
- Produces：
  - `cache_lock: threading.Lock`
  - `add_notices(data, notices) -> set`（新消息 id）
  - `ingest(account) -> dict`：`{"new": set, "note"?: str, "notify_error"?: str}`；账号找不到微信时抛 `ValueError`
  - peek 状态文件：`{"cursor": int, "reminded": float}`
  - `pending_view` 返回的每个聊天带上缓存里除 `messages` 外的字段（`group`、`chat_id`、`name_unknown`）

- [ ] **Step 1: 写失败测试。** 在 `PeekTest` 后面加：

```python
class IngestTest(ServerCase):
    def pending(self):
        status, body = self.jcall("POST", "/wechat/pending", {})
        self.assertEqual(status, 200, body)
        return body

    def test_first_call_skips_history(self):
        add_notif(self.db, time.time() - 60)
        body = self.pending()
        self.assertEqual(body["count"], 0)
        self.assertEqual(server.load_peek(None), {"cursor": 1, "reminded": 0})

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
        self.assertEqual(server.load_peek(None), {"cursor": 2, "reminded": 5})

    def test_cursor_follows_rows_not_stats(self):
        server.save_peek(None, {"cursor": 0, "reminded": 0})
        item = {"rec": 5, "chat": "张三", "id": "wxid_zs", "preview": "在吗", "time": "2026-09-29T14:25:30"}
        orig = server.read_notifications
        server.read_notifications = lambda bundle, cursor=None, since=None: {"items": [item], "last": 5, "lo": 1, "hi": 9}
        self.addCleanup(setattr, server, "read_notifications", orig)
        self.pending()
        self.assertEqual(server.load_peek(None)["cursor"], 5)

    def test_gap_note(self):
        for _ in range(10):
            add_notif(self.db, time.time())
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM record WHERE rec_id <= 8")   # 100 条上限把没读到的挤掉了
        con.commit()
        con.close()
        server.save_peek(None, {"cursor": 5, "reminded": 0})
        self.assertIn("可能漏了", self.pending()["note"])

    def test_notify_error_keeps_cache_and_cursor(self):
        server.save_peek(None, {"cursor": 3, "reminded": 0})
        server.NOTIFY_DBS = [os.path.join(self.tmp, "nope")]
        body = self.pending()
        self.assertIn("打不开通知数据库", body["notify_error"])
        self.assertEqual(server.load_peek(None)["cursor"], 3)

    def test_save_failure_keeps_cursor(self):
        self.pending()
        add_notif(self.db, time.time())
        def broken(account, data):
            raise OSError("磁盘满了")
        self.addCleanup(setattr, server, "save_pending", server.save_pending)
        server.save_pending = broken
        self.assertEqual(self.jcall("POST", "/wechat/pending", {})[0], 400)
        self.assertEqual(server.load_peek(None)["cursor"], 0)   # 下次还会重收这一条

    def test_ingest_and_ack_do_not_overwrite_each_other(self):
        self.pending()
        server.save_pending(None, {"next_id": 2, "chats": {"李四": {"group": False, "messages": [
            {"id": 1, "text": "x", "added": "2026-09-29T00:00:00"}]}}})
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
        self.jcall("GET", "/wechat/peek")   # peek 不拿全局锁，ack（POST）只会被 cache_lock 挡住
        for _ in range(50):
            if acked:
                break
            time.sleep(0.1)
        self.assertEqual(sorted(server.load_pending(None)["chats"]), ["张三"])

    def test_unread_does_not_touch_peek_state(self):
        self.wx_out = json.dumps({"chats": []})
        self.jcall("POST", "/wechat/unread", {})
        self.assertIsNone(server.load_peek(None))
```

在 `PeekTest` 里删掉 `test_new_until_unread_succeeds`、`test_notification_during_unread_still_new`、`test_unread_ok_even_if_peek_state_fails`、`test_partial_unread_rewakes_later`、`test_badge_against_base`（它们测的是 `mark_unread_done` 更新 peek 状态，这次删掉）。

- [ ] **Step 2: 跑测试，确认失败。** `/usr/bin/python3 -m unittest tests.test_images.IngestTest -v`：预期多数因为 `pending` 里没有新消息、peek 状态格式不对而失败；`test_ingest_and_ack...` 因为 `add_notices` 不存在而 `AttributeError`。

- [ ] **Step 3: 锁。** 把

```python
peek_lock = threading.Lock()   # peek 不拿全局锁，只用它保护 peek 状态文件的「读→改→写」
```

换成

```python
cache_lock = threading.Lock()   # peek 不拿全局锁；待处理缓存和 peek 状态的「读→改→写」都在这把锁里做
```

- [ ] **Step 4: `add_notices` 和 `pending_view`。** 在 `clear_pending` 后面加：

```python
def add_notices(data, notices) -> set:
    """把通知收进待处理：按对方内部 ID 归到聊天，备注改名时整个聊天跟着改名；返回新消息 id。"""
    now, new = time.strftime("%Y-%m-%dT%H:%M:%S"), set()
    for n in notices:
        name = n["chat"] or n["id"]
        old = next((k for k, c in data["chats"].items() if n["id"] and k != name and c.get("chat_id") == n["id"]), None)
        if old is not None and n["chat"]:
            moved = data["chats"].pop(old)
            moved.pop("name_unknown", None)
            if name in data["chats"]:
                moved["messages"] = sorted(moved["messages"] + data["chats"][name]["messages"], key=lambda m: m["id"])
            data["chats"][name] = moved
        entry = data["chats"].setdefault(name, {"group": False, "messages": []})
        entry.update(group=n["id"].endswith("@chatroom"), chat_id=n["id"])
        if not n["chat"]:
            entry["name_unknown"] = True
        item = dict(notice_message(n), id=data["next_id"], added=now)
        data["next_id"] += 1
        entry["messages"].append(item)
        new.add(item["id"])
    return new
```

`pending_view` 换成（带上聊天的其他字段）：

```python
def pending_view(data, new_ids=()) -> dict:
    """缓存里所有还没处理的消息，这次新进来的标 new。"""
    return {name: dict({k: v for k, v in c.items() if k != "messages"},
                       messages=[dict(m, new=True) if m["id"] in new_ids else m for m in c["messages"]])
            for name, c in data["chats"].items() if c["messages"]}
```

- [ ] **Step 5: `ingest`。** 在 `save_peek` 后面加：

```python
def ingest(account) -> dict:
    """把这个账号上次之后的新通知收进待处理，不碰微信；返回新消息 id，以及 note、notify_error。"""
    bundle, out = wechat_bundle(account), {"new": set()}
    with cache_lock:
        st = load_peek(account) or {}
        cursor = st.get("cursor")
        try:
            if cursor is not None:
                got = read_notifications(bundle, cursor=cursor)
                if cursor and got["lo"] is not None and got["lo"] > cursor:
                    out["note"] = "通知太多，可能漏了一部分（每个微信只保留最近 100 条通知）"
                cursor = got["last"] or cursor
            elif "since" in st:   # 从旧版本升级：按时间补收一次，之后改用 cursor
                got = read_notifications(bundle, since=st["since"])
                cursor = max(got["hi"] or 0, got["last"] or 0)
            else:                 # 第一次：历史通知不收
                got = read_notifications(bundle)
                cursor = got["hi"] or 0
        except OSError as e:
            out["notify_error"] = str(e)
            return out
        if got["items"]:
            data = load_pending(account)
            out["new"] = add_notices(data, got["items"])
            save_pending(account, data)
        save_peek(account, {"cursor": cursor, "reminded": st.get("reminded", 0)})
    return out
```

- [ ] **Step 6: `wechat-pending` 先收再返回。** `a_wechat_pending` 换成：

```python
def a_wechat_pending(p):
    """先把新通知收进待处理（不碰微信），再返回全部待处理；新收进来的标 new。"""
    try:
        got = ingest(p.get("account"))
    except ValueError as e:   # 找不到这个账号的微信：照样返回现有待处理
        got = {"new": set(), "notify_error": str(e)}
    with cache_lock:
        view = pending_view(load_pending(p.get("account")), got["new"])
    result = {"ok": True, "pending": view, "count": sum(len(c["messages"]) for c in view.values())}
    result.update({k: got[k] for k in ("note", "notify_error") if k in got})
    return result
```

- [ ] **Step 7: 其他改缓存的地方都进 `cache_lock`，删 `mark_unread_done`。**
  - `a_wechat_send`：把 `try:` 里的三行（`data = load_pending(...)`、`n = clear_pending(...)`、`save_pending(...)`）包进 `with cache_lock:`。
  - `a_wechat_ack`：`chat = text_arg(p, "chat")` 之后，把读、判断、清、写都包进 `with cache_lock:`：

```python
def a_wechat_ack(p):
    chat = text_arg(p, "chat")
    with cache_lock:
        data = load_pending(p.get("account"))
        if chat not in data["chats"]:
            raise ValueError(f"待处理里没有「{chat}」")
        upto = p.get("upto_id")
        n = clear_pending(data, chat, None if upto is None else int(upto))
        save_pending(p.get("account"), data)
    return {"ok": True, "acked": n}
```

  - `a_wechat_unread`：删掉 `started = time.time()` 那行和 `partial = ...`、`mark_unread_done(...)` 三行及其注释；把 `data, new = load_pending(...)` 到 `result["pending"] = ...` 这一段包进 `with cache_lock:`：

```python
    with cache_lock:
        data, new = load_pending(p.get("account")), set()
        if result["ok"] and not p.get("list_only"):
            new = add_pending(data, result.get("chats", []))
            try:
                save_pending(p.get("account"), data)
            except OSError as e:
                raise ValueError(f"待处理缓存写不进去（{e}），原始输出：{out}")
        result["pending"] = pending_view(data, new)
    return result
```

  - 删掉整个 `mark_unread_done` 函数。`a_wechat_peek` 里的 `with peek_lock:` 临时改成 `with cache_lock:`（Task 3 重写）。
  - `grep -n "peek_lock\|mark_unread_done" mac_agent_server.py tests/test_images.py` 应该没有结果。

- [ ] **Step 8: 跑全部测试，确认通过。**

- [ ] **Step 9: 提交。** `bridge 从通知收新消息进待处理，wechat-pending 先收再返回；改缓存的地方共用一把锁`

---

### Task 3: peek 只按「有新消息」和「超时」唤醒

**Files:** Modify `mac_agent_server.py`（`a_wechat_peek`；文件头部接口说明）；Test `tests/test_images.py`（`PeekTest`、`MabTest.test_peek`）

**Interfaces:** Consumes Task 2 的 `ingest(account) -> dict`、`cache_lock`、peek 状态 `{"cursor", "reminded"}`。

- [ ] **Step 1: 改 `PeekTest`。** 删掉 `test_first_peek_ignores_history`、`test_unread_ok_even_if_bundle_lookup_crashes`，加下面的用例；`test_account_state_is_separate` 里的 `self.assertEqual(seen, ["work"])` 改成 `self.assertEqual(set(seen), {"work"})`（新 peek 收消息和读角标各查一次 bundle ID）；`test_stale_reminds_once_per_interval`、`test_stale_minutes_env`、`test_account_state_is_separate`、`test_bad_account_is_400`、`test_does_not_wait_for_global_lock`、`test_logged_only_when_waking` 保留；`test_notify_db_missing`、`test_badge_none_when_not_running` 换成下面的版本；`state()`、`unread()` 两个辅助方法删掉。

```python
    def test_first_peek_ignores_history(self):
        add_notif(self.db, time.time() - 60)
        self.badge = 3
        body = self.peek()
        self.assertEqual((body["wake"], body["reasons"], body["new"], body["pending"], body["badge"]),
                         (False, [], [], 0, 3))
        self.assertNotIn("badge_base", body)
        self.assertNotIn("incomplete", body)

    def test_new_message_wakes_once(self):
        self.peek()
        add_notif(self.db, time.time(), title="张三", body="[图片] ", chatname="wxid_zs")
        body = self.peek()
        self.assertEqual((body["reasons"], body["pending"]), (["new"], 1))
        self.assertEqual(body["new"], [{"chat": "张三", "id": 1, "text": "[图片] ", "needs_read": ["image"]}])
        body = self.peek()
        self.assertEqual((body["wake"], body["new"], body["pending"]), (False, [], 1))   # 已经收过了

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
```

`test_stale_reminds_once_per_interval` 里原来的 `st["reminded"] -= 31 * 60` 照旧可用（状态仍有 `reminded`）。

`MabTest.test_peek` 换成：

```python
    def test_peek(self):
        server.save_peek("work", {"cursor": 0, "reminded": 0})
        add_notif(self.db, time.time())
        r = self.mab("wechat-peek", "-a", "work")
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertEqual((body["wake"], body["new"][0]["chat"]), (True, "张三"))
```

- [ ] **Step 2: 跑测试，确认失败。** `/usr/bin/python3 -m unittest tests.test_images.PeekTest tests.test_images.MabTest.test_peek -v`：预期 `test_new_message_wakes_once`（`new` 的格式不同、没有 `pending` 增加）、`test_first_peek_ignores_history`（还有 `badge_base`）等失败。

- [ ] **Step 3: 实现。** `a_wechat_peek` 换成：

```python
def a_wechat_peek(account):
    """不碰微信：先把新通知收进待处理，再看要不要唤醒 worker（有新消息，或待处理超时没人管）。"""
    minutes, now = stale_minutes(), time.time()
    got = ingest(account)
    result = {"ok": True, "badge": read_badge(wechat_bundle(account))}
    result.update({k: got[k] for k in ("note", "notify_error") if k in got})
    reasons = ["new"] if got["new"] else []
    with cache_lock:
        data = load_pending(account)
        result["stale"] = stale_chats(data, time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - minutes * 60)))
        st = load_peek(account) or {"reminded": 0}
        if result["stale"] and now - st.get("reminded", 0) >= minutes * 60:
            reasons.append("stale")
            st["reminded"] = now
            save_peek(account, st)
    result["new"] = [dict({k: m[k] for k in ("id", "text", "sender", "needs_read") if k in m}, chat=name)
                     for name, c in data["chats"].items() for m in c["messages"] if m["id"] in got["new"]][-50:]
    result.update(pending=sum(len(c["messages"]) for c in data["chats"].values()), reasons=reasons, wake=bool(reasons))
    return result
```

注意 `test_new_message_wakes_once` 断言 `new` 的每项正好是 `{"chat", "id", "text", "needs_read"}`（有 `sender` 时也带上）。

文件头部接口说明里 `POST /wechat/pending` 和 `GET /wechat/peek` 两行改成：

```
  POST /wechat/pending {"account":"work"}                     先从系统通知收新消息，再返回待处理（还没回复或 ack 的），不碰微信
  GET  /wechat/peek?account=work                              收新消息进待处理，有新消息或待处理超时就 wake，不碰微信、不排队
```

- [ ] **Step 4: 跑全部测试，确认通过。**

- [ ] **Step 5: 提交。** `peek 只按新消息和超时唤醒，角标只作参考`

---

### Task 4: 文档

**Files:** Modify `README.md`、`docs/muse-prompt.md`

- [ ] **README「自动回复：用 peek 判断要不要唤醒」整节重写**，要点：
  - 新消息从 macOS 的通知记录里读，peek 和 `wechat-pending` 都会先收一遍，不切微信、不滚会话列表、不点开聊天；
  - 待处理消息的字段：`sender`（群聊）、`needs_read`（`image` / `truncated` / `mention`）、聊天的 `chat_id`、`name_unknown`；
  - 通知正文最多 200 字节，长文字会被截掉；群里被 @ 时看不到内容；图片只显示 `[图片]`——这三种要用 `wechat-read --images` 打开读；
  - peek 返回示例（按 spec 第 2 节），唤醒理由只有 `new`、`stale`；
  - 前提：微信设置里打开「通知显示消息详情」；每个微信只保留最近 100 条通知；收不到的情况（通知被清掉、免打扰群没被 @、没开消息详情）不会兜底；
  - 手动跑 `wechat-unread` 会把同样的消息再收一遍，自动回复时不要用它。
- [ ] **README HTTP API 表**：`/wechat/pending`、`/wechat/peek` 两行按新行为改说明。
- [ ] **muse-prompt「自动回复」小节**换成 spec「Muse 的新流程」的 3 条，`<账号>` 写成 `<账号别名>`。
- [ ] 用本地不提交的敏感词名单（`grep -nEf <名单文件>`）检查 `README.md`、`docs/muse-prompt.md`，没有命中。
- [ ] **提交。** `README 和 Muse 说明改成从通知收消息`

---

### Task 5: 真机

在临时 bridge（8799 端口，`BRIDGE_LOG` 指到 scratchpad）上做，只在 zhiwuzhu 上收；从生产号发消息用 `./wechat/wx-send.sh -a <生产号别名> "<zhiwuzhu 在生产号里的聊天名>" "…"`。

- [ ] **Step 1: 前台时来消息有没有通知（需要用户操作）。** 让用户把 zhiwuzhu 的微信切到前台、停在文件传输助手，用手机给 zhiwuzhu 发一条。看通知数据库里 zhiwuzhu 的 bundle ID 下有没有新记录。**没有的话停下来，把结果告诉用户再决定。**
- [ ] **Step 2: 第一次 peek。** `wake: false`，历史不收。
- [ ] **Step 3: 文字、图片、长文字各发一条。** 发之前把访达切到前台；peek：`wake: true`、`reasons: ["new"]`，`new` 里 3 条，图片带 `needs_read: ["image"]`、长文字带 `["truncated"]`；peek 前后前台 App 不变，zhiwuzhu 微信的会话列表没滚（`--dump` 看 AX 标题还是文件传输助手）。
- [ ] **Step 4: 再 peek 一次。** `wake: false`，`pending: 3`。
- [ ] **Step 5: 读图。** `mab wechat-read "<聊天名>" -n 5 --images -a zhiwuzhu`：图片那条有 `local_path`。
- [ ] **Step 6: 回复后清账。** `mab wechat-send "<聊天名>" "收到" -a zhiwuzhu`：`acked: 3`，`wechat-pending` 为空。
- [ ] **Step 7: 收尾。** 停掉临时 bridge；删掉测试生成的 peek 状态（`~/.cache/wx-send/peek/zhiwuzhu.json`）；跑一遍全部测试；把结果写进 spec 末尾的「真机结果」（不写真实聊天名），提交：`通知收消息设计文档补充真机结果`

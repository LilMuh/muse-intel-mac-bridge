# 微信待处理消息缓存 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `wechat-unread` 读到的新消息先存进 Mac 本地的待处理缓存，回复成功或 `wechat-ack` 之后才清掉；读取进度改为成功输出结果之后才统一保存。

**Architecture:** 缓存由 Python 服务端负责，放在 `~/.cache/wx-send/pending/<账号>.json`，写文件时原子替换，读写都在服务端的全局锁里进行。Swift 只改一处：`readNew` 不再立刻保存读取进度，改为 `printUnreadDetails` 成功输出后统一保存。inbox 的清理从 bash 挪到服务端，清理时跳过缓存里还在引用的图片。

**Tech Stack:** Python 3.9+ 标准库；Swift 5.10。

**Spec:** `docs/superpowers/specs/2026-09-28-wechat-pending-cache-design.md`

## Global Constraints

- 分支：`feat/wechat-read-image`。
- Python 代码要兼容 3.9；`mab.py` 只能用标准库。
- 没有指定账号时，缓存的账号名用 `default`。账号名不能包含 `/`，也不能是 `.` 或 `..`。
- 只有发送真正成功（`code == 0`）而且不是 `dry_run` 时才清缓存，只清发送前已有的 id。
- 时间行不单独进缓存，写进它后面每条消息的 `time` 字段。
- 读图后清理 inbox 用 `WX_INBOX_DAYS`，默认 3 天，必须是非负整数，否则报错。
- 提交信息用中文，末尾加上 `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` 和 `Claude-Session: https://claude.ai/code/session_01FVD1Ziho3oPbQo4ghQ1zyB`。
- 真机测试只在 zhiwuzhu ↔ work 之间进行。限制只读 1 个聊天时，直接给 wx-send.sh 设 `WX_UNREAD_MAX_CHATS=1`（它是 wx-send 自己的环境变量），或者给 mab 传 `--max-chats 1`。**不要**把环境变量设在 mab 上。

## Review Focus

1. **wx-send 失败**（退出码 6 等）时，缓存不能被改动，返回里仍然带上当前的 `pending`。
2. **回复失败或试运行**：`unconfirmed`、失败、`dry_run` 都不能清缓存。
3. **缓存文件写一半程序退出**：写临时文件再 `os.replace`，旧文件不会被写坏。
4. **中途被打断的读取**：读取进度不能推进。Task 3 用真机验证：读到一半把微信切走，确认读取进度文件没有更新。
5. **inbox 清理**：缓存里还在引用的图片，就算超过天数也不能删。

---

### Task 1: 服务端的缓存函数和接口

**Files:** Modify `mac_agent_server.py`；Test `tests/test_images.py`

**Interfaces（Produces）:**
- `PENDING_DIR: str`
- `pending_path(account) -> str`
- `load_pending(account) -> dict`
- `save_pending(account, data) -> None`
- `add_pending(data, chats) -> set`：新消息的 id 集合
- `pending_view(data, new_ids=()) -> dict`
- `clear_pending(data, chat, upto=None) -> int`
- `prune_inbox(days) -> None`
- 接口：`POST /wechat/pending`、`POST /wechat/ack`；`/wechat/unread` 返回里加 `pending`；`/wechat/send` 返回里加 `acked`
- 测试辅助：`ServerCase` 增加 `self.wx_code`（假 `run_wx` 的退出码），`setUp` 把 `server.PENDING_DIR` 指到临时目录

- [ ] **Step 1: 写失败测试。**

`ServerCase` 的改动：
- 在 `setUp` 里加 `self.wx_code = 0`，并加上 `server.PENDING_DIR = os.path.join(self.tmp, "pending")`；
- `fake_run_wx` 改为返回 `self.wx_code, self.wx_out, ""`。

然后新增 `PendingTest(ServerCase)`，覆盖以下用例：

```python
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
        server.INBOX = os.path.join(self.tmp, "inbox"); os.makedirs(server.INBOX)
        for n in ("in-1.png", "in-2.png"):
            p = os.path.join(server.INBOX, n)
            with open(p, "wb") as f: f.write(png())
            os.utime(p, (1, 1))
        self.unread()   # 缓存里引用了 in-1.png
        server.prune_inbox(3)
        self.assertEqual(sorted(os.listdir(server.INBOX)), ["in-1.png"])
```

- [ ] **Step 2: 运行测试，确认失败。** 预期的失败原因：返回里没有 `pending`，`/wechat/pending` 返回 404，没有 `clear_pending` 等函数。

- [ ] **Step 3: 实现。**
  - 在 `INBOX` 下面加一行：`PENDING_DIR = os.path.expanduser("~/.cache/wx-send/pending")`
  - 在 `inbox_file` 后面加上下面这组函数：

```python
def pending_path(account) -> str:
    name = account or "default"
    if "/" in name or name in (".", ".."):
        raise ValueError(f"账号名不合规：{name!r}")
    return os.path.join(PENDING_DIR, name + ".json")


def load_pending(account) -> dict:
    try:
        with open(pending_path(account), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"next_id": 1, "chats": {}}


def save_pending(account, data):
    """先写临时文件再改名，写到一半退出也不会把旧缓存写坏。"""
    os.makedirs(PENDING_DIR, exist_ok=True)
    path = pending_path(account)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def add_pending(data, chats) -> set:
    """把 unread 读到的新消息编号放进缓存，时间行写进后面消息的 time；返回新 id。"""
    now, new = time.strftime("%Y-%m-%dT%H:%M:%S"), set()
    for c in chats:
        if not c.get("messages"):
            continue
        entry = data["chats"].setdefault(c["name"], {"group": bool(c.get("group")), "messages": []})
        entry["group"] = bool(c.get("group"))
        t = None
        for m in c["messages"]:
            if m.get("type") == "time":
                t = m.get("text")
                continue
            item = {k: v for k, v in m.items() if k != "type"}
            item.update(id=data["next_id"], added=now)
            if t:
                item["time"] = t
            if c.get("note"):
                item["note"] = c["note"]
            data["next_id"] += 1
            entry["messages"].append(item)
            new.add(item["id"])
    return new


def pending_view(data, new_ids=()) -> dict:
    """缓存里所有还没处理的消息，这次新进来的标 new。"""
    return {name: {"group": c["group"], "messages": [dict(m, new=True) if m["id"] in new_ids else m for m in c["messages"]]}
            for name, c in data["chats"].items() if c["messages"]}


def clear_pending(data, chat, upto=None) -> int:
    """清掉这个聊天 id ≤ upto 的待处理消息（upto 为空就全清），返回清掉几条。"""
    c = data["chats"].get(chat)
    if not c:
        return 0
    keep = [m for m in c["messages"] if upto is not None and m["id"] > upto]
    n = len(c["messages"]) - len(keep)
    if keep:
        c["messages"] = keep
    else:
        del data["chats"][chat]
    return n


def prune_inbox(days):
    """删掉 inbox 里超过 days 天、而且待处理缓存里没有引用的图片。"""
    keep = set()
    for f in os.listdir(PENDING_DIR) if os.path.isdir(PENDING_DIR) else []:
        if f.endswith(".json"):
            with open(os.path.join(PENDING_DIR, f), encoding="utf-8") as fp:
                keep |= {m["image"] for c in json.load(fp)["chats"].values() for m in c["messages"] if m.get("image")}
    cutoff = time.time() - days * 86400
    for f in os.listdir(INBOX) if os.path.isdir(INBOX) else []:
        p = os.path.join(INBOX, f)
        if f.startswith("in-") and f not in keep and os.path.getmtime(p) < cutoff:
            os.remove(p)


def inbox_days() -> int:
    v = os.environ.get("WX_INBOX_DAYS", "3")
    if not v.isdigit():
        raise ValueError(f"WX_INBOX_DAYS 必须是非负整数：{v!r}")
    return int(v)
```

  - `a_wechat_read`：在 `run_wx` 之前加上 `if p.get("images"): prune_inbox(inbox_days())`。
  - `a_wechat_unread`：最后一行改成下面这样，`images` 时同样先调用 `prune_inbox`：

```python
    if p.get("images"):
        prune_inbox(inbox_days())
    code, out, err = run_wx(args, p.get("account"), env, timeout=1800)
    result = wx_json(code, out, err)
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

  - `a_wechat_send`：在 `run_wx` 前面、后面各加一段：

```python
    data = load_pending(p.get("account"))
    ids = [m["id"] for m in data["chats"].get(to, {}).get("messages", [])]
    ...（原来的 run_wx）
    acked = 0
    if code == 0 and not p.get("dry_run") and ids:
        data = load_pending(p.get("account"))
        acked = clear_pending(data, to, max(ids))
        save_pending(p.get("account"), data)
```

    返回的字典里加上 `"acked": acked`。
  - 新增两个接口：

```python
def a_wechat_pending(p):
    view = pending_view(load_pending(p.get("account")))
    return {"ok": True, "pending": view, "count": sum(len(c["messages"]) for c in view.values())}


def a_wechat_ack(p):
    chat, data = text_arg(p, "chat"), load_pending(p.get("account"))
    if chat not in data["chats"]:
        raise ValueError(f"待处理里没有「{chat}」")
    upto = p.get("upto_id")
    n = clear_pending(data, chat, None if upto is None else int(upto))
    save_pending(p.get("account"), data)
    return {"ok": True, "acked": n}
```

    在 `ACTIONS` 里注册 `"wechat/pending": a_wechat_pending, "wechat/ack": a_wechat_ack`。
  - 在服务端顶部的说明里补上这两个接口。

- [ ] **Step 4: 运行全部测试，确认通过。**

- [ ] **Step 5: 提交。** 提交信息：`bridge 新增待处理消息缓存：unread 入缓存，回复或 ack 后清除`

---

### Task 2: mab.py

**Files:** Modify `mab.py`；Test `tests/test_images.py`（放在 `MabTest` 里）

- [ ] **Step 1: 写失败测试。**
  - `test_pending_and_ack_commands`：用 `mab wechat-unread` 让一条消息进缓存；`mab wechat-pending` 的输出里 `count == 1`；`mab wechat-ack 张三` 的输出里 `acked == 1`。
  - `test_unread_images_downloads_pending_once`：`wx_out` 里 `chats` 和 `pending` 引用的是同一张 `in-1.png`（第二次调用 unread 时，旧的那条消息只出现在 `pending` 里）。断言 `pending` 里那条消息有 `local_path`，而且 save-dir 里只有一个文件。

- [ ] **Step 2: 运行测试，确认失败。**

- [ ] **Step 3: 实现。**
  - `fetch_images` 收集要下载的消息时，把 `result.get("pending", {})` 里每个聊天的 `messages` 也加进来。
  - 用 `done = {}` 记下已经处理过的文件名：同一个文件名只下载一次，后面遇到时直接复用它的 `local_path` 或 `download_error`。
  - 新增两个命令：

```python
    wpd = sub.add_parser("wechat-pending"); wpd.add_argument("-a", "--account")
    wak = sub.add_parser("wechat-ack"); wak.add_argument("chat"); wak.add_argument("--upto", type=int); wak.add_argument("-a", "--account")
    ...
    elif a.cmd == "wechat-pending":
        post("/wechat/pending", {"account": a.account})
    elif a.cmd == "wechat-ack":
        p = {"chat": a.chat, "account": a.account}
        if a.upto is not None:
            p["upto_id"] = a.upto
        post("/wechat/ack", p)
```

  - 在顶部用法说明里补上这两个命令。

- [ ] **Step 4: 运行测试，确认通过。**

- [ ] **Step 5: 提交。** 提交信息：`mab.py 新增 wechat-pending、wechat-ack，读图时一并下载待处理里的图片`

---

### Task 3: Swift 延后保存读取进度，删掉 bash 的 inbox 清理

**Files:** Modify `wechat/wx-send.sh`

- [ ] **Step 1: 改代码。**
  - 在 `Session` 里加一个属性：`var cursorsToSave: [(String, [Msg])] = []   // 成功输出结果后才保存，中途退出时下次还能读到`
  - `readNew` 里的 `saveCursor(r.name, all)` 改成 `cursorsToSave.append((r.name, all))`。
  - `printUnreadDetails` 开头的 `imagesTaken = 0; imagesSkipped = 0` 那一行，同时加上 `cursorsToSave = []`；在最后的 `say(...)` 后面加上 `for (name, all) in cursorsToSave { saveCursor(name, all) }`。
  - 删掉 bash 里 `if [[ "${WX_IMAGES:-}" == "1" ...` 那三行清理代码，只保留 `export WX_INBOX=...`，并把注释改成「读图：图片存到仓库的 inbox/（由 bridge 负责清理）」。

- [ ] **Step 2: 静态检查。** 跑类型检查、`tests/run_swift_tests.sh` 和 Python 测试，全部通过。

- [ ] **Step 3: 真机验证读取进度不会提前保存（Review Focus 第 4 条）。**
  1. work 连发 5 张图给「测试号A」，让 zhiwuzhu 有未读。
  2. 执行 `--forget 测试号`，并记下读取进度文件的状态：`ls -la ~/.cache/wx-send/cursor/zhiwuzhu/`。
  3. 在后台启动 `WX_UNREAD_MAX_CHATS=1 WX_IMAGES=1 ./wechat/wx-send.sh -a zhiwuzhu --unread`，2 秒后用 `osascript -e 'tell application "Finder" to activate'` 把微信切走。
  4. 预期：退出码 6，而且读取进度目录里**没有**「测试号」对应的新文件。
  5. 再不切走地跑一次，这 5 张图要能读出来，读取进度也正常保存。

- [ ] **Step 4: 提交。** 提交信息：`读未读时成功输出后才保存读取进度；inbox 清理交给 bridge`

---

### Task 4: 文档

**Files:** Modify `README.md`、`docs/muse-prompt.md`

- [ ] **README：**
  - 在「读未读消息」一节里新增一小节「待处理缓存」，说明：
    - 新消息进缓存，一直留到被处理；
    - 回复成功或 `wechat-ack` 之后清掉；
    - 用 `wechat-pending` 随时查看，不碰微信；
    - 缓存文件在 `~/.cache/wx-send/pending/`；
    - 群里回复一次就算把这个群处理完了。
  - HTTP API 表格加上 `/wechat/pending` 和 `/wechat/ack` 两行，`/wechat/unread` 的说明里补上「返回 pending」。
  - 客户端命令列表加上 `wechat-pending`、`wechat-ack`。
- [ ] **muse-prompt：** 把「所有未读」这一条换成 spec 第 6 节的三步工作流程。
- [ ] **提交。** 提交信息：`README 和 Muse 说明补充待处理缓存`

---

### Task 5: 真机端到端

- [ ] **Step 1: 起一个临时 bridge。** 用 8799 端口在后台启动；收尾时只停这个端口上的进程。
- [ ] **Step 2: 准备一条未读。** zhiwuzhu 先切到文件传输助手，然后 work 发一条文字「缓存测试」。
- [ ] **Step 3: 读进缓存。** `mab wechat-unread --max-chats 1 -a zhiwuzhu`。预期：`pending` 里有「测试号」这条消息，并且标了 `new`。
- [ ] **Step 4: 再读一次。** 不回复，再执行一次 `wechat-unread --max-chats 1`（没有新消息）。预期：`pending` 里仍然有这条，但不再标 `new`。
- [ ] **Step 5: 回复后清除。** `mab wechat-send 测试号 "收到" -a zhiwuzhu`。预期：`acked == 1`，`wechat-pending` 的 `count == 0`。
- [ ] **Step 6: 用 ack 清除。** 让 work 再发一条，执行 unread，再用 `wechat-ack 测试号` 清掉。预期 `acked == 1`。
- [ ] **Step 7: 收尾。** 停掉临时 bridge，删掉测试时生成的缓存条目和 inbox 文件，跑一遍全部测试。

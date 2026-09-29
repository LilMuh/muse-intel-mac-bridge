# 用系统通知收微信消息 · 设计

日期：2026-09-29
分支：`feat/wechat-notify-pending`

## 目标

收新消息不再切微信、不再滚会话列表、不再逐个点开聊天。新消息直接从 macOS 的通知记录里读出来，写进待处理缓存；只有通知里看不全的消息（图片、被截断的长文字、群里被 @），Muse 才打开那个聊天去读。

## 为什么要改

现在的 `wechat-unread` 每次都要：切到微信 → 把会话列表滚到顶 → 一屏屏往下翻找未读（白名单里的免打扰群要全部找到才停）→ 逐个点开聊天读新消息 → 再标回未读。Muse 每次被唤醒，微信界面就被翻一遍。

## 实测依据（2026-09-29）

- 每个微信实例在通知数据库里各有一个 bundle ID（如 `com.tencent.xinwechat`、`com.tencent.xinwechat2`），按账号就能分开。
- 通知的 `titl` 是消息送达那一刻的聊天名；备注改名后，之后的通知跟着变（只见过一次，就是改备注那一刻）。
- `usda` 里的 `chatname` 是对方的内部 ID：私聊是 `wxid_…` 或老账号的原始微信号，群聊以 `@chatroom` 结尾。
- 聊天被读过、回过之后，通知记录还在；每个实例只保留最近 100 条。`record.rec_id` 只增不减。
- 消息被撤回后，它的通知记录被删掉。
- 正文：
  - 文字原样；**最多 200 字节（UTF-8），超出直接截掉，没有省略号**（502 字的中文只剩前 66 个字，300 个英文字母只剩 200 个）。
  - 图片是 `[图片] `（带一个空格）；还见过 `[动画表情]`、`[个人名片]`。
  - 群聊一般是 `发送人: 内容`；被 @ 时是 `XX在群聊中@了你` / `XX在群聊中@了所有人`，**没有消息内容**。免打扰的群被 @ 时也会发通知。
- 「通知显示消息详情」没开时，没有 `titl`，正文只有一句通用提示。

## 已确定的决定

- 新消息由 peek 写进待处理（`wechat-pending` 也会先收一遍），都不碰微信。
- 群聊也进待处理。
- 只有通知里看不全的消息标 `needs_read`；Muse 只对这些聊天用 `wechat-read --images` 打开读，纯文字的直接按待处理回复。
- 不做撤回检测，不做兜底轮。
- 旧的 `wechat-unread` 原样保留，不再出现在自动回复流程里。
- 角标不再作为唤醒理由，只在 peek 返回里给个数字参考。

## 设计

### 1. 收消息：`ingest(account)`

peek 和 `wechat-pending` 开头都调用它。

1. 按账号找到微信的 bundle ID（现有 `wechat_bundle`）。
2. 读 peek 状态里的 `cursor`（上次读到的 `rec_id`），查这个 bundle ID 下 `rec_id > cursor` 的通知，按 `rec_id` 从小到大。
3. 每条通知变成一条待处理消息：

```json
{"id": 57, "text": "明天几点？", "time": "14:25", "added": "2026-09-29T14:25:30", "rec": 36439,
 "sender": "李四", "needs_read": ["image"]}
```

- `time`：通知送达的本地时间（时:分），`added`：写进缓存的时间，`rec`：通知的 `rec_id`。
- 群聊（`chatname` 以 `@chatroom` 结尾）：
  - `XX在群聊中@了你` / `XX在群聊中@了所有人` → `sender` 是 XX，`text` 原样，`needs_read` 含 `mention`；
  - `发送人: 内容` → 拆成 `sender` 和 `text`；
  - 其他格式 `text` 原样。
- `needs_read`（没有就不带这个字段）：
  - `image`：`text` 去掉首尾空白后是 `[图片]`；
  - `truncated`：正文 UTF-8 长度 ≥ 196 字节（可能被截断）；
  - `mention`：见上。
4. 归到哪个聊天：待处理缓存仍然按聊天名存（回复、`ack` 都按名字），每个聊天多存一个 `chat_id`（`chatname`）。
   - 新通知的 `chat_id` 已经在另一个名字下（备注改名了）→ 把那个聊天整体改成新名字，再追加。
   - 没有 `titl`（没开消息详情）→ 用 `chat_id` 当聊天名，聊天上标 `"name_unknown": true`。
5. 写完缓存后把 `cursor` 改成这批最大的 `rec_id`。缓存和状态在同一把锁里读改写。
6. 可能漏了：`cursor` 不是初始值，而这个 bundle ID 现存最小的 `rec_id` 已经大于 `cursor`（100 条上限把没读到的挤掉了）→ 返回里加 `note`：「通知太多，可能漏了一部分」。
7. 通知数据库读不了 → 不改缓存和 `cursor`，返回里带 `notify_error`。

**第一次和从旧版本升级**：
- 没有 peek 状态：`cursor` 取这个 bundle ID 现在最大的 `rec_id`，历史通知不收。
- 有旧状态（有 `since`、没有 `cursor`）：这一次按 `delivered_date > since` 收，之后改用 `cursor`，升级前后的消息一条不漏。

### 2. peek

`GET /wechat/peek?account=`，照旧不拿全局锁。

```json
{"ok": true, "wake": true, "reasons": ["new", "stale"],
 "new": [{"chat": "张三", "id": 57, "text": "在吗", "needs_read": ["image"]}],
 "pending": 4, "stale": [{"chat": "李四", "count": 2, "oldest": "2026-09-29T13:40:00"}],
 "badge": 3, "note": "…", "notify_error": "…"}
```

- 唤醒理由只剩两个：`new`（这次收进了新消息）、`stale`（待处理超过 `WX_PEEK_STALE_MIN` 分钟没处理，每个间隔最多提醒一次，同现在）。
- 去掉 `badge_base`、`incomplete`；`badge` 只作参考。
- 不唤醒、没有 `notify_error` 时不记请求日志（同现在）。

### 3. `wechat-pending`

`POST /wechat/pending` 先 `ingest`，再返回整个待处理；这次新收进来的标 `"new": true`。收消息失败时照样返回现有待处理，带上 `notify_error`。

### 4. 锁

peek 不拿全局锁，而 `send`、`ack`、`unread` 也会改待处理缓存。新增 `cache_lock`（取代现在的 `peek_lock`），所有「读缓存 → 改 → 写回」都在它里面做：`ingest`、`send` 成功后清缓存、`ack`、`unread` 写缓存、peek 改状态。拿锁的时间只有读写文件那一下，不包括调用 wx-send。

### 5. 去掉的东西

- `mark_unread_done`：`unread` 不再更新 peek 状态（新消息的进度只看 `cursor`）。
- peek 状态里的 `since`（升级后）、`badge_base`、`incomplete`。

### 6. 不变的东西

- 回复成功自动清掉这个聊天发送前已有的待处理；`ack`；超时提醒。
- 旧的 `wechat-unread` 原样保留。手动跑它时，读到的消息会和通知收进来的重复，文档里写明。
- `wechat-read --images` 照旧用搜索打开聊天，不滚会话列表。

## Muse 的新流程

1. hook 每 3 分钟 `wechat-peek -a <账号>`，`wake` 为 true 才唤醒 worker；上一轮没跑完不唤醒。
2. worker：`wechat-pending -a <账号>` 拿待处理，按聊天处理：
   - 聊天里有 `needs_read` 的消息 → `wechat-read "<聊天名>" -n <够用的条数> --images -a <账号>`，看图片、完整长文字、被 @ 的内容；
   - 只有纯文字 → 直接按待处理的内容回复；
   - 群聊按群规则处理（不回的直接 `ack`）；
   - `name_unknown` 的聊天 → 不回，告诉用户「通知没开消息详情」。
3. 回复成功自动清账；不回复的 `ack`；需要用户定夺的留着，每 30 分钟提醒一次。

## 风险

- **微信在前台时来的消息有没有通知：未验证。** 如果没有，Muse 操作这个微信的那段时间里进来的消息会漏掉，而且没有兜底。上线前必须真机验证：微信切到前台、停在文件传输助手，用手机发一条消息，看通知数据库里有没有这条。
- 通知被手动清掉、免打扰群没被 @ 的消息、「通知显示消息详情」没开时的内容，都收不到或收不全；按决定不做兜底。
- 撤回：消息在写进待处理之后才撤回的，Muse 可能照样回复。

## 测试

- **Python 单元测试**（临时 sqlite 按真实表结构造通知，`rec_id` 递增）：
  - 私聊文字进待处理，`chat_id`、`time`、`rec` 正确；只收本账号 bundle ID 的
  - 图片、≥196 字节、`@了你`、`@了所有人` 各自的 `needs_read`；196 字节以下不标 `truncated`
  - 群聊拆 `sender`；其他格式原样
  - 备注改名：同一个 `chat_id` 换名字后整体改名
  - 没有 `titl`：用 `chat_id` 当名字，标 `name_unknown`
  - 第一次：历史不收；旧状态有 `since`：按时间补收一次后改用 `cursor`
  - 可能漏了的 `note`；通知数据库读不了时缓存和 `cursor` 不变、带 `notify_error`
  - peek：`new` 唤醒；同一批通知只收一次；`stale` 照旧；不再有 `badge_base`、`incomplete`
  - `wechat-pending` 先收再返回，新收的标 `new`
  - `cache_lock`：peek 收消息的同时 `send` 清缓存，两边的修改都在（用替身让 `run_wx` 在收消息期间执行）
  - 回复成功只清发送前已有的；`ack` 照旧
- **真机**（生产号发给 zhiwuzhu，只在 zhiwuzhu 上收）：
  - **前台时来消息有没有通知**（上面的风险）
  - 文字、图片、长文字各发一条 → peek 唤醒、待处理内容和 `needs_read` 正确，微信没被切到前台、会话列表没滚
  - `wechat-read --images` 能读到那张图
  - 回复后待处理清空

# 微信新消息探测（peek）· 设计

日期：2026-09-29
分支：`feat/wechat-read-image`（接在待处理缓存后面）

## 目标

Muse 不再每 20 分钟跑一轮完整的 `wechat-unread`，改成由它那边的 hook 每 1~2 分钟调一次轻量接口 `GET /wechat/peek`，返回 `wake: true` 时才唤醒 agent 走完整流程（`unread` → 按 SOP 回复 → `ack`）。

要求：

1. **轻**：不切微信到前台、不用 AX、不点开任何聊天，几十毫秒内返回；不拿全局锁，不被正在跑的 `unread`/`send` 卡住。
2. **知道是谁**：不碰微信也能看到新消息是哪个聊天发来的、大概说了什么。
3. **不会没人回**：Muse 被唤醒后没处理完（忘了、出错、卡在等人定夺），过一段时间还会再被唤醒。
4. Mac 上不加常驻的轮询任务，一切都在 peek 请求里当场算。
5. **不漏回复**：发完、读完不停在客户的聊天上，免得客户紧接着回的消息直接变成已读。

## 为什么不能直接返回 pending 条数

待处理缓存只在调用 `wechat-unread` 时才写入（`a_wechat_unread → add_pending → save_pending`），新消息来了缓存不会变，poll 它探测不到新消息。
读微信左侧「微信」标签的「N条新消息」也不行：AX 树只有微信在前台时才有内容（`printUnreadDetails` 开头「先切到前台，AX 树才有内容」），每分钟切一次前台会打断人在 Mac 上的操作。

## 三个信号

### 1. 新通知（主信号，带发送人）

macOS 把送达的通知存在 sqlite 数据库里，普通用户权限可以只读打开：

- macOS 14：`$(getconf DARWIN_USER_DIR)com.apple.notificationcenter/db2/db`
- macOS 15 起：`~/Library/Group Containers/group.com.apple.usernoted/db2/db`（可能要给终端开「完全磁盘访问权限」）

两个路径按顺序找，先找到能打开的那个。

- 表 `app(app_id, identifier)`：`identifier` 是小写的 bundle ID，如 `com.tencent.xinwechat`、`com.tencent.xinwechat2`，每个微信实例各一个。
- 表 `record(app_id, data, delivered_date)`：`data` 是二进制 plist，`req.titl` 是聊天名，`req.body` 是消息预览；`req.usda` 是 NSKeyedArchiver 归档的字典，里面的 `chatname` 是对方的内部 ID（私聊是 `wxid_…` 或老账号的原始微信号，群聊是 `…@chatroom`），`unique_id` 是 `chatname_时间戳_序号`；`delivered_date` 是从 2001-01-01 UTC 起的秒数（加 978307200 换成 Unix 时间）。
- 每个 App 只保留最近 100 条，够用（peek 只关心上次 `unread` 之后的）。

本机实测（2026-09-29，各 100 条）：zhiwuzhu（`WeChat.app`）有 `titl` 和完整正文（最长 172 字，没见截断）；work（`WeChat2.app`）没有 `titl`，`body` 全是同一句通用提示，是这个号没开「通知显示消息详情」。两个号都有 `chatname`。

**前提：work 要在微信设置里打开「通知显示消息详情」**，否则 `new` 里只有 `id`，没有聊天名和预览。

`chatname` 不能代替 `wechat-whois`：它是不会变的内部 ID，而 `whois` 读的是资料卡上显示的「微信号」，对方改过微信号时两者不同（实测 联系人A：`wxid_aaaa1111` / `alias_a`；联系人B：`old_id_b` / `alias_b`）。Constance 的备注要资料卡上的微信号，所以发兑换码前仍然调 `whois`。

通知的 `titl` 也不一定等于会话列表里的名字（实测一个通知标题是「小明」，会话列表里是「备注前缀 小明」），所以 `new[].chat` 只供参考，回复、`ack` 用的名字以 `unread` 返回的 `pending` 为准。

peek 查出本账号 `delivered_date > since` 的所有记录，作为 `new` 返回。`since` 是上次成功的 `wechat-unread`（非 `list_only`）**开始**的时间：用开始时间而不是结束时间，是为了不漏掉 `unread` 跑的过程中进来、没被读到的消息，代价是这些已经读到的消息会多唤醒一次，而多唤醒一次只是多跑一轮没有新消息的 `unread`（读取进度对得上的聊天会直接跳过，不点开）。

只要 Muse 还没成功跑完一次 `unread`，这些通知就一直在 `new` 里，peek 一直返回 `wake: true`。

局限（由另外两个信号或 Muse 的兜底轮次补上）：

- 微信的新消息通知要开着，并且显示消息详情（见上）。
- 微信正开在这个聊天上时不发通知（见「发完、读完切回文件传输助手」）；免打扰的群不发通知。
- 在通知中心手动清除通知，记录可能跟着删掉。

### 2. Dock 角标（兜底）

`lsappinfo info -only StatusLabel -app <bundleID>` 返回 `"StatusLabel"={ "label"="3" }`，不碰微信，只读系统记录的 Dock 角标。取出其中的数字；空字符串算 0；有字但没有数字（比如一个点）算 1；App 没在运行时 `badge` 为 `null`。

角标不能直接拿「> 0」判断：`unread` 读完会把聊天标回未读，只 `ack` 不回复的聊天也一直是未读，角标会长期大于 0。所以和基线比：

- `unread` 成功后，把当时的角标记成基线 `badge_base`。
- 每次 peek：当前角标 < 基线 → 把基线降到当前值（人在手机上读掉了几条）；当前角标 > 基线 → 唤醒。

两次 peek 之间先读掉再来新消息、角标回升但没超过旧基线的情况看不出来，这时靠通知。

### 3. 超时没处理的待处理消息（不会没人回）

待处理缓存里 `added` 早于「现在 − 30 分钟」的消息算超时。有超时消息、而且距离上次因为它提醒已经超过 30 分钟，就唤醒，并把提醒时间记成现在。卡在「等人定夺」的聊天会每 30 分钟提醒一次，直到回复或 `ack`。

30 分钟可以用 `WX_PEEK_STALE_MIN` 改。

**没读全**：`unread` 返回成功，但有聊天读失败（带 `error`）或未读聊天太多被截断（note「只读了前 N 个」）时，这些消息的通知和角标已经被这次 `unread` 吸收，也不在待处理缓存里。这时 peek 状态记 `incomplete`，peek 按超时提醒的节奏（共用 `reminded`）返回原因 `incomplete`，直到下一次 `unread` 读全。（整体审查时补上）

## 接口

`GET /wechat/peek?account=work`（`account` 可省，规则同下）

```json
{"ok": true, "wake": true, "reasons": ["new", "badge", "stale", "incomplete"], "incomplete": false,
 "new": [{"chat": "张三", "id": "wxid_abc123", "preview": "在吗", "time": "2026-09-29T14:53:39"}],
 "badge": 3, "badge_base": 1,
 "pending": 4,
 "stale": [{"chat": "李四", "count": 2, "oldest": "2026-09-29T13:40:00"}],
 "notify_error": "打不开通知数据库：…"}
```

- `reasons` 列出这次唤醒的原因，可能为空（`wake: false`）。
- `new` 按时间从旧到新，最多 50 条；`time` 是本地时间；`id` 是通知里的 `chatname`；没开消息详情时 `chat`、`preview` 为空字符串。
- `pending` 是待处理缓存里的总条数，`stale` 只列超时的聊天。
- `notify_error` 只在通知数据库读不了时出现，这时另外两个信号照常工作。
- 微信没在运行：`badge: null`，照常返回其他字段。

账号到 bundle ID：

- 给了 `account`：在 `WX_ACCOUNTS`（`别名=App 路径`）里找到 App，读 `Contents/Info.plist` 的 `CFBundleIdentifier`。找不到别名 → 400。
- 没给：`WX_ACCOUNTS` 只配了一个就用它；没配就用正在运行的那个微信（`pgrep -x WeChat` 只有一个时），否则 400「请指定 account」。
- 待处理缓存和 peek 状态的账号名跟现在一样，没给时用 `default`。

## 状态存储

`~/.cache/wx-send/peek/<账号>.json`：

```json
{"since": 1790000000.0, "badge_base": 1, "reminded": 1790000000.0}
```

- 文件不存在（第一次用）：`since` 取现在，`badge_base` 取当前角标，这一次不因通知和角标唤醒（不把历史通知当成新消息），超时提醒照常。
- 写法同待处理缓存：先写临时文件再改名。
- peek 不拿全局锁，但 peek 和 `unread` 都要改这个文件，用一把单独的小锁 `peek_lock` 包住「读→改→写」，防止互相覆盖。
- `a_wechat_unread`：调用 wx-send 之前记下开始时间；成功（`ok` 且不是 `list_only`）并写完待处理缓存后，在 `peek_lock` 下把 `since` 改成开始时间、`badge_base` 改成当前角标。失败不改，下次 peek 还会唤醒。

## 撤回：只记日志

不做兼容处理，回不回由 Muse 自己看聊天内容决定。

服务端在 `/wechat/read`、`/wechat/unread` 返回的消息里发现对方的撤回提示（中文界面大概是「"张三" 撤回了一条消息」或「对方撤回了一条消息」，确切文字真机确认后写进正则；自己撤回的「你撤回了一条消息」不算）时，往 `mab-bridge.log` 记一行：账号、聊天名、提示原文。返回内容不变。

## 发完、读完切回文件传输助手

`unread` 读完会停在「文件传输助手」上，但 `send`、`read`、`whois` 做完后微信停在客户的聊天上。微信在前台且正开着这个聊天时，客户紧接着回的消息会直接变成已读，也不发通知，peek 和 `unread` 都看不到。

改动（Swift）：`send`（不是试运行）、`read`、`whois` 结束前，如果微信还在前台，就打开「文件传输助手」；切不过去只在 note 里记一句，不算失败。

代价：连续两次发给同一个人（比如先发文字再发图）时，第二次不能再用「已在该聊天，跳过搜索」，要重新搜一次，多 1~2 秒。

## 日志

每个请求都会写进 `mab-bridge.log`，每分钟一次 peek 每天多 1440 行。peek 只在 `wake: true` 或出错时记日志。

## mab.py

新增 `wechat-peek [-a]`，打印返回的 JSON。

## muse-prompt

新增一段「自动回复」：

1. hook 每 1~2 分钟运行 `python3 mab.py wechat-peek -a <账号>`，只有 `wake` 为 `true` 时才唤醒 agent；上一轮还没跑完时不要再唤醒。
2. 被唤醒后：`wechat-unread` → 以 `pending` 为准逐个处理 → 回复或 `ack`。
3. `peek` 的 `new[].chat` 只供参考，回复和 `ack` 用 `pending` 里的聊天名；发兑换码前仍然用 `wechat-whois` 查微信号。
4. 保留每 60 分钟一轮的完整流程作为兜底（免打扰白名单里的群只能靠它）。

## 不做的事

- Mac 上的常驻轮询、推送（Muse 收不了 webhook）。
- peek 自己去读微信（任何需要 AX 或前台的操作）。
- 从通知里解析群聊的发送人（`preview` 原样返回）。

## 测试

- **Python 单元测试**（通知数据库用临时 sqlite 按同样的表结构造，角标用替身函数）：
  - 第一次 peek：建状态文件，历史通知不进 `new`，不因角标唤醒；超时提醒照常
  - `since` 之后的通知进 `new`、之前的不进；只看本账号的 bundle ID；二进制 plist 解析出聊天名和预览
  - `unread` 成功后 `since` 变成开始时间、`badge_base` 更新；失败或 `list_only` 不变
  - 角标：升过基线唤醒；降了基线跟着降；空字符串算 0；App 没运行为 `null`
  - 超时：超过 30 分钟才唤醒，唤醒后 30 分钟内不重复提醒；`WX_PEEK_STALE_MIN` 生效
  - 通知数据库不存在或打不开：`notify_error`，其他照常
  - 账号解析：别名 → bundle ID、未知别名 400、多个账号不给 `account` 时 400
  - peek 不拿全局锁：全局锁被占着时 peek 照样马上返回
  - `usda` 里解出 `chatname`；没有 `titl` 时 `chat`、`preview` 为空字符串
  - 撤回：read / unread 读到对方的撤回提示时记一行日志，自己的不记，返回内容不变
  - mab 的 `wechat-peek`
- **Swift**：`send`、`read`、`whois` 结束后切回文件传输助手，试运行不切；类型检查 + 真机
- **真机**（zhiwuzhu → work，先在 work 打开「通知显示消息详情」）：
  - zhiwuzhu 发一条 → peek 在 `new` 里看到聊天名、`id` 和预览，`wake: true`，微信没有被切到前台
  - work 跑 `unread` 后 → peek `wake: false`
  - **角标实际表现**：有未读时 `lsappinfo` 读到的数字对不对；标回未读的聊天再来一条消息，角标涨不涨
  - **撤回**：zhiwuzhu 发一条再撤回，分别在 `unread` 之前和之后撤回；确认撤回提示的确切文字（中文界面）、日志记上了；看通知数据库里原来那条通知会不会被删、有没有新通知
  - **切回**：`send` 后微信停在文件传输助手；微信在后台、开着某个聊天时，这个聊天来消息有没有通知和未读
  - 好友申请会不会产生通知、在 `new` 里长什么样（只观察，记进文档）

## 真机结果（2026-09-29，work → zhiwuzhu）

- **peek 不碰微信**：peek 前后前台都是访达；`new` 里有聊天名「测试号」、`id`（`wxid_test0001`）和预览。
- **角标**：`lsappinfo` 读到的数字和未读对得上（1 → 2）；`unread` 标回未读的聊天再来一条消息，角标照样涨（2 → 3），`badge` 信号有效。
- **unread 之后**：peek 不再因为读过的消息唤醒；`unread` 跑的过程中进来的群消息（12:50:17）下次 peek 仍算新的，符合设计。
- **超时提醒**：`WX_PEEK_STALE_MIN=1` 时，过 1 分钟第一次 peek 带 `stale`，紧接着的第二次不带。
- **日志**：10 次 peek 里只记了 `wake: true` 的 8 次。
- **后台开着聊天时来消息**：微信在后台、开着「测试号」时，work 发来的消息**有通知、角标也涨**；但下一次 `unread` 把微信切到前台，这个聊天立刻变成已读，扫描不到。`unread` 是按读取进度读的，同一个聊天再来新消息时会把这些一起补读出来；如果之后没有新消息，就会漏掉。`send`/`read`/`whois` 做完切回文件传输助手就是为了避免这种情况，但人手动把微信停在某个聊天上时仍会发生。
- **切回文件传输助手**：`read`、`whois`、真实 `send` 做完都停在「文件传输助手」；试运行停在原聊天，输入框里保留内容。
- **撤回提示原文、撤回后通知的变化**：未验证（需要人手操作撤回）。
- **好友申请的通知**：未遇到，未验证。

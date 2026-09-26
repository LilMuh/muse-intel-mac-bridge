# muse-intel-mac-bridge

**让 Meta Muse（以及任何运行在 Linux VM 里的 AI agent）远程操作你的 Intel Mac。**

官方 Muse for Mac 只提供 Apple 芯片（arm64）版本。在 Intel Mac 上打开会提示“此版本不能与此版本的 macOS 配合使用”，即使系统版本已经满足要求（macOS 14+）。本项目提供一个轻量替代方案：在 Mac 上运行一个小服务，Muse 通过它的云端 VM 截图、点击、打字，从而操作你的 Mac。

> English summary at the bottom.

## 为什么不用 VNC？

直接用 VNC 截图，5K 屏幕的一帧就有几十 MB，传输很慢，容易超时。本项目在 Mac 本地完成截图、缩放和 JPEG 压缩，**每帧约 100–200 KB**，并自动处理坐标换算。

|  | VNC 截图 | muse-intel-mac-bridge |
|---|---|---|
| 单帧大小（5K 屏） | 几十 MB | ~150 KB |
| Retina 坐标换算 | agent 自己算，容易点偏 | 服务自动完成 |
| 输入中文 | 麻烦 | 支持（自动走剪贴板） |
| 需要开放的端口 | 5900 | 无（通过 Tailscale Funnel） |

## 工作原理

```
手机上的 Muse ──► Muse 云端 Linux VM ──HTTPS──► Tailscale Funnel ──► 你的 Mac
                   (运行 mab.py 客户端)                                (mac_agent_server.py)
```

- agent 看到的永远是 1280 宽的截图，坐标也用这张图的像素坐标。
- 服务把坐标换算成 macOS 逻辑坐标（Retina 下截图像素是逻辑点的 2 倍）。
- 服务默认只监听 `127.0.0.1`，由 Tailscale Funnel 提供 HTTPS 访问，所有请求都需要 token。

## 系统要求

- macOS 12 或更高版本，**Intel 或 Apple 芯片均可**
- Python 3.9+（`xcode-select --install` 会附带）
- [Tailscale](https://tailscale.com)（用于把服务暴露给云端的 agent）

## 快速开始

### 1. 下载并启动

```bash
git clone https://github.com/<你的GitHub用户名>/muse-intel-mac-bridge.git
cd muse-intel-mac-bridge
chmod +x start.sh
./start.sh
```

首次运行会自动创建虚拟环境、安装依赖，并生成 token（保存在 `.env`）。

### 2. 授予权限

打开「系统设置 → 隐私与安全性」，给你使用的终端（Terminal / iTerm）开启：

- **屏幕录制**：用于截图。没开的话截图会是黑屏或只有桌面壁纸。
- **辅助功能**：用于控制鼠标和键盘。

开启后需要**完全退出并重新打开终端**，再运行 `./start.sh`。

### 3. 本地测试

另开一个终端：

```bash
source .env
curl -H "Authorization: Bearer $AGENT_TOKEN" http://127.0.0.1:8765/screenshot -o s.jpg && open s.jpg
curl -H "Authorization: Bearer $AGENT_TOKEN" http://127.0.0.1:8765/info
```

图片大约 100–200 KB、画面正常，就说明服务工作正常。

### 4. 通过 Tailscale Funnel 暴露

1. 安装并登录 Tailscale：`brew install --cask tailscale`
2. 在 [Tailscale 管理后台](https://login.tailscale.com/admin/dns) 开启 **HTTPS**，并在 Access Controls 中允许 **Funnel**
3. 按 Ctrl+C 停掉服务，改用 `./start.sh --funnel` 启动

终端会显示形如 `https://你的mac名.xxx.ts.net` 的公网地址。

### 5. 连接 Muse

打开 [docs/muse-prompt.md](docs/muse-prompt.md)，把里面的说明发给 Muse，替换好地址和 token。先让它运行：

```bash
python3 mab.py info
```

返回 JSON 就说明连通了。

## 客户端 mab.py

在 agent 的 VM 里运行，只依赖 Python 标准库：

```bash
export MAB_URL=https://你的mac名.xxx.ts.net
export MAB_TOKEN=你的token

python3 mab.py screenshot -o screen.jpg     # 截图
python3 mab.py click 640 400                # 单击（--double 双击，--right 右键）
python3 mab.py type "你好 hello"             # 输入文字
python3 mab.py key command space            # 组合键
python3 mab.py scroll -5 640 400            # 在 (640,400) 处向下滚动
python3 mab.py drag 100 100 300 300         # 拖拽
python3 mab.py wechat-read "联系人" -n 10 -a work           # 读微信聊天记录（见下文）
python3 mab.py wechat-send "联系人" "内容" -a work          # 发微信；加 --dry-run 只粘贴不发送
python3 mab.py wechat-unread -a work                       # 读所有未读聊天的新消息（见下文）
python3 mab.py wechat-friends --accept -a work             # 通过所有好友申请（见下文）
```

## 让 Muse 收发微信（可选）

Muse 可以按联系人名字直接发微信、读聊天记录，不需要截图找坐标。Mac 端调用 `wx-send.sh` 完成操作：联系人按名字**完全一致**匹配，聊天标题、输入框、发送结果都逐字核对，任何一步对不上就停止，不会发错人。

### Mac 端准备（只需一次）

1. 脚本已经在仓库里：`wechat/wx-send.sh`，不需要另外下载。首次运行会自动编译一次，需要先装好 Xcode 命令行工具：`xcode-select --install`。
2. 在 `.env` 里配置微信账号（`.env` 不会被提交到 Git）。格式是 `别名=App 路径`，多个账号用逗号分隔：
   ```bash
   # 一个微信
   WX_ACCOUNTS=me=/Applications/WeChat.app
   # 两个微信（第二个是复制出来的 App）
   WX_ACCOUNTS=work=/Applications/WeChat.app,home=/Applications/WeChat2.app
   ```
   只开一个微信时，也可以不配置，调用时省略 `-a`。别名里不能有逗号和等号。
3. 在 Mac 终端里试运行一次（只粘贴、不发送）：
   ```bash
   WX_DRY_RUN=1 ./wechat/wx-send.sh -a work "文件传输助手" "测试"
   ```
   看到「🧪 试运行：已粘贴到「文件传输助手」的输入框」就说明正常。然后到微信里把输入框清空。
4. 重启 bridge：`./start.sh --funnel`。
5. 使用期间：微信保持登录、主窗口开着，屏幕不要锁定。发消息时微信会被切到前台，大约 1–3 秒，这时不要动鼠标和键盘。

### 教 Muse 使用（一步一步）

**第 1 步：连通。** 先按上面的「连接 Muse」把 [docs/muse-prompt.md](docs/muse-prompt.md) 发给 Muse。然后对它说：

> 运行 `python3 mab.py info`，看看 actions 里有没有 wechat/send。

有的话就说明微信功能可以用了。

**第 2 步：读一条试试。** 对 Muse 说：

> 用 mab.py 读一下微信「文件传输助手」最近 5 条消息，账号是 work。

它会运行：
```bash
python3 mab.py wechat-read "文件传输助手" -n 5 -a work
```
返回的 `items` 里就是聊天记录。

**第 3 步：试运行发送。** 对 Muse 说：

> 给微信「文件传输助手」试运行发一条「你好」，只粘贴不发送。

它会运行：
```bash
python3 mab.py wechat-send "文件传输助手" "你好" --dry-run -a work
```
到 Mac 上看，输入框里应该有「你好」但没有发出去。确认后把输入框清空。

**第 4 步：正式发送。** 对 Muse 说：

> 给微信「张三」发：明天下午三点开会。

Muse 应该先复述联系人和内容，等你回复「确认」后再运行：
```bash
python3 mab.py wechat-send "张三" "明天下午三点开会" -a work
```

**第 5 步：看结果。** 返回的 `status` 表示结果：

| status | 意思 | 怎么办 |
|---|---|---|
| `ok` | 已发出，并且在聊天记录里确认过 | — |
| `not_found` | 搜索结果里没有这个名字 | 名字要和微信里显示的**完全一致**（有备注就用备注名） |
| `duplicate_name` | 有多个同名联系人或群 | 在微信里给对方设置一个唯一的备注名 |
| `draft_in_input` | 输入框里已有草稿。直接粘贴的话，草稿会和消息一起发出去，所以先停下 | 到 Mac 上清空输入框再重试 |
| `unconfirmed_do_not_retry` | 可能已经发出，但没能确认 | **不要重发**，先用 `wechat-read` 看一下 |
| `verify_failed` | 某项核对没通过：聊天没切换过去、输入框不属于这个联系人、粘贴后内容对不上等。没有发送 | 消息可能还留在输入框里，清空后再重试 |
| `send_failed` | 发送失败：消息旁边出现了红色感叹号，通常是网络问题 | 检查网络后重试 |
| `daily_limit` | 这个账号今天已经发满 500 条 | 明天再发，或调高 `WX_DAILY_MAX` |
| `focus_lost` | 发送过程中微信被切走了 | 发送期间别动 Mac，然后重试 |
| `environment` | 屏幕锁定、微信没开、权限不够等 | 看 `output` 里的提示 |
| `bad_request` | 参数不对、账号没配置，或开着多个微信却没指定 `-a` | 看 `output` 里的提示 |

### 读未读消息（批量总结）

`wechat-unread` 一次拿到所有未读聊天的新消息，交给 Muse 总结。

**读哪些聊天：**
- 没开免打扰的未读聊天：全部读。
- 开了免打扰的聊天：都不读，只读白名单里的群。白名单写在 `.env` 里，群名用 `|` 分隔，所有账号共用：
  ```bash
  WX_MUTED_ALLOW=项目核心群|家人群
  ```
  也可以直接在微信里把要读的群改回「不免打扰」，这样就不用配置白名单。

**它会做什么：**
1. 从左侧「微信」标签读出未读总数；0 条就直接返回。
2. 扫描会话列表，找出要读的聊天。
3. 逐个点开，读新消息。**群聊**里每条消息会点一下头像，从弹出的资料卡里读出发送人的昵称和微信号（`from` 是 `me` / `other` / `system`）；**私聊**不点头像，发送人就是这个聊天本身（`group: false`）。
4. 切回你原来打开的聊天，把读过的聊天**标回未读**（右键 →「标为未读」）。标回后显示「1条未读」，不是原来的条数。

**不会重复读：** 每个聊天会记下读到的最后 3 条消息（存在本机 `~/.cache/wx-send/cursor/`，不会上传）。下次只返回这之后的新消息；会话列表的预览和上次一样的，直接跳过，不点进去。3 天没更新的记录每天自动清理一次。

**对 Muse 说：**

> 用 `python3 mab.py wechat-unread -a work` 拿到所有未读消息，按聊天逐个总结，最后列出需要我回复的。

只想看看有哪些聊天有未读、不点进去：`wechat-unread --list-only`。这时不会改变任何已读状态。

**返回示例：**
```json
{"ok": true, "total": 1, "skipped_no_new": [], "notes": [],
 "chats": [{"name": "张三", "group": false, "unread": 1, "new": 1, "muted": false, "pinned": false,
            "time": "15:42", "remarked_unread": true,
            "messages": [{"type": "message", "text": "明天几点？"}]},
           {"name": "项目群", "group": true, "unread": 1, "new": 1, "muted": false, "pinned": false,
            "time": "15:40", "remarked_unread": true,
            "messages": [{"type": "message", "text": "方案发群里了", "from": "other", "sender": "李四", "wxid": "lisi_88"}]}]}
```

**管理读取记录：**
```bash
python3 mab.py wechat-forget "张三" -a work     # 删掉某个聊天的读取记录，下次按「N条未读」重新读
python3 mab.py wechat-prune --days 3 -a work    # 删掉 3 天没更新的读取记录
```

**注意：**
- 执行期间微信会被切到前台，逐个点开聊天，群聊还要逐条点头像。未读多的时候可能要几分钟，期间不要动 Mac。
- 每次最多读 20 个聊天、每个聊天最多 50 条消息（`--max-chats` / `--max-messages` 可调）。
- 电脑上点开聊天后，手机上的未读也会被清掉；电脑上标回未读后，手机上会不会跟着变回未读还没有验证。

### 通过好友申请

`wechat-friends` 列出通讯录「新的朋友」里所有「等待验证」的申请；加 `--accept` 就全部通过。

**通过后自动打招呼（可选）：** 在 `.env` 里写第一句话，不写就只通过、不发消息：
```bash
WX_FRIEND_GREETING=你好，很高兴认识你
```
招呼是从申请详情页点「发消息」直接进入聊天发送的，不走搜索，所以新朋友和别人重名也不会发错；发送时同样核对标题和输入框，并计入每天的发送上限。

**对 Muse 说：**

> 用 `python3 mab.py wechat-friends -a work` 看看有哪些好友申请；确认后用 `--accept` 全部通过。

**返回示例：**
```json
{"ok": true, "unseen": 0, "notes": [],
 "requests": [{"name": "张三", "message": "我是群聊\"项目群\"的张三", "source": "通过群聊添加",
               "accepted": true, "greeting": "SENT"}]}
```
`greeting` 是打招呼的发送结果（`SENT` / `UNCONFIRMED` / …），没配置招呼语时没有这个字段。

**注意：**
- 每次最多处理 10 条申请；每两次通过之间随机等 3–8 秒，防风控。
- 通过的新朋友如果和已有好友同名，以后按名字 `wechat-send` 会返回 `duplicate_name`，给其中一个设个备注就好。
- 通过的记录会写进发送日志（`FRIEND_ACCEPTED`）。

### 限制

- `wechat-read` 只能读到聊天窗口里当前加载出来的消息（通常是最近十几条），也分不出每条是谁发的。
- 同一时间只处理一个请求，其余的排队等待（最多 180 秒）。
- 内置防风控：同一账号两次发送至少间隔 3 秒（间隔不够会自动等待），每天最多 500 条（`WX_DAILY_MAX` 可调）。
- 偶尔会出现点击搜索结果后聊天没有切换过去的情况，这时会返回 `verify_failed`，不会粘贴也不会发送，重试即可。

## HTTP API

所有请求需要带 `Authorization: Bearer <token>`。

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/info` | — | 版本、截图尺寸、屏幕逻辑尺寸 |
| GET | `/screenshot` | — | JPEG 图片；响应头 `X-Image-Width` / `X-Image-Height` |
| POST | `/click` | `{"x","y","button","clicks"}` | 点击 |
| POST | `/move` | `{"x","y"}` | 移动鼠标 |
| POST | `/drag` | `{"x","y","to_x","to_y"}` | 拖拽 |
| POST | `/scroll` | `{"amount","x?","y?"}` | 滚动，正数向上 |
| POST | `/type` | `{"text"}` | 输入文字（支持中文） |
| POST | `/key` | `{"keys":[...]}` | 按键或组合键 |
| POST | `/wechat/send` | `{"to","text","account?","dry_run?"}` | 发微信（见上文） |
| POST | `/wechat/read` | `{"chat","limit?","account?"}` | 读聊天记录，默认 20 条 |
| POST | `/wechat/unread` | `{"account?","list_only?","max_chats?","max_messages?"}` | 读所有未读聊天的新消息 |
| POST | `/wechat/forget` | `{"chat","account?"}` | 删掉某个聊天的读取记录 |
| POST | `/wechat/prune` | `{"days?","account?"}` | 删掉 N 天没更新的读取记录 |
| POST | `/wechat/friends` | `{"account?","accept?"}` | 列出 / 通过好友申请 |

注意：操作前至少要调用一次 `/screenshot`，服务才知道该用哪个坐标系。

## 配置

通过环境变量或 `.env` 设置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_TOKEN` | 自动生成 | 访问令牌，至少 16 位 |
| `HOST` | `127.0.0.1` | 监听地址。只有在局域网直连时才改成 `0.0.0.0` |
| `PORT` | `8765` | 端口 |
| `TARGET_W` | `1280` | 截图宽度 |
| `JPEG_QUALITY` | `60` | JPEG 质量，1–100 |
| `WX_ACCOUNTS` | 空 | 微信账号，`别名=App 路径`，多个用逗号分隔 |
| `WX_SEND` | `wechat/wx-send.sh` | 微信接口使用的脚本路径 |
| `WX_MUTED_ALLOW` | 空 | 读未读时要读的免打扰群，用 `\|` 分隔 |
| `WX_CURSOR_DAYS` | `3` | 读取记录保留几天 |
| `WX_FRIEND_GREETING` | 空 | 通过好友申请后自动发的第一句话 |

## 安全须知

这个服务相当于把你的鼠标和键盘交给了远程程序，请务必注意：

- **token 就是钥匙**。泄露了就删掉 `.env`，重新运行 `./start.sh` 生成新的。
- Funnel 地址是公网可访问的。不用的时候运行 `tailscale funnel reset` 关闭。
- **紧急停止**：把鼠标甩到屏幕左上角（pyautogui failsafe），或在服务窗口按 Ctrl+C。
- 建议给 agent 单独开一个 macOS 用户，或者至少在它工作时不要登录敏感账户。
- 所有请求都会打印在终端里，可以随时查看 agent 做了什么。

## 常见问题

**截图是黑屏或只有壁纸？**
终端没有获得「屏幕录制」权限。授权后需要完全退出终端再重新打开。

**点击没有反应？**
终端没有获得「辅助功能」权限。

**点击位置偏了？**
确认 agent 用的是最近一次截图的坐标。多显示器时只支持主显示器。

**Muse 连不上？**
Muse 的出站请求需要经过它的安全审核（Sentinel），可能需要你在手机上批准。另外，本项目要求 Muse 的 VM 能执行终端命令。

**Mac 休眠了？**
`start.sh` 会自动运行 `caffeinate`，但合上笔记本盖子仍会休眠，除非连接了外接显示器和电源。

## 免责声明

本项目是独立的社区项目，与 Meta 或 Muse 没有任何关联，也未获得其认可。“Muse”是其各自所有者的商标。使用本项目需自行承担风险。

---

## English

**Let Meta Muse (or any AI agent running in a Linux VM) control your Intel Mac.**

The official Muse for Mac app ships as arm64-only, so on Intel Macs it fails with a "this version cannot be used with this version of macOS" error even on macOS 14+. This project is a small HTTP bridge: run `mac_agent_server.py` on the Mac, expose it with Tailscale Funnel, and let the agent use `mab.py` (stdlib-only) from its VM to take screenshots, click, type and press keys.

- Screenshots are captured, downscaled to 1280px and JPEG-compressed on the Mac (~150 KB instead of tens of MB over VNC).
- The agent always works in screenshot pixel coordinates; the server maps them to macOS points (Retina-aware).
- Binds to `127.0.0.1` by default; every request needs a bearer token.
- Works on Intel and Apple silicon, macOS 12+.
- Optional WeChat endpoints (`/wechat/send`, `/wechat/read`, `/wechat/unread`, `/wechat/friends`) call `wx-send.sh` on the Mac to send/read messages by exact contact name, with no screenshots involved.

Quick start: `./start.sh` → grant Screen Recording + Accessibility to your terminal → `./start.sh --funnel` → paste [docs/muse-prompt.md](docs/muse-prompt.md) into Muse.

Not affiliated with or endorsed by Meta. MIT licensed.

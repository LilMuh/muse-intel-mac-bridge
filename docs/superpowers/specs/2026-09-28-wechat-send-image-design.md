# 微信发送图片 · 设计

日期：2026-09-28

## 目标

让 Muse 通过 bridge 给微信联系人发送**单张图片**。图片来源有三种：

1. Muse 从它的 VM 上传（例如用户刚在手机上拍的照片）
2. Mac 当前剪贴板里的图片
3. 用户手动放进项目目录 `outbox/` 的图片

发送时机由对话控制：Muse 复述「联系人 + 图片」，用户回复「确认」后立即发送。Mac 端不做定时、不做条件触发。

## 不做的事

- 定时发送、条件触发
- 一次发多张图、图文合并为一次调用（要配文字就再调一次 `wechat-send` 发文字）
- `--batch` 和好友招呼发图片
- 访问 `outbox/` 以外的本地文件
- 自动清理 `outbox/`

## 总体流程

```
Muse VM                          Mac: mac_agent_server.py                 Mac: wx-send.sh（Swift）
mab.py wechat-send --image X
  ├─ X 是 VM 里的文件 ──► POST /wechat/image/upload ─► 存 outbox/<name>
  │                       ◄── {"file": "<name>", ...}
  └─► POST /wechat/send {"to","image":"<name>"} ─► 解析为 outbox 内路径
                                                   ─► wx-send.sh --image 联系人 路径 ─► openChat → sendInChat(.image)
```

剪贴板来源：`POST /wechat/image/clipboard` 把剪贴板图片存进 `outbox/`，之后同样按文件名发送。

## 第 0 步：先试验，再写代码

在「文件传输助手」上手动试验，把结论补进本文档的「试验结论」一节：

1. 往输入框粘贴 **PNG 数据**和**文件 URL**，微信分别会怎样：作为图片还是文件？会不会弹出「发送给…」的确认框？
2. 粘贴后，输入框的 AX value 是什么？预期是 `￼`（U+FFFC）。
3. 发出后，聊天记录最后一行的 AX title 是什么（`图片` / `[图片]` / 其他）？AX frame 是什么？
4. 断网时发一张图，发送失败的标记是什么样子、在什么位置？
5. GIF、HEIC、大于 10 MB 的图，粘贴后分别会怎样？

根据试验结论，确定以下几点：
- 剪贴板的写法
- 发出后判定为图片消息的规则
- 能不能检测发送失败
- 要不要处理确认框
- 大图要不要压缩

## 组件设计

### 1. `outbox/` 目录

- 位置：仓库根目录下的 `outbox/`，加进 `.gitignore`，服务启动时自动创建。
- **只有这个目录里的图片可以发送。**
- 文件名规则：
  - 只允许字母、数字、`._-` 和中文，不能包含 `/`、`..`，也不能以 `.` 开头。
  - `os.path.realpath` 解析后的路径必须在 `outbox/` 里面，防止通过符号链接跑到外面。
  - 扩展名只能是 `jpg`、`jpeg`、`png`、`gif`、`heic`。
- 重名时不覆盖，自动加 `-1`、`-2` 后缀，最终文件名在返回值里。
- 发送后文件保留，方便重发和核对。

### 2. mac_agent_server.py

新增接口（都需要 token，都在全局锁里执行）：

| 方法 | 路径 | 请求体 | 返回 |
|---|---|---|---|
| POST | `/wechat/image/upload` | `{"name":"a.jpg","data":"<base64>"}` | `{"ok","file","width","height","bytes"}` |
| POST | `/wechat/image/clipboard` | `{"name?":"x.png"}` | 同上；剪贴板里没有图片时返回 400 |
| GET | `/wechat/images` | — | `{"ok","images":[{"file","width","height","bytes","mtime"}]}` |
| GET | `/wechat/image/thumb?file=<name>` | — | 长边 512 的 JPEG 缩略图（`sips -Z 512`），给用户确认用 |

修改 `/wechat/send`：请求体可以是 `{"to","text"}` 或 `{"to","image"}`，两者**只能二选一**，都给或都不给就返回 400。给的是 `image` 时：
1. 按上面的规则把文件名解析成 `outbox/` 里的绝对路径。
2. 调用 `run_wx(["--image", to, path], account, env)`。
3. 返回格式和发文字完全一样（`ok/code/status/dry_run/output`）。

上传处理：
- 只检查一次 `Content-Length`：超过 `WX_IMAGE_MAX_MB`（默认 50）换算后的 base64 长度就返回 413。解码后不再检查大小。这个上限只是防止请求过大撑爆内存，不是微信的限制。
- base64 解码后，按**文件头**判断格式（JPEG `FFD8FF`、PNG `89504E47`、GIF `GIF8`、HEIC `ftypheic/heix/mif1`）。文件内容和扩展名对不上时，以内容为准改正扩展名。
- HEIC 用 `sips -s format jpeg` 转成 JPEG。
- 宽高用 `sips -g pixelWidth -g pixelHeight` 读取。

读剪贴板：用 `osascript -e 'the clipboard as «class PNGf»'` 取 PNG 数据写成文件。取不到时返回「剪贴板里没有图片」。默认文件名是 `clip-YYYYMMDD-HHMMSS.png`。

### 3. wx-send.sh（bash 部分）

- 用法说明里加一行：`./wx-send.sh -a work --image "联系人" 图片路径`，只粘贴一张图片并发送。
- 参数分发：`--image) [[ $# -eq 3 ]] || usage; exec "$BIN" image "$NAME" "$APP" "$2" "$3" ;;`

### 4. wx-send.sh（Swift 部分）

**消息类型**

```swift
enum Outgoing {
    case text(String)
    case image(String)   // 文件绝对路径
}
```

`Outgoing` 负责文字和图片不一样的部分：
- `paste()`：往剪贴板里放什么、怎么粘贴
- `inBox(_ seen: String) -> Bool`：粘贴后，输入框内容对不对
- `isSentRow(_ title: String) -> Bool`：发出后，聊天记录最后一行是不是这条消息
- `logText`：日志里记什么。文字记原文，图片记 `[图片] 文件名`。

`sendOne` 和 `sendInChat` 改成接收 `Outgoing`。`greet` 和 `--batch` 传 `.text(...)`，这两处的行为不变。

**`run()`**：新增 `case "image" where args.count == 5`，作业就是 `(联系人, .image(路径))`。后面的加锁、备份剪贴板、每日上限、发送间隔、写日志、输出结果，和文字共用同一个循环。

**`.image` 在 `sendInChat` 里的流程：**

1. **发送前检查**：在 `openChat` 之前完成，不碰微信。检查文件存在、可读，并且用 `CGImageSourceCreateWithURL` 能打开。不满足就抛出 `fail(64, "BAD_IMAGE", …)`。
2. **粘贴前**：和文字一样，输入框必须属于这个联系人，而且是空的。有草稿就返回 7。
3. **粘贴**：按第 0 步的结论写剪贴板（PNG 数据或文件 URL），然后按 ⌘V。
4. **粘贴后**：3 秒内，输入框的 AX value 去掉首尾空白后必须正好是 `"\u{FFFC}"`。否则返回 `fail(4, "INPUT_MISMATCH", …)`，不发送。
5. **AX 读不到输入框时**：直接返回 `fail(4, "NO_AX_INPUT", "AX 读不到输入框，无法核对图片，已停止，未发送")`。**不改用截图识别**。
6. **发送前**：再核对一次标题，和文字一样。`DRY_RUN` 在这一步返回。
7. **发出后确认**：10 秒内满足以下全部条件才算 `SENT`：
   - 聊天记录多了一行
   - 这一行满足 `isSentRow`（规则由第 0 步确定）
   - 输入框已清空
   超时返回 `UNCONFIRMED`，退出码 10。
8. **发送失败标记**：如果第 0 步找到了可行的检测方法（用新消息那一行的 AX frame 定位红色感叹号），检测到就返回 `FAILED`。找不到方法的话，第一版不检测，在 notes 里加「未检测发送失败标记」。

**状态码**：不新增。文件有问题用 64，其他全部沿用现有的。`WX_STATUS` 和 README 里的状态表都不变。

### 5. mab.py

- `wechat-send` 的 `text` 改成可选，新增 `--image`，两者必须给一个、只能给一个。
- 用 `--image X` 时：
  - 如果 VM 里存在文件 `X`：base64 编码后调用 `/wechat/image/upload`（超时 120 秒），用服务端返回的 `file` 再调用 `/wechat/send`。
  - 否则把 `X` 当作 `outbox/` 里的文件名，直接调用 `/wechat/send`。
- 新命令：
  - `wechat-upload 路径 [--name]`：只上传，不发送
  - `wechat-clip [--name]`：把 Mac 剪贴板里的图片存进 `outbox/`
  - `wechat-images`：列出 `outbox/` 里的图片
  - `wechat-thumb 文件名 [-o thumb.jpg]`：下载缩略图

### 6. 文档

- **README**：
  - 新增「发图片」一节，讲三种来源、`outbox/` 的规则、注意事项。
  - HTTP API 表格加上新接口。
  - 配置表加上 `WX_IMAGE_MAX_MB`。
- **mab.py 和 mac_agent_server.py 顶部的说明**：加上新命令和新接口。
- **docs/muse-prompt.md**：
  - 加上发图命令。
  - 加一条规则：发图前先把联系人和图片复述给用户（剪贴板和 `outbox/` 的图片先用 `wechat-thumb` 下载缩略图给用户看），等用户回复「确认」再发。
  - 加一条排查提示：如果 VM 里找不到用户发来的照片，先告诉用户，不要猜路径。

## 出错处理

| 情况 | 结果 |
|---|---|
| 文件名不合规、路径跑出 `outbox/`、格式不对、文件不存在 | HTTP 400 或 wx-send 退出码 64，不碰微信 |
| 上传超过大小上限 | HTTP 413 |
| 剪贴板里没有图片 | HTTP 400 |
| 输入框有草稿 | `draft_in_input`（7） |
| 粘贴后输入框里不是一个 `￼`，或 AX 读不到输入框 | `verify_failed`（4），未发送 |
| 发出后 10 秒内没确认 | `unconfirmed_do_not_retry`（10） |
| 检测到发送失败标记 | `send_failed`（8） |

发送图片和发送文字一样计入发送间隔（`WX_MIN_INTERVAL`）和每日上限（`WX_DAILY_MAX`）。

## 测试

- **Python 单元测试**（`tests/test_images.py`，只用标准库 `unittest`，把 `pyautogui` 替换成假模块）：
  - 文件名消毒
  - 防止路径跑出 `outbox/`（包括 `..`、绝对路径、符号链接）
  - 按文件头判断格式
  - 重名自动加后缀
  - `text` 和 `image` 二选一的校验
  - 超过大小上限时拒绝
- **Swift 手动验证**（都在「文件传输助手」上做）：
  - 用 `WX_DRY_RUN=1` 发图，确认输入框里出现图片
  - 正式发一次，返回 `ok`
  - 输入框里留一段草稿，返回 `draft_in_input`
  - 传一个不存在的文件，返回 `bad_request`
  - 断网时发一次，看返回 `send_failed` 还是 `unconfirmed_do_not_retry`
- **端到端**：从 VM 用 `mab.py wechat-send "文件传输助手" --image photo.jpg` 发一次。

## 试验结论

2026-09-28 在 zhiwuzhu 账号的「文件传输助手」上实测，微信 Mac 版：

- **粘贴方式**：三种写法（NSImage 写入 TIFF、原始数据 PNG/JPEG/GIF、文件 URL）粘贴后，输入框里都显示为图片缩略图，都没有弹确认框，输入框的 AX value 都是 `￼`。选用 **NSImage**，和截图复制到剪贴板的效果一样。
- **图片消息的 AX 标题**：`图片`（不带方括号）。据此 `IMAGE_ROW_TITLES = ["图片"]`。实际发了两张 300×200 的红图，都返回 `SENT`，用时约 1 秒。
- **格式**：jpg、gif，以及一张约 36 MB、4000×3000 的 PNG，粘贴后都显示为图片，不会变成文件。测试用的 GIF 是静态的，**动图经过 NSImage 转成 TIFF 后大概率只剩第一帧，没有验证**。
- **草稿保护**：输入框里有文字草稿时发图，返回 `draft_in_input`（7），没有粘贴。
- **发送失败标记**：没有测试。断网会连 Claude 的会话一起断掉，所以第一版不检测，返回的 notes 里写明「未检测发送失败标记」。
- 试验期间「找不到微信主窗口」（退出码 5）偶尔出现，重试就好，和本功能无关。

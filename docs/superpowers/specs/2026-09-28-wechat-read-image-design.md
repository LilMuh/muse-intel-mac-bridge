# 微信读取图片 · 设计

日期：2026-09-28
依赖：`2026-09-28-wechat-send-image-design.md`（outbox、文件名校验、剪贴板备份）

## 目标

`wechat-read` 和 `wechat-unread` 读到图片消息时，可以把图片本身取出来：在 Mac 上存到 `inbox/`，Muse 下载到自己的 VM，然后在它和用户的对话里把图片发给用户。

取图的方式：在图片气泡上右键 →「复制」→ 从剪贴板读出图片。

## 不做的事

- 把读到的图片直接转发给微信里的别人（inbox 里的文件不能作为 `wechat-send --image` 的来源）
- 读微信的本地缓存文件
- 截取聊天气泡当作图片
- 视频、文件、表情包（只处理 AX 标题是「图片」的消息）
- Muse 怎样在它自己的对话里展示图片（这是 Muse 自身的能力，本项目只负责把原图交到它的 VM 里）

## 外部条件（需要用户去确认）

Muse 能不能在对话里给用户发图片或文件。如果不能，退一步是 Muse 看图后描述内容，本设计不用改。

## 总体流程

```
Muse: mab.py wechat-unread --images -a zhiwuzhu
  → POST /wechat/unread {"images": true}
  → 服务端设置 WX_IMAGES=1，调用 wx-send.sh --unread
      Swift：读完文字 → walkRows 逐页对行 → 遇到「图片」就调用 copyImage
             → 存为 inbox/in-20260928-141500-1.png
      输出：{"type":"message","text":"图片","image":"in-20260928-141500-1.png"}
  → mab.py 看到 image 字段，调用 GET /wechat/inbox/file?file=… 下载原图
      存到 VM 的 ./wechat-images/，并在输出里给这条消息加上 "local_path"
  → Muse 按 muse-prompt 的规则，把 local_path 指向的图片发到对话里
```

## 第 0 步：先实测，再写代码

在 zhiwuzhu 和 work 两个账号之间互相发图。这样每个账号的聊天里，都同时有「我发的」（右侧）和「对方发的」（左侧）图片。两个账号在对方通讯录里显示的名字，由用户在实测时告诉我们。

要确认的点：

1. **图片消息的 AX 结构**：「图片」这一行下面有没有 `AXImage` 之类的子元素，它的 frame 是不是正好是图片气泡的范围。如果没有，找出左侧和右侧气泡相对于这一行 frame 的固定偏移。
2. **右键菜单**：在图片气泡上右键，菜单里有哪些选项，「复制」的准确标题是什么；右键到这一行的空白处会怎样。
3. **复制后的剪贴板**：里面有哪些类型（文件 URL、PNG、TIFF、JPEG）；像素尺寸和原图是否一致。已知原图：300×200 的红图、421×747 的 `RedemptionPage.png`。
4. **还没下载原图的图片**：别人刚发来、还没点开的图片，复制时剪贴板里是原图、缩略图，还是什么都没有；原图下载需要多久。

实测结论写进本文档最后的「实测结论」一节。据此决定：气泡的定位方式、菜单项的名称、剪贴板里取哪种格式、要等多久。

## 组件设计

### 1. inbox/ 目录

- 位置：仓库根目录下的 `inbox/`，加进 `.gitignore`。bash 把 `WX_INBOX=<仓库>/inbox` 传给 Swift，服务端启动时自动创建这个目录。
- 文件名：`in-YYYYMMDD-HHMMSS-N.<扩展名>`。N 是这次调用里的序号，从 1 开始；扩展名按剪贴板里拿到的格式取 `png`、`jpg` 或 `gif`。这种文件名总能通过校验规则。
- 清理：用 `--images` 读消息时，bash 先删掉 `inbox/` 里超过 `WX_INBOX_DAYS`（默认 3）天的文件，写法和清理读取进度的 `prune` 一样。

### 2. mac_agent_server.py

- 把 `outbox_path(file)` 改成通用的 `safe_path(root, file)`，文件名规则不变。保留 `outbox_path(file) = safe_path(OUTBOX, file)`，另加 `INBOX` 常量。
- `/wechat/read` 和 `/wechat/unread` 支持 `"images": true`：往 `run_wx` 的环境变量里加上 `WX_IMAGES=1`。可以额外传 `max_images`（1–50），对应 `WX_MAX_IMAGES`，默认 10。
- 新增 `GET /wechat/inbox/file?file=<name>`：用 `safe_path(INBOX, name)` 校验后返回原文件，Content-Type 按文件头判断（沿用 `image_kind`）。文件不存在或文件名不合规时返回 400。
- 超时：`/wechat/read` 带图时，超时从 300 秒放宽到 600 秒；`/wechat/unread` 保持 1800 秒。

### 3. wx-send.sh（bash 部分）

- `WX_IMAGES` 和 `WX_MAX_IMAGES` 直接通过环境变量传给 Swift，不新增命令行参数。
- 读消息时，只要 `WX_IMAGES=1`，就先清理 inbox，再执行读取。
- 用法说明里补一句：环境变量 `WX_IMAGES=1` 表示读消息时顺便取图。

### 4. wx-send.sh（Swift 部分）

**Msg**：增加 `image: String?` 和 `imageError: String?`。`plain` 不包含这两个字段，所以读取进度和按页对行都不受影响。

**visibleRows**：返回值里多带上这一行的 AX 元素 `el`。所有图片行的标题都是「图片」，要靠元素本身区分是哪一张。

**walkRows(list, &all, start, act)**：从 `identifySenders` 里抽出来的逐页对行逻辑（滚到底部 → 一页页往上 → 把屏幕上的行对回 `all` 里的下标 → 对每个下标 `i >= start` 的行调用 `act(i, row)`）。`identifySenders` 改为基于它实现，行为不变。

**copyImage(row) -> 文件名**，失败时抛出带原因的错误，由调用方记到 `imageError`：

1. **定位**：按第 0 步的结论，优先用 AX 子元素的 frame，找不到再用固定偏移。已知这条消息的 `from` 时只试那一侧，否则左右两侧都试。
2. **右键前核对**：`AXUIElementCopyElementAtPosition` 取到的元素必须等于这一行（`CFEqual`），或者是它的子元素，否则不右键。
3. **右键和点菜单**：记下 `NSPasteboard.general.changeCount`，然后右键，等菜单出现（最多 1 秒），找到「复制」（准确标题按第 0 步），确认那个位置上是 `AXMenuItem`「复制」后再点击。找不到就按 Esc，抛出「右键菜单里没有复制」。
4. **读剪贴板**：等 `changeCount` 变化并且内容里有图片（最多 `WX_IMAGE_WAIT` 秒，默认 3 秒，第 0 步可能会调整）。读取顺序：指向图片文件的文件 URL（直接复制原文件，保留扩展名）→ PNG → JPEG → TIFF（转成 PNG）。写入 `WX_INBOX`，返回文件名。
5. **善后**：如果还有菜单开着，按 Esc；连按 3 次还关不掉，就抛出 `fail(5, "MENU_OPEN", …)`，终止整个读取，防止后面误点。

**接入方式：**

- `readNew`（unread）：原来只在群聊时调用 `identifySenders`，改为调用一次 `walkRows`，`act` 里同时做两件事：
  - 群聊且开启识别发送人：沿用现在点头像的逻辑；
  - `WX_IMAGES` 开启、这条是「图片」、而且还没超过张数上限：调用 `copyImage`。
  两件事都不需要做的时候，不调用 `walkRows`。
- `readChat`（read）：开启 `WX_IMAGES` 时，把读到的内容整理成 `[Msg]`，对最后 `limit` 条调用 `walkRows`，只做取图。
- 张数上限：这次调用里已经尝试了 `WX_MAX_IMAGES` 张以后，剩下的图片消息不再取图，并在 notes 里加一条「图片超过 N 张，只取了最近的 N 张」。`walkRows` 是从下往上走的，所以取到的是最近的 N 张。
- 输出：消息字典里增加 `"image"` 或 `"image_error"`。

**出错的处理：**

| 情况 | 结果 |
|---|---|
| 单张图片定位失败、菜单里没有「复制」、剪贴板里不是图片、超时 | 这条消息加 `image_error`，其他照常返回 |
| 菜单关不掉 | 退出码 5，终止读取 |
| 微信被切走 | 退出码 6，终止读取（和现在一样） |
| 超过张数上限 | 写进 notes |

剪贴板仍然由 read 和 unread 模式里原有的 `ClipboardBackup` 在读取前备份、结束后恢复。

### 5. mab.py

- `wechat-read` 和 `wechat-unread` 增加 `--images`、`--max-images N`、`--save-dir DIR`（默认 `./wechat-images`）。
- 带 `--images` 时，收到结果后遍历所有消息（read 的 `items`，unread 的 `chats[].messages`）。有 `image` 字段的，调用 `GET /wechat/inbox/file` 下载到 `save-dir`，然后在这条消息里加上 `"local_path"`（绝对路径）。某一张下载失败时加 `"download_error"`，不影响其他图片。
- 打印加工后的 JSON。

### 6. 文档

- README：新增「读图片」一节，讲怎么打开、会做什么、每张大约多久、张数上限、inbox 多久清理，以及隐私提醒（图片会传到 Muse 的 VM）。HTTP API 表和配置表加上新接口和新变量（`WX_MAX_IMAGES`、`WX_INBOX_DAYS`、`WX_IMAGE_WAIT`）。
- docs/muse-prompt.md：
  - 需要看图时加 `--images`。
  - 读到带 `local_path` 的图片消息时，把图片发到你和我的对话里，并注明是谁、在哪个聊天里发的。
  - 有 `image_error` 时照实告诉我。

## 测试

- **Python 单元测试**（`tests/test_images.py`）：
  - `safe_path` 对 inbox 生效，outbox 的原有测试照样通过
  - `/wechat/inbox/file`：正常返回、文件名不合规、文件不存在、Content-Type 正确
  - `/wechat/read` 和 `/wechat/unread` 带 `images: true` 时传了 `WX_IMAGES=1`，不带时没有传
  - mab 带 `--images`：返回里有 `image` 的消息被下载、加上 `local_path`；下载失败时有 `download_error`，其他图片照常下载
- **Swift**：`swiftc -typecheck`，以及不带 `WX_IMAGES` 时 read 和 unread 的回归（输出格式和原来一致）。
- **实测**（zhiwuzhu ↔ work）：
  - 两边互相发图后，分别 `--read 对方 --images`，两侧的图都要取到，尺寸和原图一致
  - work 给 zhiwuzhu 发图，让它成为未读，然后 zhiwuzhu `--unread --images`
  - 通过 bridge 用 mab 走一遍端到端，确认有 `local_path`
  - 读完后剪贴板恢复成读取之前的内容

## 实测结论

2026-09-28 在 zhiwuzhu（对方备注「测试号」）和 work（对方备注「测试号A」）之间互相发图实测：

- **AX 结构**：图片消息这一行是整宽的 `AXStaticText`，标题「图片」，**没有子元素**，也没有 `AXImage`。所以在这一行的左右两侧，距边缘 `IMG_DX = 110` 点的位置右键。对方发的图在左边；自己发的图在右边，右键到左边空白处不会弹菜单，接着试右边就行。
- **只在露出来的部分上点**：列表最上面那一行可能有一半被标题栏挡住。最初用行的垂直中点去点，会点到列表外面，已改成取「行和列表可见区域的交集」的中点；露出来不到 30 点时报「图片只露出一小部分」。
- **右键菜单**：`复制 | 编辑 | 另存为... | 打开方式 | 转发... | 收藏 | 提醒 | 多选 | 引用 | 删除/撤回`（自己发的最后一项是「撤回」，别人发的是「删除」）。菜单项名称固定为「复制」。
- **剪贴板**：点了「复制」后，剪贴板里有 `public.file-url`（微信缓存的图片文件）和 TIFF，读取时优先用文件 URL。取到的图像素尺寸和原图一致（300×200、421×747），字节不同，是微信重新编码过的，不是缩略图。
- **还没下载原图的图片**：对方刚发来、还没点开的图，在 3 秒等待内就能拿到 421×747 的原图。`WX_IMAGE_WAIT` 保持 3 秒。
- **不串位**：连着的 3 张图（300×200 / 421×747 / 300×200）顺序和尺寸都一一对应，两个账号都验证过。
- **剪贴板恢复**：读之前和读之后的 `clipboard info` 完全一样。
- **原有问题（和本功能无关）**：`--unread` 读完后会报「没能切回原来的聊天「文件传输助手」」，所以读过的聊天没有被标回未读。不带 `WX_IMAGES` 也能复现，另外处理。

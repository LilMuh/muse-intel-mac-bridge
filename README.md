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
```

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

Quick start: `./start.sh` → grant Screen Recording + Accessibility to your terminal → `./start.sh --funnel` → paste [docs/muse-prompt.md](docs/muse-prompt.md) into Muse.

Not affiliated with or endorsed by Meta. MIT licensed.

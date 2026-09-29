#!/usr/bin/env python3
"""
muse-intel-mac-bridge · Mac 端服务

让运行在云端 Linux VM 里的 AI agent（例如 Meta Muse）通过 HTTP 看到并操作你的 Mac。
专为装不了官方 Muse for Mac（仅支持 Apple 芯片）的 Intel Mac 设计，Apple 芯片同样可用。

接口（全部需要 Authorization: Bearer <token>）：
  GET  /info          截图尺寸、屏幕逻辑尺寸、版本
  GET  /screenshot    缩放压缩后的 JPEG（默认 1280 宽，约 100–200KB）
  POST /click         {"x":640,"y":400,"button":"left","clicks":1}
  POST /move          {"x":640,"y":400}
  POST /drag          {"x":100,"y":100,"to_x":300,"to_y":300}
  POST /scroll        {"amount":-5,"x":640,"y":400}   正数向上；x/y 可选
  POST /type          {"text":"hello 你好"}            非 ASCII 自动走剪贴板粘贴
  POST /key           {"keys":["command","c"]}
  POST /wechat/send   {"to":"联系人","text":"消息","account":"work","dry_run":false}
  POST /wechat/read   {"chat":"联系人","limit":20,"account":"work","images":false}
  POST /wechat/unread {"account":"work","list_only":false,"images":false}   读所有未读聊天的新消息；images=true 时顺便取图
  POST /wechat/forget {"chat":"联系人","account":"work"}      删某个聊天的读取进度
  POST /wechat/prune  {"days":3,"account":"work"}             删 N 天没更新的读取进度
  POST /wechat/friends {"account":"work","accept":false}      列出（accept=true 时通过）好友申请
  POST /wechat/pending {"account":"work"}                     待处理消息（unread 读到、还没回复或 ack 的），不碰微信
  POST /wechat/ack    {"chat":"联系人","account":"work","upto_id":12}  清掉这个聊天的待处理消息（回复成功时会自动清）
  POST /wechat/send   {"to":"联系人","image":"a.jpg"}          发 outbox 里的一张图片（text 和 image 二选一）
  POST /wechat/image/upload    {"name":"a.jpg","data":"<base64>"}  图片存进 outbox
  POST /wechat/image/clipboard {"name":"x.png"}                  Mac 剪贴板里的图片存进 outbox
  GET  /wechat/images                                           列出 outbox 里的图片
  GET  /wechat/image/thumb?file=a.jpg                           缩略图（长边 512 的 JPEG）
  GET  /wechat/inbox/file?file=in-xxx.png                       读消息时取到的原图
                      微信接口调用 wx-send.sh，按名字精确匹配，不需要截图和坐标

所有坐标都是「最近一次截图上的像素坐标」，服务自动换算成 macOS 逻辑坐标，
Retina 缩放与截图缩放比例 agent 都无需关心。

环境变量：
  AGENT_TOKEN   必填，至少 16 位
  HOST          默认 127.0.0.1（配合 Tailscale Funnel 使用最安全）
  PORT          默认 8765
  TARGET_W      截图宽度，默认 1280
  JPEG_QUALITY  默认 60
  WX_SEND       wx-send.sh 的路径，默认用仓库里的 wechat/wx-send.sh
  WX_ACCOUNTS   微信账号：别名=App 路径，多个用逗号分隔（只开一个微信可不填）
  WX_FRIEND_GREETING  通过好友申请后自动发的第一句话（不填就不发）
  WX_IMAGE_MAX_MB  单张图片上限，默认 50（只在请求阶段按请求大小检查）
  WX_MAX_IMAGES / WX_IMAGE_WAIT / WX_INBOX_DAYS  读图：每次最多几张（10）、复制后等几秒（3）、inbox 保留几天（3）
"""
import base64
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

__version__ = "0.3.0"

if sys.platform != "darwin":
    raise SystemExit("mac_agent_server.py 只能在 macOS 上运行 / This server only runs on macOS.")

import pyautogui  # noqa: E402

TOKEN = os.environ.get("AGENT_TOKEN", "")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
TARGET_W = int(os.environ.get("TARGET_W", "1280"))
QUALITY = os.environ.get("JPEG_QUALITY", "60")
WX_SEND = os.path.expanduser(os.environ.get(
    "WX_SEND", os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat", "wx-send.sh")))
OUTBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outbox")
INBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox")
PENDING_DIR = os.path.expanduser("~/.cache/wx-send/pending")   # 待处理消息缓存：unread 读到的，回复或 ack 后才清
IMAGE_MAX = int(float(os.environ.get("WX_IMAGE_MAX_MB", "50")) * 1024 * 1024)   # 防止请求过大撑爆内存，不是微信的限制
IMAGE_EXTS = ("jpg", "jpeg", "png", "gif", "heic")
IMAGE_NAME = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff-][A-Za-z0-9._ \u4e00-\u9fff-]*$")   # 允许空格：Mac 截图的文件名带空格

pyautogui.FAILSAFE = True  # 把鼠标甩到屏幕左上角可紧急中断 agent 的操作
pyautogui.PAUSE = 0.05

lock = threading.Lock()
state = {"img_w": None, "img_h": None}


def sips_size(path) -> Tuple[int, int]:
    """用 sips 读图片的像素宽高。"""
    info = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                          check=True, capture_output=True, text=True).stdout
    return int(info.split("pixelWidth:")[1].split()[0]), int(info.split("pixelHeight:")[1].split()[0])


def take_screenshot():
    """screencapture + sips（系统自带）：截主屏 → 缩到 TARGET_W 宽 → JPEG。"""
    fd, raw = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    out = raw[:-4] + ".jpg"
    try:
        # -x 静音，-m 仅主显示器，-C 包含鼠标指针
        subprocess.run(["screencapture", "-x", "-m", "-C", raw], check=True)
        if os.path.getsize(raw) == 0:
            raise RuntimeError("截图为空：请在「隐私与安全性 → 屏幕录制」中授权终端")
        subprocess.run(
            ["sips", "-Z", str(TARGET_W), "-s", "format", "jpeg",
             "-s", "formatOptions", QUALITY, raw, "--out", out],
            check=True, stdout=subprocess.DEVNULL,
        )
        w, h = sips_size(out)
        with open(out, "rb") as f:
            data = f.read()
        state["img_w"], state["img_h"] = w, h
        return data, w, h
    finally:
        for p in (raw, out):
            try:
                os.remove(p)
            except OSError:
                pass


def to_points(x, y):
    """截图像素坐标 → macOS 逻辑坐标（points）。"""
    if state["img_w"] is None:
        raise ValueError("请先调用 /screenshot 以确定坐标系 / call /screenshot first")
    pw, ph = pyautogui.size()
    return float(x) * pw / state["img_w"], float(y) * ph / state["img_h"]


def a_click(p):
    x, y = to_points(p["x"], p["y"])
    pyautogui.click(x, y, clicks=int(p.get("clicks", 1)), button=p.get("button", "left"))


def a_move(p):
    pyautogui.moveTo(*to_points(p["x"], p["y"]))


def a_drag(p):
    x, y = to_points(p["x"], p["y"])
    tx, ty = to_points(p["to_x"], p["to_y"])
    pyautogui.moveTo(x, y)
    pyautogui.dragTo(tx, ty, duration=float(p.get("duration", 0.3)), button="left")


def a_scroll(p):
    if "x" in p and "y" in p:
        pyautogui.moveTo(*to_points(p["x"], p["y"]))
    pyautogui.scroll(int(p["amount"]))


def a_type(p):
    text = p["text"]
    if text.isascii():
        pyautogui.write(text, interval=0.01)
    else:
        # pyautogui 无法直接输入中文等非 ASCII 字符，改用剪贴板粘贴
        env = dict(os.environ, LANG="en_US.UTF-8")
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=env, check=True)
        pyautogui.hotkey("command", "v")


def a_key(p):
    keys = p["keys"]
    if isinstance(keys, str):
        keys = [keys]
    pyautogui.hotkey(*keys)


def image_kind(data) -> Optional[str]:
    """按文件头判断图片格式，认不出返回 None。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:4] == b"GIF8":
        return "gif"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1"):
        return "heic"
    return None


def clean_stem(name) -> str:
    """取文件名主干，不允许的字符换成 _，去掉开头的点。"""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]", "_", stem)
    return re.sub(r"\.{2,}", ".", stem).lstrip(".")[:60]


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


def save_image(data, name=None) -> str:
    """图片存进 outbox，返回最终文件名；扩展名以内容为准，HEIC 转成 JPEG，重名加 -1、-2。"""
    kind = image_kind(data)
    if kind is None:
        raise ValueError("不是 JPEG / PNG / GIF / HEIC 图片")
    stem = clean_stem(name) or time.strftime("img-%Y%m%d-%H%M%S")
    ext = "jpg" if kind == "heic" else kind
    os.makedirs(OUTBOX, exist_ok=True)
    file, n = f"{stem}.{ext}", 0
    while os.path.lexists(os.path.join(OUTBOX, file)):
        n += 1
        file = f"{stem}-{n}.{ext}"
    path = outbox_path(file)
    if kind == "heic":
        with tempfile.NamedTemporaryFile(suffix=".heic") as tmp:
            tmp.write(data)
            tmp.flush()
            subprocess.run(["sips", "-s", "format", "jpeg", tmp.name, "--out", path],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        with open(path, "xb") as f:
            f.write(data)
    return file


def image_info(file) -> dict:
    path = outbox_path(file)
    w, h = sips_size(path)
    return {"file": file, "width": w, "height": h, "bytes": os.path.getsize(path)}


# wx-send.sh 的退出码
WX_STATUS = {
    0: "ok", 2: "not_found", 3: "duplicate_name", 4: "verify_failed", 5: "environment",
    6: "focus_lost", 7: "draft_in_input", 8: "send_failed", 9: "daily_limit",
    10: "unconfirmed_do_not_retry", 64: "bad_request",
}


def run_wx(args, account=None, env=None, timeout=300):
    if not os.path.isfile(WX_SEND):
        raise ValueError(f"找不到 wx-send.sh：{WX_SEND}（用环境变量 WX_SEND 指定）")
    cmd = [WX_SEND] + (["-a", account] if account else []) + args
    # 排队等锁最多 180 秒，首次运行还要编译
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=dict(os.environ, **(env or {})))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def text_arg(p, k):
    v = p.get(k)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"缺少 {k}")
    return v


def a_wechat_send(p):
    to = text_arg(p, "to")
    if bool(p.get("text")) == bool(p.get("image")):
        raise ValueError("text 和 image 要给一个，且只能给一个")
    if p.get("image"):
        path = outbox_path(p["image"])
        if not os.path.isfile(path):
            raise ValueError(f"outbox 里没有 {p['image']}（先用 /wechat/image/upload 或 /wechat/image/clipboard 放进去）")
        args = ["--image", to, path]
    else:
        args = [to, text_arg(p, "text")]
    env = {"WX_DRY_RUN": "1"} if p.get("dry_run") else {}
    # 只清发送前已经交给 Muse 的待处理消息
    ids = [m["id"] for m in load_pending(p.get("account"))["chats"].get(to, {}).get("messages", [])]
    code, out, err = run_wx(args, p.get("account"), env)
    result = {"ok": code == 0, "code": code, "status": WX_STATUS.get(code, "error"), "acked": 0,
              "dry_run": bool(p.get("dry_run")), "output": "\n".join(x for x in (out, err) if x)}
    if code == 0 and not p.get("dry_run") and ids:
        # 消息已经发出去了：清缓存出错也不能报失败，否则 Muse 会重发
        try:
            data = load_pending(p.get("account"))
            n = clear_pending(data, to, max(ids))
            save_pending(p.get("account"), data)
            result["acked"] = n
        except (OSError, ValueError) as e:
            result["ack_error"] = f"消息已发出，但待处理缓存没清掉：{e}"
    return result


def image_env(p) -> dict:
    """images=true 时让 wx-send.sh 读消息时顺便取图。"""
    if not p.get("images"):
        return {}
    n = int(p.get("max_images", 10))
    if not 1 <= n <= 50:
        raise ValueError("max_images 需在 1–50 之间")
    return {"WX_IMAGES": "1", "WX_MAX_IMAGES": str(n)}


def a_wechat_read(p):
    chat = text_arg(p, "chat")
    limit = int(p.get("limit", 20))
    if limit < 1:
        raise ValueError("limit 至少是 1")
    if p.get("images"):
        prune_inbox(inbox_days())
    # 取图每张要右键复制一次，放宽超时
    code, out, err = run_wx(["--read", chat, str(limit)], p.get("account"), image_env(p),
                            timeout=600 if p.get("images") else 300)
    if code != 0:
        return {"ok": False, "code": code, "status": WX_STATUS.get(code, "error"), "output": err or out}
    data = json.loads(out[out.index("{"):])
    return {"ok": True, "code": 0, "status": "ok", **data}


def wx_json(code, out, err):
    if code != 0:
        return {"ok": False, "code": code, "status": WX_STATUS.get(code, "error"), "output": err or out}
    return {"ok": True, "code": 0, "status": "ok", **json.loads(out[out.index("{"):])}


def a_wechat_unread(p):
    env = {}
    for k, var, hi in (("max_chats", "WX_UNREAD_MAX_CHATS", 50), ("max_messages", "WX_UNREAD_MAX_MSGS", 200)):
        if k in p:
            n = int(p[k])
            if not 1 <= n <= hi:
                raise ValueError(f"{k} 需在 1–{hi} 之间")
            env[var] = str(n)
    env.update(image_env(p))
    args = ["--unread"] + (["--list-only"] if p.get("list_only") else [])
    if p.get("images"):
        prune_inbox(inbox_days())
    # 要逐个点开聊天、点头像识别发送人，未读多时会很久
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


def a_wechat_forget(p):
    return wx_json(*run_wx(["--forget", text_arg(p, "chat")], p.get("account")))


def a_wechat_prune(p):
    days = int(p.get("days", 3))
    if days < 0:
        raise ValueError("days 不能是负数")
    return wx_json(*run_wx(["--prune", str(days)], p.get("account")))


def a_wechat_friends(p):
    args = ["--friends"] + (["--accept"] if p.get("accept") else [])
    # 每条申请之间随机等 3–8 秒，通过后还要打招呼
    return wx_json(*run_wx(args, p.get("account"), timeout=900))


def a_wechat_image_upload(p):
    try:
        data = base64.b64decode(text_arg(p, "data"), validate=True)
    except ValueError:   # 包括 binascii.Error 和非 ASCII 字符
        raise ValueError("data 不是合法的 base64")
    return {"ok": True, **image_info(save_image(data, p.get("name")))}


def clipboard_has_file():
    """剪贴板里是不是 Finder 复制的文件（这时能转出来的图片只是文件图标）。"""
    info = subprocess.run(["osascript", "-e", "clipboard info"], capture_output=True, text=True, timeout=10).stdout
    return "«class furl»" in info


def a_wechat_image_clipboard(p):
    if clipboard_has_file():
        raise ValueError("剪贴板里是 Finder 复制的文件，不是图片本身。要发这个文件，把它放进 outbox 再按文件名发送")
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        # 剪贴板里没有图片时，第一行就会报错退出
        subprocess.run(["osascript", "-e", "on run argv",
                        "-e", "set d to the clipboard as «class PNGf»",
                        "-e", "set f to open for access (POSIX file (item 1 of argv)) with write permission",
                        "-e", "write d to f", "-e", "close access f", "-e", "end run", tmp],
                       capture_output=True, timeout=30)
        with open(tmp, "rb") as f:
            data = f.read()
    finally:
        os.remove(tmp)
    if image_kind(data) != "png":
        raise ValueError("剪贴板里没有图片（Finder 里复制的文件不算，要复制图片本身）")
    return {"ok": True, **image_info(save_image(data, p.get("name") or time.strftime("clip-%Y%m%d-%H%M%S")))}


def list_images():
    images = []
    for f in os.listdir(OUTBOX) if os.path.isdir(OUTBOX) else []:
        try:
            info = image_info(f)
        except (ValueError, subprocess.CalledProcessError):
            continue
        info["mtime"] = int(os.path.getmtime(outbox_path(f)))
        images.append(info)
    images.sort(key=lambda i: (-i["mtime"], i["file"]))
    return {"ok": True, "images": images}


def thumbnail(file):
    """outbox 里图片的缩略图：长边 512 的 JPEG。"""
    path = outbox_path(file)
    if not os.path.isfile(path):
        raise ValueError(f"outbox 里没有 {file}")
    fd, out = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        subprocess.run(["sips", "-Z", "512", "-s", "format", "jpeg", "-s", "formatOptions", "70", path, "--out", out],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(out, "rb") as f:
            return f.read()
    finally:
        os.remove(out)


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
        path = os.path.join(INBOX, f)
        if f.startswith("in-") and f not in keep and os.path.getmtime(path) < cutoff:
            os.remove(path)


def inbox_days() -> int:
    v = os.environ.get("WX_INBOX_DAYS", "3")
    if not v.isdigit():
        raise ValueError(f"WX_INBOX_DAYS 必须是非负整数：{v!r}")
    return int(v)


ACTIONS = {
    "click": a_click, "move": a_move, "drag": a_drag,
    "scroll": a_scroll, "type": a_type, "key": a_key,
    "wechat/send": a_wechat_send, "wechat/read": a_wechat_read,
    "wechat/unread": a_wechat_unread, "wechat/forget": a_wechat_forget, "wechat/prune": a_wechat_prune,
    "wechat/friends": a_wechat_friends, "wechat/pending": a_wechat_pending, "wechat/ack": a_wechat_ack,
    "wechat/image/upload": a_wechat_image_upload, "wechat/image/clipboard": a_wechat_image_clipboard,
}


class Handler(BaseHTTPRequestHandler):
    server_version = f"muse-intel-mac-bridge/{__version__}"

    def _authed(self):
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got.encode(), f"Bearer {TOKEN}".encode())

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        path = urlparse(self.path).path
        try:
            if path == "/screenshot":
                with lock:
                    data, w, h = take_screenshot()
                return self._send(200, data, "image/jpeg",
                                  {"X-Image-Width": w, "X-Image-Height": h})
            if path == "/wechat/images":
                return self._send(200, list_images())
            if path == "/wechat/inbox/file":
                file = parse_qs(urlparse(self.path).query).get("file", [""])[0]
                return self._send(200, *inbox_file(file))
            if path == "/wechat/image/thumb":
                file = parse_qs(urlparse(self.path).query).get("file", [""])[0]
                return self._send(200, thumbnail(file), "image/jpeg")
            if path == "/info":
                pw, ph = pyautogui.size()
                return self._send(200, {
                    "version": __version__,
                    "image": [state["img_w"], state["img_h"]],
                    "screen_points": [pw, ph],
                    "actions": sorted(ACTIONS),
                })
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        action = ACTIONS.get(urlparse(self.path).path.strip("/"))
        if action is None:
            return self._send(404, {"error": "unknown action"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > IMAGE_MAX * 4 // 3 + 65536:
                # 分块读掉丢弃：不读完就回复的话，客户端常收到连接重置而不是 413
                while length > 0:
                    chunk = self.rfile.read(min(length, 65536))
                    if not chunk:
                        break
                    length -= len(chunk)
                return self._send(413, {"error": f"请求太大，图片最大 {IMAGE_MAX // 1048576} MB（WX_IMAGE_MAX_MB）"})
            payload = json.loads(self.rfile.read(length) or b"{}")
            with lock:
                result = action(payload)
            return self._send(200, result or {"ok": True})
        except pyautogui.FailSafeException:
            return self._send(409, {"error": "failsafe triggered: mouse is in a screen corner"})
        except Exception as e:
            return self._send(400, {"error": str(e)})

    def log_message(self, fmt, *args):
        print("[bridge]", self.address_string(), fmt % args, flush=True)


def main():
    if len(TOKEN) < 16:
        raise SystemExit("请设置 AGENT_TOKEN（至少 16 位随机字符）/ set AGENT_TOKEN (>= 16 chars)")
    print(f"muse-intel-mac-bridge {__version__} listening on http://{HOST}:{PORT} "
          f"(screenshot width {TARGET_W})", flush=True)
    os.makedirs(OUTBOX, exist_ok=True)
    os.makedirs(INBOX, exist_ok=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

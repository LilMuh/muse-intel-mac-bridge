#!/usr/bin/env python3
"""
muse-intel-mac-bridge · 客户端（在 agent 的 Linux VM 里运行，只依赖 Python 标准库）

先设置环境变量：
  export MAB_URL=https://你的mac.xxx.ts.net
  export MAB_TOKEN=你的token

用法：
  python3 mab.py info
  python3 mab.py screenshot [-o screen.jpg]
  python3 mab.py click X Y [--right] [--double]
  python3 mab.py move X Y
  python3 mab.py drag X Y TO_X TO_Y
  python3 mab.py scroll AMOUNT [X Y]        # 正数向上，负数向下
  python3 mab.py type "要输入的文字"
  python3 mab.py key command c              # 组合键，例如 command+c
  python3 mab.py wechat-read "联系人" [-n 20] [--images] [-a work]
  python3 mab.py wechat-send "联系人" "消息" [--dry-run] [-a work]
  python3 mab.py wechat-send "联系人" --image 图片 [--dry-run] [-a work]   # VM 里的路径会先上传；否则当作 outbox 里的文件名
  python3 mab.py wechat-unread [--list-only] [--images] [-a work]   # --images：取图片，下载到 ./wechat-images，消息里加 local_path
  python3 mab.py wechat-forget "联系人" [-a work]
  python3 mab.py wechat-prune [--days 3] [-a work]
  python3 mab.py wechat-friends [--accept] [-a work]
  python3 mab.py wechat-upload 图片路径 [--name a.jpg]
  python3 mab.py wechat-clip [--name x.png]          # Mac 剪贴板里的图片存进 outbox
  python3 mab.py wechat-images                       # 列出 outbox 里的图片
  python3 mab.py wechat-thumb a.jpg [-o thumb.jpg]   # 下载缩略图

坐标 = 最近一次 screenshot 图片上的像素坐标。
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("MAB_URL", "").rstrip("/")
TOKEN = os.environ.get("MAB_TOKEN", "")
TIMEOUT = float(os.environ.get("MAB_TIMEOUT", "30"))


def request(method, path, payload=None, timeout=TIMEOUT, fatal=True):
    if not URL or not TOKEN:
        sys.exit("请先设置 MAB_URL 和 MAB_TOKEN 环境变量 / set MAB_URL and MAB_TOKEN")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}: {e.read().decode(errors='replace')}"
    except urllib.error.URLError as e:
        msg = f"连接失败 / connection failed: {e.reason}"
    if fatal:
        sys.exit(msg)
    raise RuntimeError(msg)


def post(path, payload, timeout=TIMEOUT):
    body, _ = request("POST", path, payload, timeout)
    print(body.decode())


def upload(path, name=None):
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    body, _ = request("POST", "/wechat/image/upload",
                      {"name": name or os.path.basename(path), "data": data}, max(TIMEOUT, 120))
    return json.loads(body)


def fetch_images(result, save_dir):
    """把结果里带 image 的消息对应的原图下载到 save_dir，并加上 local_path；单张失败记 download_error。"""
    msgs = result.get("items", []) + [m for c in result.get("chats", []) for m in c.get("messages", [])]
    for m in msgs:
        if not m.get("image"):
            continue
        # 任何一张出错都只记在这条消息上，保证最后能打印结果（Mac 端已经推进了读取进度）
        try:
            body, _ = request("GET", "/wechat/inbox/file?file=" + urllib.parse.quote(m["image"]),
                              timeout=max(TIMEOUT, 120), fatal=False)
            os.makedirs(save_dir, exist_ok=True)
            path = os.path.abspath(os.path.join(save_dir, os.path.basename(m["image"])))
            with open(path, "wb") as f:
                f.write(body)
        except Exception as e:
            m["download_error"] = str(e)
            continue
        m["local_path"] = path


def post_read(path, p, a, timeout):
    """read / unread：带 --images 时先下载图片再打印。"""
    if not a.images:
        return post(path, p, timeout)
    p.update(images=True, max_images=a.max_images)
    body, _ = request("POST", path, p, timeout)
    result = json.loads(body)
    fetch_images(result, a.save_dir)
    print(json.dumps(result, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="Control a Mac running mac_agent_server.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info")
    s = sub.add_parser("screenshot"); s.add_argument("-o", "--output", default="screen.jpg")
    c = sub.add_parser("click"); c.add_argument("x", type=int); c.add_argument("y", type=int)
    c.add_argument("--right", action="store_true"); c.add_argument("--double", action="store_true")
    m = sub.add_parser("move"); m.add_argument("x", type=int); m.add_argument("y", type=int)
    d = sub.add_parser("drag")
    for k in ("x", "y", "to_x", "to_y"):
        d.add_argument(k, type=int)
    sc = sub.add_parser("scroll"); sc.add_argument("amount", type=int)
    sc.add_argument("x", type=int, nargs="?"); sc.add_argument("y", type=int, nargs="?")
    t = sub.add_parser("type"); t.add_argument("text")
    k = sub.add_parser("key"); k.add_argument("keys", nargs="+")
    wr = sub.add_parser("wechat-read"); wr.add_argument("chat")
    wr.add_argument("-n", "--limit", type=int, default=20); wr.add_argument("-a", "--account")
    ws = sub.add_parser("wechat-send"); ws.add_argument("to"); ws.add_argument("text", nargs="?")
    ws.add_argument("--image", help="VM 里的图片路径（先上传），或 outbox 里的文件名")
    ws.add_argument("--dry-run", action="store_true"); ws.add_argument("-a", "--account")
    wu = sub.add_parser("wechat-unread"); wu.add_argument("--list-only", action="store_true")
    wu.add_argument("--max-chats", type=int); wu.add_argument("--max-messages", type=int); wu.add_argument("-a", "--account")
    for sp in (wr, wu):
        sp.add_argument("--images", action="store_true", help="顺便取图片，下载到 --save-dir")
        sp.add_argument("--max-images", type=int, default=10)
        sp.add_argument("--save-dir", default="wechat-images")
    wf = sub.add_parser("wechat-forget"); wf.add_argument("chat"); wf.add_argument("-a", "--account")
    wp = sub.add_parser("wechat-prune"); wp.add_argument("--days", type=int, default=3); wp.add_argument("-a", "--account")
    wfr = sub.add_parser("wechat-friends"); wfr.add_argument("--accept", action="store_true"); wfr.add_argument("-a", "--account")
    wup = sub.add_parser("wechat-upload"); wup.add_argument("path"); wup.add_argument("--name")
    wc = sub.add_parser("wechat-clip"); wc.add_argument("--name")
    sub.add_parser("wechat-images")
    wt = sub.add_parser("wechat-thumb"); wt.add_argument("file"); wt.add_argument("-o", "--output", default="thumb.jpg")

    a = ap.parse_args()
    if a.cmd == "info":
        body, _ = request("GET", "/info"); print(body.decode())
    elif a.cmd == "screenshot":
        body, headers = request("GET", "/screenshot")
        with open(a.output, "wb") as f:
            f.write(body)
        w = headers.get("X-Image-Width"); h = headers.get("X-Image-Height")
        print(f"saved {a.output} ({w}x{h}, {len(body) // 1024} KB)")
    elif a.cmd == "click":
        post("/click", {"x": a.x, "y": a.y, "button": "right" if a.right else "left",
                        "clicks": 2 if a.double else 1})
    elif a.cmd == "move":
        post("/move", {"x": a.x, "y": a.y})
    elif a.cmd == "drag":
        post("/drag", {"x": a.x, "y": a.y, "to_x": a.to_x, "to_y": a.to_y})
    elif a.cmd == "scroll":
        p = {"amount": a.amount}
        if a.x is not None and a.y is not None:
            p.update(x=a.x, y=a.y)
        post("/scroll", p)
    elif a.cmd == "type":
        post("/type", {"text": a.text})
    elif a.cmd == "key":
        post("/key", {"keys": a.keys})
    elif a.cmd == "wechat-read":
        # 微信操作要排队（最多 180 秒），取图还要逐张右键复制，超时放宽
        post_read("/wechat/read", {"chat": a.chat, "limit": a.limit, "account": a.account}, a,
                  max(TIMEOUT, 620 if a.images else 320))
    elif a.cmd == "wechat-send":
        if (a.text is None) == (a.image is None):
            sys.exit("文字和 --image 要给一个，且只能给一个")
        p = {"to": a.to, "dry_run": a.dry_run, "account": a.account}
        if a.image is None:
            p["text"] = a.text
        elif os.path.isfile(a.image):
            p["image"] = upload(a.image)["file"]
            # 重名时服务端会改名，重试要用这个名字
            print(f"已上传为 outbox 里的 {p['image']}（重试时用 --image {p['image']}）", file=sys.stderr)
        elif "/" in a.image:
            sys.exit(f"找不到文件：{a.image}")
        else:
            p["image"] = a.image   # 当作 outbox 里的文件名
        post("/wechat/send", p, max(TIMEOUT, 320))
    elif a.cmd == "wechat-unread":
        p = {"list_only": a.list_only, "account": a.account}
        if a.max_chats: p["max_chats"] = a.max_chats
        if a.max_messages: p["max_messages"] = a.max_messages
        post_read("/wechat/unread", p, a, max(TIMEOUT, 1900))
    elif a.cmd == "wechat-forget":
        post("/wechat/forget", {"chat": a.chat, "account": a.account})
    elif a.cmd == "wechat-prune":
        post("/wechat/prune", {"days": a.days, "account": a.account})
    elif a.cmd == "wechat-friends":
        post("/wechat/friends", {"accept": a.accept, "account": a.account}, max(TIMEOUT, 920))
    elif a.cmd == "wechat-upload":
        print(json.dumps(upload(a.path, a.name), ensure_ascii=False))
    elif a.cmd == "wechat-clip":
        post("/wechat/image/clipboard", {"name": a.name})
    elif a.cmd == "wechat-images":
        body, _ = request("GET", "/wechat/images"); print(body.decode())
    elif a.cmd == "wechat-thumb":
        body, _ = request("GET", "/wechat/image/thumb?file=" + urllib.parse.quote(a.file))
        with open(a.output, "wb") as f:
            f.write(body)
        print(f"saved {a.output} ({len(body) // 1024} KB)")


if __name__ == "__main__":
    main()

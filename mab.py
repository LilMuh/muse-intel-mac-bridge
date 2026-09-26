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
  python3 mab.py wechat-read "联系人" [-n 20] [-a work]
  python3 mab.py wechat-send "联系人" "消息" [--dry-run] [-a work]
  python3 mab.py wechat-unread [--list-only] [-a work]
  python3 mab.py wechat-forget "联系人" [-a work]
  python3 mab.py wechat-prune [--days 3] [-a work]
  python3 mab.py wechat-friends [--accept] [-a work]

坐标 = 最近一次 screenshot 图片上的像素坐标。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("MAB_URL", "").rstrip("/")
TOKEN = os.environ.get("MAB_TOKEN", "")
TIMEOUT = float(os.environ.get("MAB_TIMEOUT", "30"))


def request(method, path, payload=None, timeout=TIMEOUT):
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
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"连接失败 / connection failed: {e.reason}")


def post(path, payload, timeout=TIMEOUT):
    body, _ = request("POST", path, payload, timeout)
    print(body.decode())


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
    ws = sub.add_parser("wechat-send"); ws.add_argument("to"); ws.add_argument("text")
    ws.add_argument("--dry-run", action="store_true"); ws.add_argument("-a", "--account")
    wu = sub.add_parser("wechat-unread"); wu.add_argument("--list-only", action="store_true")
    wu.add_argument("--max-chats", type=int); wu.add_argument("--max-messages", type=int); wu.add_argument("-a", "--account")
    wf = sub.add_parser("wechat-forget"); wf.add_argument("chat"); wf.add_argument("-a", "--account")
    wp = sub.add_parser("wechat-prune"); wp.add_argument("--days", type=int, default=3); wp.add_argument("-a", "--account")
    wfr = sub.add_parser("wechat-friends"); wfr.add_argument("--accept", action="store_true"); wfr.add_argument("-a", "--account")

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
        # 微信操作要排队（最多 180 秒），超时放宽
        post("/wechat/read", {"chat": a.chat, "limit": a.limit, "account": a.account}, max(TIMEOUT, 320))
    elif a.cmd == "wechat-send":
        post("/wechat/send", {"to": a.to, "text": a.text, "dry_run": a.dry_run, "account": a.account}, max(TIMEOUT, 320))
    elif a.cmd == "wechat-unread":
        p = {"list_only": a.list_only, "account": a.account}
        if a.max_chats: p["max_chats"] = a.max_chats
        if a.max_messages: p["max_messages"] = a.max_messages
        post("/wechat/unread", p, max(TIMEOUT, 1900))
    elif a.cmd == "wechat-forget":
        post("/wechat/forget", {"chat": a.chat, "account": a.account})
    elif a.cmd == "wechat-prune":
        post("/wechat/prune", {"days": a.days, "account": a.account})
    elif a.cmd == "wechat-friends":
        post("/wechat/friends", {"accept": a.accept, "account": a.account}, max(TIMEOUT, 920))


if __name__ == "__main__":
    main()

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
  POST /wechat/read   {"chat":"联系人","limit":20,"account":"work"}
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
"""
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

__version__ = "0.2.0"

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

pyautogui.FAILSAFE = True  # 把鼠标甩到屏幕左上角可紧急中断 agent 的操作
pyautogui.PAUSE = 0.05

lock = threading.Lock()
state = {"img_w": None, "img_h": None}


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
        info = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", out],
            check=True, capture_output=True, text=True,
        ).stdout
        w = int(info.split("pixelWidth:")[1].split()[0])
        h = int(info.split("pixelHeight:")[1].split()[0])
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


# wx-send.sh 的退出码
WX_STATUS = {
    0: "ok", 2: "not_found", 3: "duplicate_name", 4: "verify_failed", 5: "environment",
    6: "focus_lost", 7: "draft_in_input", 8: "send_failed", 9: "daily_limit",
    10: "unconfirmed_do_not_retry", 64: "bad_request",
}


def run_wx(args, account=None, env=None):
    if not os.path.isfile(WX_SEND):
        raise ValueError(f"找不到 wx-send.sh：{WX_SEND}（用环境变量 WX_SEND 指定）")
    cmd = [WX_SEND] + (["-a", account] if account else []) + args
    # 排队等锁最多 180 秒，首次运行还要编译
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=dict(os.environ, **(env or {})))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def text_arg(p, k):
    v = p.get(k)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"缺少 {k}")
    return v


def a_wechat_send(p):
    to, text = text_arg(p, "to"), text_arg(p, "text")
    env = {"WX_DRY_RUN": "1"} if p.get("dry_run") else {}
    code, out, err = run_wx([to, text], p.get("account"), env)
    return {"ok": code == 0, "code": code, "status": WX_STATUS.get(code, "error"),
            "dry_run": bool(p.get("dry_run")), "output": "\n".join(x for x in (out, err) if x)}


def a_wechat_read(p):
    chat = text_arg(p, "chat")
    limit = int(p.get("limit", 20))
    if not 1 <= limit <= 200:
        raise ValueError("limit 需在 1–200 之间")
    code, out, err = run_wx(["--read", chat, str(limit)], p.get("account"))
    if code != 0:
        return {"ok": False, "code": code, "status": WX_STATUS.get(code, "error"), "output": err or out}
    data = json.loads(out[out.index("{"):])
    return {"ok": True, "code": 0, "status": "ok", **data}


ACTIONS = {
    "click": a_click, "move": a_move, "drag": a_drag,
    "scroll": a_scroll, "type": a_type, "key": a_key,
    "wechat/send": a_wechat_send, "wechat/read": a_wechat_read,
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
            if path == "/info":
                pw, ph = pyautogui.size()
                return self._send(200, {
                    "version": __version__,
                    "image": [state["img_w"], state["img_h"]],
                    "screen_points": [pw, ph],
                    "actions": sorted(ACTIONS),
                })
            return self._send(404, {"error": "not found"})
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
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

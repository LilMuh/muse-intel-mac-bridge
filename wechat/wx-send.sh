#!/usr/bin/env bash
# wx-send.sh — 用命令行给 Mac 版微信发消息、读消息（v3：读辅助功能 AX 原文，截图识别只作备用）
#
# 用法：
#   ./wx-send.sh -a work "联系人或群名" "消息内容"     发送
#   ./wx-send.sh -a work --batch 名单.tsv            批量发送（每行：联系人<Tab>消息，消息里的 \n 表示换行）
#   ./wx-send.sh -a work --read "联系人" [条数]         读取聊天记录（JSON，默认最近 20 条，只含当前加载出来的）
#   ./wx-send.sh -a work --unread                        列出未读聊天（JSON；免打扰的只列 WX_MUTED_ALLOW 里的群）
#   ./wx-send.sh -a work --dump                      调试：打印识别到的标题、搜索结果、输入框、聊天底部
#   ./wx-send.sh --list                                  列出所有微信，依次切到前台帮你辨认
#   ./wx-send.sh --log [行数]                             查看最近的发送日志
#   只开了一个微信时可以省略 -a
#
# 账号：在环境变量或仓库根目录的 .env 里配置 WX_ACCOUNTS（别名=App 路径，多个用逗号分隔），例如
#   WX_ACCOUNTS=work=/Applications/WeChat.app,home=/Applications/WeChat2.app
#   只开一个微信时可以不配置
#
# 流程：检查环境 → 排队拿锁 → 防风控等待 → 切到微信
#      → 当前聊天就是目标？是则跳过搜索；否则搜索，识别结果，点击名字完全一致的那一项
#      → 核对聊天标题（完全一致）→ 确认输入框为空 → 粘贴 → 确认消息在输入框里
#      → 再核对一次标题 → 发送 → 在聊天里确认消息已出现、没有红色感叹号
#      → 恢复剪贴板（含图片）和鼠标位置 → 写日志
#
# 环境变量（都可不设）：
#   WX_DRY_RUN=1        只粘贴不发送
#   WX_TIMEOUT=5        等搜索结果 / 等聊天打开的最长秒数
#   WX_MIN_INTERVAL=3   同一账号两次发送的最小间隔（秒）
#   WX_JITTER=2         额外随机等待 0~N 秒
#   WX_DAILY_MAX=500    每个账号每天最多发送条数
#   WX_NO_CONFIRM=1     发送后不做确认（快约 0.3 秒）
#   WX_TITLE_MINX=270   聊天区域左边界（相对窗口，单位：点）
#   WX_LIST_W=420       搜索下拉框识别宽度
#   WX_INPUT_H=118      输入框文字区高度（不含上方工具栏）
#   WX_TOOLBAR_H=40     输入框上方工具栏高度
#   WX_LOG=路径          日志位置，默认 ~/Library/Logs/wx-send.log
#
# 退出码：0 成功 · 2 没搜到 · 3 同名 · 4 标题/输入校验失败 · 5 环境或窗口问题
#        6 焦点被切走 · 7 输入框有草稿 · 8 发送失败 · 9 超出每日上限 · 10 无法确认是否发出（不要直接重试）
#
# 权限（给运行它的终端，比如 Ghostty）：辅助功能、屏幕录制；首次运行时允许「自动化」弹窗。
# 首次运行会编译一次（需要 Xcode 命令行工具：xcode-select --install）。
set -euo pipefail

CACHE="${HOME}/.cache/wx-send"
LOG="${WX_LOG:-$HOME/Library/Logs/wx-send.log}"
BIN="$CACHE/wxsend"
DIR="$(cd "$(dirname "$0")" && pwd)"

# ---------- 账号：WX_ACCOUNTS 优先取环境变量，没有就读仓库根目录的 .env ----------
if [[ -z "${WX_ACCOUNTS:-}" && -f "$DIR/../.env" ]]; then
  WX_ACCOUNTS="$(sed -n 's/^WX_ACCOUNTS=//p' "$DIR/../.env" | tail -n 1 | sed "s/^[\"']//; s/[\"']\$//")"
fi
if [[ -z "${WX_MUTED_ALLOW:-}" && -f "$DIR/../.env" ]]; then
  WX_MUTED_ALLOW="$(sed -n 's/^WX_MUTED_ALLOW=//p' "$DIR/../.env" | tail -n 1 | sed "s/^[\"']//; s/[\"']\$//")"
fi
export WX_MUTED_ALLOW="${WX_MUTED_ALLOW:-}"
ACCOUNTS=()
[[ -n "${WX_ACCOUNTS:-}" ]] && IFS=',' read -ra ACCOUNTS <<< "$WX_ACCOUNTS"

trim() { sed 's/^[[:space:]]*//; s/[[:space:]]*$//' <<< "$1"; }

# 别名 → App 路径
account() {
  local e
  for e in ${ACCOUNTS[@]+"${ACCOUNTS[@]}"}; do
    [[ "$(trim "${e%%=*}")" == "$1" ]] && { trim "${e#*=}"; return 0; }
  done
  return 1
}
# App 路径 → 别名
alias_of() {
  local e
  for e in ${ACCOUNTS[@]+"${ACCOUNTS[@]}"}; do
    [[ "$(trim "${e#*=}")" == "$1" ]] && { trim "${e%%=*}"; return 0; }
  done
  return 1
}
aliases() {
  local e out=""
  for e in ${ACCOUNTS[@]+"${ACCOUNTS[@]}"}; do out+="${out:+、}$(trim "${e%%=*}")"; done
  echo "${out:-无}"
}

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 64; }

build() {
  mkdir -p "$CACHE"
  local src="$CACHE/wxsend.swift" new="$CACHE/wxsend.new.swift"
  swift_source > "$new"
  if cmp -s "$new" "$src" && [[ -x "$BIN" ]]; then rm -f "$new"; return; fi
  command -v swiftc >/dev/null || { echo "缺少 swiftc，请先运行：xcode-select --install" >&2; exit 1; }
  echo "正在编译（只在首次或脚本更新后进行，约 20–60 秒）…" >&2
  # 编译成功才更新缓存的源码，否则下次会以为没变而运行旧程序
  if swiftc -O -target "$(uname -m)-apple-macos13.0" "$new" -o "$BIN.tmp" >&2; then mv "$BIN.tmp" "$BIN"; mv "$new" "$src"
  else rm -f "$new"; echo "编译失败，请把上面的报错发给我" >&2; exit 1; fi
}

# ---------- 参数 ----------
ALIAS=""
if [[ "${1:-}" == "-a" ]]; then
  [[ $# -ge 2 ]] || usage
  ALIAS="$2"; shift 2
fi

case "${1:-}" in
  "") usage ;;
  --log)
    [[ -f "$LOG" ]] || { echo "还没有日志"; exit 0; }
    tail -n "${2:-20}" "$LOG"; exit 0 ;;
  --list)
    pids=$(pgrep -x WeChat || true)
    [[ -z "$pids" ]] && { echo "没有找到正在运行的微信"; exit 1; }
    for p in $pids; do
      echo "PID $p   启动于 $(ps -p "$p" -o lstart=)"
      echo "         $(ps -p "$p" -o command=)"
      osascript -e "tell application \"System Events\" to set frontmost of (first process whose unix id is $p) to true" >/dev/null
      echo "         ↑ 这个微信已切到前台，看看是哪个号"
      sleep 3
    done
    exit 0 ;;
esac

# ---------- 确定用哪个微信 ----------
if [[ -n "$ALIAS" ]]; then
  APP="$(account "$ALIAS")" || { echo "未知账号：${ALIAS}（已配置：$(aliases)。在 .env 的 WX_ACCOUNTS 里配置）" >&2; exit 64; }
  [[ -d "$APP" ]] || { echo "账号 ${ALIAS} 的 App 不存在：${APP}" >&2; exit 64; }
  NAME="$ALIAS"
else
  pids=($(pgrep -x WeChat || true))
  case ${#pids[@]} in
    0) echo "没有找到正在运行的微信" >&2; exit 5 ;;
    1) APP="$(ps -p "${pids[0]}" -o command= | sed 's#/Contents/MacOS/.*##')"
       NAME="$(alias_of "$APP" || basename "$APP" .app)" ;;
    *) echo "检测到 ${#pids[@]} 个微信，请用 -a 指定账号（已配置：$(aliases)。在 .env 的 WX_ACCOUNTS 里配置）" >&2; exit 64 ;;
  esac
fi

swift_source() {
cat <<'SWIFT'
import Foundation
import AppKit
import Vision
import ImageIO
import CoreGraphics
import ApplicationServices
import CoreServices
import ScreenCaptureKit

// MARK: - 配置

let env = ProcessInfo.processInfo.environment
func envD(_ k: String, _ d: Double) -> Double { Double(env[k] ?? "") ?? d }
let DRY_RUN = env["WX_DRY_RUN"] == "1"
let TIMEOUT = envD("WX_TIMEOUT", 5)
let MIN_INTERVAL = envD("WX_MIN_INTERVAL", 3)
let JITTER = max(0, envD("WX_JITTER", 2))
let DAILY_MAX = Int(envD("WX_DAILY_MAX", 500))
let CONFIRM = env["WX_NO_CONFIRM"] != "1"
let TITLE_MINX = CGFloat(envD("WX_TITLE_MINX", 270))
let TITLE_H = CGFloat(envD("WX_TITLE_H", 80))
let LIST_W = CGFloat(envD("WX_LIST_W", 420))
let INPUT_H = CGFloat(envD("WX_INPUT_H", 118))      // 输入文字区（不含上方工具栏）
let TOOLBAR_H = CGFloat(envD("WX_TOOLBAR_H", 40))   // 输入框上方的工具栏（表情、文件等图标）
let HOME = NSHomeDirectory()
let CACHE = HOME + "/.cache/wx-send"
let LOG_PATH = env["WX_LOG"] ?? (HOME + "/Library/Logs/wx-send.log")

func eprint(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }
func say(_ s: String) { FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!) }
func sleepS(_ s: Double) { if s > 0 { Thread.sleep(forTimeInterval: min(s, 600)) } }

struct WXError: Error { let code: Int32; let tag: String; let msg: String }
func fail(_ code: Int32, _ tag: String, _ msg: String) -> WXError { WXError(code: code, tag: tag, msg: msg) }

func waitUntil(_ timeout: Double, _ interval: Double = 0.05, _ cond: () -> Bool) -> Bool {
    let end = Date().addingTimeInterval(timeout)
    repeat {
        if cond() { return true }
        sleepS(interval)
    } while Date() < end
    return false
}

// MARK: - 文字工具

/// 只保留文字和数字（去掉空格、emoji、括号、标点），用于比较
func norm(_ s: String) -> String {
    String(String.UnicodeScalarView(s.unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }))
}
/// 去掉群名后面的人数，如「项目群 (11)」→「项目群」
func stripCount(_ s: String) -> String {
    s.replacingOccurrences(of: #"\s*[(（]\s*\d+\s*[)）]\s*$"#, with: "", options: .regularExpression)
}
func oneLine(_ s: String) -> String {
    s.replacingOccurrences(of: "\n", with: "⏎").replacingOccurrences(of: "\t", with: " ")
}
func preview(_ s: String, _ n: Int = 40) -> String {
    let o = oneLine(s)
    return o.count > n ? String(o.prefix(n)) + "…" : o
}
func titleMatches(_ raw: String, _ target: String) -> Bool {
    let t = norm(stripCount(raw)), g = norm(stripCount(target))
    if t.isEmpty || g.isEmpty { return false }
    if t == g { return true }
    // 名字太长被截断成「…」时：至少 6 个字一致才算
    if raw.hasSuffix("…") || raw.hasSuffix("...") { return t.count >= 6 && g.hasPrefix(t) }
    return false
}

// MARK: - 日志 / 锁 / 防风控

let tsFmt: DateFormatter = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm:ss"; return f }()
let dayFmt: DateFormatter = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; return f }()

/// 日志每行：时间 账号 联系人 状态 耗时 详情 消息预览（Tab 分隔）
func writeLog(_ account: String, _ contact: String, _ status: String, _ secs: String, _ detail: String, _ msg: String) {
    let line = [tsFmt.string(from: Date()), account, contact, status, secs, detail, preview(msg)]
        .map(oneLine).joined(separator: "\t") + "\n"
    let fm = FileManager.default
    try? fm.createDirectory(atPath: (LOG_PATH as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
    if !fm.fileExists(atPath: LOG_PATH) { fm.createFile(atPath: LOG_PATH, contents: nil) }
    if let h = FileHandle(forWritingAtPath: LOG_PATH) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile()
    }
}

func sentToday(_ account: String) -> Int {
    guard let s = try? String(contentsOfFile: LOG_PATH, encoding: .utf8) else { return 0 }
    let today = dayFmt.string(from: Date())
    return s.split(separator: "\n").filter { l in
        let f = l.split(separator: "\t", omittingEmptySubsequences: false)
        return f.count > 3 && f[0].hasPrefix(today) && String(f[1]) == account && (f[3] == "SENT" || f[3] == "UNCONFIRMED")
    }.count
}

func acquireLock() throws -> Int32 {
    try? FileManager.default.createDirectory(atPath: CACHE, withIntermediateDirectories: true)
    let fd = open(CACHE + "/lock", O_CREAT | O_RDWR, 0o644)
    if fd < 0 { throw fail(5, "LOCK", "无法创建锁文件 \(CACHE)/lock") }
    var announced = false
    let deadline = Date().addingTimeInterval(180)
    while flock(fd, LOCK_EX | LOCK_NB) != 0 {
        if !announced { eprint("⏳ 另一个发送任务正在进行，排队等待…"); announced = true }
        if Date() > deadline { throw fail(5, "LOCK_TIMEOUT", "排队等待超过 180 秒") }
        sleepS(0.2)
    }
    return fd   // 进程退出时自动释放
}

func lastSendFile(_ account: String) -> String { CACHE + "/last-" + account }

func rateLimit(_ account: String) {
    let last = (try? String(contentsOfFile: lastSendFile(account), encoding: .utf8)).flatMap { Double($0) } ?? 0
    let wait = min(MIN_INTERVAL + JITTER, MIN_INTERVAL + Double.random(in: 0...JITTER) - (Date().timeIntervalSince1970 - last))
    if wait > 0 {
        eprint(String(format: "🕐 防风控：距上次发送太近，等待 %.1f 秒", wait))
        sleepS(wait)
    }
}
func lastChatFile(_ account: String) -> String { CACHE + "/lastchat-" + account }
func rememberChat(_ account: String, _ contact: String) {
    try? contact.write(toFile: lastChatFile(account), atomically: true, encoding: .utf8)
}
func rememberedChat(_ account: String) -> String? {
    try? String(contentsOfFile: lastChatFile(account), encoding: .utf8)
}
func markSent(_ account: String) {
    try? String(Date().timeIntervalSince1970).write(toFile: lastSendFile(account), atomically: true, encoding: .utf8)
}

// MARK: - 剪贴板（完整备份：文字、图片、文件、富文本）

final class ClipboardBackup {
    private var items: [[(NSPasteboard.PasteboardType, Data)]] = []
    private var restored = false
    init() {
        for it in NSPasteboard.general.pasteboardItems ?? [] {
            items.append(it.types.compactMap { t in it.data(forType: t).map { (t, $0) } })
        }
    }
    func restore() {
        if restored { return }
        restored = true
        let pb = NSPasteboard.general
        pb.clearContents()
        let objs: [NSPasteboardItem] = items.map { pairs in
            let n = NSPasteboardItem()
            for (t, d) in pairs { n.setData(d, forType: t) }
            return n
        }
        if !objs.isEmpty { pb.writeObjects(objs) }
    }
}
var clipBackup: ClipboardBackup?

// MARK: - 输入事件

func key(_ code: CGKeyCode, cmd: Bool = false) {
    let src = CGEventSource(stateID: .combinedSessionState)
    if cmd { CGEvent(keyboardEventSource: src, virtualKey: 55, keyDown: true)?.post(tap: .cghidEventTap) }
    for down in [true, false] {
        let e = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down)
        if cmd { e?.flags = .maskCommand }
        e?.post(tap: .cghidEventTap)
        usleep(6_000)
    }
    if cmd { CGEvent(keyboardEventSource: src, virtualKey: 55, keyDown: false)?.post(tap: .cghidEventTap) }
    usleep(12_000)
}
let K_A: CGKeyCode = 0, K_F: CGKeyCode = 3, K_V: CGKeyCode = 9, K_RETURN: CGKeyCode = 36, K_ESC: CGKeyCode = 53

func click(_ p: CGPoint) {
    for t: CGEventType in [.mouseMoved, .leftMouseDown, .leftMouseUp] {
        CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: p, mouseButton: .left)?
            .post(tap: .cghidEventTap)
        usleep(t == .mouseMoved ? 25_000 : 35_000)
    }
}
func moveMouse(_ p: CGPoint) {
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?
        .post(tap: .cghidEventTap)
}

func pasteText(_ s: String) {
    let pb = NSPasteboard.general
    pb.clearContents()
    pb.setString(s, forType: .string)
    usleep(20_000)
    key(K_V, cmd: true)
}

// MARK: - 截图 + 文字识别

struct Line { var raw: String; var text: String; var x0: CGFloat; var x1: CGFloat; var cy: CGFloat; var h: CGFloat }

final class Capturer {
    private var content: AnyObject?
    private var sckBroken = false

    func image(_ rect: CGRect) -> CGImage? {
        if !sckBroken, #available(macOS 14.0, *) {
            if let img = sck(rect) { return img }
            sckBroken = true   // 这次运行后面都用系统截图命令
        }
        return shell(rect)
    }

    @available(macOS 14.0, *)
    private func sck(_ rect: CGRect) -> CGImage? {
        if content == nil {
            let sem = DispatchSemaphore(value: 0)
            SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { c, _ in
                self.content = c; sem.signal()
            }
            if sem.wait(timeout: .now() + 2) == .timedOut { return nil }
        }
        guard let c = content as? SCShareableContent else { return nil }
        var did = CGDirectDisplayID(0)
        var count: UInt32 = 0
        CGGetDisplaysWithRect(rect, 1, &did, &count)
        if count == 0 { return nil }
        let db = CGDisplayBounds(did)
        guard db.contains(rect), let display = c.displays.first(where: { $0.displayID == did }) else { return nil }
        let scale = CGFloat(CGDisplayCopyDisplayMode(did)?.pixelWidth ?? Int(db.width)) / db.width
        let cfg = SCStreamConfiguration()
        cfg.sourceRect = CGRect(x: rect.minX - db.minX, y: rect.minY - db.minY, width: rect.width, height: rect.height)
        cfg.width = Int(rect.width * scale)
        cfg.height = Int(rect.height * scale)
        cfg.showsCursor = false
        let filter = SCContentFilter(display: display, excludingWindows: [])
        var img: CGImage?
        let sem = DispatchSemaphore(value: 0)
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg) { i, _ in
            img = i; sem.signal()
        }
        if sem.wait(timeout: .now() + 2) == .timedOut { return nil }
        return img
    }

    private func shell(_ rect: CGRect) -> CGImage? {
        let tmp = NSTemporaryDirectory() + "wxsend-\(getpid()).png"
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        p.arguments = ["-x", "-R\(Int(rect.minX)),\(Int(rect.minY)),\(Int(rect.width)),\(Int(rect.height))", tmp]
        do { try p.run() } catch { return nil }
        p.waitUntilExit()
        defer { try? FileManager.default.removeItem(atPath: tmp) }
        guard let s = CGImageSourceCreateWithURL(URL(fileURLWithPath: tmp) as CFURL, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(s, 0, nil)
    }
}

func recognize(_ img: CGImage, _ rect: CGRect) -> [Line] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = false
    try? VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
    return (req.results ?? []).compactMap { o in
        guard let s = o.topCandidates(1).first?.string else { return nil }
        let b = o.boundingBox
        return Line(raw: s, text: norm(s),
                    x0: rect.minX + b.minX * rect.width, x1: rect.minX + b.maxX * rect.width,
                    cy: rect.minY + (1 - b.midY) * rect.height, h: b.height * rect.height)
    }
}

/// 同一行里被 emoji 等隔开的几段文字合并成一行
func mergeRows(_ lines: [Line]) -> [Line] {
    var rows: [[Line]] = []
    for l in lines.sorted(by: { $0.cy < $1.cy }) {
        if let f = rows.last?.first, abs(f.cy - l.cy) < max(4, min(f.h, l.h) * 0.5) {
            rows[rows.count - 1].append(l)
        } else {
            rows.append([l])
        }
    }
    var out: [Line] = []
    for row in rows {
        let s = row.sorted { $0.x0 < $1.x0 }
        var cur = s[0]
        for l in s.dropFirst() {
            let isTime = l.raw.range(of: #"^(\d{1,2}:\d{2}|昨天|前天|星期.|周.|\d{1,2}/\d{1,2}|\d{2,4}/\d{1,2}/\d{1,2})$"#,
                                     options: .regularExpression) != nil
            if l.x0 - cur.x1 < 24 && !isTime {
                cur.raw += l.raw; cur.text += l.text; cur.x1 = max(cur.x1, l.x1)
            } else {
                out.append(cur); cur = l
            }
        }
        out.append(cur)
    }
    return out.sorted { $0.cy < $1.cy }
}

/// 统计某区域内的红色像素（用于识别「发送失败」的红色感叹号）
func redPixels(_ img: CGImage, _ rect: CGRect, _ box: CGRect) -> Int {
    let s = CGFloat(img.width) / rect.width
    let px = CGRect(x: (box.minX - rect.minX) * s, y: (box.minY - rect.minY) * s,
                    width: box.width * s, height: box.height * s).integral
        .intersection(CGRect(x: 0, y: 0, width: img.width, height: img.height))
    guard !px.isEmpty, let crop = img.cropping(to: px) else { return 0 }
    let w = crop.width, h = crop.height
    var data = [UInt8](repeating: 0, count: w * h * 4)
    let ok: Bool = data.withUnsafeMutableBytes { buf in
        guard let ctx = CGContext(data: buf.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
        ctx.draw(crop, in: CGRect(x: 0, y: 0, width: w, height: h))
        return true
    }
    if !ok { return 0 }
    var n = 0
    for i in stride(from: 0, to: data.count, by: 4) where data[i] > 190 && data[i + 1] < 90 && data[i + 2] < 90 {
        n += 1
    }
    return n
}

// MARK: - 辅助功能（AX）读取：拿到的是界面原文，比截图识别准

func axAttr(_ e: AXUIElement, _ name: String) -> CFTypeRef? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(e, name as CFString, &v) == .success ? v : nil
}
func axStr(_ e: AXUIElement, _ name: String) -> String? { axAttr(e, name) as? String }
func axRole(_ e: AXUIElement) -> String { axStr(e, kAXRoleAttribute) ?? "" }
func axChildren(_ e: AXUIElement) -> [AXUIElement] { (axAttr(e, kAXChildrenAttribute) as? [AXUIElement]) ?? [] }
func axFrame(_ e: AXUIElement) -> CGRect? {
    guard let p = axAttr(e, kAXPositionAttribute), let s = axAttr(e, kAXSizeAttribute),
          CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var pt = CGPoint.zero, sz = CGSize.zero
    AXValueGetValue(p as! AXValue, .cgPoint, &pt)
    AXValueGetValue(s as! AXValue, .cgSize, &sz)
    return CGRect(origin: pt, size: sz)
}
func axParent(_ e: AXUIElement) -> AXUIElement? {
    guard let p = axAttr(e, kAXParentAttribute), CFGetTypeID(p) == AXUIElementGetTypeID() else { return nil }
    return (p as! AXUIElement)
}
/// 深度优先找第一个符合条件的元素；不进入列表内部（聊天记录可能有很多行）
func axFind(_ e: AXUIElement, _ depth: Int = 0, _ match: (AXUIElement) -> Bool) -> AXUIElement? {
    if match(e) { return e }
    if depth > 12 || axRole(e) == "AXList" { return nil }
    for c in axChildren(e) { if let f = axFind(c, depth + 1, match) { return f } }
    return nil
}
/// 两个名字是否完全一样（只忽略首尾空格和群名后的人数）
func sameName(_ a: String, _ b: String) -> Bool {
    let x = stripCount(a.trimmingCharacters(in: .whitespaces)), y = stripCount(b.trimmingCharacters(in: .whitespaces))
    return !x.isEmpty && x == y
}

/// 输入框内容是否就是这条消息：逐字一致，只允许 [微笑] 这类表情代码被微信换成图片占位符 ￼
func sameInput(_ seen: String, _ msg: String) -> Bool {
    if seen == msg { return true }
    let code = try! NSRegularExpression(pattern: #"\[[^\[\]\n]{1,8}\]"#)
    let ns = msg as NSString
    var pattern = #"\A"#, last = 0
    for m in code.matches(in: msg, range: NSRange(location: 0, length: ns.length)) {
        let lit = ns.substring(with: NSRange(location: last, length: m.range.location - last))
        pattern += NSRegularExpression.escapedPattern(for: lit)
        pattern += "(?:" + NSRegularExpression.escapedPattern(for: ns.substring(with: m.range)) + "|\u{FFFC})"
        last = m.range.location + m.range.length
    }
    if last == 0 { return false }
    pattern += NSRegularExpression.escapedPattern(for: ns.substring(from: last)) + #"\z"#
    return seen.range(of: pattern, options: .regularExpression) != nil
}

/// 会话列表里的一行：「名字 [已置顶] [N条未读] 预览 时间[消息免打扰]」
struct ChatRow { var name = ""; var unread = 0; var pinned = false; var muted = false; var preview = ""; var time = "" }

let ROW_TIME = #"\s(\d{1,2}:\d{2}|昨天(?: \d{1,2}:\d{2})?|前天|星期.|\d{1,2}/\d{1,2}|\d{2,4}/\d{1,2}/\d{1,2})$"#

/// 解析会话行。只有带「N条未读」的行才能可靠地拆出名字；免打扰的群要靠白名单里的名字来匹配
func parseChatRow(_ raw: String, allow: [String]) -> ChatRow? {
    var r = ChatRow()
    var t = raw.trimmingCharacters(in: .whitespaces)
    if t.hasSuffix("消息免打扰") { r.muted = true; t = String(t.dropLast(5)).trimmingCharacters(in: .whitespaces) }
    if let m = t.range(of: ROW_TIME, options: .regularExpression) {
        r.time = t[m].trimmingCharacters(in: .whitespaces); t = String(t[..<m.lowerBound])
    }
    if !r.muted, let m = t.range(of: #" (\d+)条未读(?= |$)"#, options: .regularExpression) {
        r.unread = Int(t[m].filter(\.isNumber)) ?? 0
        r.name = String(t[..<m.lowerBound])
        r.preview = String(t[m.upperBound...]).trimmingCharacters(in: .whitespaces)
    } else if r.muted, let n = allow.first(where: { t.hasPrefix($0 + " ") }) {
        r.name = n
        r.preview = String(t.dropFirst(n.count)).trimmingCharacters(in: .whitespaces)
        if let m = r.preview.range(of: #"^(已置顶 )?\[(\d+)条\]"#, options: .regularExpression) {
            r.unread = Int(r.preview[m].filter(\.isNumber)) ?? 0
            r.preview = String(r.preview[m.upperBound...]).trimmingCharacters(in: .whitespaces)
        }
    } else { return nil }
    for tag in [" 已置顶"] where r.name.hasSuffix(tag) { r.pinned = true; r.name = String(r.name.dropLast(tag.count)) }
    if r.preview.hasPrefix("已置顶 ") { r.pinned = true; r.preview = String(r.preview.dropFirst(4)) }
    return r.name.isEmpty ? nil : r
}

/// 聊天标题。exact=true 表示来自 AX 原文，按完全一致比较；false 表示来自截图识别
struct Title { let text: String; let exact: Bool }

// MARK: - 搜索结果解析

let HEADERS: Set<String> = ["联系人", "群聊", "功能", "聊天记录", "公众号", "服务号", "小程序", "企业微信联系人", "最常使用"]
let WEAK_SECTIONS: Set<String> = ["聊天记录", "最常使用"]   // 这两类里的同名项不算重名

struct SearchHit {
    let section: String; let name: String; let line: Line
    var exact = false     // 来自 AX 原文
    var visible = true    // 在下拉框可见范围内
}

func parseResults(_ rows: [Line]) -> [SearchHit] {
    var section: String?
    var hx: CGFloat = 0
    var out: [SearchHit] = []
    for l in rows {
        if HEADERS.contains(l.text) { section = l.text; hx = l.x0; continue }
        guard let sec = section else { continue }
        // 下拉框下面露出来的聊天列表：右侧有时间（10:14 / 昨天 / 星期一 / 9/12），看到就停
        if l.x0 > hx + 150, l.raw.range(of: #"^(\d{1,2}:\d{2}|昨天|前天|星期.|周.|\d{1,2}/\d{1,2}|\d{2,4}/\d{1,2}/\d{1,2})$"#,
                                          options: .regularExpression) != nil { break }
        guard l.x0 > hx - 10, l.x0 < hx + 200 else { continue }
        let r = l.raw.trimmingCharacters(in: .whitespaces)
        // 跳过副标题（包含:xxx / 昵称:xxx / N条相关聊天记录 …）
        if r.hasPrefix("包含") || r.contains(":") || r.contains("：") || r.contains("条相关") { continue }
        if r.contains("搜索网络结果") || r.hasPrefix("查看全部") || r.hasPrefix("更多") { continue }
        out.append(SearchHit(section: sec, name: r, line: l))
    }
    return out
}

func describe(_ hits: [SearchHit]) -> String {
    var order: [String] = []
    var bySec: [String: [String]] = [:]
    for h in hits {
        if bySec[h.section] == nil { order.append(h.section) }
        bySec[h.section, default: []].append(h.name)
    }
    return order.map { "\($0)：\(bySec[$0]!.joined(separator: "、"))" }.joined(separator: "；")
}

// MARK: - 会话

final class Session {
    let app: NSRunningApplication
    let pid: pid_t
    let account: String
    let cap = Capturer()
    var W = CGRect.zero
    var inSearch = false

    init(app: NSRunningApplication, account: String) {
        self.app = app; self.pid = app.processIdentifier; self.account = account
    }

    // 各识别区域（全局坐标，单位：点）
    var titleRect: CGRect { CGRect(x: W.minX + TITLE_MINX, y: W.minY, width: W.width - TITLE_MINX, height: TITLE_H) }
    var listRect: CGRect { CGRect(x: W.minX, y: W.minY, width: min(LIST_W, W.width), height: W.height) }
    var inputRect: CGRect { CGRect(x: W.minX + TITLE_MINX, y: W.maxY - INPUT_H, width: W.width - TITLE_MINX, height: INPUT_H) }
    var chatRect: CGRect {
        CGRect(x: W.minX + TITLE_MINX, y: W.minY + TITLE_H, width: W.width - TITLE_MINX, height: W.height - TITLE_H - INPUT_H - TOOLBAR_H)
    }
    var inputPoint: CGPoint { CGPoint(x: (W.minX + TITLE_MINX + W.maxX) / 2, y: W.maxY - 50) }

    /// 搜索下拉框如果是独立的弹出窗口，就只识别它；否则识别窗口左侧
    func resultsRect() -> CGRect {
        if let p = popupWindow() { return p.insetBy(dx: 1, dy: 1) }
        return listRect
    }
    func popupWindow() -> CGRect? {
        guard let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]] else { return nil }
        var best: CGRect?
        for w in info {
            guard (w[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
                  let bd = w[kCGWindowBounds as String] as? NSDictionary,
                  let r = CGRect(dictionaryRepresentation: bd as CFDictionary),
                  r != W, r.width > 200, r.width < 800, r.height > 80,
                  r.minX < W.minX + LIST_W, r.intersects(W),
                  r.minY > W.minY + 20, r.minY < W.minY + 140 else { continue }   // 紧贴在搜索框下面
            if best == nil || r.width * r.height > best!.width * best!.height { best = r }
        }
        return best
    }

    /// 聊天区域有没有被别的窗口（包括微信自己弹出的聊天窗口）挡住
    func guardNotOccluded() throws {
        guard let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]] else { return }
        let pane = CGRect(x: W.minX + TITLE_MINX, y: W.minY, width: W.width - TITLE_MINX, height: W.height)
        for w in info {   // 从前往后
            guard (w[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
                  ((w[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1) > 0,
                  let bd = w[kCGWindowBounds as String] as? NSDictionary,
                  let r = CGRect(dictionaryRepresentation: bd as CFDictionary),
                  r.width > 50, r.height > 50 else { continue }
            if r == W { return }
            if r.intersects(pane) {
                let owner = (w[kCGWindowOwnerName as String] as? String) ?? "某个窗口"
                throw fail(6, "OCCLUDED", "微信聊天区域被「\(owner)」的窗口挡住了，为防止误发已停止")
            }
        }
    }

    /// 输入框里的文字（忽略右下角的「发送」按钮）
    func inputText() throws -> String {
        try read(inputRect).lines
            .filter { !($0.text.hasPrefix("发送") && $0.text.count <= 4) && $0.text.lowercased() != "send" }
            .map { $0.raw }.joined(separator: " ")
    }

    func read(_ r: CGRect) throws -> (lines: [Line], img: CGImage) {
        guard let img = cap.image(r) else { throw fail(5, "CAPTURE", "截图失败：确认终端有「屏幕录制」权限") }
        return (mergeRows(recognize(img, r)), img)
    }

    // NSWorkspace.frontmostApplication 切换后要 1–2 秒才更新，所以读 AX
    func isFront() -> Bool { (axAttr(ax, kAXFrontmostAttribute) as? Bool) == true }

    func guardFront() throws {
        if !isFront() { throw fail(6, "FOCUS_LOST", "微信不在最前面了（可能你切换了窗口），为防止误发已停止") }
    }

    func activate() throws {
        if isFront() { return }
        if app.isHidden { app.unhide() }
        AXUIElementSetAttributeValue(AXUIElementCreateApplication(pid), kAXFrontmostAttribute as CFString, kCFBooleanTrue)
        if waitUntil(0.6, 0.05, { isFront() }) { return }
        _ = app.activate(options: [.activateIgnoringOtherApps])
        if !waitUntil(1.5, 0.05, { isFront() }) { throw fail(5, "ACTIVATE", "没能把微信切到前台") }
    }

    func mainWindow() -> CGRect? {
        guard let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]] else { return nil }
        var best: CGRect?
        for w in info {
            guard (w[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
                  (w[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
                  let bd = w[kCGWindowBounds as String] as? NSDictionary,
                  let r = CGRect(dictionaryRepresentation: bd as CFDictionary),
                  r.width > 500, r.height > 400 else { continue }
            if best == nil || r.width * r.height > best!.width * best!.height { best = r }
        }
        return best
    }

    /// 等主窗口停稳：切换桌面空间时窗口会滑动，中途的位置不能用来截图和点击
    func settledWindow(_ timeout: Double) -> CGRect? {
        var prev: CGRect?, done: CGRect?
        _ = waitUntil(timeout, 0.1) {
            let r = mainWindow()
            defer { prev = r }
            var n: UInt32 = 0
            guard let r = r, r == prev, CGGetDisplaysWithRect(r, 0, nil, &n) == .success, n > 0 else { return false }
            done = r
            return true
        }
        return done
    }

    func ensureWindow() throws -> CGRect {
        if let r = settledWindow(1.5) { return r }
        // 最小化了 → 还原
        let axApp = AXUIElementCreateApplication(pid)
        var v: CFTypeRef?
        if AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &v) == .success,
           let wins = v as? [AXUIElement] {
            for w in wins {
                var m: CFTypeRef?
                if AXUIElementCopyAttributeValue(w, kAXMinimizedAttribute as CFString, &m) == .success, (m as? Bool) == true {
                    AXUIElementSetAttributeValue(w, kAXMinimizedAttribute as CFString, kCFBooleanFalse)
                }
            }
        }
        if let r = settledWindow(1.5) {
            eprint("ℹ️ 微信窗口是最小化的，已自动还原")
            return r
        }
        // 主窗口被关掉了 → 发「重新打开」事件（等同于点 Dock 图标）
        let target = NSAppleEventDescriptor(processIdentifier: pid)
        let ev = NSAppleEventDescriptor(eventClass: AEEventClass(kCoreEventClass), eventID: AEEventID(kAEReopenApplication),
                                        targetDescriptor: target, returnID: AEReturnID(kAutoGenerateReturnID),
                                        transactionID: AETransactionID(kAnyTransactionID))
        _ = try? ev.sendEvent(options: [.noReply], timeout: 2)
        if let r = settledWindow(2) {
            eprint("ℹ️ 微信主窗口是关着的，已自动打开")
            return r
        }
        throw fail(5, "NO_WINDOW", "找不到微信主窗口：可能被关闭、在另一个桌面空间，或微信没有登录。请手动打开主窗口后重试")
    }

    lazy var ax: AXUIElement = {
        let a = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(a, 1)
        return a
    }()

    /// 主窗口里的聊天区域：「消息」列表的父元素；标题是它旁边的静态文字
    func axChatPane() -> AXUIElement? {
        let wins = (axAttr(ax, kAXWindowsAttribute) as? [AXUIElement]) ?? []
        guard let win = wins.first(where: { axStr($0, kAXSubroleAttribute) == "AXStandardWindow" }),
              let msgs = axFind(win, 0, { axRole($0) == "AXList" && axStr($0, kAXTitleAttribute) == "消息" }) else { return nil }
        return axParent(msgs)
    }

    /// 输入框：聊天区域里的文本框，名字是当前聊天名，值是框里的内容
    func axInput() -> AXUIElement? {
        axChatPane().flatMap { p in axChildren(p).first { axRole($0) == "AXTextArea" } }
    }

    func axTitle() -> String? {
        guard let pane = axChatPane(), let outer = axParent(pane) else { return nil }
        return axChildren(outer).lazy.compactMap { e -> String? in
            guard axRole(e) == "AXStaticText", let v = axStr(e, kAXValueAttribute), !v.isEmpty else { return nil }
            return v
        }.first
    }

    /// 聊天标题：优先读 AX，读不到才截图识别
    func title() throws -> Title? {
        if let t = axTitle() { return Title(text: t, exact: true) }
        let rows = try read(titleRect).lines
        return rows.first(where: { $0.cy < W.minY + TITLE_H }).map { Title(text: $0.raw, exact: false) }
    }

    func isTarget(_ t: Title?, _ contact: String) -> Bool {
        guard let t = t else { return false }
        return t.exact ? sameName(t.text, contact) : titleMatches(t.text, contact)
    }

    /// 截图识别出的标题被截断成「…」
    func truncated(_ t: Title) -> Bool { !t.exact && (t.text.hasSuffix("…") || t.text.hasSuffix("...")) }

    func axMainWindow() -> AXUIElement? {
        ((axAttr(ax, kAXWindowsAttribute) as? [AXUIElement]) ?? []).first { axStr($0, kAXSubroleAttribute) == "AXStandardWindow" }
    }
    func axChatList() -> AXUIElement? {
        axMainWindow().flatMap { axFind($0, 0, { axRole($0) == "AXList" && axStr($0, kAXTitleAttribute) == "会话" }) }
    }
    /// 左侧「微信」标签按钮，描述里是「N条新消息」（不含免打扰的）
    func axChatsTab() -> AXUIElement? {
        axMainWindow().flatMap { axFind($0, 0, { axRole($0) == "AXButton" && axStr($0, kAXTitleAttribute) == "WeChat" }) }
    }
    func unreadTotal() -> Int {
        guard let d = axChatsTab().flatMap({ axStr($0, kAXDescriptionAttribute) }),
              let m = d.range(of: #"\d+(?=条新消息)"#, options: .regularExpression) else { return 0 }
        return Int(d[m]) ?? 0
    }
    /// 在会话列表上滚动（正数向上，单位：点）；列表不支持用 AX 滚动，只能发滚轮事件
    func scrollChatList(_ list: AXUIElement, _ dy: Int32) {
        guard let f = axFrame(list) else { return }
        moveMouse(CGPoint(x: f.midX, y: f.midY)); usleep(30_000)
        CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 1, wheel1: dy, wheel2: 0, wheel3: 0)?.post(tap: .cghidEventTap)
        usleep(250_000)
    }

    /// 一直往上滚，直到可见的第一行不再变化（一次滚太多会被限幅，所以不能只滚一次）
    func scrollChatListToTop(_ list: AXUIElement) {
        func top() -> String {
            axChildren(list).compactMap { e -> (CGFloat, String)? in
                guard let t = axStr(e, kAXTitleAttribute), !t.isEmpty, let f = axFrame(e) else { return nil }
                return (f.minY, t)
            }.min { $0.0 < $1.0 }?.1 ?? ""
        }
        var last = top()
        for _ in 0..<80 {
            scrollChatList(list, 2000)
            let now = top()
            if now == last { return }
            last = now
        }
    }

    /// 扫描会话列表里要读的未读聊天：没开免打扰的全部要；免打扰的只要白名单里的
    func scanUnread(_ allow: [String]) throws -> (total: Int, chats: [ChatRow], scanned: Int) {
        try activate()
        W = try ensureWindow()
        if axChatList() == nil, let tab = axChatsTab(), let f = axFrame(tab) {   // 不在「微信」标签页：点一下切过去
            click(CGPoint(x: f.midX, y: f.midY))
            _ = waitUntil(1, 0.1) { axChatList() != nil }
        }
        guard let list = axChatList(), let box = axFrame(list) else { throw fail(5, "NO_AX", "AX 读不到会话列表") }
        let total = unreadTotal()
        if total == 0 && allow.isEmpty { return (0, [], 0) }
        scrollChatListToTop(list)
        var seen = Set<String>(), found = Set<String>(), out: [ChatRow] = [], sum = 0
        for _ in 0..<40 {
            var fresh = 0
            for row in axChildren(list) {
                guard let raw = axStr(row, kAXTitleAttribute), !raw.isEmpty, !seen.contains(raw) else { continue }
                seen.insert(raw); fresh += 1
                guard let r = parseChatRow(raw, allow: allow) else { continue }
                if r.muted { found.insert(r.name) }
                guard r.unread > 0 else { continue }
                out.append(r)
                if !r.muted { sum += r.unread }
            }
            // 没开免打扰的未读条数凑够了总数，白名单里的群也都找到了 → 后面不会再有要读的
            if sum >= total && found.count == Set(allow).count { break }
            if fresh == 0 || seen.count >= 200 { break }   // 到底了
            try guardFront()
            scrollChatList(list, -Int32(box.height * 0.8))
        }
        scrollChatListToTop(list)
        return (total, out, seen.count)
    }

    func printUnreadList(_ allow: [String]) throws {
        let (total, chats, scanned) = try scanUnread(allow)
        let items: [[String: Any]] = chats.map { ["name": $0.name, "unread": $0.unread, "pinned": $0.pinned,
                                                  "muted": $0.muted, "preview": $0.preview, "time": $0.time] }
        let data = try JSONSerialization.data(withJSONObject: ["total": total, "scanned": scanned, "chats": items], options: [.sortedKeys])
        say(String(data: data, encoding: .utf8)!)
    }

    func axSearchText() -> String? {
        let wins = (axAttr(ax, kAXWindowsAttribute) as? [AXUIElement]) ?? []
        guard let win = wins.first(where: { axStr($0, kAXSubroleAttribute) == "AXStandardWindow" }),
              let box = axFind(win, 0, { axRole($0) == "AXTextArea" && axStr($0, kAXTitleAttribute) == "搜索" }) else { return nil }
        return axStr(box, kAXValueAttribute)
    }

    /// 搜索下拉框（一个独立的 AXDialog 窗口，里面是一个列表）；读不到返回 nil
    func axResults() -> [SearchHit]? {
        let wins = (axAttr(ax, kAXWindowsAttribute) as? [AXUIElement]) ?? []
        guard let list = wins.lazy.filter({ axStr($0, kAXSubroleAttribute) == "AXDialog" })
                .compactMap({ w in axChildren(w).first { axRole($0) == "AXList" } }).first,
              let box = axFrame(list) else { return nil }
        let web = "搜索网络结果"
        var section: String?
        var out: [SearchHit] = []
        for e in axChildren(list) {
            guard axRole(e) == "AXStaticText", let name = axStr(e, kAXTitleAttribute), let f = axFrame(e) else { continue }
            // 分类标题行高约 28，结果行高约 57；「搜索网络结果」下面的搜索建议行高约 31，全部跳过
            if name == web { section = web; continue }
            if section == web { if f.height < 40 && HEADERS.contains(name) { section = name }; continue }
            if f.height < 40 { section = name; continue }
            guard let sec = section else { continue }
            let line = Line(raw: name, text: norm(name), x0: f.minX, x1: f.maxX, cy: f.midY, h: f.height)
            out.append(SearchHit(section: sec, name: name, line: line, exact: true, visible: box.contains(f)))
        }
        return out
    }

    func pollResults(_ contact: String) throws -> SearchHit {
        let g = norm(stripCount(contact))
        var prev: [String]?
        var since = Date()
        var last: [SearchHit] = []
        let end = Date().addingTimeInterval(TIMEOUT)
        repeat {
            try guardFront()
            let hits: [SearchHit]
            if let a = axResults() {
                // 搜索框里还不是这个名字：粘贴还没生效，结果是旧的
                if let q = axSearchText(), q != contact { prev = nil; sleepS(0.05); continue }
                hits = a
            } else {
                hits = parseResults(try read(resultsRect()).lines)
            }
            last = hits
            let sig = hits.map { $0.section + "|" + $0.name }
            if sig != prev { prev = sig; since = Date() }
            let matches = hits.filter { $0.exact ? sameName($0.name, contact) : norm(stripCount($0.name)) == g }
            // 结果 0.3 秒内没有变化才算加载完成，避免结果还在刷新时就点
            if !matches.isEmpty, Date().timeIntervalSince(since) >= 0.3 {
                let strong = matches.filter { !WEAK_SECTIONS.contains($0.section) }
                if strong.count > 1 {
                    throw fail(3, "DUPLICATE",
                               "有 \(strong.count) 个同名结果「\(contact)」（\(describe(strong))），为防发错已停止。请给对方设置唯一的备注名")
                }
                let hit = strong.first ?? matches[0]
                if !hit.visible {
                    throw fail(2, "NOT_VISIBLE", "「\(contact)」在搜索结果里，但不在下拉框的可见范围内，没法点击，已停止")
                }
                return hit
            }
            sleepS(0.1)
        } while Date() < end
        let seen = last.isEmpty ? "（没有识别到结果列表）" : describe(last)
        throw fail(2, "NOT_FOUND", "搜索结果里没有名字完全是「\(contact)」的项。识别到的结果 → \(seen)")
    }

    /// 截图识别聊天底部最后一行，看它左边有没有红色感叹号（AX 里看不到这个标记）
    func redMarkByOCR(_ g: String) -> Bool {
        guard !g.isEmpty, let r = try? read(chatRect), let b = r.lines.last,
              !b.text.isEmpty, g.hasSuffix(b.text) else { return false }
        return redPixels(r.img, chatRect, CGRect(x: b.x0 - 55, y: b.cy - 15, width: 43, height: 30)) > 40
    }

    /// AX 不可用时：截图识别确认消息开头出现在输入框里
    func checkPastedByOCR(_ msg: String, _ notes: inout [String]) throws {
        let probe = String(norm(msg).prefix(8))
        if probe.isEmpty { notes.append("消息没有文字，未校验输入框"); return }
        let inBox = waitUntil(1.5, 0.1) {
            norm((try? inputText()) ?? "").contains(probe)
        }
        if !inBox { throw fail(4, "INPUT_MISMATCH", "粘贴后在输入框里没看到这条消息，已停止，未发送") }
    }

    /// 切到微信并打开这个聊天（标题完全一致才算打开）
    func openChat(_ contact: String, _ notes: inout [String]) throws {
        if norm(contact).isEmpty { throw fail(64, "BAD_NAME", "联系人名里没有可识别的文字") }
        try activate()
        W = try ensureWindow()
        _ = waitUntil(1, 0.1) { axTitle() != nil }   // 刚切到前台时 AX 树可能还没生成

        // 1. 上一条就是发给这个人、且当前聊天标题仍是他（且标题没被截断）→ 跳过搜索
        let before = try title()
        if rememberedChat(account) == contact, let t = before, isTarget(t, contact), !truncated(t) {
            notes.append("已在该聊天，跳过搜索")
        } else {
            try guardFront()
            key(K_F, cmd: true); usleep(120_000)     // ⌘F 打开搜索
            inSearch = true
            key(K_A, cmd: true)                       // ⌘A 覆盖旧内容
            pasteText(contact)
            sleepS(0.15)
            let hit = try pollResults(contact)
            if !hit.exact { notes.append("搜索结果用截图识别") }
            try guardFront()
            click(CGPoint(x: (hit.line.x0 + hit.line.x1) / 2, y: hit.line.cy))
            inSearch = false
            var lastTitle = ""
            let opened = waitUntil(TIMEOUT, 0.1) {
                let t = (try? title()) ?? nil
                lastTitle = t?.text ?? "（无）"
                guard let t = t, isTarget(t, contact) else { return false }
                // 被截断的标题和点击前一样：可能新聊天还没加载出来，继续等
                return !(truncated(t) && t.text == before?.text)
            }
            if !opened { throw fail(4, "TITLE_MISMATCH", "点击后打开的聊天标题是「\(lastTitle)」，不是「\(contact)」，已停止") }
            rememberChat(account, contact)
        }
    }

    /// 读取聊天记录里当前加载出来的消息（最多 limit 条），JSON 输出
    func readChat(_ contact: String, _ limit: Int) throws {
        var notes: [String] = []
        try openChat(contact, &notes)
        guard let list = axChatPane().flatMap({ p in axChildren(p).first { axRole($0) == "AXList" } }) else {
            throw fail(5, "NO_AX", "AX 读不到聊天记录")
        }
        let time = #"^(\d{1,2}:\d{2}|昨天.*|前天.*|星期.*|周.*|\d{1,2}月\d{1,2}日.*|\d{4}年.*|\d{1,2}/\d{1,2}.*)$"#
        var items: [[String: String]] = []
        for row in axChildren(list) {
            // 屏幕外的消息是没有内容的占位元素，读不到
            guard var t = axStr(row, kAXTitleAttribute), !t.isEmpty else { continue }
            if t.hasSuffix(" ") { t.removeLast() }
            let h = axFrame(row)?.height ?? 0
            let isTime = h < 50 && t.range(of: time, options: .regularExpression) != nil
            items.append(["type": isTime ? "time" : "message", "text": t])
        }
        let out: [String: Any] = ["chat": contact, "items": Array(items.suffix(limit)), "notes": notes]
        let data = try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
        say(String(data: data, encoding: .utf8)!)
    }

    /// 返回 (状态, 说明)。状态：SENT / DRY_RUN / FAILED / UNCONFIRMED
    func sendOne(_ contact: String, _ msg: String) throws -> (String, String) {
        if msg.isEmpty { throw fail(64, "EMPTY_MSG", "消息是空的") }
        var notes: [String] = []
        try openChat(contact, &notes)

        // 2. 输入框：必须为空，粘贴后必须能看到消息
        try guardFront()
        try guardNotOccluded()
        click(inputPoint)
        usleep(80_000)
        if let box = axInput() {
            // 输入框的名字就是它所属的聊天名
            let owner = axStr(box, kAXTitleAttribute) ?? ""
            if !sameName(owner, contact) {
                throw fail(4, "INPUT_OWNER", "输入框属于「\(owner)」，不是「\(contact)」，已停止，未粘贴")
            }
            let draft = axStr(box, kAXValueAttribute) ?? ""
            if !draft.isEmpty {
                throw fail(7, "DRAFT", "「\(contact)」的输入框里已有内容「\(preview(draft, 20))」，为免连草稿一起发出已停止。请清空后重试")
            }
            pasteText(msg)
            var seen = ""
            let inBox = waitUntil(1.5, 0.05) {
                seen = axStr(box, kAXValueAttribute) ?? ""
                return sameInput(seen, msg)
            }
            if !inBox { throw fail(4, "INPUT_MISMATCH", "粘贴后输入框里是「\(preview(seen, 20))」，和消息不一致，已停止，未发送") }
        } else {
            notes.append("AX 读不到输入框，改用截图识别")
            let draft = try inputText()
            if !draft.isEmpty {
                throw fail(7, "DRAFT", "「\(contact)」的输入框里已有内容「\(preview(draft, 20))」，为免连草稿一起发出已停止。请清空后重试")
            }
            pasteText(msg)
            try checkPastedByOCR(msg, &notes)
        }

        // 3. 发送前最后确认：微信仍在最前、聊天仍是目标
        try guardFront()
        guard let t2 = try title(), isTarget(t2, contact) else {
            throw fail(4, "TITLE_CHANGED", "发送前聊天窗口变了，已停止，未发送（消息留在输入框里）")
        }
        if !t2.exact { notes.append("AX 读不到标题，改用截图识别核对") }
        if DRY_RUN { return ("DRY_RUN", (notes + ["消息已粘贴，未发送"]).joined(separator: "；")) }

        let msgList = axChatPane().flatMap { p in axChildren(p).first { axRole($0) == "AXList" } }
        let rowsBefore = msgList.map { axChildren($0) } ?? []

        try guardFront()
        try guardNotOccluded()
        key(K_RETURN)
        markSent(account)
        if !CONFIRM { return ("SENT", (notes + ["未做发送确认"]).joined(separator: "；")) }

        // 4. 确认：「消息」列表多了一行、内容就是这条消息，输入框已清空，旁边没有红色感叹号
        if let list = msgList, let box = axInput() {
            var lastSeen = ""
            let end = Date().addingTimeInterval(3)
            repeat {
                let rows = axChildren(list)
                if let row = rows.last {
                    lastSeen = axStr(row, kAXTitleAttribute) ?? ""
                    if lastSeen.hasSuffix(" ") { lastSeen.removeLast() }   // 微信在每条消息后面加了一个空格
                    let isNew = rows.count != rowsBefore.count || rowsBefore.last.map { !CFEqual($0, row) } ?? true
                    if isNew && sameInput(lastSeen, msg) && (axStr(box, kAXValueAttribute) ?? "x").isEmpty {
                        if redMarkByOCR(norm(msg)) {
                            return ("FAILED", (notes + ["消息旁出现红色感叹号，发送失败"]).joined(separator: "；"))
                        }
                        return ("SENT", notes.joined(separator: "；"))
                    }
                }
                sleepS(0.1)
            } while Date() < end
            return ("UNCONFIRMED", (notes + ["3 秒内没在聊天记录最后看到这条消息（最后一条：「\(preview(lastSeen, 20))」），可能已发出，请人工确认，不要直接重试"]).joined(separator: "；"))
        }

        // AX 不可用时：截图识别聊天底部
        notes.append("AX 读不到聊天记录，改用截图识别确认")
        let g = norm(msg)
        if g.isEmpty { return ("SENT", (notes + ["消息没有文字，未做发送确认"]).joined(separator: "；")) }
        var bottom = ""
        let end = Date().addingTimeInterval(3)
        repeat {
            if let r = try? read(chatRect), let b = r.lines.last {
                bottom = b.raw
                if !b.text.isEmpty && g.hasSuffix(b.text) && b.text.count >= min(4, g.count) {
                    let box = CGRect(x: b.x0 - 55, y: b.cy - 15, width: 43, height: 30)
                    if redPixels(r.img, chatRect, box) > 40 {
                        return ("FAILED", (notes + ["消息旁出现红色感叹号，发送失败"]).joined(separator: "；"))
                    }
                    if ((try? inputText()) ?? "x").isEmpty {
                        return ("SENT", notes.joined(separator: "；"))
                    }
                }
            }
            sleepS(0.15)
        } while Date() < end
        return ("UNCONFIRMED", (notes + ["3 秒内没在聊天底部看到这条消息（底部最后一行：「\(preview(bottom, 20))」），可能已发出，请人工确认，不要直接重试"]).joined(separator: "；"))
    }

    func dump() throws {
        try activate()
        W = try ensureWindow()
        func show(_ name: String, _ r: CGRect) throws {
            say("── \(name)  区域 x=\(Int(r.minX - W.minX)) y=\(Int(r.minY - W.minY)) w=\(Int(r.width)) h=\(Int(r.height))")
            for l in try read(r).lines {
                say("   y=\(Int(l.cy - W.minY))\tx=\(Int(l.x0 - W.minX))-\(Int(l.x1 - W.minX))\t\(l.raw)")
            }
        }
        say("微信窗口：x=\(Int(W.minX)) y=\(Int(W.minY)) w=\(Int(W.width)) h=\(Int(W.height))（坐标相对窗口左上角，单位：点）")
        _ = waitUntil(1, 0.1) { axTitle() != nil }
        say("── 聊天标题（AX）：\(axTitle().map { "「\($0)」" } ?? "读不到")")
        try show("聊天标题（截图识别）", titleRect)
        if let p = popupWindow() {
            say("（检测到独立的下拉框窗口：x=\(Int(p.minX - W.minX)) y=\(Int(p.minY - W.minY)) w=\(Int(p.width)) h=\(Int(p.height))）")
        }
        if let r = axResults() { say("── 搜索结果（AX）：\(r.isEmpty ? "空" : describe(r))") }
        try show("左侧 / 搜索下拉框", resultsRect())
        let hits = parseResults(try read(resultsRect()).lines)
        if !hits.isEmpty { say("   → 解析出的搜索结果：\(describe(hits))") }
        try show("输入框", inputRect)
        let inputNow = try inputText()
        say("   → 输入框文字（已忽略发送按钮）：「\(inputNow)」")
        if let box = axInput() {
            say("   → 输入框（AX）：属于「\(axStr(box, kAXTitleAttribute) ?? "")」，内容「\(axStr(box, kAXValueAttribute) ?? "")」")
        } else { say("   → 输入框（AX）：读不到") }
        say("── 聊天区域底部 3 行")
        for l in try read(chatRect).lines.suffix(3) { say("   y=\(Int(l.cy - W.minY))\tx=\(Int(l.x0 - W.minX))\t\(l.raw)") }
    }
}

// MARK: - 环境检查

func checkEnvironment() throws {
    if !AXIsProcessTrusted() {
        throw fail(5, "NO_ACCESSIBILITY", "终端没有「辅助功能」权限：系统设置 → 隐私与安全性 → 辅助功能，打开你用的终端，然后重启终端")
    }
    if !CGPreflightScreenCaptureAccess() {
        throw fail(5, "NO_SCREEN_RECORDING", "终端没有「屏幕录制」权限：系统设置 → 隐私与安全性 → 屏幕录制，打开你用的终端，然后重启终端")
    }
    if let d = CGSessionCopyCurrentDictionary() as? [String: Any],
       (d["CGSSessionScreenIsLocked"] as? NSNumber)?.boolValue == true {
        throw fail(5, "SCREEN_LOCKED", "屏幕已锁定，没法操作微信。请解锁；可以用 caffeinate -d 防止自动锁屏")
    }
    if CGDisplayIsAsleep(CGMainDisplayID()) != 0 {
        throw fail(5, "DISPLAY_ASLEEP", "显示器在睡眠，没法截图识别。请唤醒屏幕；可以用 caffeinate -d 防止熄屏")
    }
    if NSWorkspace.shared.runningApplications.contains(where: { $0.bundleIdentifier == "com.apple.ScreenSaver.Engine" }) {
        throw fail(5, "SCREENSAVER", "屏幕保护程序正在运行，请先退出屏保")
    }
}

func findApp(_ appPath: String) throws -> NSRunningApplication {
    let want = URL(fileURLWithPath: appPath).standardizedFileURL.path
    let apps = NSWorkspace.shared.runningApplications.filter {
        $0.bundleURL?.standardizedFileURL.path == want
    }
    if apps.isEmpty { throw fail(5, "NOT_RUNNING", "\(appPath) 没有在运行，请先打开并登录这个微信") }
    if apps.count > 1 { throw fail(5, "MULTI_INSTANCE", "\(appPath) 同时开了 \(apps.count) 个，没法确定用哪个") }
    return apps[0]
}

// MARK: - 主流程

func parseBatch(_ path: String) throws -> [(String, String)] {
    guard let s = try? String(contentsOfFile: path, encoding: .utf8) else { throw fail(64, "BATCH_FILE", "读不了文件 \(path)") }
    var jobs: [(String, String)] = []
    for (i, raw) in s.split(omittingEmptySubsequences: false, whereSeparator: { $0.isNewline }).enumerated() {
        let line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if line.isEmpty || line.hasPrefix("#") { continue }
        let parts = line.split(separator: "\t", maxSplits: 1).map(String.init)
        guard parts.count == 2 else { throw fail(64, "BATCH_FORMAT", "第 \(i + 1) 行格式不对，应为：联系人<Tab>消息") }
        jobs.append((parts[0].trimmingCharacters(in: .whitespaces), parts[1].replacingOccurrences(of: "\\n", with: "\n")))
    }
    if jobs.isEmpty { throw fail(64, "BATCH_EMPTY", "文件里没有要发送的内容") }
    return jobs
}

func run(_ args: [String]) -> Int32 {
    guard args.count >= 3 else { eprint("内部参数错误"); return 64 }
    let mode = args[0], account = args[1], appPath = args[2]
    do {
        try checkEnvironment()
        let session = Session(app: try findApp(appPath), account: account)
        if mode == "dump" { try session.dump(); return 0 }
        if mode == "unread" {
            let allow = (env["WX_MUTED_ALLOW"] ?? "").split(separator: "|")
                .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            let lockFd = try acquireLock()
            defer { close(lockFd) }
            let mouse = CGEvent(source: nil)?.location
            defer { if let m = mouse { moveMouse(m) } }
            try session.printUnreadList(allow)
            return 0
        }
        if mode == "read" {
            guard args.count == 5, let n = Int(args[4]), n > 0 else { eprint("内部参数错误"); return 64 }
            let lockFd = try acquireLock()
            defer { close(lockFd) }
            let clip = ClipboardBackup()
            clipBackup = clip
            let mouse = CGEvent(source: nil)?.location
            defer {
                clip.restore()
                if let m = mouse { moveMouse(m) }
            }
            do { try session.readChat(args[3], n) } catch let e as WXError {
                if session.inSearch && session.isFront() { key(K_ESC) }
                throw e
            }
            return 0
        }

        let jobs: [(String, String)]
        switch mode {
        case "send" where args.count == 5: jobs = [(args[3], args[4])]
        case "batch" where args.count == 4: jobs = try parseBatch(args[3])
        default: eprint("内部参数错误"); return 64
        }

        let lockFd = try acquireLock()
        defer { close(lockFd) }
        let clip = ClipboardBackup()
        clipBackup = clip
        let mouse = CGEvent(source: nil)?.location
        defer {
            clip.restore()
            if let m = mouse { moveMouse(m) }
        }

        var worst: Int32 = 0
        for (i, job) in jobs.enumerated() {
            let (contact, msg) = job
            let tag = jobs.count > 1 ? "[\(i + 1)/\(jobs.count)] " : ""
            let t0 = Date()
            func secs() -> String { String(format: "%.1f", Date().timeIntervalSince(t0)) }
            do {
                if !DRY_RUN {
                    if sentToday(account) >= DAILY_MAX {
                        throw fail(9, "DAILY_MAX", "「\(account)」今天已发送 \(DAILY_MAX) 条，达到上限（WX_DAILY_MAX）")
                    }
                    rateLimit(account)
                }
                let t1 = Date()
                let (st, info) = try session.sendOne(contact, msg)
                let s = String(format: "%.1f", Date().timeIntervalSince(t1))
                writeLog(account, contact, st, s, info, msg)
                let extra = info.isEmpty ? "" : "（\(info)）"
                switch st {
                case "SENT":        say("✅ \(tag)已发送给「\(contact)」，用时 \(s) 秒\(extra)")
                case "DRY_RUN":     say("🧪 \(tag)试运行：已粘贴到「\(contact)」的输入框，没有发送，用时 \(s) 秒")
                case "UNCONFIRMED": eprint("⚠️ \(tag)「\(contact)」\(info)"); worst = max(worst, 10)
                default:            eprint("❌ \(tag)「\(contact)」\(info)"); worst = max(worst, 8)
                }
            } catch let e as WXError {
                if session.inSearch && session.isFront() { key(K_ESC) }
                session.inSearch = false
                writeLog(account, contact, "FAILED:" + e.tag, secs(), e.msg, msg)
                eprint("❌ \(tag)\(e.msg)")
                worst = max(worst, e.code)
                if e.code == 5 || e.code == 6 { break }   // 环境问题或被打断：后面的也不发了
            }
        }
        return worst
    } catch let e as WXError {
        writeLog(account, "-", "FAILED:" + e.tag, "0", e.msg, "")
        eprint("❌ \(e.msg)")
        return e.code
    } catch {
        eprint("❌ \(error)")
        return 1
    }
}

// Ctrl+C：先恢复剪贴板再退出
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    let s = DispatchSource.makeSignalSource(signal: sig, queue: .global())
    s.setEventHandler {
        clipBackup?.restore()
        eprint("⛔ 已中断，剪贴板已恢复")
        exit(130)
    }
    s.resume()
    signalSources.append(s)
}

let cliArgs = Array(CommandLine.arguments.dropFirst())
DispatchQueue.global(qos: .userInitiated).async {
    exit(run(cliArgs))
}
RunLoop.main.run()   // 主线程保持运行，截图和系统通知需要它
SWIFT
}

build

case "$1" in
  --dump)  exec "$BIN" dump "$NAME" "$APP" ;;
  --batch) [[ $# -eq 2 ]] || usage; exec "$BIN" batch "$NAME" "$APP" "$2" ;;
  --read)  [[ $# -ge 2 && $# -le 3 ]] || usage; exec "$BIN" read "$NAME" "$APP" "$2" "${3:-20}" ;;
  --unread) exec "$BIN" unread "$NAME" "$APP" ;;
  -*)      usage ;;
  *)       [[ $# -eq 2 ]] || usage; exec "$BIN" send "$NAME" "$APP" "$1" "$2" ;;
esac
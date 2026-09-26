# 给 Muse 的使用说明（复制粘贴到对话里）

把下面这段发给 Muse，替换 `<URL>` 和 `<TOKEN>`。建议保存成手机备忘录，每次开新任务时发一遍。

---

你可以通过一个 HTTP 接口远程操作我的 Mac。请在你的终端里使用它。

**准备（只需一次）：**

```bash
export MAB_URL=<URL>
export MAB_TOKEN=<TOKEN>
curl -sL https://raw.githubusercontent.com/<你的GitHub用户名>/muse-intel-mac-bridge/main/mab.py -o mab.py
python3 mab.py info
```

**操作规则：**

1. 每一步操作前，先运行 `python3 mab.py screenshot -o screen.jpg`，然后查看 `screen.jpg` 了解屏幕当前状态。
2. 所有坐标都是 `screen.jpg` 图片上的像素坐标（图片默认 1280 宽）。
3. 可用命令：
   - `python3 mab.py click X Y`（加 `--double` 双击，`--right` 右键）
   - `python3 mab.py type "文字"`
   - `python3 mab.py key command space`（组合键；按键名称用 `command`、`option`、`ctrl`、`shift`、`enter`、`tab`、`esc` 等）
   - `python3 mab.py scroll -5 X Y`（负数向下滚动）
   - `python3 mab.py drag X1 Y1 X2 Y2`
4. 每次操作后重新截图，确认结果符合预期再继续下一步。
5. 打开应用最快的方式：`key command space` → `type "应用名"` → `key enter`。
6. 删除文件、发送消息、付款等不可撤销的操作，执行前先向我确认。

**微信（不用截图，直接按名字操作）：**

- 读：`python3 mab.py wechat-read "联系人" -n 10 -a <账号别名>`
- 发：`python3 mab.py wechat-send "联系人" "内容" -a <账号别名>`（加 `--dry-run` 只粘贴不发送）
- 所有未读：`python3 mab.py wechat-unread -a <账号别名>`（只看列表不点开：加 `--list-only`）。拿到后按聊天逐个总结，群聊里按 `sender` 区分是谁说的
- 联系人名字必须和微信里显示的完全一致。
- 发送前先把联系人和内容复述给我，等我回复「确认」再发。
- 返回 `status` 是 `unconfirmed_do_not_retry` 时不要重发，先用 `wechat-read` 查看是否已经发出。
- 其他非 `ok` 的状态，把 `output` 告诉我，不要自己换个名字重试。

---

## 如果 Muse 无法下载 mab.py

可以不用客户端，直接用 curl：

```bash
H="Authorization: Bearer $MAB_TOKEN"
curl -s -H "$H" "$MAB_URL/screenshot" -o screen.jpg
curl -s -H "$H" -X POST -d '{"x":640,"y":400}' "$MAB_URL/click"
curl -s -H "$H" -X POST -d '{"text":"hello"}' "$MAB_URL/type"
curl -s -H "$H" -X POST -d '{"keys":["command","space"]}' "$MAB_URL/key"
```

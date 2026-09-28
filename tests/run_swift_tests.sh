#!/usr/bin/env bash
# 把 wx-send.sh 里「对位（纯函数）」那一段和 Msg 定义取出来，和测试一起编译运行
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
src="$tmp/src.swift"
sed -n "/^cat <<'SWIFT'\$/,/^SWIFT\$/p" wechat/wx-send.sh | sed '1d;$d' > "$tmp/all.swift"
{
  echo 'import Foundation'
  sed -n '/^struct Msg: Equatable {/,/^}/p' "$tmp/all.swift"
  sed -n '/^\/\/ MARK: - 对位（纯函数）/,/^\/\/ MARK: - 对位结束/p' "$tmp/all.swift"
} > "$src"
cp tests/swift/test_align.swift "$tmp/main.swift"   # 只有 main.swift 能写顶层代码
swiftc -O "$src" "$tmp/main.swift" -o "$tmp/t" 2>&1 | grep -v '^$' || true
[[ -x "$tmp/t" ]] || { echo "编译失败"; exit 1; }
"$tmp/t"

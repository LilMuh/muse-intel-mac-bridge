// alignPage 的单元测试：由 tests/run_swift_tests.sh 和 wx-send.sh 里「对位」那一段一起编译
var failures = 0
func check(_ name: String, _ got: Int?, _ want: Int?) {
    if got != want { failures += 1; print("FAIL \(name): got \(String(describing: got)), want \(String(describing: want))") }
    else { print("ok   \(name)") }
}
func m(_ t: String) -> Msg { Msg(type: "message", text: t) }
let P = m("图片"), T = Msg(type: "time", text: "14:00")

// 第一页必须对上末尾
check("第一页对上末尾", alignPage([T, m("a"), m("b")], [m("a"), m("b")], hi: 3, known: [nil, nil]), 1)
check("第一页对不上末尾", alignPage([m("a"), m("b"), m("c")], [m("a"), m("b")], hi: 3, known: [nil, nil]), nil)
// 连着一串图片：上一页认得的行元素给出位置（审查 I-1 的场景：实际露出的是 3..7，按文字会错配成 5）
let pics = [T] + Array(repeating: P, count: 10)
check("连着的图片按行元素定位", alignPage(pics, Array(repeating: P, count: 5), hi: 10, known: [nil, nil, nil, 6, 7]), 3)
check("行元素和文字对不上", alignPage([T, m("a"), P, P], [P, P], hi: 3, known: [nil, 1]), nil)
// 认不出行元素时按文字找，只接受唯一的位置
check("文字唯一", alignPage([T, m("a"), P, m("b")], [m("a"), P], hi: 3, known: [nil, nil]), 1)
check("文字不唯一", alignPage([T, P, P, P, P], [P, P], hi: 4, known: [nil, nil]), nil)
check("空页", alignPage([m("a")], [], hi: 1, known: []), nil)

// prependRows：往上翻页后把新露出来的行接到前面
func n(_ ms: [Msg]?) -> Int? { ms?.count }
check("连着的图片翻页后按行元素接上", n(prependRows([P, P, P], [P, P, P], known: [false, true, true])), 4)
check("到顶了（整页都见过）", n(prependRows([P, P, P], [P, P, P], known: [true, true, true])), 3)
check("行元素认得但文字对不上", n(prependRows([m("a"), m("b")], [m("x"), m("y"), m("b")], known: [false, true, false])), nil)
check("认不出行元素时按文字重叠", n(prependRows([m("b"), m("c")], [m("a"), m("b")], known: [false, false])), 3)

if failures > 0 { print("\(failures) 个失败"); exit(1) }
print("全部通过")

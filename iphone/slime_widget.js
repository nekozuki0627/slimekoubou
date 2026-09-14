// スライムこうぼう ウィジェット（iPhone / Scriptable）
// ─────────────────────────────────────────────
// PC の スライムこうぼう（monitor.py）に 聞きにいって、
// Claude Code の 部屋が いま どうしているかを ウィジェットに 出す。
//
// ・🔴 きょかまち（手が 止まっている子）を いちばん 上に 赤く
// ・しごと中／へんじまち の 数、よわってる子
// ・PC と つながらない時は そう 出して、最後に 取れた 時こくを 見せる
//
// つなぎ先は 下の HOST / KEY に 入れるか、
// ウィジェットの「Parameter」に「住所|合言葉」と 書く（こちらが 優先）。
// 例） 100.x.y.z|abcdef123
//
// ⚠️ iPhone の ウィジェットは iOS の つごうで 数分〜15分おきにしか 書きかわりません。
//    いますぐ 見たい時は ウィジェットを タップして 工房の 画面を ひらいてください。

const VERSION = 1

let HOST = "__HOST__"      // 例） 100.x.y.z（Tailscale の PC の 住所）
let KEY = "__KEY__"        // 工房の 合言葉
const PORT = 8899

// ── つなぎ先を きめる ──
const param = (args.widgetParameter || "").trim()
if (param.includes("|")) {
  const [h, k] = param.split("|")
  HOST = h.trim()
  KEY = k.trim()
}
const configured = HOST && KEY && !HOST.startsWith("__") && !KEY.startsWith("__")
const BASE = `http://${HOST}:${PORT}`
const PAGE = `${BASE}/?t=${encodeURIComponent(KEY)}`

// ── 色 ──
const C = {
  bg: Color.dynamic(new Color("#FFF8EE"), new Color("#1E2330")),
  ink: Color.dynamic(new Color("#3A3A3A"), new Color("#F2EFE8")),
  sub: Color.dynamic(new Color("#8A8378"), new Color("#A7ACB8")),
  red: new Color("#E5484D"),
  redBg: Color.dynamic(new Color("#FDE3E3"), new Color("#4A2226")),
  green: new Color("#6DBE5A"),
  blue: Color.dynamic(new Color("#3E8ED0"), new Color("#7DB8EA")),
  weak: Color.dynamic(new Color("#B08A3E"), new Color("#E0B85C")),
}

// ── 前に 取れた 中身を しまっておく ──
const fm = FileManager.local()
const CACHE = fm.joinPath(fm.documentsDirectory(), "slimekoubou_last.json")

function loadCache() {
  try {
    if (fm.fileExists(CACHE)) return JSON.parse(fm.readString(CACHE))
  } catch (e) {}
  return null
}

function saveCache(data) {
  try {
    fm.writeString(CACHE, JSON.stringify({ at: Date.now(), data }))
  } catch (e) {}
}

// ── 工房に 聞きにいく ──
async function fetchStatus() {
  const req = new Request(`${BASE}/api/status`)
  req.headers = { "X-Slime-Key": KEY }
  req.timeoutInterval = 8
  let json
  try {
    json = await req.loadJSON()
  } catch (e) {
    return { ok: false, why: "net" }
  }
  const code = req.response && req.response.statusCode
  if (code === 401) return { ok: false, why: "key" }
  if (code === 403) return { ok: false, why: "closed" }
  if (!json || !Array.isArray(json.sessions)) return { ok: false, why: "net" }
  return { ok: true, data: json }
}

// ── 数える ──
function summarize(data) {
  const live = (data.sessions || []).filter(s => !s.leaving)
  const perm = live.filter(s => s.waiting === "permission")
  const busy = live.filter(s => s.busy && s.waiting !== "permission")
  const reply = live.filter(s => s.waiting === "reply" && !s.busy)
  const tired = live.filter(s => s.tired)
  return { live, perm, busy, reply, tired, widget: data.widget || 0 }
}

function hhmm(ms) {
  const f = new DateFormatter()
  f.dateFormat = "H:mm"
  return f.string(new Date(ms))
}

function mins(sec) {
  if (sec < 60) return "いま"
  if (sec < 3600) return `${Math.floor(sec / 60)}分`
  return `${Math.floor(sec / 3600)}時間`
}

function openUrl(s) {
  // Claude アプリで その子の 部屋を ひらく（id が 無ければ 工房の 画面）
  return s && s.cloud ? `claude://code/${encodeURIComponent(s.cloud)}` : PAGE
}

// ── 部品 ──
function text(stack, str, size, color, bold = false) {
  const t = stack.addText(str)
  t.font = bold ? Font.boldSystemFont(size) : Font.systemFont(size)
  t.textColor = color
  t.lineLimit = 1
  t.minimumScaleFactor = 0.6
  return t
}

function countChip(stack, label, n, color) {
  const s = stack.addStack()
  s.layoutVertically()
  text(s, String(n), 20, n ? color : C.sub, true)
  text(s, label, 10, C.sub)
  return s
}

function header(w, stale) {
  const h = w.addStack()
  h.centerAlignContent()
  text(h, "🟢 スライムこうぼう", 11, C.sub, true)
  h.addSpacer()
  if (stale) text(h, "⚠︎", 11, C.weak, true)
}

// ── ホーム画面（小・中・大） ──
function buildHome(family, st) {
  const w = new ListWidget()
  w.backgroundColor = C.bg
  w.setPadding(12, 14, 12, 14)
  w.url = PAGE

  if (!configured) {
    header(w, false)
    w.addSpacer(6)
    text(w, "つなぎ先が まだです", 14, C.ink, true)
    const t = w.addText("台本の HOST と KEY を入れるか、ウィジェットの Parameter に「住所|合言葉」を書いてください")
    t.font = Font.systemFont(11)
    t.textColor = C.sub
    return w
  }

  const stale = !st.ok
  const data = st.ok ? st.data : (st.cache && st.cache.data)
  header(w, stale && !!data)
  w.addSpacer(4)

  if (!data) {
    text(w, st.why === "net" ? "PCと つながっていません" : "工房に ことわられました", 14, C.ink, true)
    w.addSpacer(2)
    const t = w.addText(whyText(st.why))
    t.font = Font.systemFont(11)
    t.textColor = C.sub
    return w
  }

  const m = summarize(data)
  const maxRows = family === "small" ? 2 : family === "medium" ? 2 : 6

  // 🔴 きょかまち
  if (m.perm.length) {
    const box = w.addStack()
    box.layoutVertically()
    box.backgroundColor = C.redBg
    box.cornerRadius = 10
    box.setPadding(6, 8, 6, 8)
    text(box, `🔴 きょかまち ${m.perm.length}`, family === "small" ? 13 : 14, C.red, true)
    m.perm.slice(0, maxRows).forEach(s => {
      const row = box.addStack()
      if (family !== "small") row.url = openUrl(s)
      text(row, `${s.name}`, 12, C.ink, true)
      row.addSpacer()
      text(row, mins(s.waited || 0), 10, C.sub)
    })
    if (m.perm.length > maxRows) text(box, `ほか ${m.perm.length - maxRows}`, 10, C.sub)
  } else {
    text(w, m.busy.length ? "みんな はたらいてる" : "みんな しずか", family === "small" ? 14 : 15, C.ink, true)
  }

  w.addSpacer(6)

  // 数
  const nums = w.addStack()
  countChip(nums, "しごと中", m.busy.length, C.blue)
  nums.addSpacer()
  countChip(nums, "へんじまち", m.reply.length, C.green)
  if (family !== "small") {
    nums.addSpacer()
    countChip(nums, "よわり", m.tired.length, C.weak)
  }

  // 大：しごと中・へんじまちの 名前
  if (family === "large") {
    w.addSpacer(8)
    const list = [...m.busy.map(s => ["💬", s]), ...m.reply.map(s => ["…", s])].slice(0, 7)
    list.forEach(([mark, s]) => {
      const row = w.addStack()
      row.url = openUrl(s)
      row.centerAlignContent()
      text(row, `${mark} ${s.name}`, 12, C.ink)
      if (s.tired) text(row, " よわり", 10, C.weak, true)
      row.addSpacer()
      text(row, s.busy ? (s.job || "しごと中") : mins(s.waited || 0), 10, C.sub)
      w.addSpacer(2)
    })
    if (m.tired.length) {
      w.addSpacer(4)
      text(w, `🫠 あたらしい部屋に うつしたい子：${m.tired.map(s => s.name).join("・")}`, 10, C.weak)
    }
  }

  w.addSpacer()
  const foot = w.addStack()
  if (stale) {
    text(foot, `⚠︎ つながらない・${hhmm(st.cache.at)} の ようす`, 10, C.weak, true)
  } else {
    text(foot, `${hhmm(Date.now())} 更新`, 9, C.sub)
    if (m.widget > VERSION) {
      foot.addSpacer()
      text(foot, "新しい版あり", 9, C.green, true)
    }
  }
  return w
}

// ── ロック画面 ──
function buildAccessory(family, st) {
  const w = new ListWidget()
  w.url = PAGE
  const data = st.ok ? st.data : null

  if (!configured || !data) {
    if (family === "accessoryInline") {
      w.addText(configured ? "🫥 PCと つながらない" : "スライム：未設定")
    } else if (family === "accessoryCircular") {
      w.addAccessoryWidgetBackground = true
      const t = w.addText("…")
      t.font = Font.boldSystemFont(18)
      t.centerAlignText()
    } else {
      text(w, "スライムこうぼう", 12, Color.white(), true)
      text(w, configured ? "PCと つながっていません" : "つなぎ先が 未設定", 12, Color.white())
      if (st.cache) text(w, `${hhmm(st.cache.at)} の ようす まで`, 10, Color.white())
    }
    return w
  }

  const m = summarize(data)

  if (family === "accessoryInline") {
    // 1行だけ
    w.addText(m.perm.length
      ? `🔴 きょかまち ${m.perm.length}：${m.perm[0].name}`
      : `💬${m.busy.length}  …${m.reply.length}${m.tired.length ? `  🫠${m.tired.length}` : ""}`)
    return w
  }

  if (family === "accessoryCircular") {
    w.addAccessoryWidgetBackground = true
    const s = w.addStack()
    s.layoutVertically()
    s.centerAlignContent()
    const top = s.addText(m.perm.length ? "許可" : "しごと")
    top.font = Font.systemFont(9)
    top.centerAlignText()
    const n = s.addText(String(m.perm.length || m.busy.length))
    n.font = Font.boldSystemFont(m.perm.length ? 22 : 18)
    n.centerAlignText()
    return w
  }

  // accessoryRectangular
  if (m.perm.length) {
    text(w, `🔴 きょかまち ${m.perm.length}`, 13, Color.white(), true)
    m.perm.slice(0, 2).forEach(s => text(w, s.name, 12, Color.white()))
  } else {
    text(w, m.busy.length ? "みんな はたらいてる" : "みんな しずか", 13, Color.white(), true)
    text(w, `しごと中 ${m.busy.length}・へんじまち ${m.reply.length}`, 12, Color.white())
    if (m.tired.length) text(w, `よわり ${m.tired.length}`, 11, Color.white())
  }
  return w
}

function whyText(why) {
  if (why === "key") return "合言葉が ちがいます"
  if (why === "closed") return "PCで「スマホから 見る」が オフです"
  return "Tailscale と PC の 電源を 見てください"
}

// ── 本体 ──
const st = configured ? await fetchStatus() : { ok: false, why: "setup" }
if (st.ok) saveCache(st.data)
else st.cache = loadCache()

const family = config.widgetFamily || "medium"
const widget = family.startsWith("accessory")
  ? buildAccessory(family, st)
  : buildHome(family, st)

// つぎに 書きかえて ほしい 時こく（iOS が きめるので 目安）
widget.refreshAfterDate = new Date(Date.now() + (st.ok ? 5 : 10) * 60 * 1000)

if (config.runsInWidget || config.runsInAccessoryWidget) {
  Script.setWidget(widget)
} else {
  // Scriptable の中で ▶ を押した時は、中サイズの 見本を 出す
  await widget.presentMedium()
}
Script.complete()

# -*- coding: utf-8 -*-
"""
スライムこうぼう ─ Claude Code の稼働を、スライムが働く姿で見せる

動いている Claude Code のセッション 1本につき、スライムが1匹。
そのスライムが「いま何をしているか」と「なんと言ったか」を持って歩く。

  セッションが始まる → スライムが1匹ふえる
  道具を使っている   → その仕事の札を持つ
  ひとこと言った     → それを吹き出しに出す（要点だけ）
  手が空いた         → 札を置いて うろうろする
  セッションが終わる → 帰っていく

外から必要なものは何も無い（標準ライブラリだけで動く）。
"""
import json
import os
import re
import urllib.parse
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8899

# 道具が終わってからも、少しのあいだ札を出しておく（一瞬で終わる仕事も見えるように）
BUSY_HOLD = 4.0
# セッションが終わってから、スライムが帰るまでの猶予
# おわったセッションも すぐには 消さない。
# 上限まで 部屋に のこって、新しい子に 押し出されるまで いる
ENDED_HOLD = 6 * 60 * 60
# 何の合図も来ないまま この時間が過ぎたセッションは、閉じたものとみなす
SESSION_GONE = 2 * 60 * 60
# ひとことを出しておく時間
SAY_HOLD = 25.0
# 名札に出す 呼び名の長さの上限
TAG_MAX = 8

# 使った道具の名前 → スライムの担当仕事
TOOL_JOBS = [
    ("しらべもの",     ["Read", "Glob", "Grep", "NotebookRead", "ToolSearch"]),
    ("かきもの",       ["Write", "Edit", "MultiEdit", "NotebookEdit"]),
    ("コマンド",       ["Bash", "PowerShell", "BashOutput", "KillShell", "Monitor"]),
    ("ネットしらべ",   ["WebSearch", "WebFetch"]),
    ("なかまをよぶ",   ["Task", "Agent", "Workflow", "SendMessage"]),
    ("メール・よてい", ["mcp__google-workspace__"]),
    ("ブラウザそうさ", ["mcp__Claude_Browser__", "mcp__claude-in-chrome__"]),
    ("しりょうづくり", ["Artifact", "Skill", "SendUserFile"]),
]
DEFAULT_JOB = "おしごと"
THINKING = "かんがえちゅう"


def tool_job(tool_name):
    if not tool_name:
        return DEFAULT_JOB
    for label, keys in TOOL_JOBS:
        for k in keys:
            if tool_name == k or tool_name.startswith(k):
                return label
    return DEFAULT_JOB


# ─────────────────────────────────────────────
# 台本（transcript）から「チャット名」と「ひとこと」を拾う
# ─────────────────────────────────────────────
# 台本は大きくなるので、末尾だけ読む
TAIL_BYTES = 300_000
# 読みなおす間隔（同じファイルを何度も舐めないため）
REREAD_EVERY = 2.0

_read_cache = {}   # path -> {"at":ts, "mtime":ts, "title":str, "say":str}


def _tidy(text):
    """飾りを落として、要点の一文だけにする"""
    t = text.strip()
    t = re.sub(r"```.*?```", "", t, flags=re.S)      # コード塊は落とす
    t = re.sub(r"`([^`]*)`", r"\1", t)               # 引用符を外す
    t = re.sub(r"\*\*([^*]*)\*\*", r"\1", t)         # 太字の印を外す
    t = re.sub(r"^[#>\-\|\s]+", "", t)               # 見出しや表の記号
    t = t.replace("[", "").replace("]", "")          # 画面側で色の指定に使う記号
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    # 最初の一文だけ。俺の書き方だと一文目に結論が来る
    m = re.split(r"(?<=[。！？!?])", t)
    first = next((x.strip() for x in m if x.strip()), t)
    # 吹き出しに収まる長さまで刈る。「要点だけ」が身上なので短く
    if len(first) > 18:
        first = first[:17] + "…"
    return first


# 区切りらしき記号。ここまでを ひとかたまりとみなす
_SPLIT = re.compile(r"[\s・/／_＿\-−ー–—（(\[【「『:：]")


def _kind(ch):
    """文字の種類。種類が変わる所を 単語の切れ目とみなす"""
    o = ord(ch)
    if 0x30A0 <= o <= 0x30FF or ch == "ー":
        return "kana"          # カタカナ
    if 0x3040 <= o <= 0x309F:
        return "hira"          # ひらがな
    if 0x4E00 <= o <= 0x9FFF:
        return "kanji"
    if ch.isalnum():
        return "ascii"
    return "other"


def first_word(name):
    """
    チャット名から「最初のひとかたまり」を取る。
    日本語は分かち書きしないので、記号か 文字の種類の変わり目で切る。
      カイガライトの続き      → カイガライト
      エグゼのリスキリング講座 → エグゼ
      SNS運用サポート         → SNS
    """
    name = (name or "").strip()
    if not name:
        return ""
    head = _SPLIT.split(name, 1)[0].strip() or name
    k0 = _kind(head[0])
    out = head[0]
    for ch in head[1:]:
        k = _kind(ch)
        # ひらがなは「の」「を」など つなぎに多いので、切れ目にする
        if k != k0:
            break
        out += ch
    if len(out) < 2:              # 1文字で切れたら 少し伸ばす
        out = head[:4]
    return out[:TAG_MAX]


def read_transcript(path):
    """末尾だけ読んで、最新のチャット名と ひとこと を返す"""
    if not path:
        return None, None
    now = time.time()
    c = _read_cache.get(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return (c or {}).get("title"), (c or {}).get("say")
    if c and now - c["at"] < REREAD_EVERY and c["mtime"] == mtime:
        return c["title"], c["say"]

    title = (c or {}).get("title")
    say = (c or {}).get("say")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read().decode("utf-8", "ignore")
        lines = chunk.split("\n")
        if size > TAIL_BYTES:
            lines = lines[1:]          # 途中で切れた行は捨てる
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") == "custom-title" and r.get("customTitle"):
                title = r["customTitle"]
            elif r.get("type") == "assistant":
                for b in ((r.get("message") or {}).get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "text":
                        s = _tidy(b.get("text") or "")
                        if s:
                            say = s
    except Exception:
        pass

    _read_cache[path] = {"at": now, "mtime": mtime, "title": title, "say": say}
    return title, say


# ─────────────────────────────────────────────
# セッションの様子
# ─────────────────────────────────────────────
_lock = threading.Lock()
_sessions = {}   # session_id -> dict


def _touch(sid, payload):
    now = time.time()
    s = _sessions.get(sid)
    if s is None:
        s = _sessions[sid] = {
            "started": now, "busy": 0, "job": None, "job_until": 0.0,
            "ended": None, "say": None, "say_at": 0.0, "title": None,
            # つかい手の番になっている状態： None / "reply"(へんじまち) / "permission"(きょかまち)
            "waiting": None, "waiting_at": 0.0,
        }
    s["last"] = now
    if payload.get("cwd"):
        s["cwd"] = payload["cwd"]
    if payload.get("transcript_path"):
        s["transcript"] = payload["transcript_path"]
    return s


# ── おぼえておく ──
# サーバーを 入れなおしたり パソコンを 再起動しても、
# さっきまでの スライムが 部屋に のこるように、
# セッションの ようすを ファイルに 書いておく
STATE = os.path.join(HERE, "_state.json")
STATE_KEEP = 30                 # 多くても これだけ おぼえる
_state_at = 0.0


def save_state(force=False):
    global _state_at
    now = time.time()
    if not force and now - _state_at < 20.0:
        return
    _state_at = now
    try:
        with _lock:
            items = sorted(_sessions.items(),
                           key=lambda kv: kv[1].get("last", 0), reverse=True)
            keep = {}
            for sid, s in items[:STATE_KEEP]:
                d = dict(s)
                # 動いている とちゅうの ぶんは 持ちこさない。
                # 入れなおした後は「おわった子」として 部屋に のこる
                d["busy"] = 0
                d["job"] = None
                d["job_until"] = 0.0
                d["waiting"] = None
                if d.get("ended") is None:
                    d["ended"] = now
                keep[sid] = d
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved": now, "sessions": keep}, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception:
        pass


def load_state():
    """前に 書いておいた ようすを 読みもどす"""
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return 0
    now = time.time()
    n = 0
    for sid, s in (d.get("sessions") or {}).items():
        if now - s.get("last", 0) > SESSION_GONE:
            continue
        s["busy"] = 0
        s["job"] = None
        s["job_until"] = 0.0
        s["waiting"] = None
        if s.get("ended") is None:
            s["ended"] = now
        _sessions[sid] = s
        n += 1
    return n


def on_hook(event, payload):
    """Claude Code から届いた合図を、スライムの様子に翻訳する"""
    sid = payload.get("session_id") or "unknown"
    tool = payload.get("tool_name") or ""
    now = time.time()
    with _lock:
        s = _touch(sid, payload)
        # 何か 合図が 来たなら まだ 生きている
        if event not in ("end",):
            s["ended"] = None
        if event == "start":
            s["ended"] = None
            s["waiting"] = None
        elif event == "end":
            s["ended"] = now
            s["waiting"] = None
        elif event == "prompt":
            # つかい手が返事した → 待ちは解ける
            s["ended"] = None
            s["waiting"] = None
            s["job"] = THINKING
            s["job_until"] = now + BUSY_HOLD
        elif event == "pre":
            s["busy"] += 1
            s["waiting"] = None
            s["job"] = tool_job(tool)
            s["job_until"] = now + BUSY_HOLD
        elif event == "perm":
            # 許可を求めて止まっている。つかい手が答えるまで進めない
            s["waiting"] = "permission"
            s["waiting_at"] = now
        elif event == "notify":
            # 何か知らせが出ている。許可待ちが分かっている時は そちらを優先
            if not s["waiting"]:
                s["waiting"] = "reply"
                s["waiting_at"] = now
        elif event == "post":
            s["busy"] = max(0, s["busy"] - 1)
            s["waiting"] = None
            if s["busy"] == 0:
                s["job"] = THINKING
                s["job_until"] = now + BUSY_HOLD
        elif event == "stop":
            # ひと区切り。ここからは つかい手の番
            s["busy"] = 0
            s["job"] = None
            s["job_until"] = 0.0
            s["waiting"] = "reply"
            s["waiting_at"] = now


def snapshot():
    save_state()
    bridge = bridge_ids()
    """いま画面に出すべき、セッションの一覧"""
    now = time.time()
    out = []
    with _lock:
        for sid in list(_sessions.keys()):
            s = _sessions[sid]
            if s["ended"] is not None:
                if now - s["ended"] > ENDED_HOLD:
                    del _sessions[sid]
                    continue
            elif now - s.get("last", 0) > SESSION_GONE:
                del _sessions[sid]
                continue

            title, say = read_transcript(s.get("transcript"))
            if title:
                s["title"] = title
            if say and say != s.get("say"):
                s["say"] = say
                s["say_at"] = now

            job = s["job"] if now < s["job_until"] else (s["job"] if s["busy"] else None)
            folder = os.path.basename((s.get("cwd") or "").rstrip("\\/")) or ""
            name = s.get("title") or folder or "むめい"
            if is_auto_session(sid, name):
                continue          # 時間で うごく しごと。スライムには しない
            out.append({
                "id": sid,
                "name": name,
                "tag": first_word(name),
                "folder": folder,
                "job": job,
                "say": s["say"] if (s["say"] and now - s["say_at"] < SAY_HOLD) else None,
                "busy": bool(job),
                "waiting": s["waiting"],
                "cloud": bridge.get(sid),
                "waited": int(max(0, now - s["waiting_at"])) if s["waiting"] else 0,
                "leaving": s["ended"] is not None,
                "elapsed": int(max(0, now - s["started"])),
                "idle": int(max(0, now - s.get("last", now))),   # 最後に動いてからの秒数
            })
    # 「さっき動いたもの」が先に来るように並べる。
    # 画面はこの順で上から必要な数だけ出す
    def rank(x):
        if x["waiting"] == "permission":
            return 0        # 止まっている。まず目に入るべき
        if x["busy"]:
            return 1
        if x["waiting"] == "reply":
            return 2
        if not x["leaving"]:
            return 3        # まだ 生きている
        return 4            # おわった子。押し出されるのは この子から
    out.sort(key=lambda x: (rank(x), x["idle"]))
    todo = read_todo()
    return {"sessions": out, "total": len(out), "rev": page_rev(),
            "soon": next_task(),
            "todo": [{"id": t.get("id"), "title": t.get("title", ""),
                      "session": t.get("session", ""),
                      "tag": first_word(t.get("session", "")),
                      "why": t.get("why", ""),
                      "url": t.get("url", "")} for t in todo]}


def page_rev():
    """
    画面ファイルの更新時刻。開きっぱなしのスマホに
    「中身が新しくなったよ」と伝えるための目印
    """
    try:
        return int(os.path.getmtime(os.path.join(HERE, "index.html")))
    except OSError:
        return 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _log(self, msg):
        """スマホから本当に届いているかを見るための ちいさな控え"""
        try:
            with open(os.path.join(HERE, "_access.log"), "a", encoding="utf-8") as f:
                f.write("%s  %s  %s\n" % (time.strftime("%H:%M:%S"),
                                          self.client_address[0], msg))
        except Exception:
            pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if extra:
            self.send_header(extra[0], extra[1])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ── だれから 来た お願いか ──
    def _local(self):
        """このPCの中から きた か。ここからは いつでも ぜんぶ 見せる"""
        ip = (self.client_address or ("",))[0]
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _key(self):
        """お願いに ついてきた 合言葉"""
        k = self.headers.get("X-Slime-Key")
        if k:
            return k.strip()
        from urllib.parse import urlparse, parse_qs
        return (parse_qs(urlparse(self.path).query).get("t") or [""])[0].strip()

    def _allowed(self):
        """ほかの機械から でも 見せていいか。
        だめな時は こちらで 返事まで して False を かえす"""
        if self._local():
            return True
        if not SHARE:
            self._send(403, json.dumps(
                {"error": "closed",
                 "msg": "このPCの設定で「スマホから 見る」が 入っていません"},
                ensure_ascii=False))
            return False
        import hmac
        if not TOKEN or not hmac.compare_digest(self._key(), TOKEN):
            self._send(401, json.dumps(
                {"error": "key", "msg": "合言葉が ちがいます。QRを 読みなおしてください"},
                ensure_ascii=False))
            return False
        return True

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        # やり残しの チェックだけは スマホからも 受ける
        if u.path == "/api/todo/done":
            if not self._allowed():
                return
            from urllib.parse import parse_qs as _pq
            tid = (_pq(u.query).get("id") or [""])[0]
            ok = done_todo(tid)
            return self._send(200 if ok else 404, json.dumps({"ok": ok}))
        # 工房の 中身を 書きかえる ところ。このPCの中からだけ
        if u.path == "/api/config":
            if not self._local():
                return self._send(403, json.dumps({"error": "local only"}))
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:
                body = {}
            ch = {}
            if "share" in body:
                ch["share"] = bool(body["share"])
            if body.get("show") in ("all", "work", "name"):
                ch["show"] = body["show"]
            if ch:
                save_config(**ch)
                load_config()
            return self._send(200, json.dumps(
                {"share": SHARE, "show": SHOW}, ensure_ascii=False))
        # しごとの しらせ（フック）は このPCの中からしか 受けない。
        # 開けておくと、同じWiFiの だれかが うその しごとを 送りこめる
        if u.path != "/api/hook":
            return self._send(404, "not found", "text/plain; charset=utf-8")
        if not self._local():
            return self._send(403, "local only", "text/plain; charset=utf-8")
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        event = (parse_qs(u.query).get("e") or [""])[0]
        try:
            on_hook(event, payload)
        except Exception:
            pass
        return self._send(200, '{"ok":true}')

    def do_GET(self):
        import mimetypes
        from urllib.parse import unquote
        path = unquote(self.path.split("?")[0])
        if path in ("/", "/index.html"):
            # 画面そのものは だれが 見ても かまわない（中身は 入っていない）。
            # 中身を もらう時に あらためて 合言葉を みる
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/api/status":
            if not self._allowed():
                return
            snap = snapshot()
            if not self._local():
                snap = trim(snap, SHOW)
            return self._send(200, json.dumps(snap, ensure_ascii=False))
        if path == "/api/info":
            if not self._allowed():
                return
            out = {"you": USER_NAME}
            if self._local():
                # 住所と 合言葉は このPCの画面にだけ 見せる
                out["phone_url"] = f"http://{phone_ip()}:{PORT}/?t={urllib.parse.quote(TOKEN)}"
                out["pair_url"] = pair_url()
                out["qr"] = qr_svg() is not None
                out["share"] = SHARE
                out["show"] = SHOW
            return self._send(200, json.dumps(out, ensure_ascii=False))
        if path == "/qr_pair.svg":
            if not self._local():
                return self._send(403, "local only", "text/plain; charset=utf-8")
            svg = qr_svg(pair_url())
            if svg is None:
                return self._send(404, "no qr", "text/plain; charset=utf-8")
            return self._send(200, svg, "image/svg+xml; charset=utf-8")
        if path == "/qr.svg":
            if not self._local():
                return self._send(403, "local only", "text/plain; charset=utf-8")
            svg = qr_svg(f"http://{phone_ip()}:{PORT}/?t={urllib.parse.quote(TOKEN)}")
            if svg is None:
                return self._send(404, "no qr", "text/plain; charset=utf-8")
            return self._send(200, svg, "image/svg+xml; charset=utf-8")

        rel = path.lstrip("/")
        target = os.path.abspath(os.path.join(HERE, rel))
        if not target.startswith(HERE) or not os.path.isfile(target):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        # Windows は .js を text/plain と答えることがあり、それだと
        # ホーム画面アプリの係（sw.js）が動かない。ここで直しておく
        low = target.lower()
        extra = None
        if low.endswith(".js"):
            ctype = "text/javascript"
        elif low.endswith(".json"):
            ctype = "application/json"
        elif low.endswith(".apk"):
            # Android に「入れられる荷物だ」と分かる型で返す。
            # octet-stream のままだと 端末側が受けとりを進めないことがある
            ctype = "application/vnd.android.package-archive"
            extra = ('Content-Disposition',
                     'attachment; filename="%s"' % os.path.basename(target))
            self._log("APK を渡した: " + os.path.basename(target))
        with open(target, "rb") as f:
            self._send(200, f.read(), ctype, extra)

    def _file(self, name, ctype):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, name + " not found", "text/plain; charset=utf-8")


def lan_ip():
    """同じWiFi内のスマホからアクセスするための、このPCのIPを調べる"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


_BRIDGE = {}
_BRIDGE_AT = 0.0
_KIND = {}          # sessionId → (kind, entrypoint)


def bridge_ids():
    """
    ~/.claude/sessions/<pid>.json には
      sessionId       … こちらが 使っている id
      bridgeSessionId … スマホの Claude が 使う id
    が 並んでいる。その対応を ひろって おく
    """
    global _BRIDGE, _BRIDGE_AT
    now = time.time()
    if now - _BRIDGE_AT < 10.0:
        return _BRIDGE
    _BRIDGE_AT = now
    out = {}
    d = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
    try:
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                    j = json.load(f)
            except Exception:
                continue
            sid = j.get("sessionId")
            if not sid:
                continue
            if j.get("bridgeSessionId"):
                out[sid] = j["bridgeSessionId"]
            # その セッションの 素性。自動で 立ったものを 名まえに たよらず
            # 見わけるための 手がかり
            _KIND[sid] = (j.get("kind"), j.get("entrypoint"))
    except Exception:
        pass
    _BRIDGE = out
    return out


# ── 時間で うごく しごと（定期タスク） ──
_TASKS = []
_TASKS_AT = 0.0


def _task_store():
    """定期タスクの おきば。Claude の 保存フォルダの 中にある"""
    import glob
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    found = glob.glob(os.path.join(
        base, "Claude", "claude-code-sessions", "*", "*", "scheduled-tasks.json"))
    # Windows ストア版の Python からは AppData の中の Claude が 見えない。
    # そのため フックで ここに 写しを 置いてもらっている
    mirror = os.path.join(os.path.expanduser("~"), ".claude",
                          "scheduled-tasks", "_mirror.json")
    if os.path.exists(mirror):
        found.append(mirror)
    return found


def _cron_next(expr, now):
    """
    かんたんな cron（分 時 日 月 曜）から、つぎに 動く時刻を さがす。
    1分ずつ 8日先まで 見る。むずかしい書きかたには 対応しない
    """
    import datetime
    parts = expr.split()
    if len(parts) < 5:
        return None

    def hit(field, val, lo, hi):
        if field == "*":
            return True
        for part in field.split(","):
            if part.startswith("*/"):
                try:
                    if (val - lo) % int(part[2:]) == 0:
                        return True
                except ValueError:
                    pass
            elif "-" in part:
                try:
                    a, b = part.split("-")
                    if int(a) <= val <= int(b):
                        return True
                except ValueError:
                    pass
            else:
                try:
                    if int(part) == val:
                        return True
                except ValueError:
                    pass
        return False

    t = datetime.datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    t += datetime.timedelta(minutes=1)
    for _ in range(8 * 24 * 60):
        dow = (t.weekday() + 1) % 7          # cron は 日曜が 0
        if (hit(parts[0], t.minute, 0, 59) and hit(parts[1], t.hour, 0, 23)
                and hit(parts[2], t.day, 1, 31) and hit(parts[3], t.month, 1, 12)
                and hit(parts[4], dow, 0, 6)):
            return t.timestamp()
        t += datetime.timedelta(minutes=1)
    return None


def _task_label(path):
    """SKILL.md の 頭に 書いてある せつめいを 短く"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(600)
    except Exception:
        return ""
    for line in head.splitlines():
        if line.startswith("description:"):
            t = line.split(":", 1)[1].strip()
            return t[:12] + ("…" if len(t) > 12 else "")
    return ""


def task_ids():
    """
    定期タスクの 名まえ一覧。
    時間で うごく しごとが 立てた セッションは、
    会話の名まえが その まま タスクの名まえに なるので、
    それを 手がかりに スライムから 外す
    （スライム＝つかい手が 話している セッション、という決まりを 守るため）
    """
    import json as _json
    out = set()
    for path in _task_store():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = _json.load(f)
        except Exception:
            continue
        for t in data.get("scheduledTasks", []):
            if t.get("id"):
                out.add(str(t["id"]).lower())
    return out


# 会話の名まえに これが 入っていたら、時間で うごく しごと とみなす。
#
# これは「名づけの クセ」に たよった 目やすなので、
# 人によって 変えてください。空 () にすれば 名まえでは 判断しません。
# （素性で 見わける ほうが 確かです。HUMAN_KINDS を 見てください）
TASK_WORDS = ("（自動", "(自動")


# 人が 話している セッションの 素性。これ以外は 自動と みなす
HUMAN_KINDS = {"interactive"}


def note_kind(sid, name):
    """はじめて 見た 素性を 書きとめておく（あとで 判断を よくするため）"""
    k = _KIND.get(sid)
    if not k or k[0] in HUMAN_KINDS:
        return
    try:
        tab, nl = chr(9), chr(10)
        line = (time.strftime("%Y-%m-%d %H:%M:%S") + tab + repr(k) + tab
                + (name or "")[:40] + nl)
        with open(os.path.join(HERE, "_kinds.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def is_auto_session(sid, name):
    """
    その 会話は 自動で 立ったものか。
    まず 素性で 見わける（どの人でも 使える）。
    素性が わからない ぶんは 名まえで 見わける（こちらは 目やす）
    """
    bridge_ids()                      # _KIND を ためておく
    k = _KIND.get(sid)
    if k and k[0] and k[0] not in HUMAN_KINDS:
        note_kind(sid, name)
        return True
    return is_task_session(name)


def is_task_session(name):
    """名まえから 見わける（素性が わからない時の 目やす）"""
    if not name:
        return False
    for w in TASK_WORDS:
        if w in name:
            return True
    key = name.strip().lower().replace(" ", "-").replace("　", "-")
    return key in task_ids()


def next_task():
    """つぎに 動く 定期タスク。{when, label} か None"""
    global _TASKS, _TASKS_AT
    import json as _json
    now = time.time()
    if now - _TASKS_AT < 60.0:
        return _TASKS
    _TASKS_AT = now

    best = None
    for path in _task_store():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = _json.load(f)
        except Exception:
            continue
        for t in data.get("scheduledTasks", []):
            if not t.get("enabled"):
                continue
            at = None
            if t.get("fireAt"):
                at = t["fireAt"] / 1000.0
            elif t.get("cronExpression"):
                at = _cron_next(t["cronExpression"], now)
            if at is None or at < now:
                continue
            if best is None or at < best[0]:
                best = (at, t)
    if best is None:
        _TASKS = None
        return None

    at, t = best
    lt = time.localtime(at)
    today = time.localtime(now)
    if lt.tm_yday == today.tm_yday and lt.tm_year == today.tm_year:
        when = time.strftime("%H:%M", lt)
    elif at - now < 36 * 3600:
        when = "あした " + time.strftime("%H:%M", lt)
    else:
        when = time.strftime("%m/%d %H:%M", lt).lstrip("0")
    _TASKS = {"when": when, "label": _task_label(t.get("filePath", ""))}
    return _TASKS


# ── 本人でないと できない しごと ──
# 台帳（markdown）の 中から、印の ついた 行だけを ひろって 部屋に出す。
# 置き場を 2つに 分けると かならず ズレるので、台帳は 1つだけ にする。
#
#   - [ ] Driveのゴミ箱を空にする @me #ボイストランド
#     └ チェックが 空いている ＋ 印(@me) が ある行だけ 出す
#     └ #うしろ は どの しごとの ものか（なくてもよい）
#
# 台帳の場所と 印は _config.json で 変えられる。
# 人によって 台帳の 置き場は ちがうので、決めうちに しない
TODO_FILE = os.path.join(HERE, "todo.md")
TODO_MARK = "@me"
# スライムが つかい手を 呼ぶ 名まえ。ここに 書いておくと、
# このPCで 見る どの画面でも 同じ 呼び名に なる（画面ごとに 入れなおさずに すむ）
USER_NAME = ""

# ── ほかの機械から 見られないように する ──
# この工房が 出すのは「いま どの しごとを していて、なにを 待っているか」。
# 名前も やり残しも そのまま 出るので、同じWiFiに いる だれかに
# 読まれると こまる。だから：
#
#   このPCの中（127.0.0.1）から  … いつでも ぜんぶ 見せる
#   ほかの機械から              … SHARE が 入っていて、合言葉が 合った時だけ
#
# 合言葉は はじめて うごかした時に 作って _config.json に しまう。
# QRに 混ぜてあるので、スマホは 読みとるだけで 入る
CONFIG = os.path.join(HERE, "_config.json")
SHARE = False           # スマホから 見るか
SHOW = "all"            # スマホに 見せるもの all / work / name
TOKEN = ""              # 合言葉


def load_config():
    global TODO_FILE, TODO_MARK, USER_NAME, SHARE, SHOW, TOKEN
    d = {}
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    TODO_FILE = os.path.expanduser(d.get("todo_file") or TODO_FILE)
    TODO_MARK = d.get("todo_mark") or TODO_MARK
    USER_NAME = (d.get("user_name") or "").strip()
    SHARE = bool(d.get("share"))
    SHOW = d.get("show") if d.get("show") in ("all", "work", "name") else "all"
    TOKEN = (d.get("token") or "").strip()
    if not TOKEN:
        import secrets
        TOKEN = secrets.token_urlsafe(18)
        save_config(token=TOKEN)
    return d


def save_config(**changes):
    """いま書いてあるものを こわさずに、変えたぶんだけ 書きかえる"""
    d = {}
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.update(changes)
    try:
        tmp = CONFIG + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG)
        return True
    except Exception:
        return False


load_config()

_TODO_LINE = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.+?)\s*$")


def _todo_id(text):
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]


def read_todo():
    """台帳から 印のついた やり残しを ひろう"""
    out = []
    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
    except Exception:
        return out
    for line in lines:
        m = _TODO_LINE.match(line)
        if not m or m.group(1) != " ":
            continue                     # 見出しや、済みの行は とばす
        body = m.group(2)
        if TODO_MARK not in body:
            continue
        title = body.replace(TODO_MARK, "").strip()
        tag = ""
        mt = re.search(r"#([^\s#]+)\s*$", title)
        if mt:
            tag = mt.group(1)
            title = title[:mt.start()].strip()
        title = title.strip(" 　・-—")
        if not title:
            continue
        out.append({"id": _todo_id(body), "title": title,
                    "session": tag, "why": "", "url": "", "raw": body})
    return out


def done_todo(tid):
    """その行の チェックを 付ける（消さずに 残す）"""
    if not tid:
        return False
    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return False
    lines = text.split("\n")
    hit = False
    for i, line in enumerate(lines):
        m = _TODO_LINE.match(line)
        if not m or m.group(1) != " ":
            continue
        if _todo_id(m.group(2)) != tid:
            continue
        lines[i] = line.replace("[ ]", "[x]", 1)
        hit = True
        break
    if not hit:
        return False
    try:
        tmp = TODO_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
        os.replace(tmp, TODO_FILE)
        return True
    except Exception:
        return False


def ts_ip():
    """
    Tailscale を 入れていれば、その住所（100.x）を返す。
    この住所は 家の中でも 外出先でも 同じものが 使える。

    ホスト名からの 逆引きでは Tailscale の回線は 出てこないので、
    Tailscale の中の DNS(100.100.100.100) あての 経路を たずねて、
    どの回線から 出ていくかを OS に 教えてもらう
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("100.100.100.100", 80))
        ip = s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()
    a = ip.split(".")
    try:
        if a[0] == "100" and 64 <= int(a[1]) <= 127:
            return ip
    except Exception:
        pass
    return None


def phone_ip():
    """スマホに教える住所。Tailscale があれば そちらを優先する"""
    return ts_ip() or lan_ip()


def trim(snap, level):
    """ほかの機械へ わたす ぶんを、えらんだ ところまで けずる。
    このPCの画面には ぜんぶ 出したままに したいので、渡す時だけ けずる"""
    out = dict(snap)
    if level == "all":
        # ぜんぶ 見せる ときでも、PCの中の フォルダ名までは 出さない
        out["sessions"] = [{k: v for k, v in x.items() if k != "folder"}
                           for x in snap.get("sessions", [])]
        return out
    out["todo"] = []          # やり残しは 中身が 生なので まっさきに 落とす
    out["todoCount"] = 0
    out["soon"] = None        # よていの 見出しも 用件そのもの
    ss = []
    for x in snap.get("sessions", []):
        y = dict(x)
        y["say"] = None                       # ひとことは 会話の 中身そのもの
        if level == "name":
            y["job"] = None                   # なにを しているかも 伏せる
        y.pop("folder", None)
        y.pop("cloud", None)
        ss.append(y)
    out["sessions"] = ss
    return out


def pair_url():
    """
    アプリに 住所を わたす ための リンク。
    スマホの ふつうのカメラで 読みとると、そのまま アプリが ひらいて
    住所が 入る。アプリに カメラを つけなくて すむ
    """
    return ("slimekoubou://pair?host="
            + urllib.parse.quote("http://%s:%d" % (phone_ip(), PORT), safe="")
            + "&key=" + urllib.parse.quote(TOKEN, safe=""))


def qr_svg(url=None):
    """スマホ用URLのQRコードを描く。ライブラリが無ければ None"""
    try:
        import io
        import segno
    except Exception:
        return None
    try:
        buf = io.BytesIO()
        segno.make(url or f"http://{phone_ip()}:{PORT}/", error="m").save(
            buf, kind="svg", scale=1, border=2,
            dark="#3a4550", light="#ffffff", svgclass=None, lineclass=None)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return None


def already_running():
    """すでに工房が開いているか（PC起動時の二重起動よけ）"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def main():
    quiet = "--quiet" in sys.argv     # PC起動時など、勝手にブラウザを開かせたくない時

    if already_running():
        print("すでに開いています。二重には起動しません。")
        return

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"ポート {PORT} が使えませんでした: {e}")
        return

    back = load_state()
    if back:
        print(f"  まえの ようすを {back} ほん 読みもどしました")

    local = f"http://127.0.0.1:{PORT}/"
    phone = f"http://{phone_ip()}:{PORT}/"
    print("=" * 52)
    print(" スライムこうぼう 起動中")
    print(f"  このPCで見る    : {local}")
    print(f"  スマホで見る    : {phone}")
    if ts_ip():
        print("   （Tailscale の住所。家でも 外出先でも これ1つでつながる）")
    else:
        print("   （同じWiFiにつないだスマホのブラウザで開いてね）")
    print("  （このウィンドウは開いたままにしてください）")
    print("=" * 52)
    sys.stdout.flush()
    if not quiet:
        try:
            webbrowser.open(local)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        save_state(force=True)
        print("\n終了しました")


if __name__ == "__main__":
    main()

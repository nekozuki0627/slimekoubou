# -*- coding: utf-8 -*-
"""
スライムこうぼう ─ デスクトップに置くウィジェット姿

枠なし・うしろが透ける小さな窓に、工房だけを出す。
サーバー（monitor.py）が動いていなければ、この中で一緒に立ち上げる。

  つかみ方   … 窓のどこでも つかんで動かせる
  大きさ     … 右下の すみを つかんで ひっぱる
  とじ方     … 右上の × （ふだんは薄く、乗せると はっきりする）

── うしろを 透かす やりかた ──
2つ ある。

 ① 色ぬき（TransparencyKey）… ある色だけを 消す
    見た目は きれいに 透ける。けれど WebView2 は 中身を
    べつの しくみで 重ねて 描くので、Windows からは
    「窓は ぜんぶ その色」に 見えてしまう。
    → 窓ぜんぶが クリック素通りになり、なにも さわれなくなる。

 ② 窓を 切りぬく（SetWindowRgn）… 形の外は そもそも 窓ではなくなる
    外は 透けたうえに、うしろの デスクトップを そのまま さわれる。
    中は ふつうの窓 なので、これまでどおり さわれる。

②を つかう。切りぬく形は 部屋の絵（room_cut.png）の 中身のある所と、
部屋の外に ある 目じるし（帯・ボタン・すみ）のぶん。目じるしの 場所は
画面がわ（index.html）が 割合で 教えてくれる。

大きさは ぜんぶ「じっさいの 窓の 見た目の 大きさ」で かぞえる。
pywebview に 頼む数字は 画面の 拡大率で ずれるので、
MoveWindow で 直に 動かして、GetWindowRect で 直に 測る。
"""
import ctypes
import json
import os
import socket
import sys
import threading
import time

import webview

import monitor

HERE = os.path.dirname(os.path.abspath(__file__))
URL = f"http://127.0.0.1:{monitor.PORT}/?widget=1"
STATE = os.path.join(HERE, "_widget.json")
ROOM_PNG = os.path.join(HERE, "room_cut.png")
LOCK_PORT = 8898        # 二重に 開かないための 見はり口（8899 は 工房の 番号）

ASPECT = 1774 / 887     # 部屋の かたち。切りぬく形と そろえる
MIN_W, MAX_W = 320, 2400
DEF_W = 700

RGN_OR = 2
VK_LBUTTON = 0x01

u32 = ctypes.windll.user32
gdi = ctypes.windll.gdi32

_spans = None       # 部屋の絵の 行ごとの「左端〜右端」
_room_wh = (1774, 887)
_chrome = [[]]      # 部屋の外に 残しておく ぶん（窓に対する 割合で）
_grab = [False]     # すみを つかんでいる さいちゅうか
_full = [False]     # 切りぬきを いったん やめているか（設定を 開いている間）


# ── おぼえておく ──
def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        w = max(MIN_W, min(MAX_W, int(d.get("w") or DEF_W)))
        return {"w": w, "h": int(round(w / ASPECT)),
                "x": d.get("x"), "y": d.get("y")}
    except Exception:
        return {"w": DEF_W, "h": int(round(DEF_W / ASPECT)), "x": None, "y": None}


def save_state(d):
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception:
        pass


# ── 窓を 直に さわる ──
def form_hwnd():
    try:
        from webview.platforms.winforms import BrowserView
        for f in BrowserView.instances.values():
            return int(f.Handle.ToInt64())
    except Exception:
        pass
    return 0


def win_rect(hwnd):
    r = (ctypes.c_int * 4)()
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return [r[0], r[1], r[2] - r[0], r[3] - r[1]]


def set_rect(hwnd, x, y, w, h):
    u32.MoveWindow(hwnd, int(x), int(y), int(w), int(h), True)


def fit_height(w):
    """はばに 見あう たかさ（部屋と 同じ かたちに なるように）"""
    return int(round(w / ASPECT))


# ── 切りぬく形 ──
def room_spans():
    """部屋の絵の かたちを、行ごとの ひとつづきの はばで あらわす"""
    global _spans, _room_wh
    if _spans is not None:
        return _spans
    try:
        from PIL import Image
        im = Image.open(ROOM_PNG).convert("RGBA")
        w, h = im.size
        _room_wh = (w, h)
        mask = im.split()[3].point(lambda v: 255 if v > 16 else 0)
        out = []
        for y in range(h):
            bb = mask.crop((0, y, w, y + 1)).getbbox()
            out.append(None if bb is None else (bb[0], bb[2]))
        _spans = out
    except Exception:
        _spans = []      # 絵が 読めなければ 切りぬかない（ふつうの 四角い窓に なる）
    return _spans


def apply_region(hwnd, w, h):
    """いまの 窓の大きさに あわせて 切りぬきなおす"""
    if _full[0]:
        u32.SetWindowRgn(hwnd, 0, True)     # 設定を 開いている間は 四角いまま
        return
    spans = room_spans()
    if not spans or w <= 0 or h <= 0:
        return
    rw, rh = _room_wh
    # 絵は はみ出さずに まんなかへ（kaplay と 同じ 置きかた）
    sc = min(w / float(rw), h / float(rh))
    ox, oy = (w - rw * sc) / 2.0, (h - rh * sc) / 2.0
    total = gdi.CreateRectRgn(0, 0, 0, 0)
    for dy in range(h):
        iy = int((dy - oy) / sc)
        if iy < 0 or iy >= rh:
            continue
        sp = spans[iy]
        if not sp:
            continue
        piece = gdi.CreateRectRgn(int(ox + sp[0] * sc), dy,
                                  int(ox + sp[1] * sc) + 1, dy + 1)
        gdi.CombineRgn(total, total, piece, RGN_OR)
        gdi.DeleteObject(piece)
    # 部屋の外の 目じるしぶん。角の まるみも あわせる
    for c in _chrome[0]:
        try:
            l, t, cw, ch, rad = c[0] * w, c[1] * h, c[2] * w, c[3] * h, c[4] * w
        except Exception:
            continue
        if cw < 1 or ch < 1:
            continue
        d = int(max(0, min(rad, min(cw, ch) / 2)) * 2)
        if d >= 2:
            piece = gdi.CreateRoundRectRgn(int(l), int(t), int(l + cw) + 1,
                                           int(t + ch) + 1, d, d)
        else:
            piece = gdi.CreateRectRgn(int(l), int(t), int(l + cw) + 1, int(t + ch) + 1)
        gdi.CombineRgn(total, total, piece, RGN_OR)
        gdi.DeleteObject(piece)
    u32.SetWindowRgn(hwnd, total, True)   # 窓が この形を 引きとる


class Api:
    """窓の中の × や 右下のすみ から呼ばれる。

    pywebview は この入れものの「表に出ている もちもの」を
    まるごと JS に わたそうとする。窓そのものを 持たせると
    たどりきれずに 橋渡しが こわれるので、名前を _ で かくす
    """

    def __init__(self):
        self._win = None

    def _hwnd(self):
        return form_hwnd()

    def quit(self):
        for w in list(webview.windows):
            w.destroy()

    def size(self):
        """いまの 窓の 見た目の 大きさ"""
        h = self._hwnd()
        r = win_rect(h) if h else None
        if not r:
            return {"w": DEF_W, "h": fit_height(DEF_W)}
        return {"w": r[2], "h": r[3]}

    def resize(self, w, h=None):
        """設定の「まどの おおきさ」から 呼ばれる"""
        hwnd = self._hwnd()
        r = win_rect(hwnd) if hwnd else None
        if not r:
            return False
        w = max(MIN_W, min(MAX_W, int(w)))
        set_rect(hwnd, r[0], r[1], w, fit_height(w))
        return True

    def grab(self):
        """すみを つかんだ。はなすまで、こちらで 指を 追いかける。

        窓は 部屋の かたちに 切りぬいてある。大きくしていく あいだ
        指は かたちの外へ 出てしまい、画面の中の しくみでは
        動きが 届かなくなるので、指の いちを 直に 見る
        """
        if _grab[0]:
            return False
        hwnd = self._hwnd()
        if not hwnd:
            return False
        _grab[0] = True
        threading.Thread(target=_follow, args=(hwnd,), daemon=True).start()
        return True

    def shape(self, full):
        """設定の画面を 出している間は、切りぬきを いったん やめる。

        設定は 窓ぜんたいに かぶせて 出すので、
        部屋の かたちに 切りぬいたままだと 端が 欠けてしまう
        """
        want = bool(full)
        if want == _full[0]:
            return True
        _full[0] = want
        hwnd = self._hwnd()
        r = win_rect(hwnd) if hwnd else None
        if not r:
            return False
        apply_region(hwnd, r[2], r[3])
        return True

    def chrome(self, rects):
        """部屋の外に ある 目じるしの 場所（窓に対する 割合）。

        窓は 部屋の かたちに 切りぬくので、このままだと 消えてしまう。
        もらった ぶんだけ 切りぬきに 足す
        """
        try:
            got = [[float(v) for v in r] for r in (rects or [])]
        except Exception:
            return False
        if got == _chrome[0]:
            return True
        _chrome[0] = got
        hwnd = self._hwnd()
        r = win_rect(hwnd) if hwnd else None
        if r:
            apply_region(hwnd, r[2], r[3])
        return True


class _PT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int)]


def _follow(hwnd):
    """すみを はなすまで、指に ついていく"""
    p = _PT()
    u32.GetCursorPos(ctypes.byref(p))
    r0 = win_rect(hwnd)
    if not r0:
        _grab[0] = False
        return
    x0, w0 = p.x, r0[2]
    try:
        while u32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            u32.GetCursorPos(ctypes.byref(p))
            w = max(MIN_W, min(MAX_W, w0 + (p.x - x0)))
            r = win_rect(hwnd)
            if not r:
                break
            if abs(w - r[2]) >= 2:
                set_rect(hwnd, r[0], r[1], w, fit_height(w))
            time.sleep(0.02)
    finally:
        _grab[0] = False


def watch(hwnd_box):
    """動かした・大きさを変えた のを おぼえて、切りぬきも 追いかける"""
    last = None
    shape = None
    while True:
        time.sleep(0.35)
        hwnd = hwnd_box[0] or form_hwnd()
        if not hwnd:
            continue
        hwnd_box[0] = hwnd
        r = win_rect(hwnd)
        if not r or r[2] <= 0:
            continue
        if (r[2], r[3]) != shape:
            shape = (r[2], r[3])
            apply_region(hwnd, r[2], r[3])
        now = {"w": r[2], "h": r[3], "x": r[0], "y": r[1]}
        if now != last:
            last = now
            save_state(now)


def only_one():
    """すでに 開いていたら、もう1つ 出さない。

    アイコンを 二度 押しても 窓が 2つに ならないように、
    使っていない 番号を 1つ おさえて 見はりに する
    """
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sk.bind(("127.0.0.1", LOCK_PORT))
        sk.listen(1)
    except OSError:
        return None          # すでに 開いている
    return sk                # 閉じないよう 持ちつづける


def start_server():
    # すでに動いていれば monitor 側が気づいて 静かに退く
    if "--quiet" not in sys.argv:
        sys.argv.append("--quiet")
    threading.Thread(target=monitor.main, daemon=True).start()


def main():
    lock = only_one()
    if lock is None:
        return              # すでに 出ている
    start_server()
    st = load_state()
    api = Api()
    kw = {}
    if st["x"] is not None and st["y"] is not None:
        kw["x"], kw["y"] = int(st["x"]), int(st["y"])
    win = webview.create_window(
        "スライムこうぼう", URL,
        width=st["w"], height=st["h"],
        frameless=True,      # 枠なし
        easy_drag=True,      # どこでも つかんで動かせる
        on_top=True,         # ほかの窓より前に出す
        transparent=True,    # 中身の 半とうめいを そのまま 生かす
        resizable=True,
        min_size=(MIN_W, fit_height(MIN_W)),
        js_api=api,
        **kw,
    )
    api._win = win
    box = [0]

    def on_shown():
        hwnd = form_hwnd()
        if not hwnd:
            return
        box[0] = hwnd
        r = win_rect(hwnd)
        if not r:
            return
        # 頼んだ 大きさと 見た目の 大きさは ずれるので、ここで そろえる
        x = st["x"] if st["x"] is not None else r[0]
        y = st["y"] if st["y"] is not None else r[1]
        set_rect(hwnd, x, y, st["w"], fit_height(st["w"]))
        r = win_rect(hwnd) or r
        apply_region(hwnd, r[2], r[3])

    win.events.shown += on_shown
    threading.Thread(target=watch, args=(box,), daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()

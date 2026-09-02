# -*- coding: utf-8 -*-
"""
スライムこうぼう ─ デスクトップに置くウィジェット姿

枠なし・最前面・背景ぬきの小さな窓に、工房だけを出す。
サーバー（monitor.py）が動いていなければ、この中で一緒に立ち上げる。

  つかみ方   … 窓のどこでも つかんで動かせる
  大きさ     … 右下の すみを つかんで ひっぱる
  とじ方     … 右上の × （ふだんは薄く、乗せると出てくる）

枠を消した窓には、ふちが無い。
だから OS に 大きさを変えてもらえないので、
右下の すみを 自分で 用意して、ここで 窓の大きさを 変えている。
大きさと 置き場所は おぼえておいて、つぎに開いた時に そのまま出す。
"""
import json
import os
import sys
import threading
import time

import webview

import monitor

HERE = os.path.dirname(os.path.abspath(__file__))
URL = f"http://127.0.0.1:{monitor.PORT}/?widget=1"
STATE = os.path.join(HERE, "_widget.json")
LOCK_PORT = 8898        # 二重に 開かないための 見はり口（8899 は 工房の 番号）

# 部屋の かたち（1774×887）。これより 細長い/平たい 窓にしても
# 部屋の まわりに 何もない ところが できるだけ なので、この形を たもつ
ASPECT = 1774 / 887
MIN_W, MAX_W = 320, 2400
DEF_W, DEF_H = 700, 380


def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        w = int(d.get("w") or DEF_W)
        h = int(d.get("h") or DEF_H)
        return {
            "w": max(MIN_W, min(MAX_W, w)),
            "h": max(int(MIN_W / ASPECT), h),
            "x": d.get("x"),
            "y": d.get("y"),
        }
    except Exception:
        return {"w": DEF_W, "h": DEF_H, "x": None, "y": None}


def save_state(d):
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, STATE)
    except Exception:
        pass


class Api:
    """窓の中の × と 右下のすみ から呼ばれる。

    pywebview は この入れものの「表に出ている もちもの」を
    まるごと JS に わたそうとする。窓そのものを 持たせると
    たどりきれずに 橋渡しが こわれるので、名前を _ で かくす
    """

    def __init__(self):
        self._win = None

    def _w(self):
        if self._win is not None:
            return self._win
        return webview.windows[0] if webview.windows else None

    def quit(self):
        for w in list(webview.windows):
            w.destroy()

    def size(self):
        """いまの 窓の大きさ。すみを つかんだ時の 起点にする"""
        w = self._w()
        if w is None:
            return {"w": DEF_W, "h": DEF_H}
        return {"w": w.width, "h": w.height}

    def resize(self, w, h):
        """すみを ひっぱっている あいだ、何度も 呼ばれる"""
        win = self._w()
        if win is None:
            return False
        w = max(MIN_W, min(MAX_W, int(w)))
        h = max(int(MIN_W / ASPECT), int(h))
        try:
            win.resize(w, h)
            return True
        except Exception:
            return False

    def aspect(self):
        return ASPECT


def watch(win):
    """動かした・大きさを変えた のを おぼえておく。
    どの版の pywebview でも 同じように 効くように、催しを待たずに 見にいく"""
    last = None
    while True:
        time.sleep(1.5)
        try:
            now = {"w": win.width, "h": win.height, "x": win.x, "y": win.y}
        except Exception:
            return
        if not now["w"]:
            continue
        if now != last:
            last = now
            save_state(now)


def only_one():
    """すでに 開いていたら、もう1つ 出さない。

    アイコンを 二度 押しても 窓が 2つに ならないように、
    使っていない 番号を 1つ おさえて 見はりに する。
    おさえられなければ すでに だれかが 開いている
    """
    import socket
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
        transparent=True,    # 部屋のまわりは デスクトップが透ける
        resizable=True,
        min_size=(MIN_W, int(MIN_W / ASPECT)),
        js_api=api,
        **kw,
    )
    api._win = win
    threading.Thread(target=watch, args=(win,), daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()

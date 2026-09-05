#!/bin/sh
# 工房を ひらく（まだ ひらいていない時だけ）
#
# セッションが 始まるたびに 呼ばれる。
# うまく いかなかった時は、その わけを Claude に 伝える。
# だまって 何も 起きないのが いちばん こまるので、かならず 何か 言う。
# ここで つまずいても Claude の しごとは 止めない。

URL="http://127.0.0.1:8899"
MARK="$HOME/.claude/.slime-koubou-state"

# いまの ようすを おぼえておく。同じ ことを 毎回 言わないため
say_once() {
  # $1 = ようすの 名まえ / $2 = 伝えたい こと
  last=""
  [ -f "$MARK" ] && last=$(cat "$MARK" 2>/dev/null)
  if [ "$last" != "$1" ]; then
    mkdir -p "$HOME/.claude" 2>/dev/null
    printf '%s' "$1" > "$MARK" 2>/dev/null
    printf '%s\n' "$2"
  fi
}

alive() {
  curl -s -m 2 -o /dev/null "$URL/api/status" 2>/dev/null
}

# ── もう ひらいている ──
if alive; then
  say_once "ok" "[スライムこうぼう] 工房が うごいています。つかい手に「$URL をブラウザで開くと、いま動いているセッションがスライムになって見えます」と 一度だけ 伝えてください。"
  exit 0
fi

HERE=$(dirname "$0")
ROOT=$(cd "$HERE/.." 2>/dev/null && pwd)
if [ -z "$ROOT" ] || [ ! -f "$ROOT/monitor.py" ]; then
  say_once "nofiles" "[スライムこうぼう] 工房の ファイルが 見つかりません。つかい手に「スライムこうぼうを入れ直してください」と 伝えてください。"
  exit 0
fi

# ── python を さがす ──
# 呼び名は 人によって ちがう。Windows は pythonw だと 黒い窓が 出ない
PY=""
if command -v pythonw >/dev/null 2>&1; then PY=pythonw
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python  >/dev/null 2>&1; then PY=python
fi

if [ -z "$PY" ]; then
  say_once "nopython" "[スライムこうぼう] python が 見つからないので 工房を ひらけません。つかい手に『スライムこうぼうには python が必要です。https://www.python.org/downloads/ から入れて、入れる時に「Add python.exe to PATH」に必ずチェックを入れてください。入れ終わったらこのアプリを一度閉じて開き直してください』と 伝えてください。"
  exit 0
fi

# ── ひらく ──
# セッションが 終わっても 工房は 残るように、切りはなして 動かす
(cd "$ROOT" && nohup "$PY" monitor.py --quiet >/dev/null 2>&1 &) >/dev/null 2>&1

# 立ち上がるまで 少し 待つ（絵の 読みこみが あるので 気長に）
i=0
while [ $i -lt 8 ]; do
  if alive; then
    say_once "ok" "[スライムこうぼう] 工房を ひらきました。つかい手に「$URL をブラウザで開くと、いま動いているセッションがスライムになって見えます」と 一度だけ 伝えてください。"
    exit 0
  fi
  sleep 1
  i=$((i + 1))
done

# ── ひらかなかった ──
# ポートを ほかの ものが つかっている？
if curl -s -m 2 -o /dev/null "$URL" 2>/dev/null; then
  say_once "busy" "[スライムこうぼう] 8899番の 出入口を ほかの ものが つかっているようです。つかい手に「スライムこうぼうが使う8899番を別のアプリが使っています。そちらを閉じるか、スライムこうぼうに相談してください」と 伝えてください。"
else
  say_once "failed" "[スライムこうぼう] 工房が ひらきませんでした。つかい手に「スライムこうぼうが起動できませんでした。python は入っていますが動きませんでした」と 伝え、ためしに $ROOT で「$PY monitor.py」を 動かして 出てきた ことばを 見てください。"
fi
exit 0

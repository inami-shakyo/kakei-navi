#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UserPromptSubmit フック。毎回のやりとりの先頭でファクトチェックのルールを入れ直す。

毎回指示しなくても事実確認が働くようにするのが目的なので、
プロンプトの内容にかかわらず同じルールを注入する。
"""

import json
import sys

RULES = """【ファクトチェックのルール（くらしの家計ナビ）】
・金額・電話番号・しきい値・年齢や期間の要件・法令名は、一次情報で確認できたものだけを書く。
・確認できないものは推測で埋めず、【要確認】を付ける（他自治体の相場からの推測は不可）。
・既存の数字・固有名詞は、出典で変更を確認できた場合以外は書き換えない。
・同じ数字が「表示用の文章」と「計算用の数値」の2か所にある。
  住宅扶助＝SEIHO_JUTAKU と JUTAKU_FUJO、生活保護の計算例＝SEIHO_EXAMPLES と
  SEIHO_KIJUN。片方だけ直すと、画面の表示と診断結果が食い違う。
・掲載時点は DATA_ASOF・SEIHO_ASOF・TOKUREI.asof・RISK_REF.asof の4か所にある。
・THRESHOLDS・GUIDELINES は当ツール独自の目安。公的基準と書けるのは出典があるものだけ。
・確認したら docs/fact-log.md に出典URLと確認日を追記する。
・報告では、✅出典で確認した内容と、⚠️確認できず【要確認】のままにした内容を分けて書く。
詳しくは CLAUDE.md を参照。"""


def main():
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            sys.stdin.read()  # 読み捨て
    except Exception:
        pass

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": RULES,
                },
                "suppressOutput": True,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

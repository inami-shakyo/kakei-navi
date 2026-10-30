#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""くらしの家計ナビ ファクトチェック用スキャナ

git の差分（追加行・削除行）だけを見て、出典の裏付けがない事実記述
（金額・電話番号・割合・年齢や期間の要件・法令名と改定時期・設定値）を洗い出す。
既存の記述を蒸し返さないため、判定対象は必ず差分に限る。

このツールは入力された家計から診断結果を出すため、生活保護の基準額・貧困線・
しきい値といった「単位なしの数値」が判定そのものを動かす。くらしの相談ナビ版と
ちがい、編集ゾーンの中では数値リテラルもすべて事実として扱う。

使い方（いずれも stdin にフックの JSON が来る想定）:
    factcheck_scan.py postedit   # PostToolUse。警告するだけでブロックしない
    factcheck_scan.py stopgate   # Stop。検知があれば decision:block を返す
    factcheck_scan.py report     # 人間向けのテキスト一覧（/factcheck から呼ぶ）
"""

import json
import os
import re
import subprocess
import sys

MAX_SHOWN = 40

# --- 編集ゾーンの目印 -------------------------------------------------------
ZONE_START = "編集ゾーン ここから"
ZONE_END = "編集ゾーン ここまで"

# --- 検出パターン（ラベル付き） ---------------------------------------------
# 電話番号はフリーダイヤル（0120-552-039）や法テラス（0570-078374）の
# 桁の切り方も拾えるようにしている。
PATTERNS = [
    ("電話番号", re.compile(r"0\d{1,4}-\d{2,4}-\d{3,4}|0\d{2,4}-\d{6,8}")),
    ("金額", re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:万円|円)")),
    ("割合", re.compile(r"\d+(?:\.\d+)?\s*(?:％|%|割)")),
    ("年齢・期間の要件", re.compile(r"\d+\s*(?:歳|か月|ヶ月|年度|級)")),
    ("法令・改定時期", re.compile(r"(?:令和|平成)\s*\d+\s*年(?:\s*\d+\s*月)?|第\s*\d+\s*条")),
]

# 編集ゾーンの中だけで使う。単位のない数値リテラル（生活保護基準額・貧困線・
# しきい値・全国平均値など）。これを拾わないと、診断を動かす数字が素通りする。
BARE_NUM = re.compile(r"(?<![\w.#])\d+(?:\.\d+)?(?![\w.])")
# 直前のキー名（povertyLine: 1380000 の povertyLine）
KEY_BEFORE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*$")
# 年月日は設定値として数えない（2026-10-23 のような日付）
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# --- 除外：見た目の指定を含む行は対象外 -------------------------------------
STYLE_PATTERNS = [
    re.compile(r"(?:px|rem|em|vh|vw)\b"),
    re.compile(r"#[0-9a-fA-F]{3,8}\b"),
    re.compile(r"z-index"),
    re.compile(r"font-size"),
    re.compile(r"line-height"),
    re.compile(r"margin"),
    re.compile(r"padding"),
    re.compile(r"border-radius"),
    re.compile(r"rgba?\("),
    re.compile(r"translate"),
    re.compile(r"@media"),
]

JAPANESE = re.compile(r"[ぁ-んァ-ヶ一-龥]")

# 編集ゾーンの外でも、日本語が無くても対象にする項目行
DATA_FIELD = re.compile(
    r"\b(?:tel|name|note|label|hint|id|max|asof|callcenter|callcenterNote|"
    r"endpoint|setai|seikatsu|povertyLine|povertyYear|address|hours|summary|"
    r"single|multi|all|two|threeToFive|one|add|food|utility|medical|daily|"
    r"goraku|housingSurvey|total|engel|comm|car|edu|teigen|dai1|dai2|"
    r"tokureiKasan|jidoYoiku|boshi)\s*:"
)

# 文単位の免除キー
EXEMPT = re.compile(r"【要確認】|出典|根拠：|https?://")


def run(args, cwd):
    """git を叩く。失敗しても例外にせず (ok, 出力) を返す。"""
    try:
        p = subprocess.run(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError:
        return False, ""
    out = p.stdout.decode("utf-8", "replace")
    return p.returncode == 0, out


def project_dir():
    d = os.environ.get("CLAUDE_PROJECT_DIR")
    if d and os.path.isdir(d):
        return d
    ok, out = run(["git", "rev-parse", "--show-toplevel"], os.getcwd())
    if ok and out.strip():
        return out.strip()
    return os.getcwd()


def base_rev(mode, root):
    """比較元のリビジョンを決める。"""
    if mode == "postedit":
        return "HEAD"
    ok, out = run(["git", "merge-base", "HEAD", "origin/main"], root)
    if ok and out.strip():
        return out.strip()
    return "HEAD"


def diff_lines(root, base):
    """git diff -U0 を自分でパースして (種別, ファイル, 行番号, 本文) を得る。

    種別は "+"（追加行）か "-"（削除行）。対象は .html のみ。
    """
    ok, out = run(
        ["git", "diff", "-U0", "--no-color", base, "--", "*.html"], root
    )
    if not ok:
        return []

    hunk = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    rows = []
    path = None
    old_no = new_no = 0
    for line in out.split("\n"):
        if line.startswith("+++ "):
            p = line[4:].strip()
            path = None if p == "/dev/null" else re.sub(r"^b/", "", p)
            continue
        if line.startswith("--- "):
            continue
        m = hunk.match(line)
        if m:
            old_no = int(m.group(1))
            new_no = int(m.group(3))
            continue
        if path is None or not path.endswith(".html"):
            continue
        if line.startswith("+"):
            rows.append(("+", path, new_no, line[1:]))
            new_no += 1
        elif line.startswith("-"):
            rows.append(("-", path, old_no, line[1:]))
            old_no += 1
    return rows


def zone_bounds(text):
    """編集ゾーンの開始・終了行（1から数える）を返す。無ければ (None, None)。"""
    start = end = None
    for i, line in enumerate(text.split("\n"), 1):
        if start is None and ZONE_START in line:
            start = i
        elif start is not None and end is None and ZONE_END in line:
            end = i
    return start, end


def sentence_spans(line):
    """行を文に分ける。区切りは「。」・改行・データ中に2文字で現れる \\n。

    行単位で免除すると、数百字ある note や解説の行末の【要確認】1つで
    行内の数字が全部免除されてしまうため、必ず文単位で判定する。
    """
    spans = []
    start = 0
    i = 0
    n = len(line)
    while i < n:
        if line[i] in ("。", "\n", "\r"):
            i += 1
            spans.append((start, i))
            start = i
        elif line[i] == "\\" and i + 1 < n and line[i + 1] == "n":
            i += 2
            spans.append((start, i))
            start = i
        else:
            i += 1
    if start < n:
        spans.append((start, n))
    return spans or [(0, n)]


def line_is_skipped(line, in_zone):
    """見た目の指定を含む行は対象外。

    編集ゾーンの外では、日本語を含まない行も対象外にする（項目行は除く）。
    ゾーンの中は丸ごと制度データなので、日本語の有無を問わない。
    """
    for pat in STYLE_PATTERNS:
        if pat.search(line):
            return True
    if not in_zone and not JAPANESE.search(line):
        if not DATA_FIELD.search(line):
            return True
    return False


def comma_variant(num):
    """32300 → 32,300 のように3桁区切りを付けた形も返す。"""
    if "." in num or len(num) < 4:
        return None
    return "{:,}".format(int(num))


def find_values(line, in_zone):
    """1行から検知結果を拾う。免除済みの文は捨てる。

    返すのは dict のリスト:
      label    … 種別
      value    … 表示用（設定値は key:number の形）
      base_key … 比較元コミットと照合する文字列
      nums     … 台帳と照合する文字列（複数の表記ゆれ）
      sentence … その値が入っていた文
    """
    if line_is_skipped(line, in_zone):
        return []

    spans = sentence_spans(line)
    exempt = [bool(EXEMPT.search(line[s:e])) for s, e in spans]

    hits = []
    for label, pat in PATTERNS:
        for m in pat.finditer(line):
            hits.append((m.start(), m.end(), label, m.group(0), None))

    if in_zone:
        # 日付の数字は設定値として数えないよう、長さを変えずに伏せる
        masked = ISO_DATE.sub(lambda m: "x" * len(m.group(0)), line)
        for m in BARE_NUM.finditer(masked):
            key = KEY_BEFORE.search(line[: m.start()])
            num = m.group(0)
            if key is None and len(num) == 1:
                # 「1〜5人」のような注記の中の1桁は設定値として数えない
                continue
            hits.append(
                (m.start(), m.end(), "設定値", num,
                 key.group(1) if key else None)
            )

    # 重複・内包を潰す（長い方を残す）。電話番号や「32,300円」の中の
    # 数字が設定値として二重に出ないようにするため。
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept = []
    for h in hits:
        if any(k[0] <= h[0] and h[1] <= k[1] for k in kept):
            continue
        kept.append(h)

    out = []
    for start, end, label, value, key in kept:
        idx = 0
        for i, (s, e) in enumerate(spans):
            if s <= start < e:
                idx = i
                break
        if exempt[idx]:
            continue
        s, e = spans[idx]

        if label == "設定値":
            # 単位のない数値は、キー名と組にして比較元と照合する。
            # 「30」のような短い数字が、ファイル内の無関係な「30」で
            # 免除されてしまうのを防ぐため。
            display = "%s: %s" % (key, value) if key else value
            base_key = ("%s:%s" % (key, value)) if key else value
            nums = [value]
            cv = comma_variant(value)
            if cv:
                nums.append(cv)
        else:
            display = value.strip()
            base_key = value.strip()
            nums = [value.strip()]

        out.append(
            {
                "label": label,
                "value": display,
                "base_key": base_key,
                "nums": nums,
                "sentence": line[s:e].strip(),
            }
        )
    return out


def squeeze(text):
    return re.sub(r"\s+", "", text)


def load_fact_log(root):
    path = os.path.join(root, "docs", "fact-log.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def file_at(root, rev, path):
    ok, out = run(["git", "show", "%s:%s" % (rev, path)], root)
    return out if ok else ""


def current_file(root, path):
    try:
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def contains(haystack, value):
    if not haystack or not value:
        return False
    return value in haystack or squeeze(value) in squeeze(haystack)


def in_fact_log(log_text, finding):
    """台帳に載っているか。設定値は数字の前後が数字でないことまで見る。"""
    if not log_text:
        return False
    if finding["label"] != "設定値":
        return contains(log_text, finding["value"])
    for num in finding["nums"]:
        pat = re.compile(r"(?<![\d.,])%s(?![\d.])" % re.escape(num))
        if pat.search(log_text):
            return True
    return False


def scan(mode):
    root = project_dir()
    base = base_rev(mode, root)
    rows = diff_lines(root, base)
    if not rows:
        return []

    fact_log = load_fact_log(root)
    base_cache = {}
    cur_cache = {}
    zone_cache = {}

    def base_text(path):
        if path not in base_cache:
            base_cache[path] = file_at(root, base, path)
        return base_cache[path]

    def cur_text(path):
        if path not in cur_cache:
            cur_cache[path] = current_file(root, path)
        return cur_cache[path]

    def zone_for(kind, path):
        key = (kind, path)
        if key not in zone_cache:
            text = cur_text(path) if kind == "+" else base_text(path)
            zone_cache[key] = zone_bounds(text)
        return zone_cache[key]

    findings = []
    seen = set()
    for kind, path, lineno, line in rows:
        zs, ze = zone_for(kind, path)
        in_zone = bool(zs and ze and zs < lineno < ze)
        for f in find_values(line, in_zone):
            # 台帳に載っていれば確認済み扱い
            if in_fact_log(fact_log, f):
                continue
            if kind == "+":
                # 比較元の同じファイルに既にある値なら、文章だけの書き直し
                if contains(base_text(path), f["base_key"]):
                    continue
                key = ("+", path, lineno, f["label"], f["value"])
                if key in seen:
                    continue
                seen.add(key)
                kind_label = "追加"
            else:
                # 削除行にあった値が現在のファイルのどこにも残っていなければ、
                # 既存の値を無断で書き換えた／消した疑い
                if contains(cur_text(path), f["base_key"]):
                    continue
                key = ("-", path, f["label"], f["value"])
                if key in seen:
                    continue
                seen.add(key)
                kind_label = "既存の値の削除・変更"

            findings.append(
                {
                    "kind": kind_label,
                    "path": path,
                    "line": lineno,
                    "label": f["label"],
                    "value": f["value"],
                    "sentence": f["sentence"],
                }
            )
    return findings


def clip(text, width=80):
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= width else text[: width - 1] + "…"


def finding_lines(findings):
    out = []
    for f in findings[:MAX_SHOWN]:
        out.append(
            "- [%s] %s:%s %s「%s」\n    %s"
            % (f["kind"], f["path"], f["line"], f["label"], f["value"],
               clip(f["sentence"]))
        )
    rest = len(findings) - MAX_SHOWN
    if rest > 0:
        out.append("- ほか %d 件（表示は%d件まで）" % (rest, MAX_SHOWN))
    return out


HOWTO = """
対応は次の3つのいずれかです（推測で埋めるのは不可）:
  ① 一次情報で確認して docs/fact-log.md に出典URLと確認日を追記する
  ② 確認できないなら その記述に【要確認】を付ける
  ③ 書くのをやめる（省く）

あわせて確認してください:
  ・既存の値を出典なしに書き換えていないか
  ・同じ数字が「表示用の文章」と「計算用の数値」の2か所にあります。
    住宅扶助＝SEIHO_JUTAKU（単身32,300円…）と JUTAKU_FUJO（single:32300）、
    生活保護の計算例＝SEIHO_EXAMPLES（約7.1万円…）と SEIHO_KIJUN（基準額）。
    片方だけ直すと、画面の表示と診断結果が食い違います
  ・掲載時点は DATA_ASOF・SEIHO_ASOF・TOKUREI.asof・RISK_REF.asof の4か所にあります
  ・しきい値（THRESHOLDS・GUIDELINES）は当ツール独自の目安です。
    公的基準と書けるのは、出典を示せるものだけです
""".rstrip()


def warning_text(findings):
    body = "\n".join(finding_lines(findings))
    return (
        "事実チェック: 出典の裏付けが確認できない記述が %d 件あります。\n"
        "このツールは入力された家計から診断結果を出します。生活保護の基準額・"
        "貧困線・しきい値・電話番号を1か所でも間違えると、"
        "困っている方に誤った診断を返すことになります。\n\n"
        "%s\n%s" % (len(findings), body, HOWTO)
    )


def read_hook_input():
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    if mode not in ("postedit", "stopgate", "report"):
        sys.stderr.write("usage: factcheck_scan.py {postedit|stopgate|report}\n")
        return 2

    hook_input = {} if mode == "report" else read_hook_input()

    if mode == "stopgate" and hook_input.get("stop_hook_active"):
        # 二重ブロックのループ防止
        return 0

    findings = scan(mode)

    if mode == "report":
        if not findings:
            print("未確認の事実は見つかりませんでした。")
            return 0
        print("未確認の可能性がある記述 %d 件" % len(findings))
        print("")
        print("\n".join(finding_lines(findings)))
        print("")
        print(HOWTO)
        return 0

    if not findings:
        return 0

    text = warning_text(findings)

    if mode == "postedit":
        print(
            json.dumps(
                {
                    "systemMessage": "事実チェック: 未確認の記述 %d 件" % len(findings),
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": text,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "decision": "block",
                "reason": text
                + "\n\n報告は、✅出典で確認済みの内容と、"
                "⚠️確認できず【要確認】のまま残した内容を分けて書いてください。",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

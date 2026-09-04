#!/usr/bin/env python3
"""
サンライズ瀬戸（下り・東京→高松）9月26日の空席監視。

2つの独立したソースを監視し、どちらかが「空きあり」に転じたら
ntfy 経由でスマホに通知する。

  A) sunrise-checker.com  … 設備別（ノビノビ/シングル/ソロ）。人力更新で1日2〜3回。
  B) ressha-kanko.com     … 日付単位の集計のみ。e5489から自動取得。
                             生HTMLに出るのは直近7日分なので、
                             対象日が近づくと自動的に効き始める。
"""

import json
import os
import re
import sys
import urllib.request

from bs4 import BeautifulSoup

# ---- 設定 ----------------------------------------------------------------
CHECKER_URL = "https://sunrise-checker.com/seto_down.html"
KANKO_URL = "https://ressha-kanko.com/train/sunrise"
KANKO_LABEL = "サンライズ瀬戸 下り"          # 集計テキスト中の見出し

TARGET_DATE = os.environ.get("TARGET_DATE", "9/26")
WANT = ["ノビノビ座席", "シングル", "ソロ"]   # 設備名は完全一致
AVAILABLE_MARKS = {"○", "◯", "〇", "◎", "△"}
STATE_FILE = "state.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
CONTACT = os.environ.get("CONTACT", "personal-use")
UA = f"sunrise-watch/1.0 (personal use; {CONTACT})"
E5489_FORM = "https://www.jr-odekake.net/goyoyaku/campaign/sunriseseto_izumo/form.html"
# --------------------------------------------------------------------------


def month_day():
    m = re.match(r"(\d{1,2})\D+(\d{1,2})", TARGET_DATE)
    if not m:
        raise ValueError(f"TARGET_DATE が解釈できません: {TARGET_DATE}")
    return int(m.group(1)), int(m.group(2))


def notify(title, body, priority="default", tags="bell"):
    if not NTFY_TOPIC:
        print(f"[通知先未設定] {title}: {body}")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8").decode("latin-1", "ignore") or "Sunrise",
            "Priority": priority,
            "Tags": tags,
            "Click": E5489_FORM,
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30).read()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def norm(s):
    return re.sub(r"\s+", "", s)


def date_matches(text):
    """「9/26」「9月26日」「09/26」「2026年9月26日」の表記ゆれを吸収。"""
    month, day = month_day()
    return re.search(rf"(?<!\d)0?{month}\s*[/月\-\.]\s*0?{day}(?!\d)", text) is not None


def is_open(mark):
    return any(c in mark for c in AVAILABLE_MARKS)


# ---- ソース A: sunrise-checker（設備別）-----------------------------------
def scrape_checker():
    soup = BeautifulSoup(fetch(CHECKER_URL), "html.parser")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")

        header_map = {}
        for row in rows:
            cells = [norm(c.get_text()) for c in row.find_all(["th", "td"])]
            if any(c in WANT for c in cells):
                header_map = {i: c for i, c in enumerate(cells) if c in WANT}
                break
        if not header_map:
            continue

        for row in rows:
            cells = row.find_all(["th", "td"])
            if not cells or not date_matches(norm(cells[0].get_text())):
                continue
            texts = [norm(c.get_text()) for c in cells]
            found = {
                name: texts[i]
                for i, name in header_map.items()
                if i < len(texts) and texts[i]
            }
            if found:
                return found
    return {}


# ---- ソース B: 観光列車ナビ（日付単位の集計）-------------------------------
def scrape_kanko():
    """(対象日の表記, ページの取得時刻) を返す。対象日が範囲外なら ("", 時刻)。"""
    text = BeautifulSoup(fetch(KANKO_URL), "html.parser").get_text("\n")

    stamp = ""
    m = re.search(r"空席状況[（(]\s*([\d\-: ]+?)\s*時点", text)
    if m:
        stamp = m.group(1).strip()

    label = norm(KANKO_LABEL)
    for line in text.split("\n"):
        flat = norm(line)
        if not flat.startswith(label):
            continue
        for chunk in re.split(r"[／/]", flat[len(label):].lstrip("：:")):
            if not date_matches(chunk):
                continue
            parts = re.split(r"[：:]", chunk, maxsplit=1)
            if len(parts) == 2 and parts[1]:
                return parts[1], stamp
    return "", stamp


# ---- 状態 ----------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    state = load_state()
    hits, logs, warned = [], [], False

    # --- ソース A ---
    try:
        checker = scrape_checker()
    except Exception as e:  # noqa: BLE001
        checker = {}
        logs.append(f"A: 取得失敗 {e}")
    else:
        if checker:
            prev = state.get("checker", {})
            for name, mark in checker.items():
                if is_open(mark) and not is_open(prev.get(name, "×")):
                    hits.append(f"[設備別] {name} {mark}")
            state["checker"] = checker
            state["checker_failed"] = False
            logs.append(f"A: {checker}")
        else:
            logs.append("A: 対象日の行が見つからず")
            if not state.get("checker_failed"):
                notify(
                    "サンライズ監視: ソースAを解析できず",
                    f"{TARGET_DATE} の行が見つかりません。構造変更の可能性。",
                    "low", "warning",
                )
                state["checker_failed"] = True
                warned = True

    # --- ソース B ---
    try:
        kanko_mark, kanko_stamp = scrape_kanko()
    except Exception as e:  # noqa: BLE001
        logs.append(f"B: 取得失敗 {e}")
    else:
        if kanko_mark:
            prev = state.get("kanko", "×")
            if is_open(kanko_mark) and not is_open(prev):
                hits.append(f"[日付単位] {kanko_mark}（{kanko_stamp} 時点）")
            state["kanko"] = kanko_mark
            logs.append(f"B: {kanko_mark} ({kanko_stamp})")
        else:
            # 7日窓の外。まだ効いていないだけなので警告しない。
            logs.append(f"B: 対象日は範囲外（ページ更新 {kanko_stamp or '不明'}）")

    if hits:
        notify(
            f"空席あり! サンライズ瀬戸 {TARGET_DATE}",
            "・" + "\n・".join(hits) + "\n\nタップして e5489 へ",
            "urgent", "steam_locomotive",
        )

    print(" | ".join(logs) + (f"  -> 通知 {hits}" if hits else ""))
    save_state(state)
    return 1 if (warned and not hits) else 0


if __name__ == "__main__":
    sys.exit(main())

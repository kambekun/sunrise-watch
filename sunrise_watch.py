#!/usr/bin/env python3
"""
サンライズ瀬戸（下り・東京→高松）9月26日の空席監視。
 
  A) sunrise-checker.com … 設備別。2段ヘッダー＋禁煙/喫煙で列が分かれる構造に対応。
  B) ressha-kanko.com    … 日付単位の集計。生HTMLは直近7日分のみ。
 
どちらかが × から ○/△ に転じたら ntfy でスマホに通知する。
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
KANKO_LABEL = "サンライズ瀬戸 下り"
 
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
    month, day = month_day()
    return re.search(rf"(?<!\d)0?{month}\s*[/月\-\.]\s*0?{day}(?!\d)", text) is not None
 
 
def is_open(mark):
    return any(c in mark for c in AVAILABLE_MARKS)
 
 
def best_mark(marks):
    """複数列（禁煙/喫煙）のうち最も良い記号を返す。"""
    for m in marks:
        if any(c in m for c in {"○", "◯", "〇", "◎"}):
            return m
    for m in marks:
        if "△" in m:
            return m
    return marks[0] if marks else ""
 
 
# ---- ソース A: sunrise-checker ---------------------------------------------
def build_column_names(header_row):
    """設備ヘッダー行から「データ列 -> 設備名」の並びを作る（colspan 展開）。"""
    names = []
    for i, cell in enumerate(header_row.find_all(["th", "td"])):
        if i == 0:          # 左端の「設備」ラベル
            continue
        try:
            span = int(cell.get("colspan", 1))
        except (TypeError, ValueError):
            span = 1
        names.extend([norm(cell.get_text())] * max(span, 1))
    return names
 
 
def scrape_checker():
    """({設備名: 記号}, 最終更新日時, 診断メモ) を返す。"""
    html = fetch(CHECKER_URL)
    soup = BeautifulSoup(html, "html.parser")
 
    updated = ""
    m = re.search(r"最終更新日時[：:]\s*([\d]{4}年[\d]{2}月[\d]{2}日\s*[\d:]+)",
                  norm(soup.get_text()).replace("最終更新日時:", "最終更新日時: "))
    if not m:
        m = re.search(r"最終更新日時[：:]?\s*(\S+?\d{1,2}:\d{2})", soup.get_text())
    if m:
        updated = m.group(1).strip()
 
    tables = soup.find_all("table")
    seen_dates = []
 
    for table in tables:
        rows = table.find_all("tr")
 
        header_row = None
        for row in rows:
            texts = [norm(c.get_text()) for c in row.find_all(["th", "td"])]
            if any(t in WANT for t in texts):
                header_row = row
                break
        if header_row is None:
            continue
 
        names = build_column_names(header_row)
        if not names:
            continue
 
        for row in rows:
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = norm(cells[0].get_text())
            if not label or label in ("設備", "日付"):
                continue
            seen_dates.append(label)
            if not date_matches(label):
                continue
 
            values = [norm(c.get_text()) for c in cells[1:]]
            found = {}
            for want in WANT:
                marks = [
                    values[i]
                    for i, name in enumerate(names)
                    if name == want and i < len(values) and values[i]
                ]
                if marks:
                    found[want] = best_mark(marks)
            if found:
                return found, updated, f"columns={len(names)}"
 
    note = (f"tables={len(tables)} 日付候補={seen_dates[:3]}…{seen_dates[-3:]}"
            if seen_dates else f"tables={len(tables)} 日付行なし")
    return {}, updated, note
 
 
# ---- ソース B: 観光列車ナビ -------------------------------------------------
def scrape_kanko():
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
        checker, updated, note = scrape_checker()
    except Exception as e:  # noqa: BLE001
        logs.append(f"A: 取得失敗 {e}")
    else:
        if checker:
            prev = state.get("checker", {})
            for name, mark in checker.items():
                if is_open(mark) and not is_open(prev.get(name, "×")):
                    hits.append(f"[設備別] {name} {mark}")
            state["checker"] = checker
            state["checker_updated"] = updated
            state["checker_failed"] = False
            logs.append(f"A: {checker} (更新 {updated or '不明'}, {note})")
        else:
            logs.append(f"A: 対象日の行が見つからず [{note}]")
            if not state.get("checker_failed"):
                notify(
                    "サンライズ監視: ソースAを解析できず",
                    f"{TARGET_DATE} の行が見つかりません。{note}",
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
 

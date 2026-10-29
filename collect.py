#!/usr/bin/env python3
"""洲弹 · 云端采集（GitHub Actions 专用，纯标准库、零依赖）

每 30 分钟由 GitHub Actions 触发一次，电脑关机也照常采集：
  1. deltaammo.com  —— 现价 / 今日最低价 / 今日最高价 / 子弹等级
  2. orzice.com     —— 3/7/30 日历史价（该站现价已登录可见，但历史价仍公开）

产物（按天分片）：
  data/YYYY-MM-DD.jsonl  每行一次快照  {"t": 时间戳, "d": [[名称,等级,现价,今日低,今日高], ...]}
  hist/YYYY-MM-DD.json   当天最后一次历史价快照（覆盖写）

三个设计取舍：
  · 不建数据库 —— Actions 跑完环境即销毁，只有提交进仓库的文本才是产出的数据
  · 不压缩 —— 追加纯文本让 git 只存增量（几十字节）；gzip 每次重写整个文件，历史反而膨胀
  · 分天按 UTC+8 —— 与玩家看到的「今天」一致，本地 APP 按同一时区对齐
"""

from __future__ import annotations

import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
HIST_DIR = os.path.join(ROOT, "hist")
TZ = timezone(timedelta(hours=8))          # 站点与玩家都在 UTC+8（中国无夏令时）

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
TIMEOUT = 25
RETRY = 3
MIN_GAP_SEC = 240          # 距上一条不足 4 分钟则跳过（防手动触发刷出密集点）


# ------------------------------------------------------------------ 网络
def http_get(url: str) -> str:
    last: Exception | None = None
    for attempt in range(RETRY + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise StopIteration from exc
            last = exc
        except Exception as exc:                     # noqa: BLE001 网络异常一律重试
            last = exc
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"抓取失败 {url}：{last}")


# ------------------------------------------------------------------ 源 A：deltaammo
DELTA_BASE = "https://www.deltaammo.com/"
DELTA_ITEM_RE = re.compile(
    r'\{"id":"(?P<id>[0-9a-f-]{8,})","name":"(?P<name>[^"]+)","grade":(?P<grade>\d+),'
    r'"imageUrl":"[^"]*","currentPrice":(?P<price>\d+),'
    r'"updatedAt":"(?P<updated>[^"]*)","todayLow":(?P<low>\d+),"todayHigh":(?P<high>\d+)\}'
)


def _parse_delta(html: str) -> dict[str, dict]:
    flat = html.replace('\\"', '"')          # RSC payload 里引号是转义的
    out: dict[str, dict] = {}
    for m in DELTA_ITEM_RE.finditer(flat):
        d = m.groupdict()
        price = int(d["price"])
        if price <= 0:
            continue
        low, high = int(d["low"]), int(d["high"])
        if low <= 0:
            low = price
        if high < low:
            high = price
        out[d["name"]] = {"grade": int(d["grade"]), "price": price,
                          "low": low, "high": high}
    return out


def fetch_delta() -> dict[str, dict]:
    items = _parse_delta(http_get(DELTA_BASE))
    if len(items) < 60:                      # 首页不全时再试 /market
        try:
            more = _parse_delta(http_get(DELTA_BASE + "market"))
            if len(more) > len(items):
                items = more
        except Exception:                    # noqa: BLE001
            pass
    return items


# ------------------------------------------------------------------ 源 B：orzice 历史价
ORZICE_BASE = "https://orzice.com/v/ammo"
ORZICE_MAX_PAGE = 12
TAG_RE = re.compile(r"<[^>]+>")
ROW_RE = re.compile(r"<tr.*?</tr>", re.S)
CELL_RE = re.compile(r"<td.*?</td>", re.S)
TABLE_RE = re.compile(r"<table.*?</table>", re.S)
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _number_of(td_html: str) -> float | None:
    """价格单元格形如 ``{{NumQfw(324)}}`` 或 ``1,148``：去标签、去模板花括号、去千分位。"""
    s = TAG_RE.sub(" ", td_html).replace("{{", " ").replace("}}", " ")
    s = s.replace(",", "").replace("，", "").strip()
    if not s or s in {"-", "—", "/"}:
        return None
    m = NUM_RE.search(s)
    return float(m.group(0)) if m else None


def _first_line(td_html: str) -> str:
    s = htmllib.unescape(TAG_RE.sub("\n", td_html)).replace("\xa0", " ")
    for line in s.split("\n"):
        line = line.strip()
        if line and "推荐方式" not in line and "金本位" not in line:
            return line
    return ""


def fetch_orzice() -> dict[str, dict]:
    """列顺序：物品/金本位/当前价/今日涨幅/3日价/3日涨/7日价/7日涨/30日价/30日涨。"""
    hist: dict[str, dict] = {}
    empty = 0
    for page in range(1, ORZICE_MAX_PAGE + 1):
        url = ORZICE_BASE + (f"?p={page}" if page > 1 else "")
        try:
            html = http_get(url)
        except StopIteration:
            break
        except Exception:                    # noqa: BLE001
            break
        table = TABLE_RE.search(html)
        if not table:
            break
        got = 0
        for tr in ROW_RE.findall(table.group(0)):
            cells = CELL_RE.findall(tr)
            if len(cells) < 10:
                continue
            name = _first_line(cells[0])
            if not name or name in {"物品", "名称"}:
                continue
            p3 = _number_of(cells[4])
            if not p3:
                continue
            p7, p30 = _number_of(cells[6]), _number_of(cells[8])
            hist[name] = {
                "p3": int(p3), "p3p": _number_of(cells[5]),
                "p7": int(p7) if p7 else None, "p7p": _number_of(cells[7]),
                "p30": int(p30) if p30 else None, "p30p": _number_of(cells[9]),
            }
            got += 1
        if got == 0:
            empty += 1
            if empty >= 2:                   # 连续两页无数据 = 到底了
                break
        else:
            empty = 0
        time.sleep(0.35)                     # 给站点留余量
    return hist


# ------------------------------------------------------------------ 落盘
def last_ts(path: str) -> int:
    """读文件最后一行的时间戳，用于判断采样间隔。"""
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            last = ""
            for line in fh:
                if line.strip():
                    last = line
        return int(json.loads(last).get("t", 0)) if last else 0
    except Exception:                        # noqa: BLE001 坏行不该让采集失败
        return 0


def append_sample(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_hist(path: str, ts: int, hist: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"t": ts, "o": hist}, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)                    # 原子替换，避免抓一半的残缺文件被提交


# ------------------------------------------------------------------ 主流程
def main() -> int:
    started = time.time()
    ts = int(started)
    day = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
    data_path = os.path.join(DATA_DIR, f"{day}.jsonl")
    hist_path = os.path.join(HIST_DIR, f"{day}.json")

    delta: dict[str, dict] = {}
    hist: dict[str, dict] = {}
    errs: list[str] = []

    try:
        delta = fetch_delta()
    except Exception as exc:                 # noqa: BLE001
        errs.append(f"deltaammo: {exc}")
    try:
        hist = fetch_orzice()
    except Exception as exc:                 # noqa: BLE001
        errs.append(f"orzice: {exc}")

    wrote = False
    gap = ts - last_ts(data_path)
    if delta and gap >= MIN_GAP_SEC:
        rows = [[n, v["grade"], v["price"], v["low"], v["high"]]
                for n, v in sorted(delta.items())]
        append_sample(data_path, {"t": ts, "d": rows})
        wrote = True
    elif delta and gap < MIN_GAP_SEC:
        print(f"[skip] 距上一条仅 {gap}s（< {MIN_GAP_SEC}s），本轮不重复落盘")

    if hist:
        write_hist(hist_path, ts, hist)

    elapsed = round(time.time() - started, 2)
    # 三态：本轮落盘 / 采样间隔太近主动跳过 / 抓不到现价（站点改版或封禁）
    state = "ok" if wrote else ("skip" if delta else "fail")
    print(f"[{state}] {day} 现价 {len(delta)} 条 / 历史价 {len(hist)} 条 / 用时 {elapsed}s")
    for e in errs:
        print("  !", e)

    # 只要现价抓到了就算这条链路健康；「跳过」是主动行为，不该让 Actions 变红
    return 0 if delta else 1


if __name__ == "__main__":
    sys.exit(main())

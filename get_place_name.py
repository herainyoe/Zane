#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
get_place_name.py —— 地名轮播 + 资料推送

运行逻辑（日期驱动 · 无状态 · 半天为周期）：
  1. 基准时间 BASE_DATE = 2026-07-12 00:00（北京时间）。序号 = 距基准时间经过的“上午/下午”个数 + 1
     （每 12 小时为一个周期：00:00–11:59 为上午、12:00–23:59 为下午，完全由运行时刻决定，无需任何状态文件）
  2. 按本期序号，分别从 city_level1.csv / county_level2.csv 取对应的市级、县级地名
  3. 调用大模型（OpenAI 兼容接口）查询这两个地名的资料
     （历史背景、风土人情、地理知识、特色产物、文化特色、社会民生等，有则列、无则省）
  4. 将资料整合为 HTML
  5. 通过 pushplus 接口推送（template=html, channel=wechat,extension）
  —— 本方案无状态：不读写序号文件、不回写仓库，天然幂等，可放心在云端定时任务反复运行

依赖：仅标准库（csv / json / re / os / urllib / datetime / html / argparse）

环境变量：
  LLM_API_KEY    大模型 API Key（仅 --auto 自动查资料模式需要）
  LLM_BASE_URL   兼容接口地址，默认 https://api.openai.com/v1
  LLM_MODEL      模型名，默认 gpt-4o-mini
  PUSHPLUS_TOKEN pushplus 推送 token（脚本已内置默认值）

运行模式（命令行）：
  python get_place_name.py                 # --auto：调用 LLM 查资料 → 渲染 → 推送（自托管用）
  python get_place_name.py --compute       # 仅按当前时刻算出本期地名，打印 JSON，不查资料、不推送
  python get_place_name.py --report --md research.md
                                          # 读取 research.md（模型已写好的资料）→ 渲染 HTML → 推送
  —— 云端定时任务推荐用 --compute + --report 组合：由模型负责“查资料”，脚本只做取地名/渲染/推送
"""

import csv
import json
import os
import re
import html as _html
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ---------------------- 配置 ----------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CITY_FILE = os.path.join(BASE_DIR, "city_level1.csv")
COUNTY_FILE = os.path.join(BASE_DIR, "county_level2.csv")

# 序号基准时间：2026-07-12 00:00（北京时间）。每经过一个“上午/下午”（12 小时）序号 +1
BASE_DATE = datetime(2026, 7, 12, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
HALF_DAY = timedelta(hours=12)

PUSHPLUS_URL = "http://www.pushplus.plus/batchSend"
# 优先读环境变量；为空时回退到下方默认值（避免 CI 中传了空 secret 反而把默认值覆盖掉）
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN") or "105d09684cdd41a888866c3b4ca81844"

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1"
LLM_MODEL = os.environ.get("LLM_MODEL") or "gpt-4o-mini"

TZ = timezone(timedelta(hours=8))  # 北京时间


# ---------------------- 读取地名 ----------------------
def read_csv_rows(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def get_period_place():
    """按当前时刻确定性地推导本期序号，并从两个 CSV 取对应的市、县地名。

    序号 = 距 BASE_DATE 经过的“上午/下午”个数 + 1（每 12 小时为一个周期），
    完全由运行时刻决定，无需读取/写入任何状态文件，
    因此在云端定时任务等无状态环境中可安全反复运行（同一时刻多次运行得到同一结果）。

    市、县数量不对等（市 393，区县 3210）。取市的规则：
      - 当本期序号 <= 市的最大数目时：市、县各自按序号独立取值；
      - 当本期序号 > 市的最大数目时：市名改用“当次县区所属的地市”
        （通过 county["pid"] 反查 city["id"] 得到），使市县在行政区划上真正对应。
    """
    now = datetime.now(TZ)
    index = int((now - BASE_DATE).total_seconds() // HALF_DAY.total_seconds()) + 1
    if index < 1:
        index = 1  # 基准时间之前兜底为第 1 期
    # 第奇数个周期为上午、偶数为下午（period1=上午第1天, period2=下午第1天, ...）
    period_label = "上午" if index % 2 == 1 else "下午"

    cities = read_csv_rows(CITY_FILE)
    counties = read_csv_rows(COUNTY_FILE)

    # 县区始终按序号取模轮播
    county = counties[(index - 1) % len(counties)]

    # 构建 city_id -> city 行 的索引，用于按 pid 反查县区所属地市
    city_by_id = {c["id"]: c for c in cities}

    used_parent = False  # 标记本次市名是否来自“县区所属地市”
    if index <= len(cities):
        city = cities[(index - 1) % len(cities)]
    else:
        parent = city_by_id.get(county.get("pid"))
        if parent is not None:
            city = parent
            used_parent = True
        else:
            city = cities[(index - 1) % len(cities)]

    return {
        "index": index,
        "city": {
            "name": city["ext_name"],
            "id": city["id"],
            "pinyin": city["pinyin"],
        },
        "county": {
            "name": county["ext_name"],
            "id": county["id"],
            "pinyin": county["pinyin"],
        },
        "city_from_parent": used_parent,
        "period_label": period_label,
        "total_cities": len(cities),
        "total_counties": len(counties),
    }


# ---------------------- 资料查询（大模型） ----------------------
SYSTEM_PROMPT = (
    "你是一位博学的中国地理与人文百科全书式助手。"
    "请根据用户提供的市、县地名，整理该地的资料，涵盖（有则列，无则省）："
    "历史背景、风土人情、地理知识、特色产物、文化特色、社会民生，"
    "以及其他一切比较突出、有特色的方面。内容要准确、有条理、可读性强，"
    "使用 Markdown 小标题（## / ###）分节，可用 - 列表。"
)


def query_place_info(city_name, county_name):
    """调用 OpenAI 兼容接口查询地名资料，返回 Markdown 文本。"""
    if not LLM_API_KEY:
        raise RuntimeError(
            "未配置 LLM_API_KEY 环境变量，无法自动查询资料。"
            "请在运行前设置 LLM_API_KEY（以及可选的 LLM_BASE_URL / LLM_MODEL）。"
        )
    user_prompt = (
        f"请整理以下两个地点的资料：\n"
        f"市级：{city_name}\n"
        f"县级：{county_name}\n"
        f"请分别介绍两地，并突出其最具特色的方面。"
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
    }
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


# ---------------------- Markdown -> HTML ----------------------
def _inline(text):
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


def md_to_html(md):
    lines = md.split("\n")
    out = []
    in_list = False
    for line in lines:
        s = line.rstrip()
        if not s.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if s.startswith("### "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h3>{_html.escape(s[4:])}</h3>")
        elif s.startswith("## "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h2>{_html.escape(s[3:])}</h2>")
        elif s.startswith("# "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h1>{_html.escape(s[2:])}</h1>")
        elif s.startswith("- ") or s.startswith("* "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_inline(_html.escape(s[2:]))}</li>")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<p>{_inline(_html.escape(s))}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


# ---------------------- HTML 渲染 ----------------------
PAGE_CSS = """
<style>
  body { margin:0; background:#f5f6f8; font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; color:#1f2329; }
  .card { max-width:720px; margin:24px auto; background:#fff; border-radius:14px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,.06); }
  .head { background:linear-gradient(135deg,#2b5876,#4e4376); color:#fff; padding:26px 28px; }
  .head h1 { margin:0; font-size:26px; }
  .head .sub { opacity:.85; font-weight:400; font-size:20px; }
  .meta { margin-top:8px; font-size:13px; opacity:.85; }
  .content { padding:22px 28px 30px; line-height:1.8; font-size:15px; }
  .content h2 { font-size:20px; border-left:4px solid #4e4376; padding-left:10px; margin-top:26px; }
  .content h3 { font-size:17px; color:#2b5876; margin-top:18px; }
  .content p { margin:10px 0; }
  .content ul { margin:8px 0; padding-left:22px; }
  .content li { margin:5px 0; }
  .footer { text-align:center; color:#9aa0a6; font-size:12px; padding:14px; }
</style>
"""


def render_html(place, md_text):
    body = md_to_html(md_text)
    city = place["city"]["name"]
    county = place["county"]["name"]
    title = f"{city} · {county} —— 地名轮播 No.{place['index']}（{place['period_label']}）"
    parent_tag = "（所属地市）" if place.get("city_from_parent") else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>{PAGE_CSS}</head>
<body>
<div class="card">
  <div class="head">
    <h1>{city}{parent_tag} <span class="sub">· {county}</span></h1>
    <div class="meta">第 {place['index']} 期（{place['period_label']}） · 拼音 {place['city']['pinyin']} / {place['county']['pinyin']} · 共 {place['total_cities']} 市 / {place['total_counties']} 区县</div>
  </div>
  <div class="content">{body}</div>
  <div class="footer">由 get_place_name.py 自动生成 · pushplus 推送</div>
</div>
</body></html>"""


# ---------------------- pushplus 推送 ----------------------
def push_plus(title, content_html):
    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content_html,
        "template": "html",
        "channel": "wechat,extension",
    }
    req = urllib.request.Request(
        PUSHPLUS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------- 各模式入口 ----------------------
def mode_compute():
    """仅按当前时刻算出本期地名并打印 JSON（不查资料、不推送）。"""
    place = get_period_place()
    out = {
        "index": place["index"],
        "period_label": place["period_label"],
        "city": place["city"]["name"],
        "county": place["county"]["name"],
        "city_from_parent": place["city_from_parent"],
    }
    print(json.dumps(out, ensure_ascii=False))
    return place


def mode_report(md_path):
    """读取模型写好的资料(md)，渲染 HTML 并推送到 pushplus。"""
    place = get_period_place()
    _src = "所属地市" if place.get("city_from_parent") else "独立序号"
    print(f"本期序号: {place['index']}（{place['period_label']}）| 市级: {place['city']['name']}（{_src}） | 县级: {place['county']['name']}")

    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    html = render_html(place, md)
    title = f"{place['city']['name']} · {place['county']['name']}（第{place['index']}期 · {place['period_label']}）"

    result = push_plus(title, html)
    print("推送结果:", result)
    if result.get("code") == 200:
        print("推送成功（本方案无状态，无需回写序号）。")
    else:
        print("推送未成功。")
    return result


def mode_auto():
    """--auto：调用 LLM 查资料 → 渲染 → 推送（自托管 / 有 API key 时用）。"""
    place = get_period_place()
    _src = "所属地市" if place.get("city_from_parent") else "独立序号"
    print(f"本期序号: {place['index']}（{place['period_label']}）| 市级: {place['city']['name']}（{_src}） | 县级: {place['county']['name']}")

    md = query_place_info(place["city"]["name"], place["county"]["name"])
    html = render_html(place, md)
    title = f"{place['city']['name']} · {place['county']['name']}（第{place['index']}期）"

    result = push_plus(title, html)
    print("推送结果:", result)
    if result.get("code") == 200:
        print("推送成功（本方案无状态，无需回写序号）。")
    else:
        print("推送未成功。")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="地名轮播 + 资料推送")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--compute", action="store_true", help="仅计算本期地名并打印 JSON")
    group.add_argument("--report", action="store_true", help="读取资料 md 并渲染推送")
    group.add_argument("--auto", action="store_true", help="调用 LLM 查资料并推送（需 LLM_API_KEY）")
    parser.add_argument("--md", default="research.md", help="--report 时使用的资料 md 文件路径")
    args = parser.parse_args()

    if args.compute:
        mode_compute()
    elif args.report:
        mode_report(args.md)
    else:
        # 默认即 --auto
        mode_auto()

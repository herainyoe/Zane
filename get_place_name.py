#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
get_place_name.py —— 地名轮播 + 资料推送

运行逻辑（对应需求）：
  1. 从 read_index.json 读取上一次的读取序号 last_index
  2. 本次序号 = last_index + 1
  3. 按本次序号，分别从 city_level1.csv / county_level2.csv 取对应的市级、县级地名
  4. 调用大模型（OpenAI 兼容接口）查询这两个地名的资料
     （历史背景、风土人情、地理知识、特色产物、文化特色、社会民生等，有则列、无则省）
  5. 将资料整合为 HTML
  6. 通过 pushplus 接口推送（template=html, channel=wechat,extension）
  7. 推送成功后，将本次序号写回 read_index.json

依赖：仅标准库（csv / json / math / os / urllib / datetime / html / re）

环境变量（可选）：
  LLM_API_KEY    大模型 API Key（必填才能自动查资料）
  LLM_BASE_URL   兼容接口地址，默认 https://api.openai.com/v1
  LLM_MODEL      模型名，默认 gpt-4o-mini
  PUSHPLUS_TOKEN pushplus 推送 token（脚本已内置默认值）
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
INDEX_FILE = os.path.join(BASE_DIR, "read_index.json")

PUSHPLUS_URL = "http://www.pushplus.plus/batchSend"
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "105d09684cdd41a888866c3b4ca81844")

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

TZ = timezone(timedelta(hours=8))  # 北京时间


# ---------------------- 序号管理 ----------------------
def load_index():
    if not os.path.exists(INDEX_FILE):
        return {"last_index": 0, "last_city": None, "last_county": None, "updated_at": None}
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_index(record):
    record["updated_at"] = datetime.now(TZ).isoformat()
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)


# ---------------------- 读取地名 ----------------------
def read_csv_rows(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def get_next_place():
    """读取上一次序号 +1，并从两个 CSV 中取出对应的市、县地名。

    市、县数量不对等（市 393，区县 3210）。取市的规则：
      - 当本次序号 new_index <= 市的最大数目时：市、县各自按序号独立取值；
      - 当 new_index > 市的最大数目时：市名不再对市表取模轮播，而是改用
        “本次县区所属的地市”（通过 county["pid"] 反查 city["id"] 得到），
        这样超出市表范围后，展示的市与当次县区在行政区划上真正对应。
    """
    index_record = load_index()
    last_index = index_record.get("last_index", 0)
    new_index = last_index + 1

    cities = read_csv_rows(CITY_FILE)
    counties = read_csv_rows(COUNTY_FILE)

    # 县区始终按序号取模轮播
    county = counties[(new_index - 1) % len(counties)]

    # 构建 city_id -> city 行 的索引，用于按 pid 反查县区所属地市
    city_by_id = {c["id"]: c for c in cities}

    used_parent = False  # 标记本次市名是否来自“县区所属地市”
    if new_index <= len(cities):
        # 未超过市的最大数目：市、县各自独立取序号
        city = cities[(new_index - 1) % len(cities)]
    else:
        # 超过市的最大数目：以当次县区所属的地市替代市名
        parent = city_by_id.get(county.get("pid"))
        if parent is not None:
            city = parent
            used_parent = True
        else:
            # 兜底：pid 查不到所属地市时，回退到取模轮播
            city = cities[(new_index - 1) % len(cities)]

    return {
        "index": new_index,
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
    title = f"{city} · {county} —— 地名轮播 No.{place['index']}"
    parent_tag = "（所属地市）" if place.get("city_from_parent") else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>{PAGE_CSS}</head>
<body>
<div class="card">
  <div class="head">
    <h1>{city}{parent_tag} <span class="sub">· {county}</span></h1>
    <div class="meta">第 {place['index']} 期 · 拼音 {place['city']['pinyin']} / {place['county']['pinyin']} · 共 {place['total_cities']} 市 / {place['total_counties']} 区县</div>
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


# ---------------------- 主流程 ----------------------
def main():
    place = get_next_place()
    _src = "所属地市" if place.get("city_from_parent") else "独立序号"
    print(f"本次序号: {place['index']} | 市级: {place['city']['name']}（{_src}） | 县级: {place['county']['name']}")

    md = query_place_info(place["city"]["name"], place["county"]["name"])
    html = render_html(place, md)
    title = f"{place['city']['name']} · {place['county']['name']}（第{place['index']}期）"

    result = push_plus(title, html)
    print("推送结果:", result)

    if result.get("code") == 200:
        save_index({
            "last_index": place["index"],
            "last_city": place["city"]["name"],
            "last_county": place["county"]["name"],
        })
        print("已写入 read_index.json（last_index = %d）" % place["index"])
    else:
        print("推送未成功，未更新序号。")


if __name__ == "__main__":
    main()

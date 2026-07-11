"""
计算当前时间距离 2026-07-12 00:00 经过了多少个 6h（向上取整），
作为序号读取市级和县级行政区划文件中的地名并返回。
"""
import csv
import math
from datetime import datetime

# 基准时间
BASE_TIME = datetime(2026, 7, 12, 0, 0, 0)

# 文件路径（与脚本同目录）
CITY_FILE = "city_level1.csv"
COUNTY_FILE = "county_level2.csv"


def get_index_and_names():
    now = datetime.now()
    delta = now - BASE_TIME

    # 经过的总秒数 / 6小时 = 经过的6h周期数
    hours_elapsed = delta.total_seconds() / 3600
    periods = hours_elapsed / 6

    # 向上取整，最小为 1
    index = math.ceil(periods) if periods > 0 else 1

    # 读取市级数据
    with open(CITY_FILE, "r", encoding="utf-8-sig") as f:
        cities = list(csv.DictReader(f))

    # 读取县级数据
    with open(COUNTY_FILE, "r", encoding="utf-8-sig") as f:
        counties = list(csv.DictReader(f))

    # 序号对数据量取模，确保不越界
    city_idx = (index - 1) % len(cities)
    county_idx = (index - 1) % len(counties)

    city = cities[city_idx]
    county = counties[county_idx]

    result = {
        "index": index,
        "city_name": city["ext_name"],
        "city_id": city["id"],
        "county_name": county["ext_name"],
        "county_id": county["id"],
        "city_pinyin": city["pinyin"],
        "county_pinyin": county["pinyin"],
        "total_cities": len(cities),
        "total_counties": len(counties),
    }

    return result


if __name__ == "__main__":
    result = get_index_and_names()
    print(f"序号: {result['index']}")
    print(f"市级: {result['city_name']} (ID: {result['city_id']}, 拼音: {result['city_pinyin']})")
    print(f"县级: {result['county_name']} (ID: {result['county_id']}, 拼音: {result['county_pinyin']})")

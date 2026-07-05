"""
跨区域品类市场雷达 - Vercel Serverless Function
将原 FastAPI 后端改写为 Vercel Python Runtime 兼容格式
"""

import json
import hashlib
import math
import logging
import os
import time
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from typing import Optional

import requests

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
DATA_TIMEOUT = 3  # 外部请求需要快速失败并降级，避免拖慢页面。
GOOGLE_TRENDS_BUDGET = 6

VALID_MARKETS = {
    "US": "United States", "JP": "Japan", "KR": "South Korea",
    "AE": "United Arab Emirates", "DE": "Germany", "TH": "Thailand",
    "PH": "Philippines", "GB": "United Kingdom", "FR": "France",
    "BR": "Brazil", "IN": "India", "AU": "Australia", "CA": "Canada",
    "MX": "Mexico", "IT": "Italy", "ES": "Spain", "ID": "Indonesia",
    "MY": "Malaysia", "SG": "Singapore", "VN": "Vietnam",
}

CHART_COLORS = [
    "#6B8E8E", "#C4A882", "#B8928A", "#8B9DAF",
    "#A8B5A0", "#D4A574", "#9B8EA8",
]

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────
def get_date_range(months: int = 12):
    """获取日期范围字符串"""
    end = datetime.now()
    start = end - timedelta(days=months * 30)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def safe_value(v, default=0):
    """安全转换数值"""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────
# 数据源 1: Google Trends (pytrends)
# ─────────────────────────────────────────────
def fetch_google_trends(keyword: str, markets: list, timeframe: str = "today 12-m"):
    """
    获取 Google Trends 数据：搜索趋势 + 相关查询 + 地区热度
    优先尝试 pytrends，失败则用模拟数据优雅降级。
    """
    result = {
        "search_trends": [],
        "consumer_focus": [],
        "region_heatmap": [],
        "source_status": "pending",
        "source_type": "live",
        "source_label": "Checking Google Trends",
        "error": None,
    }

    try:
        from pytrends.request import TrendReq
        logger.info(f"[Google Trends] 尝试通过 pytrends 获取 '{keyword}' 数据...")

        started_at = time.monotonic()
        pytrends = TrendReq(hl='en-US', tz=360, timeout=DATA_TIMEOUT)

        # 模块1: 各市场的搜索趋势
        trends_data = []
        for i, market in enumerate(markets):
            if time.monotonic() - started_at > GOOGLE_TRENDS_BUDGET:
                logger.warning("[Google Trends] 时间预算用尽，停止继续请求市场数据")
                break

            try:
                pytrends.build_payload([keyword], geo=market, timeframe=timeframe)
                iot = pytrends.interest_over_time()

                time_series = []
                if iot is not None and not iot.empty:
                    for idx, row in iot.iterrows():
                        # 跳过 isPartial 列
                        val = row.get(keyword, 0)
                        time_series.append({
                            "date": idx.strftime("%Y-%m-%d"),
                            "value": safe_value(val),
                        })

                color = CHART_COLORS[i % len(CHART_COLORS)]
                trends_data.append({
                    "market": market,
                    "market_name": VALID_MARKETS.get(market, market),
                    "color": color,
                    "data": time_series,
                })

                # 模块2: 相关查询（仅第一个市场）
                if i == 0:
                    try:
                        rq = pytrends.related_queries()
                        if keyword in rq and rq[keyword] is not None:
                            top_data = rq[keyword].get("top")
                            rising_data = rq[keyword].get("rising")
                            top_list = []
                            rising_list = []
                            if top_data is not None and not top_data.empty:
                                for _, row in top_data.head(10).iterrows():
                                    top_list.append({
                                        "query": str(row.get("query", "")),
                                        "value": safe_value(row.get("value", 0)),
                                    })
                            if rising_data is not None and not rising_data.empty:
                                for _, row in rising_data.head(10).iterrows():
                                    rising_list.append({
                                        "query": str(row.get("query", "")),
                                        "value": str(row.get("value", "")),
                                    })
                            result["consumer_focus"].append({
                                "market": market,
                                "top": top_list,
                                "rising": rising_list,
                            })
                    except Exception as e:
                        logger.warning(f"[Google Trends] 相关查询获取失败: {e}")

            except Exception as e:
                logger.warning(f"[Google Trends] 市场 {market} 失败: {e}")

        result["search_trends"] = trends_data

        # 模块3: 地区热度（全球数据）
        try:
            pytrends.build_payload([keyword], geo="", timeframe=timeframe)
            ibr = pytrends.interest_by_region(resolution='COUNTRY', inc_low_vol=True, inc_geo_code=True)
            if ibr is not None and not ibr.empty:
                regions = []
                for idx, row in ibr.iterrows():
                    regions.append({
                        "geo_code": row.get("geoCode", ""),
                        "geo_name": idx,
                        "value": safe_value(row.get(keyword, 0)),
                    })
                regions.sort(key=lambda x: x["value"], reverse=True)
                result["region_heatmap"] = regions[:20]
        except Exception as e:
            logger.warning(f"[Google Trends] 区域数据获取失败: {e}")

        if trends_data:
            has_data = any(t["data"] for t in trends_data)
            result["source_status"] = "success" if has_data else "partial"
            result["source_type"] = "live" if has_data else "partial"
            result["source_label"] = "Live relative search interest" if has_data else "Partial live data"
            if not has_data:
                result["error"] = "部分数据获取失败"
        else:
            result = generate_mock_trends(keyword, markets)

    except ImportError:
        logger.warning("[Google Trends] pytrends 未安装，使用模拟数据")
        result = generate_mock_trends(keyword, markets)
    except Exception as e:
        logger.error(f"[Google Trends] 整体失败: {e}")
        result = generate_mock_trends(keyword, markets)

    return result


# ─────────────────────────────────────────────
# 数据源 2: NewsAPI.org
# ─────────────────────────────────────────────
def fetch_news(keyword: str, page_size: int = 15):
    """获取行业新闻"""
    result = {
        "articles": [],
        "source_status": "pending",
        "source_type": "live",
        "source_label": "Checking NewsAPI",
        "error": None,
    }

    if not NEWS_API_KEY:
        result["source_status"] = "skipped"
        result["source_type"] = "unavailable"
        result["source_label"] = "NewsAPI key not configured"
        result["error"] = "NEWS_API_KEY 环境变量未配置"
        return result

    from_date, _ = get_date_range(months=1)

    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": keyword,
                "from": from_date,
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": page_size,
                "apiKey": NEWS_API_KEY,
            },
            timeout=DATA_TIMEOUT,
        )
        data = resp.json()

        if data.get("status") == "ok":
            articles = []
            for a in data.get("articles", []):
                articles.append({
                    "title": a.get("title", ""),
                    "source": a.get("source", {}).get("name", "Unknown"),
                    "url": a.get("url", ""),
                    "description": a.get("description", ""),
                    "published_at": a.get("publishedAt", ""),
                    "image_url": a.get("urlToImage", ""),
                    "type": "news",
                })
            result["articles"] = articles
            result["source_status"] = "success"
            result["source_type"] = "live"
            result["source_label"] = "Live NewsAPI articles"
        else:
            result["source_status"] = "error"
            result["source_type"] = "unavailable"
            result["source_label"] = "NewsAPI unavailable"
            result["error"] = data.get("message", "NewsAPI 返回错误")

    except requests.Timeout:
        result["source_status"] = "timeout"
        result["source_type"] = "unavailable"
        result["source_label"] = "NewsAPI timed out"
        result["error"] = "NewsAPI 请求超时"
    except Exception as e:
        result["source_status"] = "error"
        result["source_type"] = "unavailable"
        result["source_label"] = "NewsAPI unavailable"
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# 数据源 3: Reddit（优先 OAuth，未配置时尝试公开 JSON 降级）
# ─────────────────────────────────────────────
def get_reddit_access_token() -> Optional[str]:
    """使用 app-only OAuth 获取 Reddit token。未配置凭证时返回 None。"""
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        return None

    auth = f"{REDDIT_CLIENT_ID}:{REDDIT_CLIENT_SECRET}".encode("utf-8")
    basic = base64.b64encode(auth).decode("ascii")
    resp = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        data={"grant_type": "client_credentials"},
        headers={
            "Authorization": f"Basic {basic}",
            "User-Agent": "MarketRadar/1.1 by yongnini",
        },
        timeout=DATA_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Reddit OAuth 返回 {resp.status_code}")

    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Reddit OAuth 未返回 access_token")
    return token


def append_reddit_posts(result: dict, data: dict):
    """从 Reddit listing 响应中提取帖子。"""
    posts = data.get("data", {}).get("children", [])
    for p in posts:
        d = p.get("data", {})
        result["posts"].append({
            "title": d.get("title", ""),
            "subreddit": d.get("subreddit", ""),
            "url": f"https://reddit.com{d.get('permalink', '')}",
            "score": safe_value(d.get("score", 0)),
            "comments": safe_value(d.get("num_comments", 0)),
            "created": datetime.fromtimestamp(
                d.get("created_utc", 0), tz=timezone.utc
            ).isoformat(),
            "type": "reddit",
        })


def fetch_reddit(keyword: str, limit: int = 10):
    """获取 Reddit 讨论"""
    result = {
        "posts": [],
        "source_status": "pending",
        "source_type": "live",
        "source_label": "Checking Reddit",
        "error": None,
    }

    try:
        token = get_reddit_access_token()
        if token:
            resp = requests.get(
                "https://oauth.reddit.com/search",
                params={"q": keyword, "limit": limit, "sort": "relevance", "t": "year"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "MarketRadar/1.1 by yongnini",
                },
                timeout=DATA_TIMEOUT,
            )
            if resp.status_code == 200:
                append_reddit_posts(result, resp.json())
                result["source_status"] = "success"
                result["source_type"] = "live"
                result["source_label"] = "Live Reddit OAuth data"
            else:
                result["source_status"] = "error"
                result["source_type"] = "unavailable"
                result["source_label"] = "Reddit OAuth unavailable"
                result["error"] = f"Reddit OAuth 返回 {resp.status_code}"
            return result

        resp = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": keyword, "limit": limit, "sort": "relevance", "t": "year"},
            headers={"User-Agent": "MarketRadar/1.1 by yongnini"},
            timeout=DATA_TIMEOUT,
        )
        if resp.status_code == 200:
            append_reddit_posts(result, resp.json())
            result["source_status"] = "success"
            result["source_type"] = "live"
            result["source_label"] = "Live Reddit public JSON"
        else:
            result["source_status"] = "error"
            result["source_type"] = "unavailable"
            result["source_label"] = "Reddit OAuth not configured"
            result["error"] = f"Reddit public JSON 返回 {resp.status_code}；配置 REDDIT_CLIENT_ID 和 REDDIT_CLIENT_SECRET 可启用 OAuth"
    except Exception as e:
        result["source_status"] = "error"
        result["source_type"] = "unavailable"
        result["source_label"] = "Reddit unavailable"
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# 模块4: 策略建议生成（规则引擎）
# ─────────────────────────────────────────────
def generate_strategy(trends_data: list, consumer_focus: list, region_data: list, keyword: str) -> dict:
    """基于数据生成策略建议"""
    strategy = {
        "scores": {
            "market_potential": 0,
            "competition_level": 0,
            "entry_timing": 0,
        },
        "pillars": [],
        "summary": "",
    }

    # 基于趋势数据计算分数
    if trends_data:
        all_values = []
        growth_trends = []
        for market_data in trends_data:
            data_points = market_data.get("data", [])
            if len(data_points) >= 2:
                values = [d["value"] for d in data_points]
                all_values.extend(values)
                mid = len(values) // 2
                first_half = sum(values[:mid]) / max(mid, 1)
                second_half = sum(values[mid:]) / max(len(values) - mid, 1)
                if first_half > 0:
                    growth = (second_half - first_half) / first_half
                    growth_trends.append(growth)

        if all_values:
            avg_interest = sum(all_values) / len(all_values)
            strategy["scores"]["market_potential"] = min(10, round(avg_interest / 10, 1))

        if growth_trends:
            avg_growth = sum(growth_trends) / len(growth_trends)
            if avg_growth > 0.2:
                strategy["scores"]["entry_timing"] = 8.5
            elif avg_growth > 0:
                strategy["scores"]["entry_timing"] = 7.0
            elif avg_growth > -0.1:
                strategy["scores"]["entry_timing"] = 5.5
            else:
                strategy["scores"]["entry_timing"] = 4.0

    # 竞争度：基于消费者关注的多样性
    query_diversity = 0
    for cf in consumer_focus:
        if isinstance(cf, dict):
            top_queries = cf.get("top", [])
            query_diversity += len(top_queries)
    if query_diversity > 20:
        strategy["scores"]["competition_level"] = 7.5
    elif query_diversity > 10:
        strategy["scores"]["competition_level"] = 6.0
    else:
        strategy["scores"]["competition_level"] = 4.5

    # 策略支柱
    avg_potential = strategy["scores"]["market_potential"]
    avg_timing = strategy["scores"]["entry_timing"]

    pillars = []

    if avg_potential >= 6:
        pillars.append({
            "title": "市场进入策略",
            "icon": "🎯",
            "description": f"市场潜力评分 {avg_potential}/10，建议积极布局核心市场",
            "actions": ["聚焦 Top 3 高热度市场优先投放", "建立品牌认知和本地化内容", "预留 6 个月市场培育期"],
        })
    else:
        pillars.append({
            "title": "市场进入策略",
            "icon": "🎯",
            "description": f"市场潜力评分 {avg_potential}/10，建议谨慎试水",
            "actions": ["选择 1-2 个市场小规模测试", "通过跨境电商平台降低试错成本", "设定 3 个月数据验证节点"],
        })

    pillars.append({
        "title": "产品定位建议",
        "icon": "💡",
        "description": "基于消费者搜索行为优化产品卖点",
        "actions": ["关注 Rising Queries 中的新兴需求", "针对 Top Queries 优化 listing 关键词", "考虑差异化功能以避开红海竞争"],
    })

    pillars.append({
        "title": "营销节奏规划",
        "icon": "📅",
        "description": "结合搜索趋势周期制定营销计划",
        "actions": ["识别搜索高峰月份，提前 4-6 周启动广告", "关注区域性节日/促销节点", "淡季维持品牌曝光，旺季集中转化"],
    })

    if region_data:
        top_regions = region_data[:5]
        region_names = ", ".join([r.get("geo_name", r.get("geo_code", "")) for r in top_regions])
        pillars.append({
            "title": "区域扩展路线",
            "icon": "🌍",
            "description": f"核心热度区域: {region_names}",
            "actions": ["优先覆盖热度 Top 5 区域", "根据区域特征定制营销策略", "建立区域仓储/物流能力"],
        })
    else:
        pillars.append({
            "title": "区域扩展路线",
            "icon": "🌍",
            "description": "区域数据待补充，建议通过平台数据进一步分析",
            "actions": ["利用 Amazon/平台后台数据补充区域分析", "参考行业报告确定重点市场", "小批量测试多区域市场反应"],
        })

    growth_signal = "上升" if avg_timing >= 7 else ("稳定" if avg_timing >= 5 else "下降")
    pillars.append({
        "title": "风险与机会",
        "icon": "⚡",
        "description": f"市场趋势整体{growth_signal}，需关注竞争动态",
        "actions": ["持续监测搜索热度变化", "关注新进入者和价格竞争", "建立供应链韧性以应对需求波动"],
    })

    strategy["pillars"] = pillars

    # 总结
    market_count = len(trends_data)
    avg_score = sum(strategy["scores"].values()) / 3
    strategy["summary"] = (
        f"针对「{keyword}」品类，在 {market_count} 个目标市场中的综合评分为 {avg_score:.1f}/10。"
        f"市场潜力 {strategy['scores']['market_potential']}/10，"
        f"竞争水平 {strategy['scores']['competition_level']}/10，"
        f"进入时机 {strategy['scores']['entry_timing']}/10。"
    )

    return strategy


# ─────────────────────────────────────────────
# 模拟数据（降级用）
# ─────────────────────────────────────────────
def generate_mock_trends(keyword: str, markets: list) -> dict:
    """生成合理的模拟趋势数据（用于数据源不可用时的降级展示）"""
    seed = int(hashlib.md5(keyword.encode()).hexdigest()[:8], 16)

    now = datetime.now()
    dates = []
    for i in range(52):  # 52 周
        d = now - timedelta(weeks=51 - i)
        dates.append(d.strftime("%Y-%m-%d"))

    trends = []
    consumer_focus = []

    for i, market in enumerate(markets):
        base = 20 + ((seed + i * 37) % 50)
        growth = 0.002 + ((seed + i * 13) % 10) * 0.001
        noise_amp = 5 + ((seed + i * 7) % 10)

        data_points = []
        for j, date in enumerate(dates):
            value = base + growth * j * 50 + noise_amp * math.sin(j * 0.3 + seed * 0.01 + i)
            value = max(5, min(100, value + ((seed * (j + 1)) % 7) - 3))
            data_points.append({"date": date, "value": round(value)})

        trends.append({
            "market": market,
            "market_name": VALID_MARKETS.get(market, market),
            "color": CHART_COLORS[i % len(CHART_COLORS)],
            "data": data_points,
        })

        # 模拟消费者关注
        mock_queries = {
            "portable blender": ["blender bottle", "personal blender", "USB blender", "rechargeable blender", "mini blender"],
            "yoga mat": ["non slip yoga mat", "thick yoga mat", "eco yoga mat", "travel yoga mat"],
            "air purifier": ["HEPA air purifier", "car air purifier", "quiet air purifier", "smart air purifier"],
        }
        base_queries = mock_queries.get(keyword.lower(), [
            f"{keyword} review", f"best {keyword}", f"{keyword} 2026",
            f"cheap {keyword}", f"{keyword} vs alternative"
        ])
        top_queries = [
            {"query": q, "value": 100 - j * 15}
            for j, q in enumerate(base_queries[:5])
        ]
        rising_queries = [
            {"query": f"{keyword} " + suffix, "value": f"+{200 + (seed + j) % 300}%"}
            for j, suffix in enumerate(["alternative", "for travel", "under $30", "2026", "pro"])
        ]
        consumer_focus.append({
            "market": market,
            "top": top_queries,
            "rising": rising_queries,
        })

    # 模拟区域热度
    all_regions = [
        ("US", "United States"), ("GB", "United Kingdom"), ("CA", "Canada"),
        ("AU", "Australia"), ("DE", "Germany"), ("JP", "Japan"),
        ("KR", "South Korea"), ("FR", "France"), ("IN", "India"),
        ("BR", "Brazil"), ("MX", "Mexico"), ("IT", "Italy"),
        ("ES", "Spain"), ("NL", "Netherlands"), ("SE", "Sweden"),
    ]
    region_heatmap = []
    for j, (code, name) in enumerate(all_regions):
        value = max(5, 95 - j * 6 + ((seed + j * 11) % 10) - 5)
        region_heatmap.append({
            "geo_code": code,
            "geo_name": name,
            "value": round(value),
        })
    region_heatmap.sort(key=lambda x: x["value"], reverse=True)

    return {
        "search_trends": trends,
        "consumer_focus": consumer_focus,
        "region_heatmap": region_heatmap,
        "source_status": "mock",
        "source_type": "fallback",
        "source_label": "Fallback estimate",
        "error": "使用模拟数据（Google Trends 服务不可用）",
    }


# ─────────────────────────────────────────────
# Reddit VOC 离线快照（rdt-cli 采集，用于 OAuth 不可用时展示）
# ─────────────────────────────────────────────
def generate_reddit_voc_snapshot(keyword: str, reddit_result: dict) -> Optional[dict]:
    """返回离线 Reddit Voice-of-Customer 快照。仅在 live Reddit 不可用时使用。"""
    if reddit_result.get("posts"):
        return None

    normalized = keyword.strip().lower()
    if "portable blender" not in normalized and "personal blender" not in normalized:
        return None

    return {
        "status": "offline_snapshot",
        "source": "rdt-cli Reddit search export",
        "collected_at": "2026-07-05",
        "matched_query": "portable blender",
        "sample_size": 20,
        "summary": (
            "Offline Reddit VOC snapshot shows buyers care less about novelty and more about "
            "whether a portable blender can reliably handle frozen fruit, ice, protein shakes, "
            "quiet morning use, easy cleaning, and commuting or dorm constraints."
        ),
        "themes": [
            {
                "title": "Frozen ingredients are the trust test",
                "signal": "Users repeatedly ask whether compact blenders can handle ice, frozen berries, bananas, dates, nuts, and fibrous ingredients without stalling.",
                "evidence": [
                    "Smoothies thread: buyer compares Nutribullet and Ninja Blast for ice and frozen berries.",
                    "IndianFood thread: users warn that dates, nuts, turmeric, amla, and coconut need torque, not just blade speed.",
                ],
                "actions": [
                    "Lead product messaging with frozen-fruit and ice performance tests.",
                    "Show short proof videos instead of only lifestyle photos.",
                ],
            },
            {
                "title": "Quiet convenience drives purchase occasions",
                "signal": "Several posts mention early mornings, roommates, work, commuting, dorms, and protein shakes as the reason to choose portable over full-size blenders.",
                "evidence": [
                    "Smoothies thread: user wants a blender for 6am workdays without waking roommates.",
                    "New Zealand thread: user wants a blender to drop in a work bag for afternoon protein and banana.",
                ],
                "actions": [
                    "Position around office, car, dorm, and early-morning routines.",
                    "Add noise and leak-proof claims where the product can support them.",
                ],
            },
            {
                "title": "Durability and cleaning are conversion blockers",
                "signal": "Users hesitate because reviews mention weak blades, products stopping after some time, messy blending, and uncertainty about whether portable is worth it.",
                "evidence": [
                    "IndianFood thread: Amazon reviews mention weak blades and units stopping after some time.",
                    "DeinfluencingPH threads: buyers ask whether a portable blender is worth buying or a waste of money.",
                ],
                "actions": [
                    "Surface warranty, motor protection, and replacement policy close to the CTA.",
                    "Use cleaning simplicity as a primary comparison point.",
                ],
            },
        ],
        "competitor_mentions": [
            {"name": "Ninja Blast", "signal": "Frequent comparison option; mixed confidence on frozen fruit."},
            {"name": "Nutribullet portable", "signal": "Common shortlist brand for work-bag protein shakes."},
            {"name": "BlendJet", "signal": "Relevant category reference for portable blending, even when not always named in top posts."},
        ],
        "sample_posts": [
            {
                "title": "Best portable blenders for protein shakes",
                "subreddit": "Smoothies",
                "url": "https://www.reddit.com/r/Smoothies/comments/1nvqwwb/best_portable_blenders_for_protein_shakes/",
                "signal": "Ice and frozen berries are decisive comparison criteria.",
            },
            {
                "title": "Why your portable blender keeps failing with Indian ingredients and what to look for instead",
                "subreddit": "IndianFood",
                "url": "https://www.reddit.com/r/IndianFood/comments/1tpyd68/why_your_portable_blender_keeps_failing_with/",
                "signal": "Ingredient toughness varies by cuisine and needs torque-led positioning.",
            },
            {
                "title": "Best portable blender",
                "subreddit": "Smoothies",
                "url": "https://www.reddit.com/r/Smoothies/comments/1uaestk/best_portable_blender/",
                "signal": "Quiet early-morning and work use cases motivate portable purchase.",
            },
        ],
    }


def generate_offline_reddit_posts(keyword: str) -> list:
    """返回 rdt-cli 采集的真实 Reddit 帖子导出。不是实时 API。"""
    normalized = keyword.strip().lower()
    if "portable blender" not in normalized and "personal blender" not in normalized:
        return []

    return [
        {
            "title": "Best portable blenders for protein shakes",
            "subreddit": "Smoothies",
            "url": "https://www.reddit.com/r/Smoothies/comments/1nvqwwb/best_portable_blenders_for_protein_shakes/",
            "score": 5,
            "comments": 12,
            "created": datetime.fromtimestamp(1759369311.0, tz=timezone.utc).isoformat(),
            "description": "Buyer compares Nutribullet and Ninja Blast for ice, frozen berries, bananas, and protein shakes.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
        {
            "title": "Why your portable blender keeps failing with Indian ingredients and what to look for instead",
            "subreddit": "IndianFood",
            "url": "https://www.reddit.com/r/IndianFood/comments/1tpyd68/why_your_portable_blender_keeps_failing_with/",
            "score": 67,
            "comments": 37,
            "created": datetime.fromtimestamp(1779960104.0, tz=timezone.utc).isoformat(),
            "description": "Users discuss torque needs for dates, nuts, turmeric, amla, frozen fruit, and coconut pieces.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
        {
            "title": "Best portable blender",
            "subreddit": "Smoothies",
            "url": "https://www.reddit.com/r/Smoothies/comments/1uaestk/best_portable_blender/",
            "score": 5,
            "comments": 7,
            "created": datetime.fromtimestamp(1781905772.0, tz=timezone.utc).isoformat(),
            "description": "Weight-loss and workday use case: quiet early-morning protein shakes without waking roommates.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
        {
            "title": "Recommendations for a small personal blender",
            "subreddit": "Smoothies",
            "url": "https://www.reddit.com/r/Smoothies/comments/1rliir4/recommendations_for_a_small_personal_blender/",
            "score": 5,
            "comments": 13,
            "created": datetime.fromtimestamp(1772719632.0, tz=timezone.utc).isoformat(),
            "description": "Buyer wants a compact blender for ice and frozen fruit, comparing Ninja and Nutribullet.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
        {
            "title": "Portable blender to make shakes?",
            "subreddit": "IndianFood",
            "url": "https://www.reddit.com/r/IndianFood/comments/1ruzd8x/portable_blender_to_make_shakes/",
            "score": 1,
            "comments": 8,
            "created": datetime.fromtimestamp(1773633879.0, tz=timezone.utc).isoformat(),
            "description": "Concern that Amazon-reviewed portable blenders stop working or have weak blades; BlendJet is considered.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
        {
            "title": "Best portable blender suggestions?",
            "subreddit": "IndianFood",
            "url": "https://www.reddit.com/r/IndianFood/comments/1mstesc/best_portable_blender_suggestions/",
            "score": 2,
            "comments": 10,
            "created": datetime.fromtimestamp(1755443416.0, tz=timezone.utc).isoformat(),
            "description": "Use case centers on whey protein, fruit, and easy cleaning.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
        {
            "title": "Mini blenders that actually make smoothie life easier in 2026",
            "subreddit": "Smoothies",
            "url": "https://www.reddit.com/r/Smoothies/comments/1rk04vc/mini_blenders_that_actually_make_smoothie_life/",
            "score": 9,
            "comments": 4,
            "created": datetime.fromtimestamp(1772568887.0, tz=timezone.utc).isoformat(),
            "description": "Post highlights frozen berries, mess reduction, fast mornings, and easy cleaning as success criteria.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
        {
            "title": "The portable nutribullet blenders... what's your opinion on those?",
            "subreddit": "newzealand",
            "url": "https://www.reddit.com/r/newzealand/comments/1uc6x6g/the_portable_nutribullet_blenders_whats_your/",
            "score": 2,
            "comments": 11,
            "created": datetime.fromtimestamp(1782091329.0, tz=timezone.utc).isoformat(),
            "description": "Work-bag use case for afternoon protein and banana; buyer wants views beyond negative reviews.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
        {
            "title": "DIM - Portable Blender",
            "subreddit": "deinfluencingPH",
            "url": "https://www.reddit.com/r/deinfluencingPH/comments/1tgi85b/dim_portable_blender/",
            "score": 3,
            "comments": 12,
            "created": datetime.fromtimestamp(1780410863.0, tz=timezone.utc).isoformat(),
            "description": "Buyer is deciding whether portable is worth it compared with a regular blender.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
        {
            "title": "Portable Blender",
            "subreddit": "tsa",
            "url": "https://www.reddit.com/r/tsa/comments/1rooxrk/portable_blender/",
            "score": 8,
            "comments": 23,
            "created": datetime.fromtimestamp(1773025651.0, tz=timezone.utc).isoformat(),
            "description": "Travel and college use case raises carry-on, removable blade, and lithium battery questions.",
            "type": "reddit",
            "collection_type": "offline_real_export",
            "collected_at": "2026-07-05",
        },
    ]


# ─────────────────────────────────────────────
# API 业务逻辑
# ─────────────────────────────────────────────
def build_radar_response(params: dict) -> tuple[int, dict]:
    """接收查询参数并返回 (HTTP 状态码, JSON 数据)。"""
    try:
        keyword = params.get("keyword", "")
        markets_str = params.get("markets", "US,JP,KR,AE,DE,TH,PH")
        timeframe = params.get("timeframe", "today 12-m")

        if not keyword:
            return 400, {"error": "keyword 参数是必需的"}

        # 解析市场列表
        market_list = [m.strip().upper() for m in markets_str.split(",") if m.strip()]
        market_list = [m for m in market_list if m in VALID_MARKETS]
        if not market_list:
            market_list = ["US", "JP", "KR", "AE", "DE", "TH", "PH"]

        logger.info(f"[Radar] keyword={keyword}, markets={market_list}")

        # 外部数据源彼此独立，并行执行可以显著降低 Vercel 免费函数超时风险。
        with ThreadPoolExecutor(max_workers=3) as executor:
            trends_future = executor.submit(fetch_google_trends, keyword, market_list, timeframe)
            news_future = executor.submit(fetch_news, keyword)
            reddit_future = executor.submit(fetch_reddit, keyword)

            trends_result = trends_future.result()
            news_result = news_future.result()
            reddit_result = reddit_future.result()

        # 生成策略建议
        strategy = generate_strategy(
            trends_result.get("search_trends", []),
            trends_result.get("consumer_focus", []),
            trends_result.get("region_heatmap", []),
            keyword,
        )

        reddit_posts = reddit_result.get("posts", [])
        reddit_source = {
            "status": reddit_result.get("source_status", "unknown"),
            "type": reddit_result.get("source_type", "unknown"),
            "label": reddit_result.get("source_label", ""),
            "error": reddit_result.get("error"),
        }
        offline_reddit_posts = []
        if not reddit_posts:
            offline_reddit_posts = generate_offline_reddit_posts(keyword)
            if offline_reddit_posts:
                reddit_posts = offline_reddit_posts
                reddit_source = {
                    "status": "offline",
                    "type": "offline_export",
                    "label": "Offline real Reddit export",
                    "error": reddit_result.get("error"),
                    "collected_at": "2026-07-05",
                    "sample_size": len(offline_reddit_posts),
                }

        # 组装返回数据
        response_data = {
            "keyword": keyword,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "markets": market_list,
            "timeframe": timeframe,
            "search_trends": trends_result.get("search_trends", []),
            "consumer_focus": trends_result.get("consumer_focus", []),
            "region_heatmap": trends_result.get("region_heatmap", []),
            "strategy": strategy,
            "news": news_result.get("articles", []),
            "reddit": reddit_posts,
            "reddit_voc": generate_reddit_voc_snapshot(keyword, reddit_result),
            "data_sources": {
                "google_trends": {
                    "status": trends_result.get("source_status", "unknown"),
                    "type": trends_result.get("source_type", "unknown"),
                    "label": trends_result.get("source_label", ""),
                    "error": trends_result.get("error"),
                },
                "newsapi": {
                    "status": news_result.get("source_status", "unknown"),
                    "type": news_result.get("source_type", "unknown"),
                    "label": news_result.get("source_label", ""),
                    "error": news_result.get("error"),
                },
                "reddit": reddit_source,
            },
        }

        return 200, response_data

    except Exception as e:
        logger.error(f"[Radar] 未捕获异常: {e}", exc_info=True)
        return 500, {"error": f"服务器内部错误: {str(e)}"}


# ─────────────────────────────────────────────
# Vercel Serverless Function 入口
# ─────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):
    """Vercel Python Runtime 会加载这个 BaseHTTPRequestHandler 子类。"""

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        params = {key: values[0] for key, values in query.items() if values}
        status_code, payload = build_radar_response(params)
        self._send_json(status_code, payload)

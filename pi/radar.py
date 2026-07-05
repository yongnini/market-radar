"""
跨区域品类市场雷达 - Vercel Serverless Function
将原 FastAPI 后端改写为 Vercel Python Runtime 兼容格式
"""

import json
import hashlib
import math
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
NEWS_API_KEY = "15a04a2e71b54c1688132eb969a9984e"
DATA_TIMEOUT = 25  # 每个数据源超时秒数（Vercel 函数有 10s/60s 限制）

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
        "error": None,
    }

    try:
        from pytrends.request import TrendReq
        logger.info(f"[Google Trends] 尝试通过 pytrends 获取 '{keyword}' 数据...")

        pytrends = TrendReq(hl='en-US', tz=360, timeout=DATA_TIMEOUT)

        # 模块1: 各市场的搜索趋势
        trends_data = []
        for i, market in enumerate(markets):
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
    result = {"articles": [], "source_status": "pending", "error": None}

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
        else:
            result["source_status"] = "error"
            result["error"] = data.get("message", "NewsAPI 返回错误")

    except requests.Timeout:
        result["source_status"] = "timeout"
        result["error"] = "NewsAPI 请求超时"
    except Exception as e:
        result["source_status"] = "error"
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# 数据源 3: Reddit（公开 JSON API，无需认证）
# ─────────────────────────────────────────────
def fetch_reddit(keyword: str, limit: int = 10):
    """获取 Reddit 讨论"""
    result = {"posts": [], "source_status": "pending", "error": None}

    try:
        resp = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": keyword, "limit": limit, "sort": "relevance", "t": "year"},
            headers={"User-Agent": "MarketRadar/1.0"},
            timeout=DATA_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
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
            result["source_status"] = "success"
        else:
            result["source_status"] = "error"
            result["error"] = f"Reddit 返回 {resp.status_code}"
    except Exception as e:
        result["source_status"] = "error"
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
        "error": "使用模拟数据（Google Trends 服务不可用）",
    }


# ─────────────────────────────────────────────
# Vercel Serverless Function 入口
# ─────────────────────────────────────────────
def handler(request):
    """
    Vercel Python Runtime 入口函数
    接收 HTTP 请求，返回市场雷达分析数据
    """
    try:
        # 解析查询参数
        params = request.args if hasattr(request, 'args') else {}
        keyword = params.get("keyword", "")
        markets_str = params.get("markets", "US,JP,KR,AE,DE,TH,PH")
        timeframe = params.get("timeframe", "today 12-m")

        if not keyword:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "keyword 参数是必需的"}),
            }

        # 解析市场列表
        market_list = [m.strip().upper() for m in markets_str.split(",") if m.strip()]
        market_list = [m for m in market_list if m in VALID_MARKETS]
        if not market_list:
            market_list = ["US", "JP", "KR", "AE", "DE", "TH", "PH"]

        logger.info(f"[Radar] keyword={keyword}, markets={market_list}")

        # 串行调用各数据源（serverless 环境不适合 asyncio 并发）
        trends_result = fetch_google_trends(keyword, market_list, timeframe)
        news_result = fetch_news(keyword)
        reddit_result = fetch_reddit(keyword)

        # 生成策略建议
        strategy = generate_strategy(
            trends_result.get("search_trends", []),
            trends_result.get("consumer_focus", []),
            trends_result.get("region_heatmap", []),
            keyword,
        )

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
            "reddit": reddit_result.get("posts", []),
            "data_sources": {
                "google_trends": {
                    "status": trends_result.get("source_status", "unknown"),
                    "error": trends_result.get("error"),
                },
                "newsapi": {
                    "status": news_result.get("source_status", "unknown"),
                    "error": news_result.get("error"),
                },
                "reddit": {
                    "status": reddit_result.get("source_status", "unknown"),
                    "error": reddit_result.get("error"),
                },
            },
        }

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Cache-Control": "public, max-age=300",
            },
            "body": json.dumps(response_data, ensure_ascii=False, default=str),
        }

    except Exception as e:
        logger.error(f"[Radar] 未捕获异常: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": f"服务器内部错误: {str(e)}"}, ensure_ascii=False),
        }

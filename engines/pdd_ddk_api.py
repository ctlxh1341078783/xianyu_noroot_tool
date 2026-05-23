"""
多多客（多多进宝）API 客户端
基于拼多多开放平台官方接口，替代 uiautomator2 手机操控

核心接口:
  - pdd.ddk.goods.search    关键词搜索商品
  - pdd.ddk.goods.detail    商品详情查询
"""

import hashlib
import time
import requests
from typing import List, Dict, Optional

API_URL = "https://gw-api.pinduoduo.com/api/router"

# 默认配置（全局 settings 未注入时使用）
_config = {
    "client_id": "1d579cfa102f49739df6dd0fb5509344",
    "client_secret": "b26130ca96b05ae68d1c90823d0b4f5b660e5ce9",
    "pid": "44415548_315962171",
    "daily_limit": 2000,
}


def init_config(settings: dict):
    """从 settings.json 的 ddk 段注入配置"""
    ddk = settings.get("ddk", {})
    if ddk.get("client_id"):
        _config["client_id"] = ddk["client_id"]
    if ddk.get("client_secret"):
        _config["client_secret"] = ddk["client_secret"]
    if ddk.get("pid"):
        _config["pid"] = ddk["pid"]
    if ddk.get("daily_limit"):
        _config["daily_limit"] = ddk["daily_limit"]


def _client_id() -> str:
    return _config["client_id"]


def _client_secret() -> str:
    return _config["client_secret"]


def _pid() -> str:
    return _config["pid"]


def _daily_limit() -> int:
    return _config["daily_limit"]


# 调用计数器
_call_count = 0


def _sign(params: Dict[str, str]) -> str:
    """
    拼多多 API 签名算法
    1. 参数按 key 字母序升序排列
    2. 拼接 key + value（无分隔符）
    3. 首尾加上 client_secret
    4. MD5 后转大写
    """
    sorted_items = sorted(params.items())
    param_str = "".join(f"{k}{v}" for k, v in sorted_items)
    sign_str = f"{_client_secret()}{param_str}{_client_secret()}"
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


def _request(api_type: str, biz_params: Dict[str, str]) -> dict:
    """发送 API 请求，自动附加 _pid() 和 custom_parameters"""
    global _call_count

    if _call_count >= _daily_limit():
        return {"error": "日调用额度已用完", "_skipped": True}

    params = {
        "type": api_type,
        "client_id": _client_id(),
        "timestamp": str(int(time.time())),
        "data_type": "JSON",
        "pid": _pid(),
        "custom_parameters": '{"new":1}',
    }
    params.update(biz_params)
    params["sign"] = _sign(params)

    try:
        resp = requests.post(API_URL, data=params, timeout=15)
        _call_count += 1
        return resp.json()
    except Exception as e:
        _call_count += 1
        return {"error": str(e)}


def search_goods(
    keyword: str,
    page: int = 1,
    page_size: int = 20,
    sort_type: int = 0,
    with_coupon: bool = False,
    cat_id: Optional[int] = None,
    range_from: Optional[int] = None,
    range_to: Optional[int] = None,
) -> List[Dict]:
    """
    多多进宝商品搜索

    Args:
        keyword:     搜索关键词
        page:        页码（从 1 开始）
        page_size:   每页数量（最大 50）
        sort_type:   排序: 0=综合 1=销量 2=价格升 3=价格降
        with_coupon: 是否只要优惠券商品
        cat_id:      商品类目 ID
        range_from:  价格区间下限（分）
        range_to:    价格区间上限（分）

    Returns:
        商品列表，每个商品已做单位转换（分→元，千分比→百分比）
    """
    page_size = max(page_size, 10)  # API 要求 10-100
    biz_params: Dict[str, str] = {
        "keyword": keyword,
        "page": str(page),
        "page_size": str(page_size),
        "sort_type": str(sort_type),
        "with_coupon": str(with_coupon).lower(),
    }
    if cat_id is not None:
        biz_params["cat_id"] = str(cat_id)
    if range_from is not None and range_to is not None:
        biz_params["range_list"] = f'[{{"range_from":{range_from},"range_to":{range_to}}}]'

    result = _request("pdd.ddk.goods.search", biz_params)

    if "error" in result:
        return []

    goods_list = (
        result.get("goods_search_response", {})
        .get("goods_list", [])
    )

    return [_normalize_goods(g) for g in goods_list]


def search_goods_all_pages(
    keyword: str,
    max_pages: int = 5,
    page_size: int = 50,
    sort_type: int = 1,
    **kwargs,
) -> List[Dict]:
    """翻页搜索，一次性拉取多页结果"""
    all_goods: List[Dict] = []
    seen_ids: set = set()

    for page in range(1, max_pages + 1):
        goods = search_goods(
            keyword, page=page, page_size=page_size,
            sort_type=sort_type, **kwargs
        )
        if not goods:
            break
        new_count = 0
        for g in goods:
            gid = g.get("goods_id")
            if gid and gid not in seen_ids:
                seen_ids.add(gid)
                all_goods.append(g)
                new_count += 1
        if new_count == 0:
            break

    return all_goods


def get_goods_detail(goods_id_list: List[int]) -> List[Dict]:
    """批量查询商品详情"""
    biz_params = {
        "goods_id_list": str(goods_id_list),
    }
    result = _request("pdd.ddk.goods.detail", biz_params)

    if "error" in result:
        return []

    goods_list = (
        result.get("goods_detail_response", {})
        .get("goods_details", [])
    )
    return [_normalize_goods(g) for g in goods_list]


def _normalize_goods(g: dict) -> dict:
    """单位转换：分→元，千分比→百分比"""
    g["min_group_price_yuan"] = round(g.get("min_group_price", 0) / 100, 2)
    g["min_normal_price_yuan"] = round(g.get("min_normal_price", 0) / 100, 2)
    g["coupon_discount_yuan"] = round(g.get("coupon_discount", 0) / 100, 2)
    g["promotion_rate_pct"] = round(g.get("promotion_rate", 0) / 10, 1)

    # 拼单价减去优惠券 = 实际到手价
    g["actual_price_yuan"] = round(
        g["min_group_price_yuan"] - g["coupon_discount_yuan"], 2
    )
    return g


def search_to_xianyu_format(
    keyword: str,
    max_items: int = 20,
    scroll_pages: int = 3,
    sort_type: int = 1,
) -> List[Dict]:
    """
    搜索并转换为闲鱼货源匹配引擎兼容的格式，
    完全替代 PinduoduoMobileController.search_and_collect()

    Args:
        keyword:      搜索关键词
        max_items:    最大商品数
        scroll_pages: 翻页数
        sort_type:    排序（默认按销量）

    Returns:
        List[Dict] 兼容原有 extract_products_from_xml 的输出格式
    """
    goods = search_goods_all_pages(
        keyword,
        max_pages=scroll_pages,
        page_size=min(max_items, 50),
        sort_type=sort_type,
    )

    products = []
    for g in goods:
        commission = round(g["actual_price_yuan"] * g["promotion_rate_pct"] / 100, 2)
        products.append({
            "title": g.get("goods_name", ""),
            "price": g["actual_price_yuan"],
            "price_original": g["min_group_price_yuan"],
            "sales": g.get("sales_tip", ""),
            "sales_count": _parse_sales(g.get("sales_tip", "0")),
            "goods_id": g.get("goods_id", ""),
            "mall_name": g.get("mall_name", ""),
            "pic_url": g.get("goods_thumbnail_url", ""),
            "image_url": g.get("goods_image_url", ""),
            "has_coupon": g.get("has_coupon", False),
            "coupon_discount": g["coupon_discount_yuan"],
            "coupon_min_order": round(g.get("coupon_min_order_amount", 0) / 100, 2),
            "promotion_rate": g["promotion_rate_pct"],
            "commission": commission,
            # 综合价值分: 佣金占比高 + 销量多 → 更值得进货
            "value_score": round(
                commission * 10 + _parse_sales(g.get("sales_tip", "0")) / 1000, 1
            ),
            "goods_sign": g.get("goods_sign", ""),
            "category_name": g.get("category_name", ""),
            "goods_url": f"https://mobile.yangkeduo.com/goods.html?goods_id={g.get('goods_id', '')}",
            "source": "ddk_api",
        })

    # 按综合价值分降序，取最优的 max_items 件
    products.sort(key=lambda x: x["value_score"], reverse=True)
    return products[:max_items]


def _parse_sales(tip: str) -> int:
    """解析销量文案 → 数字，如 '10万+' → 100000, '已拼1000件' → 1000"""
    import re
    tip = tip.replace("已拼", "").replace("件", "").strip()

    if "万" in tip:
        num = re.findall(r"[\d.]+", tip)
        if num:
            return int(float(num[0]) * 10000)

    num = re.findall(r"\d+", tip)
    if num:
        return int(num[0])
    return 0


def verify_api():
    """验证 API 连通性：用一个简单查询测试"""
    result = _request("pdd.ddk.goods.search", {
        "keyword": "手机壳",
        "page": "1",
        "page_size": "10",
    })
    return result


def get_call_count() -> int:
    """获取当前已调用次数"""
    return _call_count


def reset_call_counter():
    """重置调用计数器（每日零点调用）"""
    global _call_count
    _call_count = 0


class DdkSupplyFinder:
    """
    多多进宝 API 货源查找器
    接口与 PinduoduoMobileController.search_and_collect() 兼容，
    可直接替换或作为双通道的主通道使用。
    """

    def __init__(self):
        self.log = print

    def set_logger(self, log_fn):
        self.log = log_fn

    def search_and_collect(
        self, keyword: str, scroll_pages: int = 5, max_items: int = 50
    ) -> list:
        """
        通过多多进宝 API 搜索商品（默认5页×50件，速度快无需省页数）

        Args:
            keyword:      搜索关键词
            scroll_pages: 搜索页数（DDK API快，默认5页覆盖更多候选）
            max_items:    最大返回商品数（默认50，取利润+佣金最优）
        """
        self.log(f"  🔍 [DDK API] 搜索: 【{keyword}】")

        if _call_count >= _daily_limit():
            self.log(f"  ⚠️ [DDK API] 日调用额度已用完 ({_call_count}/{_daily_limit()})")
            return []

        try:
            products = search_to_xianyu_format(
                keyword,
                max_items=max_items,
                scroll_pages=scroll_pages,
                sort_type=1,  # 按销量排序
            )
            self.log(f"  ✅ [DDK API] 找到 {len(products)} 件商品 (累计调用 {_call_count}/{_daily_limit()})")
            return products
        except Exception as e:
            self.log(f"  ❌ [DDK API] 搜索失败: {e}")
            return []


if __name__ == "__main__":
    import json

    print("=" * 60)
    print("多多客 API 连通性测试")
    print("=" * 60)

    resp = verify_api()
    if "error" in resp:
        print(f"❌ API 调用失败: {resp['error']}")
    elif "error_response" in resp:
        err = resp["error_response"]
        print(f"❌ 业务错误: {err.get('error_msg', err)}")
    else:
        goods_list = (
            resp.get("goods_search_response", {})
            .get("goods_list", [])
        )
        if goods_list:
            g = goods_list[0]
            print(f"✅ API 连通成功！")
            print(f"   示例商品: {g.get('goods_name', 'N/A')}")
            print(f"   价格: {g.get('min_group_price', 0) / 100}元")
            print(f"   销量: {g.get('sales_tip', 'N/A')}")
            print(f"   佣金: {g.get('promotion_rate', 0) / 10}%")
        else:
            print("⚠️  API 连通但未返回商品")
            print(json.dumps(resp, ensure_ascii=False, indent=2)[:500])

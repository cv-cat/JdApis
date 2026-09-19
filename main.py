# coding: utf-8
"""JdApis 快速体验。

直接修改下面的关键词或 SKU，然后运行 ``python main.py``。
登录请运行 ``python login_demo.py``。
"""

import json

from loguru import logger

from jd_apis.jd_api import JdAPI
from utils.common_util import init


KEYWORD = "机械键盘"
SKU_ID = "100087543376"


def pretty(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def search_demo(auth):
    total, wares = JdAPI.search_wares(auth, KEYWORD, page=1)
    if not wares:
        logger.warning("商品搜索没有返回数据，可能命中频控或 605 风控")
        hotwords = JdAPI.get_search_hotwords(auth)
        names = []

        def collect(value):
            if isinstance(value, dict):
                if value.get("n") and len(names) < 12:
                    names.append(str(value["n"]))
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(hotwords.get("data", []))
        logger.info("当前搜索热词：" + "、".join(names))
        return
    logger.success(f"“{KEYWORD}”共 {total} 条，当前返回 {len(wares)} 条")
    pretty(wares)


if __name__ == "__main__":
    auth, _ = init()
    alive, message = JdAPI.check_session(auth)
    if not alive:
        raise SystemExit(f"{message}；请先运行 python login_demo.py")

    logger.success(f"登录态有效：{message}")
    search_demo(auth)

    # 更多直接调用示例，按需取消注释：
    # pretty(JdAPI.get_product_detail(auth, SKU_ID))
    # pretty(JdAPI.get_product_comments(auth, SKU_ID))
    # pretty(JdAPI.get_related_search(auth, SKU_ID))
    # pretty(JdAPI.get_cart_num(auth))
    # pretty(JdAPI.get_browse_history(auth))
    # pretty(JdAPI.get_follow_products(auth))
    # pretty(JdAPI.get_order_list(auth))

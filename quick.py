# coding: utf-8
"""JdApis 一分钟体验：扫码登录后搜索一个商品。"""

import os
from pathlib import Path

from loguru import logger

from builder.auth import JdAuth
from jd_apis.jd_api import JdAPI
from jd_apis.jd_login_api import JdLoginAPI
from utils.common_util import use_utf8_stdout
from utils.jd_cookie import bootstrap


DEFAULT_KEYWORD = "电脑"
QR_PATH = Path(__file__).with_name("qrcode.png")


def show_qrcode(_png: bytes, path: str):
    """保存后的二维码交给系统图片查看器，扫码过程仍是纯 HTTP。"""
    qr_path = Path(path).resolve()
    logger.info(f"二维码已保存到：{qr_path}")
    if os.name == "nt":
        try:
            os.startfile(str(qr_path))
            logger.info("二维码已用系统图片查看器打开，请用京东 App 扫码并确认")
        except OSError:
            logger.info("请手动打开上面的 qrcode.png，再用京东 App 扫码并确认")


def print_wares(wares):
    for index, item in enumerate(wares[:10], 1):
        sku = item.get("skuId") or "-"
        name = item.get("name") or "未命名商品"
        price = item.get("price") or "-"
        shop = item.get("shop") or "-"
        print(f"{index:>2}. {name}\n    SKU: {sku}  价格: ¥{price}  店铺: {shop}")


def main():
    use_utf8_stdout()
    keyword = input(f"搜索关键词 [{DEFAULT_KEYWORD}]: ").strip() or DEFAULT_KEYWORD

    auth = bootstrap(JdAuth.load(), site="passport")
    alive, account = JdAPI.check_session(auth)
    if not alive:
        logger.info("当前没有可用登录态，准备获取京东扫码二维码")
        success, message, auth = JdLoginAPI.qr_login(
            auth,
            qr_path=str(QR_PATH),
            on_qrcode=show_qrcode,
        )
        if not success:
            raise SystemExit(f"扫码登录失败：{message}")
        auth.save()
        alive, account = JdAPI.check_session(auth)
        if not alive:
            raise SystemExit(f"扫码成功但登录态校验失败：{account}")

    logger.success(f"登录态有效：{account or auth.pin}")
    logger.info(f"正在搜索：{keyword}")
    total, wares = JdAPI.search_wares(auth, keyword, page=1)
    if not wares:
        raise SystemExit("搜索没有返回商品，请稍后重试或检查登录态")
    logger.success(f"找到约 {total} 件商品，当前展示前 {min(len(wares), 10)} 条")
    print_wares(wares)


if __name__ == "__main__":
    main()

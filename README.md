<div align="center">
    <a href="https://www.python.org/">
        <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
    </a>
    <a href="https://nodejs.org/zh-cn/">
        <img src="https://img.shields.io/badge/nodejs-20%2B-green" alt="NodeJS 20+">
    </a>
</div>

# 🛒 JD Platform

**✨ 京东数据采集解决方案，支持商品详情、订单列表与视频资源抓取**

当你需要让 AI Agent 感知京东商品生态——自动采集商品信息、订单数据、视频资源——第一道墙往往不是模型能力，而是**平台签名鉴权能力的缺失**。

本项目做的事很简单：把这道墙拆掉。

**⚠️ 严禁用于爬取用户隐私、违规商业用途！本项目仅供学习与技术研究使用，后果自负。**

## 🌟 功能特性

- ✅ **h5st 签名自动计算**
  - 内嵌 JS 运行时（`execjs`），自动生成京东 `h5st` 鉴权参数
  - 适配京东最新 `h5st 4.2` 接口鉴权协议
- 🛍️ **商品详情采集**
  - 支持通过 SKU ID 获取商品详情（`mview_switch`）
  - 自动生成 `generateParams` 请求体与签名
- 📦 **订单列表采集**
  - 支持获取京东个人订单列表（`order_list_m`）
  - 基于 `curl_cffi` 模拟真实浏览器指纹请求
- 🎬 **视频资源爬取**
  - 支持按 VID 批量爬取京东商品视频信息（腾讯视频接口）
  - 自动筛选 1920×1080 / 1080×1920 规格视频播放地址

## 🛠️ 快速开始

### ⛳ 运行环境

- Python 3.10+
- Node.js 20+

### 🎯 本地安装

```bash
pip install -r requirements.txt
npm install
```

### 🎨 Cookie 配置

在浏览器中打开 [www.jd.com](https://www.jd.com)，**登录账号**后按 `F12` 打开开发者工具，点击「网络」→ 找任意一个 API 请求 → 复制请求头中的 `Cookie` 字段值。

> ⚠️ 注意：必须登录后获取的 Cookie 才有效，`pt_key` / `pt_pin` 字段用于身份认证，缺失将导致请求失败。

将获取到的 Cookie 字符串填入对应脚本的 `cookies` 字典中。

## 📁 项目结构

```
JD/
├── main.py          # 商品详情请求（mview_switch）
├── order.py         # 订单列表请求（order_list_m）
├── order_test.py    # 订单列表测试（完整 URL 直接请求）
├── test.py          # 京东商品视频批量爬取
├── static/
│   └── JD.js        # h5st 签名算法 JS 实现
└── utils/
    └── JDUtils.py   # Python 封装，调用 JD.js 生成签名
```

## 📡 核心模块说明

### `utils/JDUtils.py`

通过 `execjs` 调用 `static/JD.js` 提供两个核心函数：

| 函数 | 说明 |
|------|------|
| `generate_h5st(body)` | 根据请求体生成 `h5st` 签名参数 |
| `generateParams(sku)` | 根据 SKU ID 生成商品详情请求体 |

### `main.py` — 商品详情

请求 `https://api.m.jd.com/mview/switch`，获取指定 SKU 的商品详情数据。

**关键参数**

| 参数 | 说明 |
|------|------|
| `sku` | 商品 SKU ID（如 `100087543376`） |
| `h5st` | 由 `generate_h5st(body)` 自动计算 |

### `order.py` — 订单列表

请求 `https://api.m.jd.com/client.action`，获取当前账号的订单列表（`functionId=order_list_m`）。

**关键参数**

| 参数 | 说明 |
|------|------|
| `page` | 订单列表页码，从 `1` 开始 |
| `pageSize` | 每页条数，默认 `10` |
| `h5st` | 由 `generate_h5st(body)` 自动计算 |

### `test.py` — 视频爬取

批量遍历京东视频 VID，调用 `https://api.m.jd.com/tencent/video_v3` 接口，筛选出符合分辨率要求的视频播放地址，结果写入 `all_play_urls.txt`。

```python
# 示例：从 VID 2107007690 往前批量爬取
start = 2107007690
end = 0
for vid in range(start, end, -1):
    res_json = get_one_video_info(vid, cookie_str)
```

## 🍥 日志

| 日期       | 说明                                      |
|----------|-------------------------------------------|
| 26/04/10 | 项目初始化，完成 h5st 签名、商品详情、订单列表、视频爬取模块 |

## 🤝 欢迎贡献 PR

本项目欢迎任何形式的贡献！如果你有新功能想法、Bug 修复或文档改进，欢迎提交 PR。

- Fork 本仓库并在新分支上开发
- 保持代码风格与现有代码一致
- PR 描述中请简要说明改动内容和目的
- 也欢迎通过 Issue 提出建议或报告问题

## 🧸 额外说明
1. 感谢 star⭐ 和 follow📰！不时更新
2. 作者的联系方式在主页里，有问题可以随时联系我
3. 可以关注下作者的其他项目，欢迎 PR 和 issue
4. 感谢赞助！如果此项目对您有帮助，请作者喝一杯奶茶~~ （开心一整天😊😊）
5. thank you~~~

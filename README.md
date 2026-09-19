<div align="center">
  <p>
    <a href="https://github.com/cv-cat/JdApis">
      <img width="360" src="./author/logo.png" alt="JdApis logo">
    </a>
  </p>
  <p>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python 3.10+"></a>
    <a href="https://nodejs.org/"><img src="https://img.shields.io/badge/Node.js-20%2B-green" alt="Node.js 20+"></a>
    <a href="https://github.com/cv-cat/JdApis"><img src="https://img.shields.io/badge/browser-not_required-brightgreen" alt="No browser required"></a>
  </p>

  # 🛒 JdApis

  **京东 PC 登录、商品搜索与咚咚接口的纯程序实现**
</div>

JdApis 把京东网页端的登录、设备参数、请求签名和商品接口封装成可以直接调用的 Python API。

扫码登录、手机号登录、短信验证、人机验证、h5st 签名和搜索请求都在程序内完成，不依赖 Selenium、Playwright 或常驻浏览器。

> 本项目仅用于学习、研究和合法的个人自动化。请遵守京东平台规则，控制请求频率，不要提交或传播 Cookie、手机号、验证码等隐私数据。

## ✨ 功能特性

- 🔐 **纯程序登录**
  - 京东 App 扫码登录，自动轮询并获取 `thor/pin`
  - 手机号登录，自动处理 JCAP、人机验证和额外安全短信
  - 登录态保存到系统用户目录，不写入 Git 工作区
- 🧩 **网页参数本地复现**
  - h5st 5.3 / tk03 签名
  - PC 设备票据、WebM 指纹和 JCAP 本地运行时
  - `curl_cffi` Chrome TLS/HTTP2 传输指纹
- 🛍️ **商品接口**
  - 商品搜索、详情、评论、相关搜索
  - 购物车、浏览历史、关注商品和订单列表
  - 搜索热词、联想词和会员权益
- 💬 **咚咚 WebSocket**
  - 会话初始化、心跳、消息接收和文本发送
- 🧱 **会话持久化**
  - 响应链上的 `Set-Cookie` 自动合并并原子落盘
  - 不按 Cookie 名称做白名单过滤，适配服务端轮换票据

## 🚀 快速开始

### 运行环境

- Python 3.10+
- Node.js 20+
- Windows、Linux 或 macOS

### 安装依赖

```bash
pip install -r requirements.txt
npm install
```

### 一键扫码搜索

直接运行 Quick Demo：

```bash
python quick.py
```

程序会：

1. 检查本地登录态；没有有效会话时生成 `qrcode.png`，并用系统图片查看器打开。
2. 使用京东 App 扫码并确认登录。
3. 提示输入搜索关键词，直接输出前 10 个商品。

默认搜索关键词是 `电脑`，回车即可使用。登录态有效时会复用现有会话，不会重复扫码；想重新扫码时删除系统 auth 文件或设置新的 `JDAPIS_AUTH_FILE`。

### 分步使用

扫码或手机号登录：

```bash
python login_demo.py
```

编辑 `login_demo.py` 顶部的 `LOGIN_MODE`：

```python
LOGIN_MODE = "qr"   # 京东 App 扫码
# LOGIN_MODE = "sms"  # 手机号 + 短信
```

登录后运行商品示例：

```bash
python main.py
```

关键词和示例 SKU 都在文件顶部，保持直接修改变量的风格，不使用命令行参数解析。

## 🧑‍💻 代码调用

```python
from jd_apis.jd_api import JdAPI
from utils.common_util import init

auth, _ = init()

alive, account = JdAPI.check_session(auth)
if not alive:
    raise RuntimeError("请先运行 python login_demo.py")

total, wares = JdAPI.search_wares(auth, "机械键盘", page=1)
print(total, wares[:3])
```

常用方法：

| 模块 | 方法 | 用途 |
| --- | --- | --- |
| `JdLoginAPI` | `qr_login(auth)` | 纯程序扫码登录 |
| `JdSmsLoginAPI` | `login(auth, mobile, code_provider)` | 手机号与短信登录 |
| `JdAPI` | `search_wares(auth, keyword)` | 搜索并提取商品列表 |
| `JdAPI` | `get_product_detail(auth, sku)` | 商品详情 |
| `JdAPI` | `get_product_comments(auth, sku)` | 商品评论 |
| `JdAPI` | `get_cart_num(auth)` | 购物车数量 |
| `JdAPI` | `get_order_list(auth)` | 订单列表 |
| `JdChatWS` | `start()` / `send_text()` | 咚咚消息收发 |

## 🔒 登录态与隐私

默认登录态位置：

```text
Windows: %LOCALAPPDATA%\JdApis\auth.json
Linux:   ~/.local/state/JdApis/auth.json
macOS:   ~/Library/Application Support/JdApis/auth.json
```

可以通过环境变量覆盖：

```text
JDAPIS_AUTH_FILE=D:\private\JdApis\auth.json
```

不要把这个文件放进仓库。`.gitignore` 已忽略 Cookie、`.env`、二维码、抓包文件、运行日志和本地指纹种子。

如果已有 Cookie，也可以复制 `.env.example` 为 `.env` 并填写 `JD_COOKIES`；更推荐运行 `quick.py` 或 `login_demo.py` 让程序自己建立会话。

## 📁 项目结构

```text
JdApis/
├── quick.py                # 扫码登录 + 搜索的最短体验入口
├── login_demo.py           # 扫码 / 手机号登录
├── main.py                 # 商品 API 体验入口
├── chat_demo.py            # 咚咚 WebSocket 示例
├── builder/                # Cookie、Header、参数装配
├── jd_apis/                # 登录、商品与聊天 API
├── utils/                  # h5st、设备参数、HTTP 与会话工具
├── static/                 # 官方 JS/WASM 运行时与 JCAP 模型
├── author/logo.png         # 项目标识
├── requirements.txt
└── package.json
```

`static/jcap/run/models/` 下的 ONNX 文件是本地验证码识别运行时的一部分，不能随意删除。二维码、Cookie、日志和分析报告不属于运行时文件，均不会进入公开提交。

## 🗝️ 使用提示

- 登录成功不代表票据永久有效；每次业务请求前可以先调用 `JdAPI.check_session(auth)`。
- 京东会根据账号、设备和请求频率触发风控。遇到 403/605 时先降低频率，再确认登录态。
- JCAP 首次加载模型需要一些时间，程序会把验证状态放在脱敏的 `_verification` 字段中。
- h5st、设备画像和 WebM 使用同一套版本画像；网页更新后如果接口整体变化，请同步更新本地运行时。
- 不要提交 `auth.json`、Cookie、手机号、短信验证码、抓包文件或任何个人数据。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request：

- 描述网页版本、接口名称和可复现的响应现象
- 不要上传登录态、验证码、抓包票据或本地路径
- 保持 `builder` / `jd_apis` / `utils` 的分层结构

如果这个项目对你有帮助，欢迎点一个 Star ⭐

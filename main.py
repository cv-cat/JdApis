import json
import requests
from utils.JDUtils import generateParams, generate_h5st

headers = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "referer": "https://item.m.jd.com/",
    "sec-ch-ua": "\"Microsoft Edge\";v=\"129\", \"Not=A?Brand\";v=\"8\", \"Chromium\";v=\"129\"",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "\"Windows\"",
    "sec-fetch-dest": "script",
    "sec-fetch-mode": "no-cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0"
}
# 登录后从浏览器拷 cookie，不要把真实值提交上来
cookies = {
    "pt_key": "",
    "pt_pin": "",
    "3AB9D23F7A4B3C9B": "",
    "3AB9D23F7A4B3CSS": "",
}
url = "https://api.m.jd.com/mview/switch"
sku = '100087543376'
body = generateParams(sku)
body = json.dumps(body, separators=(',', ':'))
params = {
    "loginType": "2",
    "appid": "m_core",
    "uuid": "6515493382471277899",
    "functionId": "mview_switch",
    "scval": sku,
    "x-api-eid-token": "",
    "jsonp": "skuInfoCB",
    "h5st": generate_h5st(body),
    "body": body, # '\{' + body[1:-1] + '\}'
    "appCode": "ms0ca95114"
}
while True:
    response = requests.get(url, headers=headers, cookies=cookies, params=params)
    res_text = response.text
    print(res_text)


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
cookies = {
    "__jda": "181111935.1727437789361709524098.1727437789.1727437789.1727437789.1",
    "__jdc": "181111935",
    "mba_muid": "1727437789361709524098",
    "wxa_level": "1",
    "retina": "0",
    "cid": "9",
    "jxsid": "17274377894891576198",
    "appCode": "ms0ca95114",
    "webp": "1",
    "visitkey": "6515493382471277899",
    "3AB9D23F7A4B3C9B": "AEL6S4YLDBR3R3DHKTTBSU62NQHPXCZ53TWFCWZRYRNI6YIXPIK2KJIUK4WPH23GPZEKHEIHMO6LT47DED6LS3WXKY",
    "sc_width": "2560",
    "shshshfpa": "6c38e0a5-4af1-b624-74c8-669b4a2a37ea-1727437791",
    "shshshfpx": "6c38e0a5-4af1-b624-74c8-669b4a2a37ea-1727437791",
    "jcap_dvzw_fp": "aUz3s6qkmh_4n1xxVprFtQga4a1gtptvo8QR1w5hqeaO9OrHnNHeqzxLPUrum06_6cHBemhgTrSZvYG4Kvn9lA==",
    "TrackerID": "Fd3swbbL9FLWSOfUN6Yu96aR4yT0_JVnajJDHz_ioCC45nX3oK8PY0-iEDyNzRrPzIOMvDJaX7fv-JE74xzzMKO2hI2w7tHKAMvapgFXsyn4DjcwpUT12fNFXZ-dfZxk",
    "pt_key": "AAJm9p1sADABec8-xXHQnI-q0mp9dQ9bmq4s8DNy4-jDWB-8rwKbh7S3h1_CP1GQn32IfGNx55Y",
    "pt_pin": "jd_6b2773bb64ff9",
    "pt_token": "yobf5kyc",
    "pwdt_id": "jd_6b2773bb64ff9",
    "sfstoken": "tk01md1e41c93a8sMiszYWFJREtZuGW68wZhhlwB+TA9WpeosUtGS9TYZ2zeABWbt0czYwqhb1hP7Sr1K0mL+lMJD3io",
    "whwswswws": "",
    "warehistory": "\"100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,100087543382,\"",
    "__jdb": "181111935.13.1727437789361709524098|1.1727437789",
    "wqmnx1": "MDEyNjM1MGg6ZWptZDE3OG09bVhwOTNnPWxqRnhua19odW89b3BfdXBlX2F0MTRfPVUzNDI3TWwwbiAwVzspbEszIE1pZSBtOS5mNTYvMDFGZmFhQjRRRVMpRilI",
    "mba_sid": "17274316817618428858267312171.44",
    "3AB9D23F7A4B3CSS": "jdd03AEL6S4YLDBR3R3DHKTTBSU62NQHPXCZ53TWFCWZRYRNI6YIXPIK2KJIUK4WPH23GPZEKHEIHMO6LT47DED6LS3WXKYAAAAMSGNLPNGYAAAAADGLY63DKRGXMGIX",
    "_gia_d": "1",
    "__wga": "1727438191255.1727433823900.1727431683078.1727418002678.8.3",
    "__jdv": "181111935%7Candroidapp%7Ct_335139774%7Cappshare%7CCopyURL%7C1727438191257",
    "PPRD_P": "UUID.1372461710-LOGID.1727438191259.1960122129",
    "shshshfpb": "BApXSYGpeMPdAzk61uLZYkMYBD-zx7IYwBmUCcL4V9xJ1ItZfQtDUwE283yn4ZdJ1dey5mzeBsg",
    "__jd_ref_cls": "MProductdetail_ChooseFloorClick"
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
    "x-api-eid-token": "jdd03AEL6S4YLDBR3R3DHKTTBSU62NQHPXCZ53TWFCWZRYRNI6YIXPIK2KJIUK4WPH23GPZEKHEIHMO6LT47DED6LS3WXKYAAAAMSGNINXYQAAAAADSR2K67LPK4ZXYX",
    "jsonp": "skuInfoCB",
    "h5st": generate_h5st(body),
    "body": body, # '\{' + body[1:-1] + '\}'
    "appCode": "ms0ca95114"
}
while True:
    response = requests.get(url, headers=headers, cookies=cookies, params=params)
    res_text = response.text
    print(res_text)


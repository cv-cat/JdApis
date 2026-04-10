# coding: utf-8
import subprocess
from functools import partial
subprocess.Popen = partial(subprocess.Popen, encoding="utf-8")
import execjs


try:
    JD = execjs.compile(open("../static/JD.js", "r", encoding='utf-8').read())
except:
    JD = execjs.compile(open("static/JD.js", "r", encoding='utf-8').read())

def generateParams(sku):
    return JD.call("generateParams", sku)

def generate_h5st(body):
    return JD.call("generate_h5st", body)


#!/usr/bin/env python3
"""闲鱼详情接口监听 — 被动捕获 itemDO.soldCnt 等字段"""
import frida
import sys
import json
from datetime import datetime

JS_CODE = open(__import__('pathlib').Path(__file__).parent / "detail_monitor.js", "r").read()

# 记录已打印的 itemId，去重用
seen = set()

def on_message(msg, data):
    if msg["type"] == "send":
        payload = msg.get("payload", "")
        if not isinstance(payload, dict):
            return
        t = payload.get("type", "")

        if t == "detail":
            item_id = payload.get("itemId", "?")
            if item_id in seen:
                print(f"[DETAIL] {item_id} (重复) soldCnt={payload.get('soldCnt')}")
                return
            seen.add(item_id)

            print()
            print("=" * 60)
            print(f"  ★ 抓获详情数据! source={payload.get('source','?')}")
            print(f"  itemId : {item_id}")
            print(f"  title  : {payload.get('title', '')}")
            print(f"  时间   : {datetime.now().strftime('%H:%M:%S')}")
            print("=" * 60)
            print()
            print(">>> ★ 销量/热度字段 ★ <<<")
            print(f"  soldCnt        = {payload.get('soldCnt')}")
            print(f"  soldPrice      = {payload.get('soldPrice')}")
            print(f"  wantCnt        = {payload.get('wantCnt')}")
            print(f"  collectCnt     = {payload.get('collectCnt')}")
            print(f"  browseCnt      = {payload.get('browseCnt')}")
            print(f"  commentCount   = {payload.get('commentCount')}")
            print()
            print(">>> 价格字段 <<<")
            print(f"  price          = {payload.get('price')}")
            print(f"  oldPrice       = {payload.get('oldPrice')}")
            print()
            print(">>> sellerDO <<<")
            print(f"  nick              = {payload.get('sellerNick')}")
            print(f"  hasSoldNumInteger = {payload.get('hasSoldNumInteger')}")
            print(f"  sellerItemCount   = {payload.get('sellerItemCount')}")
            print()
            print(f"  bodyLen: {payload.get('bodyLen','?')}")
            print(f"  itemDO 字段数: {payload.get('itemDOKeys','?')}")
            print("=" * 60)
            print()

        elif t == "http_req":
            print(f"[HTTP] {payload.get('url','')[:130]}")
        elif t == "mtop_detail":
            print(f"[MTOP] {payload.get('api','')} len={payload.get('len','')}")
        elif t == "status":
            print(f"[STATUS] {payload['msg']}")
        elif t == "okhttp_detail":
            print(f"[OKHTTP] url={payload.get('url','')[:100]}")
    elif msg["type"] == "error":
        print(f"[ERROR] {msg}")

def main():
    device = frida.get_usb_device()
    print(f"设备: {device.name}")

    # Attach 到已运行的闲鱼进程
    for proc in device.enumerate_processes():
        if proc.name == "闲鱼":
            pid = proc.pid
            break
    else:
        print("错误: 找不到闲鱼进程，请在模拟器上手动打开闲鱼")
        return
    print(f"Attach 到闲鱼进程 PID={pid}")
    session = device.attach(pid)

    script = session.create_script(JS_CODE)
    script.on("message", on_message)
    script.load()

    print("=" * 50)
    print("  闲鱼详情监听已就绪")
    print("  请在闲鱼 App 中打开任意商品详情页")
    print("  按 Ctrl+C 退出")
    print("=" * 50)
    print()

    import time
    try:
        print("等待 60 秒，期间任何详情页请求都会被捕获...", flush=True)
        for _ in range(120):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n退出监听")
        session.detach()

if __name__ == "__main__":
    main()

"""测试 collector.js 所有 RPC 方法"""
import frida, time, json, sys, subprocess, io
from pathlib import Path

# Fix Windows GBK encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

GADGET_HOST = "127.0.0.1:27042"
BRIDGES_DIR = Path(r"C:\Users\tao\AppData\Local\Programs\Python\Python312\Lib\site-packages\frida_tools\bridges")
SCRIPT_DIR = Path(__file__).parent

def load_script(session, bridge_js_path, user_js_path):
    bridge = bridge_js_path.read_text(encoding="utf-8")
    user = user_js_path.read_text(encoding="utf-8")
    combined = bridge + "\n" + user
    script = session.create_script(combined)

    def on_msg(msg, data):
        if msg["type"] == "send":
            payload = msg.get("payload", {})
            if isinstance(payload, dict) and payload.get("type") == "frida:load-bridge":
                stem = payload["name"].lower()
                bridge_src = (BRIDGES_DIR / f"{stem}.js").read_text(encoding="utf-8")
                script.post({
                    "type": "frida:bridge-loaded",
                    "filename": f"{stem}.js",
                    "source": bridge_src,
                })
                return
            print(f"[LOG] {payload}")
        elif msg["type"] == "error":
            desc = msg.get("description", str(msg)[:200])
            stack = msg.get("stack", "")
            print(f"[ERR] {desc}")
            if stack:
                print(f"[ERR] {stack[:300]}")

    script.on("message", on_msg)
    script.load()
    return script

def rpc(script, method, *args):
    """Call RPC method and return parsed result"""
    try:
        fn = getattr(script.exports_sync, method)
        result = fn(*args)
        if isinstance(result, str):
            try:
                return json.loads(result)
            except:
                return result
        return result
    except Exception as e:
        return {"error": str(e)}

def main():
    # Get PID
    pid = int(subprocess.check_output(
        ["adb", "shell", "pidof", "com.taobao.idlefish"]
    ).decode().strip())
    print(f"[*] PID: {pid}")

    device = frida.get_device_manager().add_remote_device(GADGET_HOST)
    session = device.attach(pid)

    bridge_path = SCRIPT_DIR / "bridge_loader.js"
    collector_path = SCRIPT_DIR / "collector.js"
    script = load_script(session, bridge_path, collector_path)

    time.sleep(5)  # Wait for Java bridge + collector init
    status = rpc(script, "status")
    print(f"[*] Status: {status}")

    if not status.get("ready"):
        print("[!] Collector not ready, abort")
        session.detach()
        return

    # === Test 1: Search ===
    print("\n=== Test 1: Search ===")
    result = rpc(script, "search", "手机壳", 1, 5)
    if "error" not in result:
        print(f"  keyword: {result.get('keyword')}")
        print(f"  page: {result.get('page')}")
        print(f"  count: {result.get('count')}")
        print(f"  numFound: {result.get('numFound')}")
        print(f"  hasMore: {result.get('hasMore')}")
        print(f"  maxPrice: {result.get('maxPrice')}")
        print(f"  minPrice: {result.get('minPrice')}")
        items = result.get("items", [])
        if items:
            try:
                print(f"  First item: {json.dumps(items[0], ensure_ascii=False)[:300]}")
            except:
                print(f"  First item: {json.dumps(items[0], ensure_ascii=True)[:300]}")
    else:
        print(f"  ERROR: {result}")

    # === Test 2: Get Detail ===
    print("\n=== Test 2: Get Detail ===")
    if result.get("items"):
        item_id = result["items"][0]["itemId"]
        print(f"  Fetching detail for itemId={item_id}")
        detail = rpc(script, "get_detail", item_id)
        if isinstance(detail, str):
            print(f"  Detail: {detail[:200]}...")
        elif isinstance(detail, dict) and "error" not in detail:
            print(f"  Detail keys: {list(detail.keys())[:20]}")
        else:
            print(f"  Detail: {str(detail)[:200]}")
    else:
        print("  No items to get detail from")

    # === Test 3: Get Comments ===
    print("\n=== Test 3: Get Comments ===")
    if result.get("items"):
        item_id = result["items"][0]["itemId"]
        print(f"  Fetching comments for itemId={item_id}")
        comments = rpc(script, "get_comments", item_id, 1)
        if isinstance(comments, str):
            print(f"  Comments: {comments[:200]}...")
        elif isinstance(comments, dict) and "error" not in comments:
            print(f"  Comments keys: {list(comments.keys())[:20]}")
        else:
            print(f"  Comments: {str(comments)[:200]}")

    # === Test 4: Market Tabs ===
    print("\n=== Test 4: Market Tabs ===")
    kw = "手机壳"
    tabs = rpc(script, "get_market_tabs", kw)
    if isinstance(tabs, str):
        print(f"  Tabs: {tabs[:300]}...")
    elif isinstance(tabs, dict) and "error" not in tabs:
        print(f"  Tabs keys: {list(tabs.keys())[:10]}")
        # Extract spuId/categoryId
        try:
            tab_list = tabs.get("result", tabs.get("resultList", []))
            for t in tab_list:
                st = t.get("searchTabType", "")
                extra = t.get("extra", t.get("data", {}))
                print(f"  {t.get('showName', '?'):8s} type={st:30s} spuId={extra.get('spuId', 'N/A')}")
        except Exception as e:
            print(f"  Parse error: {e}")
    else:
        print(f"  Tabs: {str(tabs)[:200]}")

    # === Test 5: Market Topbar (if tabs available) ===
    print("\n=== Test 5: Market Topbar ===")
    spu_id = None
    category_id = None
    spu_name = ""
    category_name = ""
    if isinstance(tabs, dict):
        try:
            for t in tabs.get("result", tabs.get("resultList", [])):
                if t.get("searchTabType") == "SEARCH_TAB_MARKET":
                    extra = t.get("extra", t.get("data", {}))
                    spu_id = extra.get("spuId")
                    category_id = extra.get("categoryId", "")
                    spu_name = extra.get("spuName", "")
                    category_name = extra.get("categoryName", "")
                    break
        except:
            pass

    if spu_id:
        print(f"  spuId={spu_id} spuName={spu_name}")
        topbar = rpc(script, "get_market_topbar", kw, spu_id, category_id, spu_name, category_name)
        print(f"  Topbar: {str(topbar)[:300]}")
    else:
        print("  No market tab with spuId found")

    # === Test 6: Market History Sale ===
    print("\n=== Test 6: Market History Sale ===")
    if spu_id:
        hs = rpc(script, "get_market_history_sale", kw, spu_id, category_id, spu_name, category_name, 1)
        print(f"  HistorySale p1: {str(hs)[:300]}")
        # Page 2
        hs2 = rpc(script, "get_market_history_sale", kw, spu_id, category_id, spu_name, category_name, 2)
        print(f"  HistorySale p2: {str(hs2)[:300]}")
    else:
        print("  No spuId")

    # === Test 7: Market Price Trend ===
    print("\n=== Test 7: Market Price Trend ===")
    if spu_id:
        pt = rpc(script, "get_market_price_trend", kw, spu_id, category_id, spu_name, category_name)
        print(f"  PriceTrend: {str(pt)[:300]}")
    else:
        print("  No spuId")

    print("\n[*] All tests done")
    session.detach()

if __name__ == "__main__":
    main()

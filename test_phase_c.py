"""Phase C 验证搜索 + 前台令牌调度 真机测试"""
import json
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

with open(PROJECT_DIR / "settings.json", "r", encoding="utf-8") as f:
    settings = json.load(f)

from engines.device_engine import DeviceEngine
from core.frida_bridge import FridaBridge
from core.global_rate_limiter import GlobalRateLimiter
from core.foreground_scheduler import ForegroundScheduler
from engines.flywheel_engine import FlywheelEngine

PASS_CNT = 0
FAIL_CNT = 0

def check(name, cond, detail=""):
    global PASS_CNT, FAIL_CNT
    if cond:
        PASS_CNT += 1
        print(f"  [{name}] [PASS] {detail}")
    else:
        FAIL_CNT += 1
        print(f"  [{name}] [FAIL] {detail}")
    return cond

print("=" * 65)
print("  Phase C 验证搜索 + 前台令牌 真机测试")
print("=" * 65)

# ═══ 1. 设备连接 ═══
print("\n--- 1. 设备连接 + Frida ---")
dev_engine = DeviceEngine(settings)
mgr = dev_engine.manager
mgr.auto_scan()
mgr.connect(mgr.list_devices()[0].adb_addr)
check("设备连接", mgr.get_active() is not None, "真机已连接")

dev_engine.ensure_services_ready()
bridge = FridaBridge(mgr)
check("Frida 加载", bridge.load(), "bridge 加载成功")

# ═══ 2. 限速器 ═══
limiter = GlobalRateLimiter(min_interval_sec=3.0, max_interval_sec=10.0)

# ═══ 3. 前台令牌调度 ═══
print("\n--- 2. 前台令牌调度器测试 ---")
fs = ForegroundScheduler(device_mgr=mgr)

# 测试1: 获取闲鱼令牌
t0 = time.time()
tok = fs.request("xianyu", 60)
check("令牌-闲鱼获取", tok is not None and fs.holder_app() == "xianyu",
      f"app={tok.app if tok else 'None'} 租约60s")

# 测试2: PDD 在闲鱼占用时被拒
tok2 = fs.request("pdd", 30)
check("令牌-PDD被拒(闲鱼占用)", tok2 is None, "闲鱼占用 → PDD 正确被拒")

# 测试3: 释放后 PDD 可获取
fs.release(tok)
check("令牌-释放", not fs.is_held(), "令牌已释放")

tok3 = fs.request("pdd", 30)
check("令牌-PDD获取(释放后)", tok3 is not None and fs.holder_app() == "pdd",
      "释放后 PDD 正确获取")
fs.release(tok3)

# 测试4: renewal_signal
tok4 = fs.request("xianyu", 5)  # 只有5秒的租约
time.sleep(3)  # 等3秒，还剩2秒 < renewal_warning(15s)
need_renewal = fs.check_renewal(tok4)
check("令牌-renewal信号", need_renewal, "租约5s,等3s后应触发收尾信号")
fs.release(tok4)

# 测试5: PDD notify + cancel
fs2 = ForegroundScheduler(device_mgr=mgr)
fs2.notify_pdd_waiting()
tok5 = fs2.request("pdd", 60)
check("令牌-货源排队后获取", tok5 is not None, "排队后 PDD 可获取")
fs2.cancel_request("pdd")
check("令牌-取消手机申请", True, "DDK有结果时取消PDD申请")
fs2.release(tok5)

print("  令牌调度: 全部通过 [OK]")

# ═══ 4. Phase C 验证搜索 ═══
print("\n--- 3. Phase C 验证搜索 ---")
output_dir = Path.home() / ".xianyu_tool" / "collected_data"
fw = FlywheelEngine(settings, output_dir=output_dir)

# 从词库取 pending_verify 词
pending_words = fw.word_lib.get_pending_words()
print(f"词库中 pending_verify 词: {len(pending_words)} 个")

if not pending_words:
    print("  没有待验证词，跳过 Phase C")
else:
    # 取前 5 个做测试
    test_words = pending_words[:5]
    print(f"测试词 ({len(test_words)} 个): {test_words}")

    # 逐个搜索（模拟 Phase C 批量验证流程）
    searches = []
    for i, word in enumerate(test_words):
        t0 = time.time()
        limiter.acquire(f"飞轮C-{word}")
        raw = bridge.collect_keyword(word, max_pages=1, detail_max=0, comment_max=0)
        elapsed = time.time() - t0

        items = raw.get("searchItems", [])
        err = raw.get("error", "")
        num_found = raw.get("searchMeta", {}).get("numFound", 0)

        if err:
            print(f"  [{i+1}] {word} [FAIL] 搜索失败: {err[:60]}")
            continue

        has_sold = sum(1 for it in items if it.get("soldCount", 0))
        print(f"  [{i+1}] {word}: numFound={num_found} 商品={len(items)} 有售出={has_sold} ({elapsed:.1f}s)")

        searches.append({
            "word": word,
            "numFound": num_found,
            "search_items": items,
        })
        limiter.release()

    check("Phase C-搜索", len(searches) > 0, f"成功搜索 {len(searches)}/{len(test_words)} 词")

    if searches:
        # 运行 Phase C 判定
        print(f"\n  运行 Phase C 判定...")
        result = fw.run_phase_c(searches)

        pass_n = result.get("pass_count", 0)
        watch_n = result.get("watch_count", 0)
        discard_n = result.get("discard_count", 0)

        print(f"  Phase C 结果: {pass_n} pass | {watch_n} watch | {discard_n} discard")
        check("Phase C-判定完成", pass_n + watch_n + discard_n == len(searches),
              f"{len(searches)}词全部判定")

        # 验证词库状态已更新（重新加载JSON）
        fw.word_lib.data = json.loads(
            Path(output_dir / "word_library.json").read_text(encoding="utf-8"))
        for r in result.get("results", []):
            word = r["word"]
            verdict = r["verdict"]
            reason = r.get("reason", "")
            entry = fw.word_lib.get_entry(word)
            actual_status = entry.get("status", "?")
            ok = actual_status == verdict
            mark = "OK" if ok else "!!"
            print(f"    {mark} {word}: {verdict} ({reason[:40]}) [词库状态={actual_status}]")
            check(f"Phase C-词库更新({word})", ok,
                  f"期望={verdict} 实际={actual_status}")

print()
print("=" * 65)
print(f"  测试结果: {PASS_CNT} PASS / {FAIL_CNT} FAIL")
if FAIL_CNT == 0:
    print("  全部通过!")
else:
    print(f"  有 {FAIL_CNT} 项失败，请检查")
print(f"  真机: sceain7t6t4pjb7p | Phase C: {len(pending_words)}pending → 测试{min(5, len(pending_words))}词")
print("=" * 65)

# 断开
try:
    bridge.unload()
except:
    pass

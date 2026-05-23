"""
100种子词批量采集 — 防风控强化版
分批跑 / 随机间隔 / 风控检测 / 断点续跑 / 30分钟冷却 / 自动恢复
"""
import json, sys, time, random, threading
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from engines.device_engine import DeviceEngine
from engines.collection_engine import CollectionEngine
from engines.keyword_scorer_v3 import KeywordScorerV3
from engines.product_scorer_v3 import ProductScorerV3

BATCH_SIZE = 12
BATCH_PAUSE_MIN = 180    # 正常批间暂停 3-6 分钟
BATCH_PAUSE_MAX = 360
RISK_PAUSE_MIN = 1800    # 风控暂停 30-45 分钟
RISK_PAUSE_MAX = 2700
RISK_MAX_CONSECUTIVE = 3  # 连续风控3次 → 彻底停止
INTER_KW_DELAY = (15, 30)

RISK_SIGNALS = [
    "验证", "频繁", "滑块", "风控", "限制", "captcha",
    "verify", "block", "too many", "rate limit",
]


def is_risk(search_result: dict) -> bool:
    """检测是否触发风控"""
    error = str(search_result.get("error", "")).lower()
    for sig in RISK_SIGNALS:
        if sig.lower() in error:
            return True
    items = search_result.get("searchItems", [])
    if len(items) == 0 and not error:
        # 空结果也可能是风控（非正常情况）
        meta = search_result.get("searchMeta", {})
        nf = meta.get("numFound", 0)
        if nf == 0:
            return True
    return False


def send_notify(settings: dict, msg: str):
    """发送 webhook 通知（如果配置了）"""
    webhook = settings.get("api", {}).get("webhook_url", "")
    if not webhook:
        return
    try:
        import requests
        requests.post(webhook, json={
            "msgtype": "text",
            "text": {"content": f"[闲鱼采集] {msg}"}
        }, timeout=5)
    except Exception:
        pass


def main():
    base = Path(__file__).parent
    settings_path = base / "settings.json"
    seed_path = base / "engines" / "seed_keywords.json"
    output_dir = base / "collected_data"
    ckpt_path = output_dir / "_batch_progress.json"

    with open(settings_path) as f:
        settings = json.load(f)
    with open(seed_path) as f:
        seeds = json.load(f)

    all_keywords = seeds["flat_keywords"]

    # 恢复断点
    if ckpt_path.exists():
        with open(ckpt_path) as f:
            progress = json.load(f)
        done_kws = set(progress.get("done", []))
        print(f"📋 恢复断点: {len(done_kws)} 词已完成")
    else:
        done_kws = set()
        progress = {"done": [], "batch": 0, "risk_count": 0,
                     "started": datetime.now().isoformat()}

    pending = [kw for kw in all_keywords if kw not in done_kws]
    if not pending:
        print("所有词已完成！")
        return

    random.shuffle(pending)

    print(f"={'='*60}")
    print(f"防风控批量采集 v2")
    print(f"总词数: {len(all_keywords)}, 已完成: {len(done_kws)}, 剩余: {len(pending)}")
    print(f"策略: 每批{BATCH_SIZE}词 → 暂停{BATCH_PAUSE_MIN//60}-{BATCH_PAUSE_MAX//60}分钟")
    print(f"风控: 检测到自动暂停{RISK_PAUSE_MIN//60}-{RISK_PAUSE_MAX//60}分钟")
    print(f"={'='*60}")

    dev_engine = DeviceEngine(settings)
    devices = dev_engine.list_devices()
    if not devices:
        print("❌ 无设备")
        return
    target = devices[0]
    state = dev_engine.connect(target.adb_addr)
    print(f"设备: {target.name}, 连接={state.connected}\n")

    scorer_cfg = {
        "keyword_model": {"grades": {"S": 90, "A": 30, "B": 20, "C": 10, "D": 0}},
        "product_model": {"grades": {"S": 90, "A": 30, "B": 20, "C": 10, "D": 0}},
    }
    kw_scorer = KeywordScorerV3(scorer_cfg)
    pd_scorer = ProductScorerV3(scorer_cfg)

    batch_num = progress.get("batch", 0)
    risk_consecutive = progress.get("risk_count", 0)

    while pending:
        batch_num += 1
        batch = pending[:BATCH_SIZE]
        pending = pending[BATCH_SIZE:]

        print(f"\n── 第{batch_num}批: {len(batch)}词 ──")
        print(f"词: {', '.join(batch[:5])}... | {datetime.now().strftime('%H:%M:%S')}")

        batch_settings = json.loads(json.dumps(settings))
        sp = random.randint(2, 4)
        batch_settings["collection"]["search_pages"] = sp
        batch_settings["collection"]["detail_max"] = random.randint(2, 4)
        batch_settings["collection"]["comment_max"] = 0
        batch_settings["collection"]["kw_push_threshold"] = 30
        batch_settings["collection"]["pd_push_threshold"] = 30

        # 词间抖动
        for kw in batch:
            time.sleep(random.uniform(*INTER_KW_DELAY))

        engine = CollectionEngine(dev_engine, batch_settings)
        engine.set_scorers(kw_scorer, pd_scorer)

        done_event = threading.Event()
        batch_results = {}
        risk_detected = False

        def on_complete(kw_results, pd_results, supply_pushed):
            batch_results["kw"] = kw_results
            batch_results["pd"] = pd_results
            done_event.set()

        engine.set_callbacks(on_complete=on_complete)
        engine.start(batch, output_dir=output_dir)
        done_event.wait(timeout=900)

        # ★ 风控检测
        for kw in batch:
            kw_file = output_dir / f"{kw}.json"
            if kw_file.exists():
                try:
                    with open(kw_file) as f:
                        d = json.load(f)
                    if is_risk(d):
                        risk_detected = True
                        print(f"  ⚠️ 风控信号: {kw}")
                        break
                except Exception:
                    pass

        if risk_detected:
            risk_consecutive += 1
            progress["risk_count"] = risk_consecutive

            # 保存断点
            done_kws.update(batch[: len(batch)//2])  # 保守：只标记一半完成
            progress["done"] = list(done_kws)
            progress["batch"] = batch_num
            progress["last_risk_time"] = datetime.now().isoformat()
            ckpt_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2))

            if risk_consecutive >= RISK_MAX_CONSECUTIVE:
                print(f"\n⛔ 连续风控{risk_consecutive}次！已停止。请检查设备后重新运行。")
                send_notify(settings, f"⛔ 连续风控{risk_consecutive}次！采集已停止，请检查设备")
                return

            pause = random.randint(RISK_PAUSE_MIN, RISK_PAUSE_MAX)
            print(f"\n🛡 风控暂停 {pause//60}分钟")
            print(f"   断点已保存，恢复后从第{batch_num}批继续")
            send_notify(settings, f"风控触发！暂停{pause//60}分钟")

            for i in range(pause, 0, -60):
                print(f"   剩余 {i//60}分钟...")
                time.sleep(60)

            # 恢复连接
            print("▶ 重连设备...")
            dev_engine.connect(target.adb_addr)
        else:
            risk_consecutive = max(0, risk_consecutive - 1)
            progress["risk_count"] = risk_consecutive
            done_kws.update(batch)
            progress["done"] = list(done_kws)
            progress["batch"] = batch_num
            progress["last_batch_time"] = datetime.now().isoformat()
            ckpt_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2))

            remaining = len(all_keywords) - len(done_kws)
            print(f"✅ 第{batch_num}批完成: {len(done_kws)}/{len(all_keywords)} ({remaining}剩余)")

            # 正常批间暂停
            if pending:
                pause = random.randint(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX)
                print(f"⏸ 冷却 {pause//60}分钟...")
                for i in range(pause, 0, -60):
                    if i % 120 == 0:
                        print(f"   剩余 {i//60}分钟...")
                    time.sleep(60)
                # 确认设备
                if not dev_engine.get_active() or not dev_engine.get_active().app_pid:
                    print("  重连设备...")
                    dev_engine.connect(target.adb_addr)

    # 完成
    ckpt_path.unlink(missing_ok=True)
    print(f"\n🎉 全部完成！{len(all_keywords)}词")
    send_notify(settings, f"采集完成！{len(all_keywords)}词全部处理完毕")


if __name__ == "__main__":
    main()

# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：闲鱼数据采集分析工具 v3"""
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

PROJECT_ROOT = Path(SPECPATH)

# 收集 uiautomator2 的资产文件 (assets/u2.jar, assets/app-uiautomator.apk 等)
u2_datas = collect_data_files("uiautomator2")

# 收集 frida_tools 的 bridge 文件 (bridges/java.js, bridges/objc.js, bridges/swift.js)
ft_datas = collect_data_files("frida_tools")

# 收集 text2vec 的模型数据文件
try:
    t2v_datas = collect_data_files("text2vec")
except Exception:
    t2v_datas = []

a = Analysis(
    [str(PROJECT_ROOT / "gui_main.py")],
    pathex=[
        str(PROJECT_ROOT),
    ],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / "batch_collect.js"), "."),
        (str(PROJECT_ROOT / "collector.js"), "."),
        (str(PROJECT_ROOT / "bridge_loader.js"), "."),
        (str(PROJECT_ROOT / "settings.json"), "."),
        (str(PROJECT_ROOT / "models_config.json"), "."),
        (str(PROJECT_ROOT / "version.json"), "."),
        (str(PROJECT_ROOT / "assets" / "app_icon.ico"), "assets"),
    ] + u2_datas + ft_datas + t2v_datas,
    hiddenimports=[
        "frida",
        "frida_tools",
        "uiautomator2",
        "matplotlib",
        "matplotlib.backends.backend_tkagg",
        "openpyxl",
        "PIL",
        "text2vec",
        "text2vec.sentence_model",
        "text2vec.similarity",
        "text2vec.word2vec",
        "transformers",
        "transformers.models.auto",
        "transformers.models.auto.tokenization_auto",
        "transformers.models.auto.modeling_auto",
        "tokenizers",
        "tokenizers.decoders",
        "tokenizers.normalizers",
        "tokenizers.pre_tokenizers",
        "tokenizers.processors",
        "huggingface_hub",
        "sentence_transformers",
        "requests",
        "engines",
        "engines.device_engine",
        "engines.collection_engine",
        "engines.keyword_scorer_v3",
        "engines.product_scorer_v3",
        "engines.supply_finder_engine",
        "engines.pdd_supply_finder_v2",
        "gui",
        "gui.app",
        "gui.theme",
        "gui.tabs",
        "gui.tabs.tab_device",
        "gui.tabs.tab_collection",
        "gui.tabs.tab_dashboard",
        "gui.tabs.tab_supply_finder",
        "gui.tabs.tab_charts",
        "gui.widgets",
        "gui.widgets.status_bar",
        "gui.widgets.log_panel",
        "gui.widgets.funnel_progress",
        "gui.widgets.tree_helpers",
        "gui.dialogs",
        "gui.dialogs.settings_dialog",
        "utils",
        "utils.config_manager",
        "utils.log_manager",
        "utils.platform_utils",
        "exporter",
        "exporter.excel_exporter",
        "core",
        "core.device_mgr",
        "core.frida_bridge",
        "core.log_handler",
        "core.webhook",
        "core.data_io",
        "core.market_collector",
        "core.product_collector",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter.test",
        "unittest",
        "test",
        "apk_work",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="闲鱼数据采集分析工具",
    icon=str(PROJECT_ROOT / "assets" / "app_icon.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    [],
    name="闲鱼数据采集分析工具",
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

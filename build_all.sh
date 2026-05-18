#!/bin/bash
# 闲鱼数据采集分析工具 — macOS 一键构建（含 .dmg 生成）
set -e

RELEASE_DIR="../XianyuTool_release"

echo "========================================"
echo "  闲鱼数据采集分析工具 — macOS 构建"
echo "========================================"
echo ""

echo "[1/6] 转换图标..."
python3 tools/convert_icon.py

echo ""
echo "[2/6] 构建主程序..."
pyinstaller build_gui.spec --noconfirm

echo ""
echo "[3/6] 构建卸载程序..."
pyinstaller build_uninstaller.spec --noconfirm

echo ""
echo "[4/6] 复制卸载程序到主应用目录..."
UNINST_SRC="dist/闲鱼工具卸载程序"
UNINST_DST="dist/闲鱼数据采集分析工具/"
if [ -f "$UNINST_SRC" ]; then
    cp "$UNINST_SRC" "$UNINST_DST"
    echo "  已复制卸载程序"
else
    echo "  ⚠ 卸载程序不存在，跳过"
fi

echo ""
echo "[5/6] 构建安装程序..."
pyinstaller build_installer.spec --noconfirm

echo ""
echo "[6/6] 同步产物到分发包目录..."
mkdir -p "$RELEASE_DIR"
cp -a dist/* "$RELEASE_DIR/"
echo "  已同步到 $RELEASE_DIR"

echo ""
echo "========================================"
echo "  构建完成！"
echo "  开发目录 dist/"
echo "  分发包   $RELEASE_DIR/"
echo "========================================"

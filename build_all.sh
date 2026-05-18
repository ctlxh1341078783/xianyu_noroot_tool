#!/bin/bash
# 闲鱼数据采集分析工具 — macOS 一键构建（含 .dmg 生成）
set -e

echo "========================================"
echo "  闲鱼数据采集分析工具 — macOS 构建"
echo "========================================"
echo ""

echo "[1/5] 转换图标..."
python3 tools/convert_icon.py

echo ""
echo "[2/5] 构建主程序..."
pyinstaller build_gui.spec --noconfirm

echo ""
echo "[3/5] 构建卸载程序..."
pyinstaller build_uninstaller.spec --noconfirm

echo ""
echo "[4/5] 复制卸载程序到主应用目录..."
UNINST_SRC="dist/闲鱼工具卸载程序"
UNINST_DST="dist/闲鱼数据采集分析工具/"
if [ -f "$UNINST_SRC" ]; then
    cp "$UNINST_SRC" "$UNINST_DST"
    echo "  已复制卸载程序"
else
    echo "  ⚠ 卸载程序不存在，跳过"
fi

echo ""
echo "[5/5] 构建安装程序..."
pyinstaller build_installer.spec --noconfirm

echo ""
echo "========================================"
echo "  构建完成！"
echo "  dist/闲鱼数据采集分析工具/   主程序"
echo "  dist/闲鱼工具安装程序/       安装程序"
echo "  dist/闲鱼工具卸载程序/       卸载程序"
echo ""
echo "  生成 .dmg（可选）:"
echo "    ./build_dmg.sh"
echo "========================================"

#!/bin/bash
# 将安装程序打包为 .dmg（仅 macOS）
set -e

APP_DIR="dist/闲鱼工具安装程序"
DMG_NAME="闲鱼工具安装程序"
DMG_FILE="dist/${DMG_NAME}.dmg"
TMP_DIR="dist/_dmg_temp"

if [ ! -d "$APP_DIR" ]; then
    echo "错误: 未找到 $APP_DIR，请先运行 build_all.sh"
    exit 1
fi

echo "正在生成 .dmg..."

# 1. 准备临时目录
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"
cp -R "$APP_DIR/" "$TMP_DIR/"

# 2. 创建 Applications 符号链接（Mac 安装惯例）
ln -sf /Applications "$TMP_DIR/Applications"

# 3. 用 hdiutil 创建 .dmg
hdiutil create -volname "$DMG_NAME" \
    -srcfolder "$TMP_DIR" \
    -ov -format UDZO \
    "$DMG_FILE"

# 4. 清理
rm -rf "$TMP_DIR"

echo ""
echo "✓ 已生成: $DMG_FILE"
echo "  用户双击 .dmg 后把应用拖到 Applications 文件夹即可安装"

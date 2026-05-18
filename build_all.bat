@echo off
chcp 65001 >nul
echo ========================================
echo   闲鱼数据采集分析工具 — 一键构建
echo ========================================
echo.

echo [1/5] 转换图标...
python tools/convert_icon.py
if %errorlevel% neq 0 exit /b %errorlevel%

echo.
echo [2/5] 构建主程序...
pyinstaller build_gui.spec --noconfirm
if %errorlevel% neq 0 exit /b %errorlevel%

echo.
echo [3/5] 构建卸载程序...
pyinstaller build_uninstaller.spec --noconfirm
if %errorlevel% neq 0 exit /b %errorlevel%

echo.
echo [4/5] 复制卸载程序到 dist...
copy /Y "dist\闲鱼工具卸载程序\闲鱼工具卸载程序.exe" "dist\闲鱼数据采集分析工具\"
if %errorlevel% neq 0 (
    echo 警告: 复制卸载程序失败，安装包将不含卸载程序
)

echo.
echo [5/5] 构建安装程序...
pyinstaller build_installer.spec --noconfirm
if %errorlevel% neq 0 exit /b %errorlevel%

echo.
echo ========================================
echo   构建完成！
echo   dist/闲鱼工具安装程序/闲鱼工具安装程序.exe  安装程序
echo   dist/闲鱼数据采集分析工具/                   主程序目录
echo   dist/闲鱼工具卸载程序/闲鱼工具卸载程序.exe  卸载程序
echo ========================================
pause

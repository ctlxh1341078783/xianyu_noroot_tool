@echo off
set "RELEASE_DIR=..\XianyuTool_release"

echo ========================================
echo   闲鱼数据采集分析工具 - 一键构建
echo ========================================
echo.

echo [1/6] 生成图标...
python tools/convert_icon.py
if %errorlevel% neq 0 exit /b %errorlevel%

echo.
echo [2/6] 构建主程序...
pyinstaller build_gui.spec --noconfirm
if %errorlevel% neq 0 exit /b %errorlevel%

echo.
echo [3/6] 构建卸载程序...
pyinstaller build_uninstaller.spec --noconfirm
if %errorlevel% neq 0 exit /b %errorlevel%

echo.
echo [4/6] 复制卸载程序到 dist...
copy /Y "dist\闲鱼工具卸载程序.exe" "dist\闲鱼数据采集分析工具\" >nul
if %errorlevel% neq 0 (
    echo 警告: 复制卸载程序失败，安装包将不含卸载程序
)

echo.
echo [5/6] 构建安装程序...
pyinstaller build_installer.spec --noconfirm
if %errorlevel% neq 0 exit /b %errorlevel%

echo.
echo [6/6] 同步产物到分发包目录...
if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"
xcopy "dist\*" "%RELEASE_DIR%\" /E /Y /Q >nul
echo   已同步到 %RELEASE_DIR%

echo.
echo ========================================
echo   构建完成！
echo   开发目录 dist/
echo   分发包   %RELEASE_DIR%
echo ========================================
pause

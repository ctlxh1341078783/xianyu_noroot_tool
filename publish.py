"""
一键发布脚本：更新版本 → 构建 → 提交Git → 推送
用法：改完代码后，在终端运行 python publish.py
"""
import json
import sys
import subprocess
import datetime
from pathlib import Path

ROOT = Path(__file__).parent
VERSION_FILE = ROOT / "version.json"


def load_version() -> dict:
    return json.loads(VERSION_FILE.read_text(encoding="utf-8"))


def save_version(data: dict):
    VERSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run(cmd: str, **kwargs):
    """运行命令，实时输出，失败抛异常"""
    print(f"  → {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=str(ROOT), **kwargs)
    if result.returncode != 0:
        print(f"  ✗ 失败 (exit code {result.returncode})")
        sys.exit(1)
    return result


def main():
    print("=" * 60)
    print("  闲鱼数据采集分析工具 — 一键发布")
    print("=" * 60)
    print()

    # 1. 读取当前版本
    ver = load_version()
    print(f"当前版本: v{ver['version']} (构建 {ver['build_number']})")
    print(f"发布日期: {ver.get('build_date', '未知')}")
    print()

    # 2. 输入新版本号
    parts = ver["version"].split(".")
    suggested = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    new_ver = input(f"新版本号 (回车默认 {suggested}): ").strip()
    if not new_ver:
        new_ver = suggested

    # 3. 输入更新内容
    print()
    print("更新内容（一行一条，空行结束）:")
    changelog = []
    while True:
        line = input("  > ").strip()
        if not line:
            break
        changelog.append(line)

    if not changelog:
        print("未输入更新内容，取消发布。")
        sys.exit(0)

    # 4. 更新 version.json
    new_build = ver["build_number"] + 1
    today = datetime.date.today().isoformat()
    ver["version"] = new_ver
    ver["build_number"] = new_build
    ver["build_date"] = today
    ver["changelog"] = changelog + ver.get("changelog", [])
    save_version(ver)
    print()
    print(f"✓ version.json 已更新: v{new_ver} (构建 {new_build}, {today})")

    # 5. 构建
    print()
    print("-" * 60)
    print("开始构建...")
    print("-" * 60)

    run("python tools/convert_icon.py")
    print()
    run("pyinstaller build_gui.spec --noconfirm")
    print()
    run("pyinstaller build_uninstaller.spec --noconfirm")
    print()

    uninst_src = "dist/闲鱼工具卸载程序/闲鱼工具卸载程序.exe"
    uninst_dst = "dist/闲鱼数据采集分析工具/"
    run(f'copy /Y "{uninst_src}" "{uninst_dst}"')
    print()

    run("pyinstaller build_installer.spec --noconfirm")
    print()
    print("✓ 构建完成")

    # 6. Git 操作
    print()
    print("-" * 60)
    print("提交到 Git...")
    print("-" * 60)

    commit_msg = f"v{new_ver}: {'; '.join(changelog[:3])}"
    run("git add .")
    run(f'git commit -m "{commit_msg}"')
    print()
    run("git push")
    print()
    print(f"✓ 已推送: {commit_msg}")

    # 7. 提示发布 Release
    print()
    print("=" * 60)
    print("  发布完成！")
    print("=" * 60)
    print()
    print(f"  ✓ 版本: v{new_ver} (构建 {new_build})")
    print(f"  ✓ 更新: {', '.join(changelog)}")
    print()
    print("  下一步 — 在 GitHub 上发布 Release：")
    print(f"  1. 打开仓库的 Releases 页面")
    print(f"  2. 创建新 Release，Tag: v{new_ver}")
    print(f"  3. 上传安装包 (dist/闲鱼工具安装程序/ 打包成 zip)")
    print()
    print("  或者用 gh 命令行：")
    zip_path = ROOT / "dist" / "闲鱼工具安装程序.zip"
    zip_path.unlink(missing_ok=True)
    print(f"    powershell Compress-Archive -Path dist/闲鱼工具安装程序/* "
          f"-DestinationPath dist/闲鱼工具安装程序.zip")
    print(f'    gh release create v{new_ver} dist/闲鱼工具安装程序.zip '
          f'--title "v{new_ver}" --notes "{chr(10).join(changelog)}"')
    print()


if __name__ == "__main__":
    main()

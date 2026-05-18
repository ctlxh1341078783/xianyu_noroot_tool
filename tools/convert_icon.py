"""开发工具：将 PNG 图标转为 ICO 多尺寸格式（仅在 PNG 更新时需要运行）"""
from PIL import Image
from pathlib import Path

def main():
    project_root = Path(__file__).parent.parent
    assets_dir = project_root / "assets"
    png_path = assets_dir / "icon_source.png"
    ico_path = assets_dir / "app_icon.ico"

    # ICO 已存在且 PNG 源不在 → 无需转换
    if ico_path.exists() and not png_path.exists():
        print(f"ICO 已存在，跳过转换 ({ico_path})")
        return

    if not png_path.exists():
        print(f"请将图标 PNG 放到 {png_path}")
        print(f"或直接使用现有的 {ico_path}")
        return

    print(f"读取: {png_path}")
    img = Image.open(png_path)
    print(f"  原始大小: {img.size}")

    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icons = []
    for s in sizes:
        icons.append(img.resize(s, Image.LANCZOS))
        print(f"  生成 {s[0]}x{s[1]}")

    icons[0].save(ico_path, format="ICO",
                  sizes=[(i.width, i.height) for i in icons],
                  append_images=icons[1:])
    size_kb = ico_path.stat().st_size / 1024
    print(f"已保存: {ico_path} ({size_kb:.0f} KB)")

if __name__ == "__main__":
    main()

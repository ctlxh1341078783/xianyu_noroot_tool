"""一次性工具：将 PNG 图标转为 ICO 多尺寸格式"""
from PIL import Image
import os
import sys

def main():
    png_path = os.path.expanduser(r"C:\Users\tao\Downloads\闲鱼采集工具图标设计.png")
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

    if not os.path.exists(png_path):
        print(f"图标文件不存在: {png_path}")
        sys.exit(1)

    os.makedirs(out_dir, exist_ok=True)
    ico_path = os.path.join(out_dir, "app_icon.ico")

    print(f"读取: {png_path}")
    img = Image.open(png_path)
    print(f"  原始大小: {img.size}")

    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icons = []
    for s in sizes:
        r = img.resize(s, Image.LANCZOS)
        icons.append(r)
        print(f"  生成 {s[0]}x{s[1]}")

    icons[0].save(ico_path, format="ICO", sizes=[(i.width, i.height) for i in icons],
                  append_images=icons[1:])
    size_kb = os.path.getsize(ico_path) / 1024
    print(f"已保存: {ico_path} ({size_kb:.0f} KB)")

if __name__ == "__main__":
    main()

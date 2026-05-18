"""全局主题配置：橘黄色主题 颜色/字体/样式"""
import sys
from pathlib import Path

# ── 配色方案（橘黄色主题） ──
BG = "#F5F6FA"           # 全局背景（柔白）
SURF = "#FFFFFF"          # 卡片背景
SURF2 = "#F8F9FD"         # 次级背景
FG = "#3D4152"            # 主文字
FG_M = "#8A90A8"          # 次要文字
FG_L = "#B0B5C5"          # 浅文字
ACC = "#FF6B35"           # 主题橘 主色
ACC_H = "#E85A2C"         # 主题橘 深色
SUCC = "#27AE60"          # 成功绿
WARN = "#F59E0B"          # 警告黄
DANGER = "#E74C3C"        # 危险红
BRD = "#E2E5F0"           # 边框浅灰
BRD_F = "#D1D5DB"         # 边框深灰

# ── 等级颜色 ──
GRADE_COLORS = {
    "S": ("#FFF3CD", "#B8860B"),         # 金底深金字
    "A": ("#E8F8EF", "#047857"),          # 绿底绿字
    "B": ("#E8F0FB", "#1D4ED8"),          # 蓝底蓝字
    "C": ("#FFF0E5", "#C2410C"),          # 橙底橙字
    "D": ("#FEE2E2", "#B91C1C"),          # 红底红字
    "N/A": ("#F3F4F6", "#6B7280"),
}

# ── 日志级别颜色 ──
LEVEL_EMOJI = {"DEBUG": "[D]", "INFO": "[I]", "WARN": "[W]", "ERROR": "[E]"}
LOG_COLORS = {"DEBUG": "#6B7280", "INFO": "#1F2937", "WARN": "#D97706", "ERROR": "#DC2626"}

# ── 线程状态色 ──
THREAD_COLORS = {
    "idle": "#9CA3AF",
    "connecting": "#F59E0B",
    "connected": "#10B981",
    "collecting": "#FF6B35",
    "error": "#EF4444",
    "disconnected": "#9CA3AF",
}

# ── 字体配置 ──
def get_system_fonts():
    """返回平台自适应字体"""
    if sys.platform == "darwin":
        return {
            "ui": ("PingFang SC", 10),
            "ui_bold": ("PingFang SC", 10, "bold"),
            "heading": ("PingFang SC", 12, "bold"),
            "mono": ("SF Mono", 9),
            "title": ("PingFang SC", 18, "bold"),
        }
    else:
        return {
            "ui": ("Microsoft YaHei", 9),
            "ui_bold": ("Microsoft YaHei", 9, "bold"),
            "heading": ("Microsoft YaHei", 11, "bold"),
            "mono": ("Consolas", 9),
            "title": ("Microsoft YaHei", 16, "bold"),
        }

FONTS = get_system_fonts()

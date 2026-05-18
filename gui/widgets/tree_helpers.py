"""Treeview辅助：标签着色、列配置"""
from typing import List, Tuple
import tkinter as tk
from tkinter import ttk
from gui.theme import GRADE_COLORS, FONTS


def setup_tree_style():
    style = ttk.Style()
    style.configure("Treeview", font=FONTS["ui"], rowheight=24)
    style.configure("Treeview.Heading", font=FONTS["ui_bold"])
    return style


def tag_rows_by_grade(tree: ttk.Treeview, grade_col: int, iid_col: int = 0):
    """按等级列着色每行"""
    for item in tree.get_children():
        values = tree.item(item, "values")
        if len(values) > grade_col:
            grade = values[grade_col]
            bg, fg = GRADE_COLORS.get(grade, ("#F3F4F6", "#6B7280"))
            tag = f"grade_{grade}"
            tree.tag_configure(tag, background=bg, foreground=fg)
            tree.item(item, tags=(tag,))


def make_columns(tree: ttk.Treeview, columns: List[Tuple[str, int, str]]):
    """
    columns: [(name, width, anchor), ...]
    anchor: "w"/"center"/"e"
    """
    col_names = [c[0] for c in columns]
    tree["columns"] = col_names
    tree.column("#0", width=0, stretch=False)
    for name, width, anchor in columns:
        tree.column(name, width=width, anchor=anchor)
        tree.heading(name, text=name)

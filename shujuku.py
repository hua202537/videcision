from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pymysql

from config_manager import load_db_config

DECISION_RESULT_TABLE = "decision_result_simulation"
RESULT_PROJECT_BATCH_COLUMNS = ("projectname", "batchname")


def get_db_connection():
    db = load_db_config()
    return pymysql.connect(
        host=db["DB_HOST"],
        port=int(db["DB_PORT"]),
        user=db["DB_USER"],
        password=db["DB_PASSWORD"],
        database=db["DB_NAME"],
        charset="utf8mb4",
        use_unicode=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _serialize_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _serialize_row_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date, Decimal)):
        return _serialize_cell(value)
    return value


def _build_column_list(columns: List[str]) -> List[Dict[str, Any]]:
    return [
        {"columnName": column_name, "indexNumber": index}
        for index, column_name in enumerate(columns)
    ]


def query_table_as_table_data(
    table_name: str,
    where_sql: str = "",
    params: Optional[Tuple[Any, ...]] = None,
) -> Dict[str, Any]:
    sql = f"SELECT * FROM `{table_name}`"
    if where_sql:
        sql += f" WHERE {where_sql}"

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"DESCRIBE `{table_name}`")
            columns = [row["Field"] for row in cursor.fetchall()]
            cursor.execute(sql, params or ())
            rows = cursor.fetchall()

    data = [[_serialize_cell(row.get(column)) for column in columns] for row in rows]
    return {
        "columnList": _build_column_list(columns),
        "data": data,
        "total": len(data),
    }


def query_decision_result_project_batch_list() -> Dict[str, Any]:
    """从 decision_result_simulation 查询全部 projectname、batchname 组合。"""
    columns = list(RESULT_PROJECT_BATCH_COLUMNS)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT DISTINCT `projectname`, `batchname`
                FROM `{DECISION_RESULT_TABLE}`
                ORDER BY `projectname`, `batchname`
                """
            )
            rows = cursor.fetchall()

    data = [[_serialize_cell(row.get(column)) for column in columns] for row in rows]
    return {
        "columnList": _build_column_list(columns),
        "data": data,
        "total": len(data),
    }


def query_decision_result_simulation(
    projectname: str,
    batchname: str,
) -> Dict[str, Any]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT *
                FROM `{DECISION_RESULT_TABLE}`
                WHERE `projectname` = %s AND `batchname` = %s
                ORDER BY `timestamp` DESC
                """,
                (projectname, batchname),
            )
            rows = cursor.fetchall()

    results = []
    for row in rows:
        item = {key: _serialize_row_value(value) for key, value in row.items()}
        results.append(item)

    return {
        "results": results,
        "total": len(results),
    }


import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import csv
import threading

class MySQLBrowser:
    def __init__(self, root):
        self.root = root
        self.root.title("MySQL 数据库浏览器 - 完整数据查看")
        self.root.geometry("1100x750")

        db = load_db_config()
        self.host = db["DB_HOST"]
        self.port = int(db["DB_PORT"])
        self.user = db["DB_USER"]
        self.password = db["DB_PASSWORD"]

        # 尝试连接，指定字符集
        try:
            self.conn = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                charset='utf8mb4',
                use_unicode=True
            )
            self.cursor = self.conn.cursor()
        except Exception as e:
            messagebox.showerror("连接错误", f"无法连接数据库：{e}\n请检查主机、端口、用户名和密码。")
            self.root.destroy()
            return

        # ---------- 左侧：数据库树 ----------
        left_frame = ttk.Frame(self.root, width=300)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)
        left_frame.pack_propagate(False)

        self.tree = ttk.Treeview(left_frame, show="tree")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        left_scroll = ttk.Scrollbar(left_frame, orient="vertical", command=self.tree.yview)
        left_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=left_scroll.set)

        # ---------- 右侧：数据展示区 ----------
        right_frame = ttk.Frame(self.root)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # 顶部工具栏（按钮 + 搜索框 + 排序控件）
        toolbar = ttk.Frame(right_frame)
        toolbar.pack(fill=tk.X, padx=5, pady=5)

        self.export_btn = ttk.Button(toolbar, text="导出当前表为 CSV", command=self.export_to_csv, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=2)

        self.refresh_btn = ttk.Button(toolbar, text="刷新数据", command=self.refresh_current_table, state=tk.DISABLED)
        self.refresh_btn.pack(side=tk.LEFT, padx=2)

        # 新增：复制整个表格按钮
        self.copy_all_btn = ttk.Button(toolbar, text="复制整个表格", command=self.copy_all_table, state=tk.DISABLED)
        self.copy_all_btn.pack(side=tk.LEFT, padx=2)

        # 搜索功能
        ttk.Label(toolbar, text="搜索:").pack(side=tk.LEFT, padx=(10, 2))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=20)
        self.search_entry.pack(side=tk.LEFT, padx=2)
        self.search_btn = ttk.Button(toolbar, text="查找", command=self.apply_filter, state=tk.DISABLED)
        self.search_btn.pack(side=tk.LEFT, padx=2)
        self.clear_filter_btn = ttk.Button(toolbar, text="清除过滤", command=self.clear_filter, state=tk.DISABLED)
        self.clear_filter_btn.pack(side=tk.LEFT, padx=2)

        # 排序控件
        ttk.Label(toolbar, text="排序:").pack(side=tk.LEFT, padx=(10, 2))
        self.sort_column_var = tk.StringVar()
        self.sort_column_combo = ttk.Combobox(toolbar, textvariable=self.sort_column_var, width=15, state="readonly")
        self.sort_column_combo.pack(side=tk.LEFT, padx=2)
        self.sort_order_var = tk.StringVar(value="升序")
        self.sort_order_combo = ttk.Combobox(toolbar, textvariable=self.sort_order_var, values=["升序", "降序"], width=6, state="readonly")
        self.sort_order_combo.pack(side=tk.LEFT, padx=2)
        self.sort_btn = ttk.Button(toolbar, text="应用排序", command=self.apply_sort, state=tk.DISABLED)
        self.sort_btn.pack(side=tk.LEFT, padx=2)

        # 表格框架
        table_frame = ttk.Frame(right_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 创建表格（Treeview）和滚动条
        self.table = ttk.Treeview(table_frame, show="headings")
        v_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        h_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)

        self.table.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        # 右键菜单
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="复制单元格内容", command=self.copy_cell)
        self.context_menu.add_command(label="复制整行（制表符分隔）", command=self.copy_row)
        self.table.bind("<Button-3>", self.show_context_menu)

        # 状态栏
        self.status_label = ttk.Label(right_frame, text="请双击左侧表名查看完整数据", relief=tk.SUNKEN, anchor=tk.W)
        self.status_label.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)

        # 记录当前打开的数据库和表
        self.current_db = None
        self.current_table = None
        self.original_data = None   # 所有原始数据（列表的列表）
        self.original_columns = None
        self.filtered_data = None   # 过滤后的数据
        self.current_filter = ""    # 当前搜索词

        # 绑定事件
        self.tree.bind("<<TreeviewOpen>>", self.on_expand)
        self.tree.bind("<Double-1>", self.on_double_click)

        # 加载所有数据库
        self.load_databases()

    # ---------- 数据库树相关 ----------
    def load_databases(self):
        try:
            self.cursor.execute("SHOW DATABASES")
            databases = self.cursor.fetchall()
            for (db_name,) in databases:
                node = self.tree.insert("", tk.END, text=f"{db_name} (加载中...)")
                self.tree.insert(node, tk.END, text="loading...")
        except Exception as e:
            messagebox.showerror("错误", f"加载数据库失败：{e}")

    def load_tables(self, db_name, db_node):
        children = self.tree.get_children(db_node)
        for child in children:
            self.tree.delete(child)

        try:
            self.cursor.execute(f"USE `{db_name}`")
            self.cursor.execute("SHOW TABLES")
            tables = self.cursor.fetchall()
            table_names = [t[0] for t in tables]
            for table_name in table_names:
                self.tree.insert(db_node, tk.END, text=table_name)
            self.tree.item(db_node, text=f"{db_name} ({len(table_names)} 张表)")
        except Exception as e:
            messagebox.showerror("错误", f"加载表失败：{e}")
            self.tree.item(db_node, text=db_name)

    def on_expand(self, event):
        node = self.tree.focus()
        if not node:
            return
        parent = self.tree.parent(node)
        if parent == "":
            children = self.tree.get_children(node)
            if len(children) == 1 and self.tree.item(children[0], "text") == "loading...":
                db_name = self.tree.item(node, "text").split(" ")[0]
                self.load_tables(db_name, node)

    def on_double_click(self, event):
        node = self.tree.focus()
        if not node:
            return
        parent = self.tree.parent(node)
        if parent != "":
            db_name = self.tree.item(parent, "text").split(" ")[0]
            table_name = self.tree.item(node, "text")
            self.load_full_table(db_name, table_name)

    # ---------- 数据加载与展示 ----------
    def load_full_table(self, db_name, table_name):
        self.current_db = db_name
        self.current_table = table_name
        self.status_label.config(text=f"正在加载 {db_name}.{table_name} 的全部数据，请稍候...")
        self.export_btn.config(state=tk.DISABLED)
        self.refresh_btn.config(state=tk.DISABLED)
        self.copy_all_btn.config(state=tk.DISABLED)
        self.search_btn.config(state=tk.DISABLED)
        self.clear_filter_btn.config(state=tk.DISABLED)
        self.sort_btn.config(state=tk.DISABLED)

        # 清空表格和过滤状态
        self.table.delete(*self.table.get_children())
        self.table["columns"] = ()
        self.original_data = None
        self.original_columns = None
        self.filtered_data = None
        self.current_filter = ""
        self.search_var.set("")
        self.sort_column_combo.set('')
        self.sort_column_combo['values'] = []

        def load_task():
            try:
                self.cursor.execute(f"USE `{db_name}`")
                # 获取列名
                self.cursor.execute(f"DESCRIBE `{table_name}`")
                columns = [col[0] for col in self.cursor.fetchall()]
                # 获取所有数据
                self.cursor.execute(f"SELECT * FROM `{table_name}`")
                rows = self.cursor.fetchall()
                # 转换为列表的列表
                data = [list(row) for row in rows]
                # 在主线程中更新
                self.root.after(0, self.display_table, columns, data, len(rows))
            except Exception as e:
                self.root.after(0, self.show_load_error, str(e))

        threading.Thread(target=load_task, daemon=True).start()

    def display_table(self, columns, data, row_count):
        self.original_columns = columns
        self.original_data = data
        self.filtered_data = data.copy()   # 初始无过滤
        self.current_filter = ""

        # 更新排序下拉框的选项
        self.sort_column_combo['values'] = columns
        if columns:
            self.sort_column_combo.current(0)  # 默认第一列

        # 配置表格列
        self.table["columns"] = columns
        for col in columns:
            self.table.heading(col, text=col)
            self.table.column(col, width=120, anchor="w")

        # 填充数据
        self._populate_table(self.filtered_data)

        self.status_label.config(text=f"当前表：{self.current_db}.{self.current_table}  总行数：{row_count}")
        self.export_btn.config(state=tk.NORMAL)
        self.refresh_btn.config(state=tk.NORMAL)
        self.copy_all_btn.config(state=tk.NORMAL)
        self.search_btn.config(state=tk.NORMAL)
        self.clear_filter_btn.config(state=tk.NORMAL)
        self.sort_btn.config(state=tk.NORMAL)

    def _populate_table(self, data):
        """根据给定的数据（列表的列表）刷新表格内容"""
        self.table.delete(*self.table.get_children())
        for i, row in enumerate(data):
            values = [str(v) if v is not None else "" for v in row]
            self.table.insert("", tk.END, values=values, iid=str(i))

    def show_load_error(self, err_msg):
        messagebox.showerror("加载失败", f"无法加载表数据：{err_msg}")
        self.status_label.config(text="加载失败，请重试或检查表是否存在")
        self.export_btn.config(state=tk.DISABLED)
        self.refresh_btn.config(state=tk.DISABLED)
        self.copy_all_btn.config(state=tk.DISABLED)
        self.search_btn.config(state=tk.DISABLED)
        self.clear_filter_btn.config(state=tk.DISABLED)
        self.sort_btn.config(state=tk.DISABLED)

    # ---------- 搜索过滤 ----------
    def apply_filter(self):
        """根据用户输入的关键词过滤数据"""
        keyword = self.search_var.get().strip()
        if not keyword:
            self.clear_filter()
            return

        self.current_filter = keyword
        filtered = []
        for row in self.original_data:
            row_text = " ".join(str(v) for v in row if v is not None)
            if keyword.lower() in row_text.lower():
                filtered.append(row)
        self.filtered_data = filtered
        # 过滤后重新排序（保持当前排序设置）
        self._reapply_sort_if_needed()
        self.status_label.config(
            text=f"当前表：{self.current_db}.{self.current_table}  过滤后行数：{len(filtered)} / {len(self.original_data)}"
        )

    def clear_filter(self):
        """清除过滤，恢复显示所有数据"""
        self.current_filter = ""
        self.search_var.set("")
        self.filtered_data = self.original_data.copy() if self.original_data else []
        self._reapply_sort_if_needed()
        total = len(self.original_data) if self.original_data else 0
        self.status_label.config(
            text=f"当前表：{self.current_db}.{self.current_table}  总行数：{total}"
        )

    # ---------- 排序功能 ----------
    def apply_sort(self):
        """手动应用排序"""
        self._reapply_sort_if_needed()
        # 状态栏提示
        col_name = self.sort_column_var.get()
        order = self.sort_order_var.get()
        if col_name:
            self.status_label.config(text=f"已按 {col_name} {order} 排序")

    def _reapply_sort_if_needed(self):
        """根据当前选择的列和顺序对 filtered_data 进行排序，并刷新表格"""
        if not self.filtered_data or not self.original_columns:
            return
        col_name = self.sort_column_var.get()
        if not col_name or col_name not in self.original_columns:
            return
        col_index = self.original_columns.index(col_name)
        reverse = (self.sort_order_var.get() == "降序")

        try:
            # 尝试按数值或日期排序，否则按字符串排序
            # 先获取该列的所有值，判断类型
            sample_values = [row[col_index] for row in self.filtered_data if row[col_index] is not None]
            if not sample_values:
                # 全部为 None，按字符串排序（但会出错，直接按字符串）
                sort_key = lambda row: str(row[col_index]) if row[col_index] is not None else ''
            else:
                # 检查是否为数字或日期
                first = sample_values[0]
                if isinstance(first, (int, float)):
                    sort_key = lambda row: row[col_index] if row[col_index] is not None else (0 if isinstance(first, int) else 0.0)
                elif hasattr(first, 'strftime'):  # datetime 对象
                    sort_key = lambda row: row[col_index] if row[col_index] is not None else ''
                else:
                    sort_key = lambda row: str(row[col_index]) if row[col_index] is not None else ''
            self.filtered_data.sort(key=sort_key, reverse=reverse)
        except Exception as e:
            # 如果排序失败（比如混合类型），回退到字符串排序
            sort_key = lambda row: str(row[col_index]) if row[col_index] is not None else ''
            self.filtered_data.sort(key=sort_key, reverse=reverse)

        self._populate_table(self.filtered_data)

    # ---------- 复制功能 ----------
    def show_context_menu(self, event):
        row_id = self.table.identify_row(event.y)
        col_id = self.table.identify_column(event.x)
        if not row_id or not col_id:
            return
        self.context_menu.row_id = row_id
        self.context_menu.col_id = col_id
        self.context_menu.post(event.x_root, event.y_root)

    def copy_cell(self):
        import pyperclip
        row_id = self.context_menu.row_id
        col_id = self.context_menu.col_id
        col_index = int(col_id[1:]) - 1 if col_id.startswith('#') else None
        if col_index is None:
            return
        item = self.table.item(row_id)
        values = item['values']
        if col_index < len(values):
            cell_text = values[col_index]
            try:
                pyperclip.copy(cell_text)
                self.status_label.config(text=f"已复制：{cell_text[:50]}{'...' if len(cell_text)>50 else ''}")
            except:
                messagebox.showerror("复制失败", "无法复制到剪贴板，请检查 pyperclip 是否安装。")

    def copy_row(self):
        import pyperclip
        row_id = self.context_menu.row_id
        item = self.table.item(row_id)
        values = item['values']
        row_text = "\t".join(values)
        try:
            pyperclip.copy(row_text)
            self.status_label.config(text=f"已复制整行，共 {len(values)} 列")
        except:
            messagebox.showerror("复制失败", "无法复制到剪贴板，请检查 pyperclip 是否安装。")

    def copy_all_table(self):
        import pyperclip
        """复制整个当前显示的表格（包括列头）"""
        if not self.filtered_data or not self.original_columns:
            messagebox.showwarning("无数据", "当前没有可复制的表数据")
            return
        # 构建列头行
        header = "\t".join(self.original_columns)
        # 构建数据行
        rows = []
        for row in self.filtered_data:
            rows.append("\t".join(str(v) if v is not None else "" for v in row))
        all_text = header + "\n" + "\n".join(rows)
        try:
            pyperclip.copy(all_text)
            self.status_label.config(text=f"已复制整个表格（{len(self.filtered_data)} 行，{len(self.original_columns)} 列）")
        except:
            messagebox.showerror("复制失败", "无法复制到剪贴板，请检查 pyperclip 是否安装。")

    # ---------- 其他功能 ----------
    def refresh_current_table(self):
        if self.current_db and self.current_table:
            self.load_full_table(self.current_db, self.current_table)

    def export_to_csv(self):
        if not self.original_data or not self.original_columns:
            messagebox.showwarning("无数据", "当前没有可导出的表数据")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV文件", "*.csv"), ("所有文件", "*.*")],
            initialfile=f"{self.current_table}.csv"
        )
        if not file_path:
            return

        try:
            with open(file_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(self.original_columns)
                writer.writerows(self.original_data)
            messagebox.showinfo("导出成功", f"数据已导出到：{file_path}\n共 {len(self.original_data)} 行。")
        except Exception as e:
            messagebox.showerror("导出失败", f"保存文件时出错：{e}")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    root = tk.Tk()
    app = MySQLBrowser(root)
    app.run()
"""Windows GUI for NetConfigCollector."""
import csv
import os
import queue
import sys
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from inventory import Inventory

from collector import COLUMNS, PROFILES, collect, device_from_row, read_inventory, write_summary

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NetConfigCollector"


class App:
    def __init__(self, root, library_path=None):
        self.root = root
        self.library = Inventory(library_path if library_path is not None else STATE / 'inventory.json')
        self.project_credentials = {}
        self.active_project = None
        root.title("设备配置采集器 1.4.0")
        root.geometry("1180x880")
        root.minsize(1020, 760)
        root.configure(background="#f3f6fb")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#f3f6fb", foreground="#23324a")
        style.configure("TFrame", background="#f3f6fb")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f3f6fb")
        style.configure("Card.TLabel", background="#ffffff")
        style.configure("Muted.TLabel", foreground="#738197")
        style.configure("CardMuted.TLabel", background="#ffffff", foreground="#738197")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 23, "bold"), foreground="#182b49")
        style.configure("Section.TLabel", background="#ffffff", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("TButton", background="#ffffff", bordercolor="#dbe3ef", lightcolor="#ffffff",
                        darkcolor="#ffffff", borderwidth=1, padding=(12, 7))
        style.map("TButton", background=[("active", "#eef4ff"), ("disabled", "#f5f7fa")],
                  foreground=[("disabled", "#a3adbc")])
        style.configure("Primary.TButton", background="#2964df", foreground="#ffffff",
                        bordercolor="#2964df", lightcolor="#2964df", darkcolor="#2964df")
        style.map("Primary.TButton", background=[("disabled", "#b7c9ed"), ("active", "#1e50bd")],
                  foreground=[("disabled", "#edf2fc"), ("!disabled", "#ffffff")])
        style.configure("TEntry", fieldbackground="#ffffff", bordercolor="#dbe3ef", padding=7)
        style.configure("TCombobox", fieldbackground="#ffffff", bordercolor="#dbe3ef", padding=6,
                        arrowsize=13)
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")],
                  selectbackground=[("readonly", "#ffffff")], selectforeground=[("readonly", "#23324a")])
        style.configure("TCheckbutton", background="#ffffff", padding=4)
        style.map("TCheckbutton", background=[("active", "#ffffff")])
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", rowheight=34,
                        borderwidth=0, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#edf2f8", foreground="#63718a",
                        font=("Microsoft YaHei UI", 10, "bold"), padding=(10, 7), relief="flat")
        style.map("Treeview", background=[("selected", "#dce9ff")], foreground=[("selected", "#173d80")])
        style.configure("Horizontal.TProgressbar", background="#2964df", troughcolor="#e5ecf7",
                        borderwidth=0, lightcolor="#2964df", darkcolor="#2964df")
        self.devices = []
        self.credentials = {}
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.last_folder = None
        self.controls = []
        self.details = {}
        self.output = tk.StringVar(value=str(BASE / "backups"))
        self.workers = tk.StringVar(value="4")
        self.timeout = tk.StringVar(value="180")
        self.encoding = tk.StringVar(value="utf-8")
        self.auto_trust = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="添加或导入设备，设置登录凭据后即可开始。")
        self.counts = tk.StringVar(value="0 台设备   ·   成功 0   ·   失败 0")
        self.selection_text = tk.StringVar(value="选中设备后点击“编辑设备”，或直接双击设备行。")
        main = ttk.Frame(root, padding=24)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(header, text="设备配置采集器", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="本地 SSH 采集    /    v1.4.0", style="Muted.TLabel").pack(side="right", anchor="s", pady=8)
        scope = ttk.Frame(main)
        scope.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self.project = tk.StringVar()
        self.area = tk.StringVar()
        for text, variable, attribute in (("项目", self.project, "project_box"), ("区域", self.area, "area_box")):
            ttk.Label(scope, text=text).pack(side="left", padx=(0, 8))
            box = ttk.Combobox(scope, textvariable=variable, width=20, state="readonly")
            box.pack(side="left", padx=(0, 12))
            box.bind("<<ComboboxSelected>>", self.switch_area)
            setattr(self, attribute, box)
            self.controls.append(box)
        for text, kind in (("新建项目", "project"), ("新建区域", "area")):
            button = ttk.Button(scope, text=text, command=lambda kind=kind: self.manage_inventory("create", kind))
            button.pack(side="left", padx=(0, 6))
            self.controls.append(button)
        manage = ttk.Menubutton(scope, text="管理 ▾")
        menu = tk.Menu(manage, tearoff=False)
        for label, action, kind in (("重命名项目", "rename", "project"), ("重命名区域", "rename", "area"),
                                    ("删除项目", "delete", "project"), ("删除区域", "delete", "area")):
            menu.add_command(label=label, command=lambda action=action, kind=kind: self.manage_inventory(action, kind))
        manage.configure(menu=menu)
        manage.pack(side="left", padx=(4, 0))
        self.controls.append(manage)

        inventory = ttk.Frame(main, style="Card.TFrame", padding=16)
        inventory.grid(row=2, column=0, sticky="nsew")
        inventory.columnconfigure(0, weight=1)
        inventory.rowconfigure(2, weight=1)
        heading = ttk.Frame(inventory, style="Card.TFrame")
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(heading, text="设备清单", style="Section.TLabel").pack(side="left")
        ttk.Label(heading, textvariable=self.counts, style="CardMuted.TLabel").pack(side="right")
        bar = ttk.Frame(inventory, style="Card.TFrame")
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for label, command in [
            ("添加设备", self.add_device), ("编辑设备", self.edit_selected),
            ("删除选中", self.remove), ("导入 CSV", self.import_csv),
            ("保存清单", self.export_inventory), ("导出模板", self.template),
        ]:
            button = ttk.Button(bar, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            self.controls.append(button)
        ttk.Button(bar, text="采集详情", command=self.show_detail).pack(side="right")
        table = ttk.Frame(inventory, style="Card.TFrame")
        table.grid(row=2, column=0, sticky="nsew")
        columns = ("name", "host", "platform", "credential", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended", height=6)
        for col, title, width in zip(columns, ("设备名称", "SSH 地址", "设备类型", "凭据组", "采集状态"),
                                     (270, 180, 180, 110, 180)):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=width, minwidth=90, anchor="w")
        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("odd", background="#f7f9fd")
        self.tree.tag_configure("success", foreground="#15805a")
        self.tree.tag_configure("failed", foreground="#c34545")
        self.tree.tag_configure("working", foreground="#2964df")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Double-1>", self.double_click)
        self.tree.bind("<<TreeviewSelect>>", self.selected_changed)
        ttk.Label(inventory, textvariable=self.selection_text, style="CardMuted.TLabel",
                  wraplength=900).grid(row=3, column=0, sticky="w", pady=(10, 0))

        settings = ttk.Frame(main, style="Card.TFrame", padding=16)
        settings.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        row = ttk.Frame(settings, style="Card.TFrame")
        row.pack(fill="x", pady=(0, 10))
        ttk.Label(row, text="采集设置", style="Section.TLabel").pack(side="left")
        button = ttk.Button(row, text="设置登录凭据", command=self.edit_credentials)
        button.pack(side="right")
        self.controls.append(button)
        destination = ttk.Frame(settings, style="Card.TFrame")
        destination.pack(fill="x")
        ttk.Label(destination, text="保存目录", style="Card.TLabel").pack(side="left")
        entry = ttk.Entry(destination, textvariable=self.output)
        entry.pack(side="left", fill="x", expand=True, padx=10)
        self.controls.append(entry)
        browse = ttk.Button(destination, text="选择目录", command=self.browse)
        browse.pack(side="right")
        self.controls.append(browse)
        options = ttk.Frame(settings, style="Card.TFrame")
        options.pack(fill="x", pady=(12, 6))
        for text, variable, values, width in [
            ("并发台数", self.workers, ("1", "2", "4", "8"), 5),
            ("命令超时 / 秒", self.timeout, ("60", "180", "300", "600", "900"), 6),
            ("文字编码", self.encoding, ("utf-8", "gb18030"), 10),
        ]:
            ttk.Label(options, text=text, style="Card.TLabel").pack(side="left", padx=(0, 8))
            box = ttk.Combobox(options, textvariable=variable, values=values, width=width, state="readonly")
            box.pack(side="left", padx=(0, 22))
            self.controls.append(box)
        trust = ttk.Checkbutton(settings, text="首次连接自动信任 SSH 指纹；已知指纹变化时仍拒绝连接",
                                variable=self.auto_trust)
        trust.pack(anchor="w")
        self.controls.append(trust)
        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        self.start_button = ttk.Button(actions, text="采集当前区域", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.controls.append(self.start_button)
        self.retry_button = ttk.Button(actions, text="重试失败设备", command=self.retry_failed)
        self.retry_button.pack(side="left", padx=(8, 0))
        self.controls.append(self.retry_button)
        self.stop_button = ttk.Button(actions, text="停止采集", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="打开结果目录", command=self.open_results).pack(side="right")
        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.grid(row=5, column=0, sticky="ew", pady=(14, 8))
        ttk.Label(main, textvariable=self.status, wraplength=960).grid(row=6, column=0, sticky="w")
        ttk.Label(main, text="密码仅保留在本次会话中  ·  采集文件可能包含设备密钥，请妥善保管",
                  style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=(8, 0))
        self.load_area()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)


    def load_area(self):
        project, area = self.library.selected
        if self.active_project is not None:
            self.project_credentials[self.active_project] = self.credentials
        self.credentials = self.project_credentials.setdefault(project, {})
        self.active_project = project
        self.project.set(project)
        self.area.set(area)
        self.project_box.configure(values=list(self.library.data["projects"]))
        self.area_box.configure(values=list(self.library.data["projects"][project]))
        self.devices = self.library.devices()
        self.last_folder = None
        self.progress.configure(value=0)
        self.refresh()
        self.status.set(f"{project} / {area}：{len(self.devices)} 台设备；设备信息自动保存。")

    def persist_devices(self, devices):
        try:
            if devices != self.library.devices():
                self.library.save_devices(devices)
        except (OSError, ValueError) as exc:
            raise ValueError(f"保存设备库失败，修改未生效：{exc}") from exc

    def switch_area(self, event=None):
        if self.running:
            self.project.set(self.library.selected[0])
            self.area.set(self.library.selected[1])
            return
        project = self.project.get()
        old_project, old_area = self.library.selected
        area = (next(iter(self.library.data["projects"][project])) if project != old_project else self.area.get())
        try:
            self.persist_devices(self.devices)
            self.library.select(project, area)
        except (OSError, ValueError) as exc:
            self.project.set(old_project)
            self.area.set(old_area)
            messagebox.showerror("切换失败", str(exc), parent=self.root)
            return
        self.load_area()

    def manage_inventory(self, action, kind):
        if self.running:
            return
        project, area = self.library.selected
        label = "项目" if kind == "project" else "区域"
        name = project if kind == "project" else area
        if action == "delete":
            if not messagebox.askyesno("删除" + label,
                    f"删除{label}“{name}”及其中保存的设备清单？\n已有采集文件不会删除。",
                    parent=self.root):
                return
            value = None
        else:
            value = simpledialog.askstring("新建" + label if action == "create" else "重命名" + label,
                                           label + "名称：",
                                           initialvalue=name if action == "rename" else "",
                                           parent=self.root)
            if value is None:
                return
        try:
            self.persist_devices(self.devices)
            if action == "delete":
                self.library.delete(kind)
            else:
                getattr(self.library, action)(kind, value)
        except (OSError, ValueError) as exc:
            messagebox.showerror("操作失败", str(exc), parent=self.root)
            return
        if kind == "project" and action in ("rename", "delete"):
            self.project_credentials.pop(project, None)
            if action == "rename":
                self.project_credentials[self.library.selected[0]] = self.credentials
            self.active_project = None
        self.load_area()


    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.details.clear()
        for i, device in enumerate(self.devices):
            self.tree.insert("", "end", iid=str(i), values=(device.name, f"{device.host}:{device.port}",
                             device.platform, device.credential, "待采集"),
                             tags=("odd" if i % 2 else "even",))
        self.update_counts()
        self.selected_changed()
        self.status.set(f"已加载 {len(self.devices)} 台设备；已设置 {len(self.credentials)} 个凭据组。")

    def import_csv(self):
        filename = filedialog.askopenfilename(filetypes=[("CSV 设备清单", "*.csv")])
        if not filename:
            return
        try:
            devices = read_inventory(filename)
            if self.devices and not messagebox.askyesno("替换当前区域清单",
                    f"将替换“{self.project.get()} / {self.area.get()}”的设备清单，继续吗？", parent=self.root):
                return
            self.persist_devices(devices)
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        self.devices = devices
        self.refresh()

    def template(self):
        filename = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="devices.csv",
                                               filetypes=[("CSV", "*.csv")])
        if not filename:
            return
        try:
            with open(filename, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=COLUMNS)
                writer.writeheader()
                for i, (name, platform) in enumerate([
                    ("NE-01", "Huawei NE"), ("AR-01", "Huawei AR"), ("CE-01", "Huawei CE"),
                    ("USG-01", "Huawei USG"), ("H3C-01", "H3C Comware"), ("K1280-A", "Hillstone StoneOS"),
                    ("K5680-A", "Hillstone StoneOS"),
                ], 1):
                    writer.writerow(dict(name=name, host=f"192.0.2.{i}", platform=platform,
                                         port=22, credential="default"))
            messagebox.showinfo("模板已保存", "示例使用文档专用地址，请替换为真实设备地址。")
        except OSError as exc:
            messagebox.showerror("写入失败", str(exc))

    def add_device(self):
        self.device_dialog()

    def edit_selected(self):
        if self.running:
            return
        selected = self.tree.selection()
        if len(selected) != 1:
            messagebox.showinfo("编辑设备", "请先选中一台设备。")
            return
        self.device_dialog(int(selected[0]))

    def save_device(self, values, index=None):
        if self.running:
            raise ValueError("采集期间不能修改设备，请先停止采集。")
        device = device_from_row(values)
        if any(i != index and d.host.lower() == device.host.lower() and d.port == device.port
               for i, d in enumerate(self.devices)):
            raise ValueError("设备地址和端口已存在")
        updated = list(self.devices)
        if index is None:
            updated.append(device)
        else:
            updated[index] = device
        self.persist_devices(updated)
        previous_status = self.tree.set(str(index), "status") if index is not None else ""
        if index is None:
            index = len(self.devices)
            self.devices.append(device)
            self.tree.insert("", "end", iid=str(index))
        else:
            self.devices[index] = device
        self.tree.item(str(index), values=(device.name, f"{device.host}:{device.port}",
                       device.platform, device.credential,
                       "待重试" if previous_status in ("失败", "待重试") else "待采集"),
                       tags=("odd" if index % 2 else "even",))
        self.details.pop(str(index), None)
        self.tree.selection_set(str(index))
        self.tree.see(str(index))
        self.update_counts()
        self.selected_changed()
        self.status.set(f"已自动保存到 {self.project.get()} / {self.area.get()}。")

    def show_dialog(self, dialog):
        """Position the completed dialog before showing it."""
        dialog.update_idletasks()
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            parent = user32.GetAncestor(self.root.winfo_id(), 2)
            window = user32.GetAncestor(dialog.winfo_id(), 2)
            parent_rect, rect = wintypes.RECT(), wintypes.RECT()
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if (user32.GetWindowRect(parent, ctypes.byref(parent_rect)) and
                    user32.GetWindowRect(window, ctypes.byref(rect)) and
                    user32.GetMonitorInfoW(user32.MonitorFromWindow(parent, 2), ctypes.byref(info))):
                width, height = rect.right - rect.left, rect.bottom - rect.top
                x = (parent_rect.left + parent_rect.right - width) // 2
                y = (parent_rect.top + parent_rect.bottom - height) // 2
                x = max(info.work.left, min(x, info.work.right - width))
                y = max(info.work.top, min(y, info.work.bottom - height))
                user32.SetWindowPos(window, None, x, y, 0, 0, 0x0015)
        else:
            x = max(0, self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2)
            y = max(0, self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_reqheight()) // 2)
            dialog.geometry(f"+{x}+{y}")
        dialog.deiconify()
        dialog.lift(self.root)
        dialog.grab_set()
        dialog.focus_set()

    def device_dialog(self, index=None):
        if self.running:
            return
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("编辑设备" if index is not None else "添加设备")
        dialog.resizable(False, False)
        dialog.configure(background="#ffffff")
        dialog.transient(self.root)
        panel = ttk.Frame(dialog, style="Card.TFrame", padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="编辑设备" if index is not None else "添加设备",
                  style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        initial = (("", "", "Huawei CE", "22", "default") if index is None else
                   tuple(str(getattr(self.devices[index], key)) for key in COLUMNS))
        values = {key: tk.StringVar(value=value) for key, value in zip(COLUMNS, initial)}
        for i, (key, title) in enumerate(zip(COLUMNS, ("名称", "IP / 主机名", "设备类型", "SSH 端口", "凭据组")), 1):
            ttk.Label(panel, text=title, style="Card.TLabel").grid(row=i, column=0, pady=8, padx=(0, 18), sticky="w")
            widget = (ttk.Combobox(panel, textvariable=values[key], values=list(PROFILES), state="readonly", width=30)
                      if key == "platform" else ttk.Entry(panel, textvariable=values[key], width=32))
            widget.grid(row=i, column=1, pady=8, sticky="ew")
        def save():
            try:
                self.save_device({key: var.get() for key, var in values.items()}, index)
            except ValueError as exc:
                messagebox.showerror("输入错误", str(exc), parent=dialog)
                return
            dialog.destroy()
        buttons = ttk.Frame(panel, style="Card.TFrame")
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="保存修改" if index is not None else "添加设备",
                   style="Primary.TButton", command=save).pack(side="left")
        dialog.bind("<Escape>", lambda event: dialog.destroy())
        dialog.bind("<Return>", lambda event: save())
        self.show_dialog(dialog)

    def double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return "break"
        self.tree.selection_set(item)
        if self.running or self.tree.identify_column(event.x) == "#5":
            self.show_detail()
        else:
            self.edit_selected()
        return "break"

    def selected_changed(self, event=None):
        selected = self.tree.selection()
        if len(selected) == 1:
            item = selected[0]
            device = self.devices[int(item)]
            detail = self.details.get(item, "")
            self.selection_text.set(detail.split("\n")[0] if detail else
                                    f"{device.name}  ·  {device.platform}  ·  双击编辑；双击状态列查看采集详情。")
        else:
            self.selection_text.set(f"已选中 {len(selected)} 台设备" if selected else
                                    "选中设备后点击“编辑设备”，或直接双击设备行。")

    def update_counts(self):
        statuses = [self.tree.set(item, "status") for item in self.tree.get_children()]
        self.counts.set(f"{len(self.devices)} 台设备   ·   成功 {statuses.count('成功')}   ·   失败 {statuses.count('失败')}")

    def export_inventory(self):
        if self.running:
            return
        if not self.devices:
            messagebox.showinfo("保存清单", "请先添加或导入设备。")
            return
        filename = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="devices.csv",
                                               filetypes=[("CSV 设备清单", "*.csv")])
        if not filename:
            return
        try:
            with open(filename, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=COLUMNS)
                writer.writeheader()
                for device in self.devices:
                    writer.writerow({key: getattr(device, key) for key in COLUMNS})
            self.status.set("当前设备清单已保存，不包含登录密码。")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))


    def remove(self):
        selected = {int(item) for item in self.tree.selection()}
        if self.running or not selected:
            return
        devices = [d for i, d in enumerate(self.devices) if i not in selected]
        try:
            self.persist_devices(devices)
        except ValueError as exc:
            messagebox.showerror("删除失败", str(exc), parent=self.root)
            return
        self.devices = devices
        self.refresh()

    def edit_credentials(self):
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("登录凭据 · 仅本次运行有效")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        group = tk.StringVar(value="default")
        username = tk.StringVar()
        password = tk.StringVar()
        groups = sorted({"default"} | set(self.credentials) | {d.credential for d in self.devices})
        for i, (title, var) in enumerate((("凭据组", group), ("用户名", username), ("密码", password))):
            ttk.Label(dialog, text=title).grid(row=i, column=0, padx=12, pady=8, sticky="w")
            if i == 0:
                widget = ttk.Combobox(dialog, textvariable=var, values=groups)
                widget.bind("<<ComboboxSelected>>", lambda e: load())
            else:
                widget = ttk.Entry(dialog, textvariable=var, show="*" if i == 2 else "")
            widget.grid(row=i, column=1, padx=12, pady=8)
        def load():
            user, secret = self.credentials.get(group.get(), ("", ""))
            username.set(user)
            password.set(secret)
        def save():
            if not group.get().strip() or not username.get().strip() or not password.get():
                messagebox.showerror("输入错误", "凭据组、用户名和密码均须填写。", parent=dialog)
                return
            self.credentials[group.get().strip()] = (username.get().strip(), password.get())
            password.set("")
            self.status.set(f"已设置 {len(self.credentials)} 个凭据组。")
            dialog.destroy()
        load()
        ttk.Label(dialog, text="不同账号使用不同凭据组，在设备清单中指定。").grid(
            row=3, column=0, columnspan=2, padx=12, pady=8)
        ttk.Button(dialog, text="保存到本次会话", command=save).grid(row=4, column=1, padx=12, pady=12)
        self.show_dialog(dialog)

    def browse(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output.set(folder)

    def show_detail(self, event=None):
        selected = self.tree.selection()
        if selected and selected[0] in self.details:
            messagebox.showinfo("采集详情", self.details[selected[0]])
        else:
            messagebox.showinfo("采集详情", "请选择已完成采集的设备。")

    def set_running(self, running):
        self.running = running
        for control in self.controls:
            control.configure(state="disabled" if running else
                              ("readonly" if isinstance(control, ttk.Combobox) else "normal"))
        self.stop_button.configure(state="normal" if running else "disabled")

    def retry_failed(self):
        self.start(retry_failed=True)

    def start(self, retry_failed=False):
        if self.running:
            return
        jobs = [(i, device) for i, device in enumerate(self.devices)
                if not retry_failed or self.tree.set(str(i), "status") in ("失败", "待重试")]
        if retry_failed and not jobs:
            messagebox.showinfo("无需重试", "当前没有失败或待重试的设备。")
            return
        if not self.devices:
            messagebox.showerror("无法开始", "请先导入清单或添加设备。")
            return
        missing = sorted({d.credential for _, d in jobs} - set(self.credentials))
        if missing:
            messagebox.showerror("缺少凭据", "请在“登录凭据”设置以下组：\n" + "、".join(missing))
            return
        try:
            if not self.output.get().strip():
                raise ValueError("请选择保存目录")
            self.persist_devices(self.devices)
            project, area = self.library.selected
            folder = Path(self.output.get()) / project / area / (("retry_" if retry_failed else "") +
                                              datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
            folder.mkdir(parents=True, exist_ok=False)
            (folder / ".write_test").write_text("ok", encoding="utf-8")
            (folder / ".write_test").unlink()
            STATE.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            messagebox.showerror("保存目录不可用", str(exc))
            return
        self.last_folder = folder
        self.cancel.clear()
        if not retry_failed:
            self.refresh()
        for i, _ in jobs:
            self.tree.set(str(i), "status", "待采集")
            self.details.pop(str(i), None)
        self.batch_total = len(jobs)
        self.update_counts()
        self.selected_changed()
        self.progress.configure(maximum=len(jobs), value=0)
        self.set_running(True)
        self.status.set("正在连接。首次指纹自动信任。" if self.auto_trust.get() else "正在连接。首次指纹需要手动确认。")
        args = (jobs, dict(self.credentials), folder,
                int(self.workers.get()), int(self.timeout.get()), self.encoding.get(), self.auto_trust.get())
        threading.Thread(target=self.batch, args=args, daemon=True).start()

    def confirm_key(self, host, algorithm, fingerprint):
        answer = []
        ready = threading.Event()
        self.events.put(("trust", (host, algorithm, fingerprint, answer, ready)))
        while not ready.wait(0.1):
            if self.cancel.is_set():
                return False
        return bool(answer and answer[0]) and not self.cancel.is_set()

    def batch(self, jobs, credentials, folder, workers, timeout, encoding, auto_trust):
        results = []
        error = ""
        confirm = (lambda *args: True) if auto_trust else self.confirm_key
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                def task(i, device):
                    self.events.put(("status", (i, "准备连接")))
                    return collect(device, credentials[device.credential], folder, i + 1,
                                   STATE / "known_hosts", confirm, self.cancel, timeout, encoding,
                                   on_status=lambda value: self.events.put(("status", (i, value))))
                futures = {pool.submit(task, i, device): i for i, device in jobs}
                for future in as_completed(futures):
                    i = futures[future]
                    result = future.result()
                    results.append((i, result))
                    self.events.put(("result", (i, result, folder)))
                    write_summary(folder, [r for _, r in sorted(results)])
        except Exception as exc:
            error = f"任务或汇总写入失败：{type(exc).__name__}；请检查已保存的单台文件"
        finally:
            credentials.clear()
            self.events.put(("done", (results, error)))

    def poll(self):
        try:
            for _ in range(100):
                kind, payload = self.events.get_nowait()
                if kind == "trust":
                    host, algorithm, fingerprint, answer, ready = payload
                    accepted = False
                    if not self.cancel.is_set():
                        accepted = messagebox.askyesno(
                            "首次连接：核对 SSH 主机指纹",
                            f"设备：{host}\n算法：{algorithm}\n\n{fingerprint}\n\n"
                            "请与设备管理员或已有可信记录核对。\n是否信任并保存此指纹？",
                            parent=self.root)
                    answer.append(accepted)
                    ready.set()
                elif kind == "status":
                    i, status = payload
                    self.tree.set(str(i), "status", status)
                    self.tree.item(str(i), tags=("odd" if i % 2 else "even", "working"))
                elif kind == "result":
                    i, result, folder = payload
                    self.tree.set(str(i), "status", result["status"])
                    self.details[str(i)] = result["message"] + "\n文件：" + (str(folder / result["file"]) if result["file"] else "无")
                    self.tree.item(str(i), tags=("odd" if i % 2 else "even",
                                   "success" if result["status"] == "成功" else "failed"))
                    self.update_counts()
                    self.selected_changed()
                    self.progress.configure(value=float(self.progress["value"]) + 1)
                    self.status.set(f"{result['name']}：{result['status']} · {result['message']}")
                elif kind == "done":
                    results, error = payload
                    successes = sum(r["status"] == "成功" for _, r in results)
                    self.progress.configure(value=len(results))
                    self.set_running(False)
                    self.status.set(error or f"本次完成：成功 {successes} / 本次 {self.batch_total}；选中设备点击“采集详情”。")
                    if error:
                        messagebox.showerror("任务异常", error)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def stop(self):
        self.cancel.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止；已建立会话会中断，连接 / 认证阶段需等待网络超时。")

    def open_results(self):
        folder = self.last_folder or Path(self.output.get())
        if folder.is_dir():
            os.startfile(str(folder))
        else:
            messagebox.showinfo("尚无结果", "完成采集后可打开结果目录。")

    def close(self):
        if self.running:
            self.stop()
            messagebox.showinfo("正在停止", "任务停止后请再次关闭窗口。已完成文件会保留。")
            return
        self.credentials.clear()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    if "--smoke-test" in sys.argv:
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            app = App(root, Path(temporary) / "inventory.json")
            root.update()
            root.destroy()
    else:
        try:
            app = App(root)
        except (OSError, ValueError) as exc:
            root.withdraw()
            messagebox.showerror("无法加载设备库", str(exc), parent=root)
            root.destroy()
        else:
            root.mainloop()



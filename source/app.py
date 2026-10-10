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
from tkinter import filedialog, messagebox, ttk

from collector import COLUMNS, PROFILES, collect, device_from_row, read_inventory, write_summary

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NetConfigCollector"


class App:
    def __init__(self, root):
        self.root = root
        root.title("设备配置采集器 1.3")
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
        ttk.Label(header, text="本地 SSH 采集    /    v1.3", style="Muted.TLabel").pack(side="right", anchor="s", pady=8)
        ttk.Label(main, text="华为 NE / AR / CE / USG     ·     H3C Comware     ·     山石 StoneOS",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 16))

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
        self.start_button = ttk.Button(actions, text="开始采集全部设备", style="Primary.TButton", command=self.start)
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
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)


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
        self.status.set("设备已更新。可点击“保存清单”保存为 CSV，下次直接导入。")

    def device_dialog(self, index=None):
        if self.running:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("编辑设备" if index is not None else "添加设备")
        dialog.resizable(False, False)
        dialog.configure(background="#ffffff")
        dialog.transient(self.root)
        dialog.grab_set()
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
        self.devices = [d for i, d in enumerate(self.devices) if i not in selected]
        self.refresh()

    def edit_credentials(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("登录凭据 · 仅本次运行有效")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
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
            folder = Path(self.output.get()) / (("retry_" if retry_failed else "") +
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
    app = App(root)
    if "--smoke-test" in sys.argv:
        root.update()
        root.destroy()
    else:
        root.mainloop()



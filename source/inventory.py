"""Local project device inventory. Credentials are never stored here."""
import copy
import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from collector import COLUMNS, device_from_row


def valid_name(value):
    if not isinstance(value, str):
        raise ValueError("项目名称必须是文字")
    name = value.strip()
    if (not name or len(name) > 40 or name.endswith(".") or
            re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or
            name.split(".")[0].upper() in
            {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}):
        raise ValueError("名称须为 1–40 个字符，不得含路径符号、末尾句点或 Windows 保留名")
    return name


def unique_name(mapping, name, old=None):
    name = valid_name(name)
    if any(key != old and key.casefold() == name.casefold() for key in mapping):
        raise ValueError("名称已存在")
    return name


class Inventory:
    def __init__(self, path):
        self.path = Path(path)
        self.loaded = self.path.read_bytes() if self.path.exists() else None
        if self.loaded is None:
            self.data = {"version": 2, "selected": "默认项目", "projects": {"默认项目": []}}
        else:
            try:
                self.data = json.loads(self.loaded.decode("utf-8-sig"))
                self.validate(self.data)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise ValueError(f"设备库无法读取，请保留原文件并从备份恢复：{self.path}") from exc
            if self.data["version"] == 1:
                migrated = self.migrate(self.data)
                backup = self.path.with_suffix(".v1.bak")
                if not backup.exists():
                    with backup.open("xb") as stream:
                        stream.write(self.loaded)
                self.commit(migrated)

    @staticmethod
    def validate(data):
        if not isinstance(data, dict) or data.get("version") not in (1, 2):
            raise ValueError("不支持的设备库格式")
        projects = data["projects"]
        if not isinstance(projects, dict) or not projects:
            raise ValueError("设备库至少需要一个项目")
        names = set()
        for project, content in projects.items():
            if valid_name(project) != project or project.casefold() in names:
                raise ValueError("重复或无效项目")
            names.add(project.casefold())
            if data["version"] == 1:
                if not isinstance(content, dict) or not content:
                    raise ValueError("旧版区域格式错误")
                areas = set()
                for area in content:
                    if valid_name(area) != area or area.casefold() in areas:
                        raise ValueError("重复或无效区域")
                    areas.add(area.casefold())
                lists = content.values()
            else:
                lists = [content]
            for rows in lists:
                if not isinstance(rows, list):
                    raise ValueError("设备清单格式错误")
                seen = set()
                for row in rows:
                    if not isinstance(row, dict) or set(row) != set(COLUMNS):
                        raise ValueError("设备字段错误")
                    if any(not isinstance(row[k], str) for k in COLUMNS if k != "port"):
                        raise ValueError("设备字段类型错误")
                    device = device_from_row(row)
                    key = (device.host.casefold(), device.port)
                    if key in seen:
                        raise ValueError("设备地址重复")
                    seen.add(key)
        selected = data["selected"]
        if data["version"] == 1:
            if (not isinstance(selected, list) or len(selected) != 2 or
                    not all(isinstance(x, str) for x in selected) or
                    selected[0] not in projects or selected[1] not in projects[selected[0]]):
                raise ValueError("所选区域不存在")
        elif not isinstance(selected, str) or selected not in projects:
            raise ValueError("所选项目不存在")

    @staticmethod
    def migrate(data):
        projects = {}
        selected = None
        # Reserve existing project names before allocating names for multiple areas.
        reserved = {name.casefold() for name in data["projects"]}
        for project, areas in data["projects"].items():
            for area, rows in areas.items():
                if len(areas) == 1:
                    name = project
                else:
                    base = (project + " - " + area)[:40].rstrip(". ")
                    name, suffix = base, 2
                    while name.casefold() in reserved:
                        tail = f" ({suffix})"
                        name = base[:40 - len(tail)].rstrip(". ") + tail
                        suffix += 1
                reserved.add(name.casefold())
                projects[name] = rows
                if data["selected"] == [project, area]:
                    selected = name
        return {"version": 2, "selected": selected, "projects": projects}

    @property
    def selected(self):
        return self.data["selected"]

    def devices(self):
        return [device_from_row(row) for row in self.data["projects"][self.selected]]

    def commit(self, data):
        self.validate(data)
        current = self.path.read_bytes() if self.path.exists() else None
        if current != self.loaded:
            raise ValueError("设备库已被另一个窗口修改，请重新打开程序后操作")
        content = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="inventory-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if self.loaded is not None:
                backup = self.path.with_suffix(".json.bak")
                backup.write_bytes(self.loaded)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.data = data
        self.loaded = content

    def save_devices(self, devices):
        data = copy.deepcopy(self.data)
        data["projects"][self.selected] = [asdict(device) for device in devices]
        self.commit(data)

    def select(self, project):
        data = copy.deepcopy(self.data)
        data["selected"] = project
        self.commit(data)

    def create(self, name):
        data = copy.deepcopy(self.data)
        name = unique_name(data["projects"], name)
        data["projects"][name] = []
        data["selected"] = name
        self.commit(data)

    def rename(self, name):
        data = copy.deepcopy(self.data)
        name = unique_name(data["projects"], name, self.selected)
        data["projects"] = {name if key == self.selected else key: value
                            for key, value in data["projects"].items()}
        data["selected"] = name
        self.commit(data)

    def delete(self):
        data = copy.deepcopy(self.data)
        if len(data["projects"]) <= 1:
            raise ValueError("至少保留一个项目；可先新建再删除")
        del data["projects"][self.selected]
        data["selected"] = next(iter(data["projects"]))
        self.commit(data)

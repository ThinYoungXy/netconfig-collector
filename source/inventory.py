"""Local project/area device inventory. Credentials are never stored here."""
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
        raise ValueError("项目 / 区域名称必须是文字")
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
            self.data = {"version": 1, "selected": ["默认项目", "默认区域"],
                         "projects": {"默认项目": {"默认区域": []}}}
        else:
            try:
                self.data = json.loads(self.loaded.decode("utf-8-sig"))
                self.validate(self.data)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise ValueError(f"设备库无法读取，请保留原文件并从备份恢复：{self.path}") from exc

    @staticmethod
    def validate(data):
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("不支持的设备库格式")
        projects = data["projects"]
        if not isinstance(projects, dict) or not projects:
            raise ValueError("设备库至少需要一个项目")
        project_names = set()
        for project, areas in projects.items():
            if valid_name(project) != project or project.casefold() in project_names:
                raise ValueError("重复或无效项目")
            project_names.add(project.casefold())
            if not isinstance(areas, dict) or not areas:
                raise ValueError("项目至少需要一个区域")
            area_names = set()
            for area, rows in areas.items():
                if valid_name(area) != area or area.casefold() in area_names:
                    raise ValueError("重复或无效区域")
                area_names.add(area.casefold())
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
                        raise ValueError("区域内设备地址重复")
                    seen.add(key)
        selected = data["selected"]
        if (not isinstance(selected, list) or len(selected) != 2 or
                not all(isinstance(x, str) for x in selected) or
                selected[0] not in projects or selected[1] not in projects[selected[0]]):
            raise ValueError("所选区域不存在")

    @property
    def selected(self):
        return tuple(self.data["selected"])

    def devices(self):
        project, area = self.selected
        return [device_from_row(row) for row in self.data["projects"][project][area]]

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
        project, area = self.selected
        data["projects"][project][area] = [asdict(device) for device in devices]
        self.commit(data)

    def select(self, project, area):
        data = copy.deepcopy(self.data)
        data["selected"] = [project, area]
        self.commit(data)

    def create(self, kind, name):
        data = copy.deepcopy(self.data)
        project, area = self.selected
        if kind == "project":
            name = unique_name(data["projects"], name)
            data["projects"][name] = {"默认区域": []}
            data["selected"] = [name, "默认区域"]
        else:
            name = unique_name(data["projects"][project], name)
            data["projects"][project][name] = []
            data["selected"] = [project, name]
        self.commit(data)

    def rename(self, kind, name):
        data = copy.deepcopy(self.data)
        project, area = self.selected
        mapping = data["projects"] if kind == "project" else data["projects"][project]
        old = project if kind == "project" else area
        name = unique_name(mapping, name, old)
        # Rebuild to preserve display order.
        renamed = {name if key == old else key: value for key, value in mapping.items()}
        if kind == "project":
            data["projects"] = renamed
            data["selected"] = [name, area]
        else:
            data["projects"][project] = renamed
            data["selected"] = [project, name]
        self.commit(data)

    def delete(self, kind):
        data = copy.deepcopy(self.data)
        project, area = self.selected
        mapping = data["projects"] if kind == "project" else data["projects"][project]
        if len(mapping) <= 1:
            raise ValueError("至少保留一个项目和一个区域；可先新建再删除")
        del mapping[project if kind == "project" else area]
        if kind == "project":
            project = next(iter(mapping))
            area = next(iter(mapping[project]))
        else:
            area = next(iter(mapping))
        data["selected"] = [project, area]
        self.commit(data)


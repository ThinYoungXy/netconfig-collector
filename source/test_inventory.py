import gc
import json
import tempfile
import subprocess
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from collector import Device
from inventory import Inventory

DEVICE = Device("CE-01", "192.0.2.1", "Huawei CE")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "inventory.json"
        self.library = Inventory(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_reopen_preserves_devices_and_selection(self):
        self.library.create("project", "项目A")
        self.library.rename("area", "外联区")
        self.library.save_devices([DEVICE])
        restored = Inventory(self.path)
        self.assertEqual(restored.selected, ("项目A", "外联区"))
        self.assertEqual(restored.devices(), [DEVICE])

    def test_areas_isolate_devices_allow_same_ip(self):
        self.library.save_devices([DEVICE])
        self.library.create("area", "DMZ")
        self.assertEqual(self.library.devices(), [])
        other = Device("USG-01", DEVICE.host, "Huawei USG")
        self.library.save_devices([other])
        self.library.select("默认项目", "默认区域")
        self.assertEqual(self.library.devices(), [DEVICE])
        self.library.select("默认项目", "DMZ")
        self.assertEqual(self.library.devices(), [other])

    def test_duplicate_endpoint_in_area_rejected(self):
        with self.assertRaises(ValueError):
            self.library.save_devices([DEVICE, DEVICE])
        self.assertEqual(self.library.devices(), [])

    def test_names_prevent_path_escape_and_collisions(self):
        for name in ("", "..", "../x", "a/b", "CON", "AUX.txt", "name.", "x" * 41):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.library.create("project", name)
        self.library.create("project", "Bank")
        with self.assertRaises(ValueError):
            self.library.create("project", "bank")

    def test_rename_preserves_devices(self):
        self.library.save_devices([DEVICE])
        self.library.rename("project", "A")
        self.library.rename("area", "B")
        self.assertEqual(Inventory(self.path).selected, ("A", "B"))
        self.assertEqual(Inventory(self.path).devices(), [DEVICE])

    def test_delete_changes_only_selected_area(self):
        self.library.save_devices([DEVICE])
        self.library.create("area", "Delete")
        self.library.delete("area")
        self.assertEqual(self.library.devices(), [DEVICE])
        with self.assertRaises(ValueError):
            self.library.delete("area")
        with self.assertRaises(ValueError):
            self.library.delete("project")

    def test_corrupt_file_is_not_overwritten(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            Inventory(self.path)
        self.assertEqual(self.path.read_text(), "{broken")

    def test_invalid_schema_is_rejected(self):
        for payload in ([], {"version": 99}, {"version": 1, "projects": {}, "selected": []}):
            self.path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                Inventory(self.path)

    def test_failed_write_preserves_memory_and_disk(self):
        self.library.save_devices([DEVICE])
        before = self.path.read_bytes()
        with patch("inventory.os.replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                self.library.create("area", "Never saved")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.library.selected, ("默认项目", "默认区域"))

    def test_backup_contains_previous_good_version(self):
        self.library.save_devices([DEVICE])
        previous = self.path.read_bytes()
        self.library.create("area", "New")
        self.assertEqual(self.path.with_suffix(".json.bak").read_bytes(), previous)

    def test_stale_window_cannot_overwrite(self):
        other = Inventory(self.path)
        self.library.save_devices([DEVICE])
        with self.assertRaisesRegex(ValueError, "另一个窗口"):
            other.save_devices([])
        self.assertEqual(Inventory(self.path).devices(), [DEVICE])

    def test_no_credentials_in_schema(self):
        self.library.save_devices([DEVICE])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"version", "selected", "projects"})
        row = data["projects"]["默认项目"]["默认区域"][0]
        self.assertEqual(set(row), {"name", "host", "platform", "port", "credential"})


class ProjectUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "inventory.json"
        self.root = tk.Tk()
        self.ui = app.App(self.root, self.path)
        self.ui.library.create("project", "项目A")
        self.ui.library.rename("area", "区域A")
        self.ui.load_area()
        self.root.update()

    def tearDown(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.ui = self.root = None
        gc.collect()
        self.temp.cleanup()

    def add(self, name="CE-01"):
        self.ui.save_device(dict(name=name, host="192.0.2.1", platform="Huawei CE"))

    def test_edit_and_remove_auto_save(self):
        self.add()
        self.assertEqual(Inventory(self.path).devices(), self.ui.devices)
        self.ui.save_device(dict(name="USG", host="192.0.2.1", platform="Huawei USG"), 0)
        self.assertEqual(Inventory(self.path).devices()[0].platform, "Huawei USG")
        self.ui.tree.selection_set("0")
        self.ui.remove()
        self.assertEqual(Inventory(self.path).devices(), [])

    def test_area_switch_restores_inventory_and_clears_stale_results(self):
        self.add()
        self.ui.library.create("area", "区域B")
        self.ui.load_area()
        self.assertEqual(self.ui.devices, [])
        self.add("OTHER")
        self.ui.details["0"] = "old result"
        self.ui.area.set("区域A")
        self.ui.switch_area()
        self.assertEqual(self.ui.devices[0].name, "CE-01")
        self.assertEqual(self.ui.details, {})
        self.assertIsNone(self.ui.last_folder)

    def test_project_credentials_isolated_not_persisted(self):
        self.ui.credentials["default"] = ("user-A", "SECRET-A")
        self.ui.library.create("project", "项目B")
        self.ui.load_area()
        self.assertEqual(self.ui.credentials, {})
        self.ui.credentials["default"] = ("user-B", "SECRET-B")
        self.ui.project.set("项目A")
        self.ui.switch_area()
        self.assertEqual(self.ui.credentials["default"], ("user-A", "SECRET-A"))
        self.assertNotIn("SECRET", self.path.read_text(encoding="utf-8"))

    def test_manage_rename_project_keeps_devices_and_credentials(self):
        self.add()
        self.ui.credentials["default"] = ("user", "SECRET")
        with patch("app.simpledialog.askstring", return_value="项目New"):
            self.ui.manage_inventory("rename", "project")
        self.assertEqual(Inventory(self.path).selected, ("项目New", "区域A"))
        self.assertEqual(self.ui.devices[0].name, "CE-01")
        self.assertEqual(self.ui.credentials["default"], ("user", "SECRET"))
        self.assertNotIn("项目A", self.ui.project_credentials)

    def test_cancel_import_keeps_saved_area(self):
        self.add()
        before = self.path.read_bytes()
        with patch("app.filedialog.askopenfilename", return_value="example.csv"), \
             patch("app.read_inventory", return_value=[Device("NEW", "192.0.2.2", "Huawei AR")]), \
             patch("app.messagebox.askyesno", return_value=False):
            self.ui.import_csv()
        self.assertEqual(self.ui.devices[0].name, "CE-01")
        self.assertEqual(self.path.read_bytes(), before)

    def test_running_blocks_area_switch_and_manage(self):
        self.ui.library.create("area", "区域B")
        self.ui.load_area()
        self.ui.set_running(True)
        self.ui.area.set("区域A")
        self.ui.switch_area()
        self.assertEqual(self.ui.area.get(), "区域B")
        with patch("app.simpledialog.askstring") as prompt:
            self.ui.manage_inventory("create", "project")
            prompt.assert_not_called()
        self.assertEqual(str(self.ui.project_box["state"]), "disabled")
        self.ui.set_running(False)

    def test_failed_save_leaves_display_unchanged(self):
        self.add()
        with patch.object(self.ui.library, "commit", side_effect=OSError("disk error")):
            with self.assertRaises(ValueError):
                self.ui.save_device(dict(name="Never", host="192.0.2.2", platform="Huawei CE"))
        self.assertEqual(len(self.ui.devices), 1)
        self.assertEqual(len(self.ui.tree.get_children()), 1)

    def test_collection_only_current_area_and_output_grouped(self):
        self.add()
        self.ui.library.create("area", "区域B")
        self.ui.load_area()
        self.add("TARGET")
        self.ui.credentials["default"] = ("test", "test-only")
        self.ui.output.set(str(Path(self.temp.name) / "backups"))
        calls = []
        def collect(device, credentials, folder, index, *args, **kwargs):
            calls.append((device.name, folder))
            return dict(name=device.name, host=device.host, platform=device.platform, status="成功",
                        file="", message="simulation", sha256="")
        with patch.object(app, "STATE", Path(self.temp.name) / "state"), patch.object(app, "collect", collect):
            self.ui.start()
            deadline = time.monotonic() + 5
            while self.ui.running and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.02)
            self.assertFalse(self.ui.running)
        self.assertEqual([name for name, _ in calls], ["TARGET"])
        self.assertEqual(calls[0][1].parent.name, "区域B")
        self.assertEqual(calls[0][1].parent.parent.name, "项目A")

    def test_restore_in_new_window(self):
        self.add()
        script = """
import sys, tkinter as tk
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app import App
root = tk.Tk()
ui = App(root, Path(sys.argv[2]))
root.update()
assert ui.project.get() == "项目A"
assert ui.area.get() == "区域A"
assert len(ui.devices) == 1 and ui.devices[0].name == "CE-01"
assert ui.credentials == {}
root.destroy()
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script,
                                 str(Path(app.__file__).parent), str(self.path)],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)


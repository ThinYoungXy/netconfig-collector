import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch
from types import SimpleNamespace

from app import App
from collector import Device, read_inventory


def descendants(widget):
    result = []
    for child in widget.winfo_children():
        result.append(child)
        result.extend(descendants(child))
    return result


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.app = App(self.root)
        self.app.devices = [Device("USG-01", "192.0.2.1", "Huawei CE"),
                            Device("CE-01", "192.0.2.2", "Huawei CE")]
        self.app.refresh()
        self.app.tree.selection_set("0")
        self.root.update()

    def tearDown(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.app = None
        self.root = None
        gc.collect()

    def test_dialogs_follow_main_window(self):
        self.root.geometry("1020x760+120+80")
        self.root.update()
        for open_dialog in (self.app.add_device, self.app.edit_selected, self.app.edit_credentials):
            with self.subTest(dialog=open_dialog.__name__):
                open_dialog()
                self.root.update()
                dialog = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
                expected_x = self.root.winfo_rootx() + self.root.winfo_width() // 2
                expected_y = self.root.winfo_rooty() + self.root.winfo_height() // 2
                self.assertLess(abs(dialog.winfo_rootx() + dialog.winfo_width() // 2 - expected_x), 30)
                self.assertLess(abs(dialog.winfo_rooty() + dialog.winfo_height() // 2 - expected_y), 40)
                self.assertEqual(dialog.grab_current(), dialog)
                dialog.destroy()

    def test_dialog_stays_on_screen_near_edge(self):
        width, height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"1020x760+{width - 120}+{height - 120}")
        self.root.update()
        self.app.edit_credentials()
        self.root.update()
        dialog = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        self.assertGreaterEqual(dialog.winfo_rootx(), 0)
        self.assertGreaterEqual(dialog.winfo_rooty(), 0)
        self.assertLessEqual(dialog.winfo_rootx() + dialog.winfo_width(), width)
        self.assertLessEqual(dialog.winfo_rooty() + dialog.winfo_height(), height)
        dialog.destroy()

    def test_edit_type_through_dialog(self):
        self.app.edit_selected()
        self.root.update()
        dialog = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        widgets = descendants(dialog)
        dropdown = next(w for w in widgets if isinstance(w, ttk.Combobox))
        self.assertEqual(dropdown.get(), "Huawei CE")
        dropdown.set("Huawei USG")
        next(w for w in widgets if isinstance(w, ttk.Button) and w["text"] == "保存修改").invoke()
        self.assertEqual(self.app.devices[0].platform, "Huawei USG")
        self.assertEqual(self.app.tree.set("0", "platform"), "Huawei USG")
        self.assertEqual(len(self.app.devices), 2)
        self.assertFalse(dialog.winfo_exists())

    def test_cancel_does_not_modify(self):
        self.app.edit_selected()
        dialog = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        widgets = descendants(dialog)
        next(w for w in widgets if isinstance(w, ttk.Combobox)).set("Huawei USG")
        next(w for w in widgets if isinstance(w, ttk.Button) and w["text"] == "取消").invoke()
        self.assertEqual(self.app.devices[0].platform, "Huawei CE")

    def test_save_all_fields_preserves_other_results(self):
        self.app.tree.set("1", "status", "成功")
        self.app.details["1"] = "existing result"
        self.app.details["0"] = "obsolete result"
        self.app.save_device(dict(name="FW", host="192.0.2.3", platform="Huawei USG",
                                  port="2222", credential="firewalls"), 0)
        self.assertEqual(self.app.devices[0], Device("FW", "192.0.2.3", "Huawei USG", 2222, "firewalls"))
        self.assertEqual(self.app.tree.set("1", "status"), "成功")
        self.assertEqual(self.app.details["1"], "existing result")
        self.assertNotIn("0", self.app.details)

    def test_duplicate_endpoint_rejected(self):
        before = list(self.app.devices)
        with self.assertRaisesRegex(ValueError, "已存在"):
            self.app.save_device(dict(name="FW", host="192.0.2.2", platform="Huawei USG", port="22"), 0)
        self.assertEqual(self.app.devices, before)

    def test_invalid_port_rejected(self):
        with self.assertRaises(ValueError):
            self.app.save_device(dict(name="FW", host="192.0.2.1", platform="Huawei USG", port="invalid"), 0)
        self.assertEqual(self.app.devices[0].port, 22)

    def test_edit_own_endpoint_allowed(self):
        self.app.save_device(dict(name="FW", host="192.0.2.1", platform="Huawei USG", port="22"), 0)
        self.assertEqual(self.app.devices[0].platform, "Huawei USG")

    def test_running_blocks_editor(self):
        self.app.set_running(True)
        with patch.object(self.app, "device_dialog") as dialog:
            self.app.edit_selected()
            dialog.assert_not_called()
        with self.assertRaisesRegex(ValueError, "采集期间"):
            self.app.save_device(dict(name="FW", host="192.0.2.1", platform="Huawei USG"), 0)

    def test_double_click_routes_correctly(self):
        with patch.object(self.app.tree, "identify_row", return_value="0"), \
             patch.object(self.app.tree, "identify_column", return_value="#3"), \
             patch.object(self.app, "edit_selected") as edit:
            self.app.double_click(SimpleNamespace(x=10, y=10))
            edit.assert_called_once()
        with patch.object(self.app.tree, "identify_row", return_value="0"), \
             patch.object(self.app.tree, "identify_column", return_value="#5"), \
             patch.object(self.app, "show_detail") as detail:
            self.app.double_click(SimpleNamespace(x=10, y=10))
            detail.assert_called_once()

    def test_save_inventory_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            filename = str(Path(temp) / "devices.csv")
            self.app.save_device(dict(name="防火墙", host="192.0.2.1", platform="Huawei USG"), 0)
            with patch("app.filedialog.asksaveasfilename", return_value=filename):
                self.app.export_inventory()
            self.assertEqual(read_inventory(filename), self.app.devices)
            self.assertNotIn("password", Path(filename).read_text(encoding="utf-8-sig"))

    def test_minimum_window_controls_visible(self):
        self.root.geometry("1020x760")
        self.root.update()
        for widget in self.app.controls + [self.app.stop_button, self.app.progress]:
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                 self.root.winfo_rootx() + self.root.winfo_width())


if __name__ == "__main__":
    unittest.main(verbosity=2)


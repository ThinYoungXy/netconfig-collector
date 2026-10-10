import csv
import gc
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
import app
from collector import Device


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.ui = app.App(self.root)
        self.ui.devices = [Device("OK", "192.0.2.1", "Huawei CE", credential="unused"),
                           Device("WAIT", "192.0.2.2", "Huawei CE"),
                           Device("FAIL", "192.0.2.3", "Huawei USG"),
                           Device("STOP", "192.0.2.4", "Hillstone StoneOS")]
        self.ui.credentials = {"default": ("test", "test-only")}
        self.ui.output.set(self.temp.name)
        self.ui.refresh()
        for index, state in enumerate(("成功", "待采集", "失败", "已停止")):
            self.ui.tree.set(str(index), "status", state)
        self.old_folder = Path(self.temp.name) / "original"
        self.old_folder.mkdir()
        self.old_file = self.old_folder / "0001_OK.txt"
        self.old_file.write_text("original success", encoding="utf-8")
        self.ui.details["0"] = str(self.old_file)
        self.ui.last_folder = self.old_folder
        self.calls = []

    def tearDown(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.ui = self.root = None
        gc.collect()
        self.temp.cleanup()

    def run_job(self, success=True, retry=True):
        def fake_collect(device, credentials, folder, index, *args, **kwargs):
            self.calls.append((device.name, index, folder))
            file = f"{index:04d}_{device.name}.txt"
            (folder / file).write_text("retry output", encoding="utf-8")
            return dict(name=device.name, host=device.host, platform=device.platform,
                        status="成功" if success else "失败", file=file, message="simulated", sha256="")
        with patch.object(app, "collect", fake_collect), \
             patch.object(app, "STATE", Path(self.temp.name) / "state"), \
             patch.object(app.messagebox, "showerror") as error:
            self.ui.retry_failed() if retry else self.ui.start()
            self.assertTrue(self.ui.running)
            self.assertEqual(str(self.ui.retry_button["state"]), "disabled")
            deadline = time.monotonic() + 5
            while self.ui.running and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.02)
            self.assertFalse(self.ui.running)
            error.assert_not_called()
            self.assertEqual(str(self.ui.retry_button["state"]), "normal")

    def test_only_failed_preserves_other_rows_and_files(self):
        self.run_job()
        self.assertEqual([(name, index) for name, index, _ in self.calls], [("FAIL", 3)])
        self.assertEqual(self.ui.tree.set("0", "status"), "成功")
        self.assertEqual(self.ui.tree.set("1", "status"), "待采集")
        self.assertEqual(self.ui.tree.set("3", "status"), "已停止")
        self.assertEqual(self.ui.tree.set("2", "status"), "成功")
        self.assertEqual(self.ui.details["0"], str(self.old_file))
        self.assertEqual(self.old_file.read_text(), "original success")
        self.assertTrue(self.ui.last_folder.name.startswith("retry_"))
        self.assertIn(str(self.ui.last_folder), self.ui.details["2"])
        with (self.ui.last_folder / "results.csv").open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([row["name"] for row in rows], ["FAIL"])
        self.assertEqual(float(self.ui.progress["maximum"]), 1)

    def test_edit_failed_device_remains_retryable(self):
        self.ui.save_device(dict(name="FIXED", host="192.0.2.3", platform="Huawei USG"), 2)
        self.assertEqual(self.ui.tree.set("2", "status"), "待重试")
        self.run_job()
        self.assertEqual(self.calls[0][0], "FIXED")

    def test_no_failed_no_network_or_new_folder(self):
        self.ui.tree.set("2", "status", "成功")
        with patch.object(app.messagebox, "showinfo") as info, patch.object(app, "collect") as collect:
            self.ui.retry_failed()
        info.assert_called_once()
        collect.assert_not_called()
        self.assertEqual(self.ui.last_folder, self.old_folder)
        self.assertFalse(self.ui.running)

    def test_repeated_failure_can_retry_again_in_new_folder(self):
        self.run_job(success=False)
        first = self.ui.last_folder
        first_report = (first / "results.csv").read_bytes()
        self.run_job(success=True)
        self.assertNotEqual(first, self.ui.last_folder)
        self.assertEqual((first / "results.csv").read_bytes(), first_report)
        self.assertEqual(len(self.calls), 2)

    def test_full_collection_still_collects_all(self):
        self.ui.credentials["unused"] = ("test", "test-only")
        self.run_job(retry=False)
        self.assertEqual(sorted(index for _, index, _ in self.calls), [1, 2, 3, 4])
        self.assertEqual(float(self.ui.progress["maximum"]), 4)

    def test_running_ignores_retry(self):
        self.ui.set_running(True)
        with patch.object(app.threading, "Thread") as thread:
            self.ui.retry_failed()
        thread.assert_not_called()
        self.ui.set_running(False)

    def test_missing_failed_credential_does_not_clear_failure(self):
        self.ui.credentials.clear()
        with patch.object(app.messagebox, "showerror") as error:
            self.ui.retry_failed()
        error.assert_called_once()
        self.assertFalse(self.ui.running)
        self.assertEqual(self.ui.tree.set("2", "status"), "失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)


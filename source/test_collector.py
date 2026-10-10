import csv
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import paramiko
from collector import (CollectionError, Device, PROFILES, Shell, collect, device_from_row,
                       read_inventory, validate_config, write_summary)

HUAWEI = "#\nsysname TEST-NE\n#\ninterface LoopBack0\n ip address 192.0.2.1 255.255.255.255\n#\nreturn"
STONE = 'Building configuration..\nRunning configuration:\n!\nVersion 5.5R11\nhostname FW\nip vrouter "trust-vr"\nexit\n!'
KEY = paramiko.RSAKey.generate(2048)


class Channel:
    def __init__(self, chunks, close=False):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = close
    def recv_ready(self):
        return bool(self.chunks)
    def recv(self, n):
        return self.chunks.pop(0)
    def sendall(self, s):
        self.sent.append(s)
    def exit_status_ready(self):
        return False


class Server(paramiko.ServerInterface):
    def __init__(self, password="test-only"):
        self.password = password
        self.ready = threading.Event()
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == self.password else paramiko.AUTH_FAILED
    def get_allowed_auths(self, username):
        return "password"
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED
    def check_channel_pty_request(self, *args):
        return True
    def check_channel_shell_request(self, channel):
        self.ready.set()
        return True


class FakeDevice:
    def __init__(self, platform, body=None, fallback=False, disconnect=False, key=KEY, prompt=None):
        self.platform = platform
        self.prompt = prompt
        self.body = body if body is not None else (STONE if platform.startswith("Hillstone") else HUAWEI)
        self.fallback = fallback
        self.disconnect = disconnect
        self.key = key
        self.commands = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(8)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
    def serve(self):
        transport = None
        try:
            connection, _ = self.sock.accept()
            transport = paramiko.Transport(connection)
            transport.add_server_key(self.key)
            server = Server()
            transport.start_server(server=server)
            channel = transport.accept(5)
            if not channel or not server.ready.wait(5):
                return
            channel.settimeout(5)
            prompt = self.prompt or ("SG-6000(M)#" if self.platform.startswith("Hillstone") else "<TEST-NE>")
            channel.sendall(prompt.encode())
            buffer = b""
            while True:
                chunk = channel.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                if b"\n" not in buffer:
                    continue
                raw, buffer = buffer.split(b"\n", 1)
                command = raw.decode().strip()
                self.commands.append(command)
                channel.sendall((command + "\r\n").encode())
                if command == PROFILES[self.platform][0]:
                    response = "% Unrecognized command\r\n" if self.fallback else ""
                    channel.sendall((response + prompt).encode())
                else:
                    if self.disconnect:
                        channel.sendall(self.body[:25].encode())
                        channel.close()
                        return
                    lines = self.body.splitlines()
                    channel.sendall(("\r\n".join(lines[:3]) + "\r\n").encode())
                    if self.fallback:
                        channel.sendall(b"-- Mo")
                        time.sleep(0.03)
                        channel.sendall(b"re --")
                        if channel.recv(1) != b" ":
                            return
                        channel.sendall(b"\r\n")
                    channel.sendall(("\r\n".join(lines[3:]) + "\r\n" + prompt).encode())
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            if transport:
                transport.close()
            self.sock.close()
    def close(self):
        self.thread.join(6)
        self.sock.close()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()

    def inventory(self, text, encoding="utf-8-sig"):
        target = self.folder / "devices.csv"
        target.write_text(text, encoding=encoding)
        return read_inventory(target)

    def test_hrp_initial_prompt(self):
        for prefix in ("HRP_M", "HRP_S"):
            with self.subTest(prefix=prefix):
                prompt = prefix + "<USG-HA-01>"
                shell = Shell(Channel([prompt.encode()]), "Huawei CE", threading.Event(), .7)
                shell.receive()
                self.assertEqual(shell.prompt, prompt)

    def test_hrp_echo_and_timestamp(self):
        prompt = "HRP_M<USG-HA-01>"
        command = "display current-configuration"
        response = prompt + command + "\r\n2026-10-10 06:12:14.11\r\n" + HUAWEI + "\r\n" + prompt
        shell = Shell(Channel([response.encode()]), "Huawei CE", threading.Event(), 2)
        shell.prompt = prompt
        body = shell.command(command)
        validate_config(body, "Huawei CE")
        self.assertNotIn(prompt, body)

    def test_real_ssh_hrp_master_standby(self):
        for platform, prefix in (("Huawei USG", "HRP_M"), ("Huawei USG", "HRP_S"), ("Huawei CE", "HRP_M")):
            with self.subTest(platform=platform, prefix=prefix):
                result, commands = self.run_device(platform, prompt=prefix + "<FW-01>")
                self.assertEqual(result["status"], "成功", result)
                self.assertEqual(commands, list(PROFILES[platform]))

    def test_inventory_unicode_and_gb18030(self):
        for encoding in ("utf-8-sig", "gb18030"):
            devices = self.inventory("name,host,platform,port,credential\n华为,192.0.2.1,Huawei NE,22,\n", encoding)
            self.assertEqual(devices[0].credential, "default")
    def test_inventory_duplicate(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            self.inventory("name,host,platform\nA,192.0.2.1,Huawei NE\nB,192.0.2.1,Huawei CE\n")
    def test_invalid_inputs(self):
        for row in [
            dict(name="", host="192.0.2.1", platform="Huawei NE"),
            dict(name="a", host="a;bad", platform="Huawei NE"),
            dict(name="a", host="host", platform="wrong"),
            dict(name="a", host="host", platform="Huawei NE", port="70000"),
            dict(name="a", host="host", platform="Huawei NE", port="ssh"),
        ]:
            with self.assertRaises(ValueError):
                device_from_row(row)
    def test_missing_header_and_empty(self):
        for text in ("x,y\n1,2\n", "name,host,platform\n"):
            with self.assertRaises(ValueError):
                self.inventory(text)
    def test_ipv6(self):
        self.assertEqual(device_from_row(dict(name="v6", host="2001:db8::1", platform="Huawei NE")).port, 22)
    def test_valid_configs(self):
        for platform in PROFILES:
            validate_config(STONE if platform.startswith("Hillstone") else HUAWEI, platform)
    def test_incomplete_config(self):
        with self.assertRaisesRegex(CollectionError, "return"):
            validate_config(HUAWEI.removesuffix("return"), "Huawei NE")
    def test_empty_and_garbage_config(self):
        for body in ("", "permission denied", "x" * 80 + "\n" * 10):
            with self.assertRaises(CollectionError):
                validate_config(body, "Hillstone StoneOS")
    def test_decoding_error(self):
        with self.assertRaisesRegex(CollectionError, "解码"):
            validate_config(HUAWEI + "\ufffd", "Huawei CE")
    def test_cancel(self):
        event = threading.Event()
        event.set()
        shell = Shell(Channel([]), "Huawei NE", event, .1)
        with self.assertRaisesRegex(CollectionError, "停止"):
            shell.receive()
    def test_timeout_keeps_partial(self):
        shell = Shell(Channel([b"partial output"]), "Huawei NE", threading.Event(), .05)
        with self.assertRaises(CollectionError) as context:
            shell.receive()
        self.assertIn("partial", context.exception.partial)
    def test_closed_connection(self):
        shell = Shell(Channel([b"partial"], close=True), "Huawei NE", threading.Event(), .1)
        with self.assertRaisesRegex(CollectionError, "关闭"):
            shell.receive()
    def test_prompt_inside_body_not_end(self):
        body = "display current-configuration\r\n<TEST-NE>\r\n" + HUAWEI + "\r\n<TEST-NE>"
        shell = Shell(Channel([body.encode()]), "Huawei NE", threading.Event(), 2)
        shell.prompt = "<TEST-NE>"
        result = shell.command("display current-configuration")
        self.assertIn("sysname TEST-NE", result)
        self.assertTrue(result.endswith("return"))
    def test_no_echo_fails(self):
        shell = Shell(Channel([(HUAWEI + "\n<TEST-NE>").encode()]), "Huawei NE", threading.Event(), .05)
        shell.prompt = "<TEST-NE>"
        with self.assertRaises(CollectionError):
            shell.command("display current-configuration")
    def test_split_pager_and_utf8(self):
        raw = ("display current-configuration\r\n# 中文\r\n-- More --").encode()
        chunks = [raw[:38], raw[38:-3], raw[-3:], ("\r\n" + HUAWEI + "\r\n<TEST-NE>").encode()]
        channel = Channel(chunks)
        shell = Shell(channel, "Huawei NE", threading.Event(), 2)
        shell.prompt = "<TEST-NE>"
        result = shell.command("display current-configuration")
        self.assertEqual(channel.sent.count(" "), 1)
        self.assertIn("中文", result)
        self.assertNotIn("-- More --", result)
    def test_pager_text_inside_config_does_not_send_space(self):
        channel = Channel([("display current-configuration\r\n" + HUAWEI.replace(
            "#\nreturn", " description literal --More--\n#\nreturn") + "\r\n<TEST-NE>").encode()])
        shell = Shell(channel, "Huawei NE", threading.Event(), 2)
        shell.prompt = "<TEST-NE>"
        body = shell.command("display current-configuration")
        self.assertNotIn(" ", channel.sent)
        self.assertIn("literal --More--", body)
    def test_cli_denial(self):
        channel = Channel([b"display current-configuration\r\nError: Permission denied\r\n<TEST-NE>"])
        shell = Shell(channel, "Huawei NE", threading.Event(), 2)
        shell.prompt = "<TEST-NE>"
        with self.assertRaisesRegex(CollectionError, "拒绝"):
            shell.command("display current-configuration")
    def run_device(self, platform="Huawei NE", accept=True, secret="test-only", cancel=None, **options):
        server = FakeDevice(platform, **options)
        device = Device("TEST", "127.0.0.1", platform, server.port)
        try:
            result = collect(device, ("test", secret), self.folder, 1, self.folder / "known_hosts",
                             lambda *args: accept, cancel or threading.Event(), 3)
            return result, server.commands
        finally:
            server.close()
    def test_real_ssh_all_profiles(self):
        for platform in PROFILES:
            with self.subTest(platform=platform):
                result, commands = self.run_device(platform)
                self.assertEqual(result["status"], "成功", result)
                self.assertEqual(commands, list(PROFILES[platform]))
                self.assertIn("127.0.0.1.txt", result["file"])
                self.assertTrue((self.folder / result["file"]).is_file())
                self.assertEqual(len(result["sha256"]), 64)
    def test_real_ssh_pager_fallback(self):
        result, _ = self.run_device("Hillstone StoneOS", fallback=True)
        self.assertEqual(result["status"], "成功", result)
        self.assertIn("空格翻页", result["message"])
    def test_real_ssh_auth_failure(self):
        result, commands = self.run_device(secret="incorrect")
        self.assertEqual(result["status"], "失败")
        self.assertIn("认证失败", result["message"])
        self.assertEqual(commands, [])
    def test_real_ssh_reject_host_key(self):
        result, commands = self.run_device(accept=False)
        self.assertEqual(result["status"], "失败")
        self.assertEqual(commands, [])
        self.assertFalse((self.folder / "known_hosts").exists())
    def test_real_ssh_disconnect_partial(self):
        result, _ = self.run_device(disconnect=True)
        self.assertEqual(result["status"], "失败")
        self.assertTrue(result["file"].endswith(".partial.txt"), result)
    def test_changed_host_key(self):
        server = FakeDevice("Huawei NE")
        keys = paramiko.HostKeys()
        keys.add(f"[127.0.0.1]:{server.port}", "ssh-rsa", paramiko.RSAKey.generate(2048))
        keys.save(str(self.folder / "known_hosts"))
        try:
            result = collect(Device("TEST", "127.0.0.1", "Huawei NE", server.port), ("test", "test-only"),
                             self.folder, 1, self.folder / "known_hosts", lambda *a: True, threading.Event(), 3)
            self.assertIn("已变化", result["message"])
            self.assertFalse(server.commands)
        finally:
            server.close()
    def test_summary_escapes_formula(self):
        result = dict(name="=1+1", host="host", platform="Huawei NE", status="失败",
                      file="", message="test", sha256="")
        write_summary(self.folder, [result])
        with (self.folder / "results.csv").open(encoding="utf-8-sig") as stream:
            self.assertEqual(next(csv.DictReader(stream))["name"], "'=1+1")
    def test_precancel_no_connect(self):
        cancel = threading.Event()
        cancel.set()
        with patch("paramiko.SSHClient.connect") as connect:
            result = collect(Device("a", "192.0.2.1", "Huawei NE"), ("x", "y"), self.folder, 1,
                             self.folder / "known_hosts", lambda *a: True, cancel)
        connect.assert_not_called()
        self.assertEqual(result["status"], "已停止")


if __name__ == "__main__":
    unittest.main(verbosity=2)


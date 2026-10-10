"""SSH configuration collection. No configuration-mode or save commands."""
import base64
import codecs
import csv
import hashlib
import ipaddress
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import paramiko

PROFILES = {
    "Huawei NE": ("screen-length 0 temporary", "display current-configuration"),
    "Huawei AR": ("screen-length 0 temporary", "display current-configuration"),
    "Huawei CE": ("screen-length 0 temporary", "display current-configuration"),
    "Huawei USG": ("screen-length 0 temporary", "display current-configuration"),
    "H3C Comware": ("screen-length disable", "display current-configuration"),
    "Hillstone StoneOS": ("terminal length 0", "show configuration running"),
}
COLUMNS = ("name", "host", "platform", "port", "credential")
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PAGER = re.compile(r"(?m)^[ \t\r]*-{2,}[ \t]*More[ \t]*-{2,}(?:[^\r\n]*?(?:break|quit|continue)[^\r\n]*)?", re.I)
CLI_ERROR = re.compile(
    r"(?im)^\s*(?:Error\s*:|%\s*(?:Error|Unrecognized|Unknown|Invalid|Incomplete|Ambiguous|Permission|Access)|"
    r"(?:Unrecognized|Unknown|Invalid|Incomplete|Ambiguous)\s+(?:command|input|parameter)|"
    r"(?:Permission|Access)\s+denied|Insufficient\s+privilege)"
)
HOST_LOCK = threading.Lock()


@dataclass(frozen=True)
class Device:
    name: str
    host: str
    platform: str
    port: int = 22
    credential: str = "default"


class CollectionError(Exception):
    def __init__(self, message, partial=""):
        super().__init__(message)
        self.partial = partial


def device_from_row(row):
    name = row.get("name", "").strip()
    host = row.get("host", "").strip()
    platform = row.get("platform", "").strip()
    credential = row.get("credential", "").strip() or "default"
    if not name or not host or platform not in PROFILES:
        raise ValueError("名称、地址必填；platform 必须是支持列表中的完整名称")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", p)
                                      for p in host.rstrip(".").split(".")):
            raise ValueError("设备地址必须是 IP 地址或合法主机名")
    try:
        port = int(row.get("port", "") or 22)
    except ValueError:
        raise ValueError("SSH 端口必须是整数")
    if not 1 <= port <= 65535:
        raise ValueError("SSH 端口须在 1–65535")
    if any(ord(c) < 32 for c in name + credential) or len(name) > 100 or len(credential) > 100:
        raise ValueError("名称或凭据组过长，或包含控制字符")
    return Device(name, host, platform, port, credential)


def read_inventory(filename):
    data = Path(filename).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("gb18030")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames or not set(("name", "host", "platform")).issubset(reader.fieldnames):
        raise ValueError("CSV 表头至少需要 name,host,platform；请使用导出模板")
    devices, seen = [], set()
    for n, row in enumerate(reader, 2):
        if not any(row.values()):
            continue
        try:
            device = device_from_row(row)
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"CSV 第 {n} 行：{exc}") from exc
        endpoint = (device.host.lower(), device.port)
        if endpoint in seen:
            raise ValueError(f"CSV 第 {n} 行：重复设备地址和端口")
        seen.add(endpoint)
        devices.append(device)
    if not devices:
        raise ValueError("设备清单为空")
    return devices


def clean_terminal(text):
    text = ANSI.sub("", text).replace("\r", "")
    # Remove backspace plus preceding character, including pager erasure.
    while "\b" in text:
        text = re.sub(r"[^\n]\x08", "", text).replace("\b", "")
    return text


def last_line(text):
    lines = text.rstrip().splitlines()
    return lines[-1].strip() if lines else ""


def is_prompt(line, platform):
    if platform.startswith("Hillstone"):
        return bool(re.fullmatch(r"[\w.()~:/-]+[>#]", line))
    prefix = r"(?:HRP_[MS])?" if platform.startswith("Huawei") else ""
    return bool(re.fullmatch(prefix + r"<[^<>\r\n]+>", line))


class Shell:
    def __init__(self, channel, platform, cancel, timeout=180, encoding="utf-8"):
        self.channel = channel
        self.platform = platform
        self.cancel = cancel
        self.timeout = timeout
        self.encoding = encoding
        self.prompt = ""

    def receive(self, command=None):
        data = ""
        decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        deadline = time.monotonic() + self.timeout
        last_rx = time.monotonic()
        echo_seen = command is None
        pager_end = 0
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                raise CollectionError("已停止", clean_terminal(data))
            if self.channel.recv_ready():
                chunk = self.channel.recv(65536)
                if not chunk:
                    raise CollectionError("SSH 连接提前关闭", clean_terminal(data))
                data += decoder.decode(chunk)
                last_rx = time.monotonic()
                if len(data) > 32 * 1024 * 1024:
                    raise CollectionError("输出超过 32 MiB 上限", clean_terminal(data))
                # Scan only the tail, retaining absolute offsets so each pager is answered once.
                start = max(0, len(data) - 2048)
                for match in PAGER.finditer(data[start:]):
                    end = start + match.start() + 1
                    if end > pager_end:
                        self.channel.sendall(" ")
                        pager_end = end
                continue
            text = clean_terminal(data)
            if command and command in text:
                echo_seen = True
            tail = last_line(text)
            ready = (tail == self.prompt) if command else is_prompt(tail, self.platform)
            if echo_seen and ready and time.monotonic() - last_rx >= 0.5:
                if not command:
                    self.prompt = tail
                return text
            if self.channel.closed or self.channel.exit_status_ready():
                raise CollectionError("SSH 连接提前关闭", text)
            time.sleep(0.03)
        raise CollectionError("等待命令结束超时（未确认完整提示符或命令回显）", clean_terminal(data))

    def command(self, command):
        if self.cancel.is_set():
            raise CollectionError("已停止")
        # Only fixed, reviewed commands are sent by the collector.
        self.channel.sendall(command + "\n")
        text = self.receive(command)
        lines = text.rstrip().splitlines()
        echo = next((i for i, line in enumerate(lines)
                     if line.strip() in (command, self.prompt + command, self.prompt + " " + command)), None)
        if echo is None:
            raise CollectionError("未识别命令回显，不能确认输出边界", text)
        body = "\n".join(lines[echo + 1:-1]).strip()
        body = PAGER.sub("", body)
        if CLI_ERROR.search(body):
            raise CollectionError("设备拒绝命令：权限不足或当前版本不支持", body)
        return body


def validate_config(text, platform):
    if "\ufffd" in text:
        raise CollectionError("输出包含解码错误；请切换 UTF-8 / GB18030 后重试", text)
    if len(text.strip()) < 40 or len(text.splitlines()) < 4:
        raise CollectionError("配置输出过短，不能确认有效配置", text)
    if not platform.startswith("Hillstone"):
        if not re.search(r"(?mi)^\s*sysname\s+\S+", text):
            raise CollectionError("未找到 sysname，不能确认有效配置", text)
        if last_line(text).lower() != "return":
            raise CollectionError("未找到配置结束标记 return，可能采集不完整", text)
    elif not re.search(r"(?mi)^\s*(?:Version\s+\S+|hostname\s+\S+|ip vrouter\s+|interface\s+)", text):
        raise CollectionError("未找到 StoneOS 配置特征，需人工核查", text)


class ConfirmHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, filename, confirm, cancel):
        self.filename = Path(filename)
        self.confirm = confirm
        self.cancel = cancel

    def missing_host_key(self, client, hostname, key):
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if self.cancel.is_set() or not self.confirm(hostname, key.get_name(), fingerprint):
            raise CollectionError("未信任设备 SSH 主机密钥")
        with HOST_LOCK:
            keys = paramiko.HostKeys()
            if self.filename.exists():
                keys.load(str(self.filename))
            prior = keys.lookup(hostname)
            if prior and (key.get_name() not in prior or prior[key.get_name()] != key):
                raise CollectionError("设备 SSH 主机密钥冲突")
            keys.add(hostname, key.get_name(), key)
            self.filename.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.filename.with_suffix(".tmp")
            keys.save(str(temporary))
            os.replace(temporary, self.filename)
        client.get_host_keys().add(hostname, key.get_name(), key)


def safe_name(text):
    return re.sub(r'[^A-Za-z0-9\u4e00-\u9fff._-]', "_", text)[:100].strip(". ") or "device"


def collect(device, credentials, folder, index, known_hosts, confirm, cancel, timeout=180, encoding="utf-8", on_status=None):
    client = paramiko.SSHClient()
    base = Path(folder) / f"{index:04d}_{safe_name(device.name)}_{safe_name(device.host)}"
    result = dict(name=device.name, host=device.host, platform=device.platform,
                  status="失败", file="", message="", sha256="")
    stage = "准备连接"
    def status(value):
        nonlocal stage
        stage = value
        if on_status:
            on_status(value)
    try:
        if cancel.is_set():
            raise CollectionError("已停止")
        username, password = credentials
        with HOST_LOCK:
            if Path(known_hosts).exists():
                client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(ConfirmHostKey(known_hosts, confirm, cancel))
        status("SSH 连接 / 认证")
        client.connect(device.host, port=device.port, username=username, password=password,
                       timeout=15, banner_timeout=15, auth_timeout=15, channel_timeout=15,
                       allow_agent=False, look_for_keys=False)
        transport = client.get_transport()
        transport.set_keepalive(15)
        channel = client.invoke_shell(width=240, height=1000)
        channel.settimeout(15)
        status("等待登录提示符")
        shell = Shell(channel, device.platform, cancel, min(timeout, 30), encoding)
        shell.receive()
        shell.timeout = timeout
        pagination, command = PROFILES[device.platform]
        warning = ""
        try:
            status("设置会话分页")
            shell.command(pagination)
        except CollectionError as exc:
            if not str(exc).startswith("设备拒绝命令"):
                raise
            warning = "关闭分页命令不支持，已使用空格翻页；"
        status("读取运行配置")
        config = shell.command(command)
        status("校验 / 保存配置")
        validate_config(config, device.platform)
        content = (config + "\n").encode("utf-8")
        destination = Path(str(base) + ".txt")
        temporary = Path(str(base) + ".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, destination)
        result.update(status="成功", file=destination.name,
                      message=warning + "已确认命令提示符和配置特征；未验证可恢复性",
                      sha256=hashlib.sha256(content).hexdigest())
    except paramiko.BadHostKeyException:
        result["message"] = "SSH 主机密钥已变化，连接被拒绝；请核实设备身份"
    except paramiko.AuthenticationException:
        result["message"] = "SSH 认证失败，请检查账号、密码和权限"
    except CollectionError as exc:
        result["message"] = str(exc)
        if str(exc) == "已停止":
            result["status"] = "已停止"
        if exc.partial:
            partial = Path(str(base) + ".partial.txt")
            try:
                partial.write_text(exc.partial, encoding="utf-8")
                result["file"] = partial.name
            except OSError:
                result["message"] += "；残缺输出保存失败"
    except (socket.timeout, TimeoutError):
        result["message"] = "网络连接或 SSH 协商超时"
    except (paramiko.SSHException, OSError, EOFError) as exc:
        # Do not include arbitrary SSH banners or credentials in reports.
        result["message"] = f"连接、SSH 协商或文件写入失败（{type(exc).__name__}）"
    except Exception as exc:
        result["message"] = f"采集异常（{type(exc).__name__}）"
    finally:
        client.close()
    if result["status"] == "失败":
        result["message"] = stage + "：" + result["message"]
    return result


def write_summary(folder, results):
    fields = ("name", "host", "platform", "status", "file", "message", "sha256")
    destination = Path(folder) / "results.csv"
    temporary = destination.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            # Prevent spreadsheet formula interpretation for names imported from inventory.
            writer.writerow({k: ("'" + str(v) if str(v).startswith(("=", "+", "-", "@")) else v)
                             for k, v in result.items()})
    os.replace(temporary, destination)


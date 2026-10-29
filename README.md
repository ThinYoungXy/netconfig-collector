# NetConfig Collector

Windows 图形界面的网络设备配置批量采集工具。当前版本：**1.6.0**。

支持华为 NE / AR / CE / USG、H3C Comware 和山石 StoneOS 的通用 SSH 采集流程。

![界面预览（示例数据）](docs/preview.png)

## 功能

- 项目设备库：清单自动保存，重启恢复上次选择，选择项目即可采集。

- CSV 导入、手动添加、编辑设备、保存清单。
- 凭据组管理，登录密码仅保存在本次会话内存中。
- 并发 SSH 采集、阶段状态、停止操作、失败设备重试。
- 每次采集或重试使用独立目录，单台配置文件及 CSV 结果汇总。
- 华为 USG 的 HRP_M / HRP_S 提示符识别。
- 首次 SSH 指纹默认自动信任，可改为手动确认；已知密钥变化仍拒绝连接。

## 从源码运行

需要 Windows 和带 Tkinter 的 Python 3.12。仓库不包含构建产物。

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r source/requirements-build.txt
.\.venv\Scripts\python.exe source/app.py
~~~

devices.csv 是模板，地址 192.0.2.x 必须替换为实际设备地址。
先新建项目并导入设备清单；每次启动填写项目登录凭据后，选择项目采集。
详细流程见 [使用说明](docs/使用说明.md)。

## 测试

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s source -p "test_*.py"
~~~

测试需要可创建桌面窗口的 Windows 会话。78 项测试覆盖采集、界面编辑及失败重试。
SSH 测试使用本机模拟服务器，不连接业务设备。测试中凭据均为虚构数据。

## 构建 Windows EXE

~~~powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name NetConfigCollector --distpath dist --workpath build --specpath build source/app.py
.\dist\NetConfigCollector.exe --smoke-test
~~~

## 范围与限制

- 仅采集当前运行配置，不执行 save、commit 或配置恢复。
- 适配以通用 CLI 为基础；模拟测试不替代真实设备验证。
- 采集文件可能包含设备密钥等敏感配置，本工具不加密或脱敏这些文件。
- 实际清单、凭据、采集结果及 known_hosts 不应提交到仓库。
- 第三方依赖许可文本位于 [source/third-party-licenses](source/third-party-licenses)。

## 文件

- source/app.py：图形界面与批量任务。
- source/collector.py：SSH、提示符、分页、配置校验。
- source/test_*.py：自动化测试。
- docs/使用说明.md：使用说明与版本更新。

## 本地设备库

设备清单保存于 %LOCALAPPDATA%\NetConfigCollector\inventory.json，
上一版备份为 inventory.json.bak。该目录独立于程序解压目录，升级程序可继续使用。
仅保存项目及设备名称 / 地址 / 类型 / 端口 / 凭据组名称，不保存登录密码。

采集结果目录：保存目录 / 项目 / 时间戳。
删除项目只删除设备库条目，保留已有采集文件。
设备库属于本地业务数据，请勿提交到公开仓库。


## v1.6.0：只按项目管理

新建项目，添加或导入设备，设置本次登录凭据，点击“采集当前项目”。
项目清单自动保存，重启恢复上次选择。项目管理菜单可重命名或删除项目。
CSV 导入仅替换当前项目，失败重试仅处理当前项目显示的失败设备。
采集中不能切换项目；切换后清空本次状态，历史文件保留。
账号和密码仅存本次会话，各项目凭据独立。

结果目录：保存目录 / 项目名称 / 时间戳；重试使用 retry_时间戳。
重命名项目不移动已有结果。至少保留一个项目。

设备库仍在 %LOCALAPPDATA%\NetConfigCollector\inventory.json。
首次打开旧版设备库时自动迁移：单区域项目保留原项目名；多区域分别变为“原项目 - 原区域”。
名称超过 40 字符时截短，重名自动增加编号；各清单原样保留，不合并或去重。
迁移前原文件另存 inventory.v1.bak，此备份不会被日常保存覆盖。
日常上一版本仍备份为 inventory.json.bak。迁移后不要用旧版程序编辑新版设备库。
设备库损坏时程序报告错误并保留原文件；检测到其他窗口修改时拒绝覆盖。
历史采集目录保持原样。全新安装不会自动导入 CSV，请自行导入当前项目。

导出清单与模板位于“更多”菜单，连接参数位于“连接选项”。

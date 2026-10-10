# 从源码运行或重建

构建环境：Windows x64，Python 3.12.14，Paramiko 5.0.0，PyInstaller 6.22.3。
不需要管理员权限即可运行程序；部署目录和输出目录需要当前用户可写。

在 source 目录打开 PowerShell：

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
.\.venv\Scripts\python.exe app.py
~~~

生成单文件 EXE：

~~~powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name NetConfigCollector --distpath .. --workpath build app.py
..\NetConfigCollector.exe --smoke-test
~~~

source/collector.py 是采集逻辑，source/app.py 是 Tkinter 界面。
test_collector.py 包含本地 SSH 模拟服务器，不连接实际业务设备；测试账号是仅用于本地模拟的虚构数据。
third-party-licenses/ 提供依赖的许可文本。


# 构建新版桌面工具

Windows x64，Python 3.12，Edge WebView2 Runtime，.NET Framework 4.6.2+。
在仓库根目录运行：

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r source/requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -s source -p "test_*.py"
.\.venv\Scripts\python.exe source/desktop.py
$desktopHtml = (Resolve-Path source/desktop.html).Path
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name NetConfigCollector --add-data "${desktopHtml}:." --distpath dist --workpath build --specpath build source/desktop.py
.\dist\NetConfigCollector.exe --smoke-test
~~~

--smoke-test 使用临时设备库，验证 WebView 和 JS/Python 桥接已加载后自动关闭；不访问真实设备库。
pywebview 自带 PyInstaller hook，包含平台 DLL 和桥接脚本。WebView2 Runtime 为系统前置依赖，不打入程序。
源码中保留的 app.py 为旧 Tkinter 入口，正式包从 desktop.py 构建。

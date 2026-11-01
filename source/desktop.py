"""Windows desktop entry point for the local WebView UI."""
import os
import sys
import tempfile
import threading
from pathlib import Path

from desktop_api import DesktopAPI


def main():
    import webview
    smoke = '--smoke-test' in sys.argv
    temporary = tempfile.TemporaryDirectory() if smoke else None
    state = Path(temporary.name) if temporary else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NetConfigCollector'
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
    try:
        api = DesktopAPI(state, base / 'backups')
        html = (Path(__file__).parent / 'desktop.html').read_text(encoding='utf-8-sig')
        window = webview.create_window('NetConfig · 设备配置采集器 2.0.1', html=html, js_api=api,
                                       width=1360, height=900, min_size=(1020, 720), background_color='#151619')
        api._window = window
        def closing():
            if api._running and not window.create_confirmation_dialog('采集尚未结束', '退出会中断正在进行的采集，确定退出吗？'):
                return False
            api._close()
        window.events.closing += closing
        def check():
            if smoke:
                # Wait for the JS bridge and initial state to render, not only a native window.
                import time
                for _ in range(100):
                    try:
                        if window.evaluate_js('window.appReady === true'):
                            window.destroy()
                            return
                    except Exception:
                        pass
                    time.sleep(.1)
                os._exit(2)
        webview.start(check, gui='edgechromium', private_mode=True, debug=False)
    except Exception as exc:
        if smoke:
            raise
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,
            f'程序未能启动：{exc}\n\n本版本需要 Microsoft Edge WebView2 Runtime。设备库不会被清空。',
            'NetConfig 启动失败', 0x10)
        return 1
    finally:
        if temporary:
            temporary.cleanup()
    return 0


if __name__ == '__main__':
    sys.exit(main())

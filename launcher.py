"""打包入口（瘦启动器）。

负责：若用户数据目录里存在「比包内自带版本更新」的代码补丁（app_override/），
就让这些 Python 模块优先加载（盖过包内的旧代码），然后再启动真正的客户端。

这样「检查更新」只需下载几百 KB 的代码补丁解压到用户目录即可生效，
无需重新下载 360MB 整包、也无需自我替换 exe。
"""
import os
import sys

# 只对「本项目自己的」顶层包/模块启用补丁；第三方库（customtkinter/playwright 等）
# 始终从包内加载，保证依赖完整。
_APP_PREFIXES = {"desktop", "web", "dy_apis", "dy_live", "utils", "builder", "static", "main"}


def _user_data_dir():
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/liangbashuazi")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "liangbashuazi")
    else:
        base = os.path.expanduser("~/.liangbashuazi")
    return base


def _read_code_version(path):
    try:
        with open(os.path.join(path, "code_version.txt"), encoding="utf-8") as fh:
            return int((fh.read() or "0").strip() or 0)
    except Exception:
        return 0


def _bundled_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def install_override():
    """若 app_override 版本 > 包内自带版本，则插入自定义查找器优先加载补丁代码。
    返回启用的 override 目录或 None。"""
    override = os.path.join(_user_data_dir(), "app_override")
    if not os.path.isdir(override):
        return None
    if _read_code_version(override) <= _read_code_version(_bundled_dir()):
        return None  # 没有更新（或补丁更旧），用包内自带

    import importlib.abc
    import importlib.machinery

    class _OverrideFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name.split(".")[0] not in _APP_PREFIXES:
                return None  # 第三方库不拦截，交给后面的查找器（包内）
            search = [override] if path is None else list(path)
            try:
                return importlib.machinery.PathFinder.find_spec(name, search)
            except Exception:
                return None

    sys.meta_path.insert(0, _OverrideFinder())
    return override


def main():
    try:
        install_override()
    except Exception:
        pass  # 补丁加载失败也不影响：退回包内自带代码
    from desktop.client import main as app_main
    app_main()


if __name__ == "__main__":
    main()

from dataclasses import dataclass
import os
import sys

from web.config import WebConfig
from web.db import connect_db, init_db
from web.services.agent_acquisition_service import AgentAcquisitionService
from web.services.crawl_service import CrawlService
from web.services.im_service import IMService
from web.services.lead_scoring_service import LeadScoringService
from web.services.live_service import LiveService
from web.services.login_service import LoginService
from web.services.session_service import SessionService
from web.tasks.broker import EventBroker
from web.tasks.manager import TaskManager


@dataclass(frozen=True)
class DesktopServices:
    app: object | None
    config: object
    agent: object
    session: object
    live: object
    im: object
    task_manager: object
    broker: object | None = None
    login: object | None = None

    @property
    def login_service(self):
        return self.login

    @property
    def session_service(self):
        return self.session

    @property
    def live_service(self):
        return self.live

    @property
    def im_service(self):
        return self.im


def build_services(overrides=None):
    overrides = overrides or {}
    config = WebConfig(overrides)
    _configure_proxy_behavior(config)
    _configure_playwright_behavior(config)

    with connect_db(config.db_path) as conn:
        init_db(conn)

    broker = EventBroker()
    task_manager = TaskManager(config.db_path, broker=broker)
    session = SessionService(config.db_path)
    crawl = CrawlService(config, session, task_manager)
    live = LiveService(config.db_path, session, task_manager, broker)
    im = IMService(config.db_path, session, task_manager, broker)
    login = LoginService(config.db_path, task_manager, broker)
    scoring = LeadScoringService()
    agent = AgentAcquisitionService(
        config.db_path,
        config.project_root / "datas" / "runtime",
        task_manager,
        crawl,
        scoring,
        im,
        live,
    )

    return DesktopServices(
        app=None,
        config=config,
        agent=agent,
        session=session,
        live=live,
        im=im,
        task_manager=task_manager,
        broker=broker,
        login=login,
    )


def _configure_proxy_behavior(config: WebConfig):
    if config.use_system_proxy:
        return
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def _configure_playwright_behavior(config: WebConfig):
    if getattr(sys, "frozen", False):
        _setup_frozen_runtime()
        return
    local_browser_dir = config.project_root / ".playwright"
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and local_browser_dir.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browser_dir)


def _frozen_browsers_dir():
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/liangbashuazi")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "liangbashuazi")
    else:
        base = os.path.expanduser("~/.liangbashuazi")
    return os.path.join(base, "ms-playwright")


def _setup_frozen_runtime():
    # 1) execjs 复用 playwright 自带 node（用户免装 Node.js）
    try:
        from playwright._impl._driver import compute_driver_executable
        node_path = compute_driver_executable()[0]
        node_dir = os.path.dirname(node_path)
        os.environ["PATH"] = node_dir + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass
    # 2) chromium：优先用「打包进发行包」的内核（用户免下载）；没有才回退用户目录（首次下载）
    bundled = None
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = os.path.join(meipass, "ms-playwright")
        if os.path.isdir(cand):
            bundled = cand
    if bundled:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled
    else:
        browsers = _frozen_browsers_dir()
        try:
            os.makedirs(browsers, exist_ok=True)
        except Exception:
            pass
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers


def ensure_chromium(on_progress=None):
    """检测 chromium，缺则用 playwright 自带 driver 下载。返回 (ok, message)。
    开发环境(.playwright 已有 chromium)会直接返回 True，不下载。
    on_progress(text)：可选回调，下载过程中持续上报「正在下载…NN%」等文案。"""
    def report(text):
        if on_progress:
            try:
                on_progress(text)
            except Exception:
                pass

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            if exe and os.path.exists(exe):
                return True, "浏览器已就绪"
    except Exception:
        pass
    try:
        import re
        import subprocess
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        drv = compute_driver_executable()
        base_env = get_driver_env()
        if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            base_env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]

        # 下载源：用户若自定义了 PLAYWRIGHT_DOWNLOAD_HOST 则只用它；
        # 否则「国内镜像优先 → 官方 CDN 兜底」（官方 CDN 在国内常下到一半失败）。
        user_host = os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST")
        if user_host:
            hosts = [user_host]
        else:
            hosts = ["https://cdn.npmmirror.com/binaries/playwright", ""]

        # 隐藏 Windows 上子进程弹出的黑色控制台窗口
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

        last = ""
        for hi, host in enumerate(hosts):
            env = dict(base_env)
            if host:
                env["PLAYWRIGHT_DOWNLOAD_HOST"] = host
            else:
                env.pop("PLAYWRIGHT_DOWNLOAD_HOST", None)
            src_label = "国内镜像" if host else "官方源"
            report(f"正在准备浏览器内核（{src_label}，约 180MB）…")
            proc = subprocess.Popen(
                [*drv, "install", "chromium"], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags,
            )
            buf = ""
            last_pct = -1
            lines = []
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                if ch in ("\r", "\n"):
                    seg = buf.strip()
                    buf = ""
                    if not seg:
                        continue
                    lines.append(seg)
                    m = re.search(r"(\d+)%", seg)
                    if m:
                        pct = int(m.group(1))
                        if pct != last_pct:
                            last_pct = pct
                            report(f"正在下载浏览器内核（{src_label}）… {pct}%")
                    elif "ownload" in seg or "下载" in seg:
                        report(f"正在下载浏览器内核（{src_label}）…")
                else:
                    buf += ch
            proc.wait()
            if proc.returncode == 0:
                return True, "浏览器下载完成"
            last = "\n".join(lines[-6:])[-300:]
            if hi < len(hosts) - 1:
                report("该下载源失败，正在切换备用源重试…")
        return False, last
    except Exception as exc:
        return False, str(exc)

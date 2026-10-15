"""Windows window and local server for the existing Predict.fun dashboard."""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import uvicorn
from fastapi import Request
from fastapi.responses import PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from predict_mm.web import create_app


APP_TITLE = "Predict MM"
SHUTDOWN_TIMEOUT_SECONDS = 120


def user_data_directory() -> Path:
    """Keep credentials, configuration, logs, and the order journal outside the exe."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    return (Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local") / "PredictMMBot"


def protect_local_app(app, base_url: str) -> None:
    """Reject browser requests from unrelated sites and unexpected Host headers."""
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])

    @app.middleware("http")
    async def check_browser_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            if (origin and origin != base_url) or (
                fetch_site and fetch_site not in {"same-origin", "none"}
            ):
                return PlainTextResponse("Forbidden", status_code=403)
        return await call_next(request)


class DesktopServer:
    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        port = self.listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.app = create_app()
        protect_local_app(self.app, self.url)
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=port, access_log=False,
            log_config=None, lifespan="on",
        ))
        self.loop: asyncio.AbstractEventLoop | None = None
        self.error: BaseException | None = None
        # Keep the process alive if a window error occurs while the bot still
        # needs time to cancel orders during shutdown.
        self.thread = threading.Thread(target=self._run, name="predict-mm-web")

    def _run(self) -> None:
        async def serve() -> None:
            self.loop = asyncio.get_running_loop()
            await self.server.serve(sockets=[self.listener])

        try:
            asyncio.run(serve())
        except BaseException as error:
            self.error = error
            logging.getLogger("predict-mm").exception("桌面版网页服务启动失败")
        finally:
            self.listener.close()

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive():
                break
            time.sleep(0.05)
        self.server.should_exit = True
        self.thread.join(timeout=2)
        raise RuntimeError(f"本地网页服务未能启动：{self.error or '启动超时'}")

    async def _stop_bot(self) -> None:
        state = self.app.state.dashboard
        await state.stop()
        if state.task is not None:
            await asyncio.shield(state.task)

    def stop(self) -> None:
        if not self.thread.is_alive():
            return
        if self.loop is None:
            raise RuntimeError("本地网页服务尚未准备好，无法确认撤单状态。")
        future = asyncio.run_coroutine_threadsafe(self._stop_bot(), self.loop)
        try:
            future.result(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except FutureTimeoutError as error:
            raise RuntimeError("等待机器人停止和撤单超时；窗口仍保持打开，请查看运行日志。") from error
        self.server.should_exit = True
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RuntimeError("网页服务仍在关闭中，请稍后重试。")


@contextmanager
def single_instance() -> Iterator[None]:
    """Prevent two desktop windows from trading against the same local journal."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(None, False, "Local\\PredictMMBotDesktop")
    if not handle:
        raise OSError("无法创建应用程序锁。")
    already_running = kernel32.GetLastError() == 183
    if already_running:
        kernel32.CloseHandle(handle)
        raise RuntimeError("应用程序已经打开，请使用现有窗口。")
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


def localized_error(message: str) -> str:
    try:
        preferences = json.loads((user_data_directory() / ".predict-mm-ui.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return message
    if not isinstance(preferences, dict) or preferences.get("language") != "en":
        return message
    translations = {
        "应用程序无法启动：": "Could not start the application: ",
        "应用程序已经打开，请使用现有窗口。": "The application is already open. Please use the existing window.",
        "无法创建应用程序锁。": "Could not create the application lock.",
        "本地网页服务未能启动：": "The local dashboard could not start: ",
        "启动超时": "Startup timed out",
        "本地网页服务尚未准备好，无法确认撤单状态。": "The dashboard is not ready; order cancellation could not be confirmed.",
        "等待机器人停止和撤单超时；窗口仍保持打开，请查看运行日志。": "Timed out waiting for the bot to stop and cancel orders. The window remains open; check the activity logs.",
        "网页服务仍在关闭中，请稍后重试。": "The dashboard is still closing. Please try again shortly.",
    }
    for source, translation in translations.items():
        message = message.replace(source, translation)
    return message


def show_error(message: str) -> None:
    message = localized_error(message)
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)
    else:
        print(message, file=sys.stderr)


def smoke_test() -> None:
    """Check that the frozen exe contains and serves the dashboard without trading."""
    previous_directory = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="predict-mm-desktop-") as directory:
        try:
            os.chdir(directory)
            desktop = DesktopServer()
            desktop.start()
            try:
                with urllib.request.urlopen(desktop.url, timeout=10) as response:
                    if b"Predict.fun" not in response.read():
                        raise RuntimeError("打包的网页文件不可用。")
            finally:
                desktop.stop()
        finally:
            os.chdir(previous_directory)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("桌面版入口目前仅支持 Windows。")
    # PyInstaller's windowed executable has no standard streams.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    is_smoke_test = "--smoke-test" in sys.argv
    try:
        import webview

        if is_smoke_test:
            smoke_test()
            return

        with single_instance():
            data_dir = user_data_directory()
            data_dir.mkdir(parents=True, exist_ok=True)
            os.chdir(data_dir)
            desktop = DesktopServer()
            desktop.start()
            try:
                window = webview.create_window(
                    APP_TITLE, desktop.url, width=1440, height=900,
                    min_size=(950, 650), background_color="#10141d",
                )

                def on_closing() -> bool:
                    try:
                        desktop.stop()
                    except Exception as error:  # noqa: BLE001
                        show_error(str(error))
                        return False
                    return True

                window.events.closing += on_closing
                webview.start(gui="edgechromium", private_mode=True)
            finally:
                if desktop.thread.is_alive() and not desktop.server.should_exit:
                    desktop.stop()
    except Exception as error:  # noqa: BLE001
        if is_smoke_test:
            log_path = os.environ.get("PREDICT_MM_SMOKE_LOG")
            if log_path:
                Path(log_path).write_text(traceback.format_exc(), encoding="utf-8")
            raise SystemExit(1) from error
        show_error(f"应用程序无法启动：{error}")
        raise SystemExit(1) from error

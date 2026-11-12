"""
WINDO AI 스마트 가상 창문 전용 데스크톱 런처 (launcher.py)
- 기존 main.py의 마이크/TTS/GPU/웹검색 환경을 100% 보존하면서
  버튼 클릭 한 번으로 서버 구동 및 브라우저 화면을 열어줍니다.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
VENV_PYTHON = BASE_DIR / ".venv" / "Scripts" / "python.exe"
MAIN_SCRIPT = BASE_DIR / "main.py"
APP_URL = "http://127.0.0.1:5001"
WINDO_PORT = 5001
COMFY_PORT = 8188


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    """지정된 로컬 포트가 열려 있는지 확인"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_chromium_browser() -> str | None:
    """주소창 없는 전용 앱 모드(--app)를 지원하는 Chrome 또는 Edge 실행 파일 탐색"""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return shutil.which("chrome") or shutil.which("msedge")


def open_app_window(fullscreen: bool = False) -> None:
    """주소창/탭이 없는 스마트 창문 전용 브라우저 창으로 열기"""
    browser_exe = find_chromium_browser()
    if browser_exe:
        args = [
            browser_exe,
            f"--app={APP_URL}",
            "--window-size=1600,940",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if fullscreen:
            args.append("--start-fullscreen")
        try:
            subprocess.Popen(args, cwd=str(BASE_DIR))
            return
        except Exception:
            pass
    webbrowser.open(APP_URL)


class WindoLauncherApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("WINDO 스마트 창문 런처")
        self.root.geometry("460x420")
        self.root.resizable(False, False)
        self.root.configure(bg="#0f172a")

        self.server_proc: subprocess.Popen | None = None
        self.is_starting = False

        self._build_ui()
        self._poll_status()

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg="#0f172a", pady=16)
        header.pack(fill="x")

        title_lbl = tk.Label(
            header,
            text="🪟 WINDO 스마트 창문 컨트롤러",
            font=("Malgun Gothic", 15, "bold"),
            fg="#f8fafc",
            bg="#0f172a",
        )
        title_lbl.pack()

        sub_lbl = tk.Label(
            header,
            text="버튼 하나로 서버 구동 및 전용 창문 화면을 즉시 실행합니다",
            font=("Malgun Gothic", 9),
            fg="#94a3b8",
            bg="#0f172a",
        )
        sub_lbl.pack(pady=(4, 0))

        status_card = tk.Frame(
            self.root,
            bg="#1e293b",
            highlightbackground="#334155",
            highlightthickness=1,
            padx=16,
            pady=12,
        )
        status_card.pack(fill="x", padx=24, pady=(0, 14))

        row1 = tk.Frame(status_card, bg="#1e293b")
        row1.pack(fill="x", pady=2)
        tk.Label(
            row1,
            text="WINDO 메인 서버 (127.0.0.1:5001)",
            font=("Malgun Gothic", 10, "bold"),
            fg="#e2e8f0",
            bg="#1e293b",
        ).pack(side="left")
        self.server_status_lbl = tk.Label(
            row1,
            text="확인 중...",
            font=("Malgun Gothic", 10, "bold"),
            fg="#38bdf8",
            bg="#1e293b",
        )
        self.server_status_lbl.pack(side="right")

        row2 = tk.Frame(status_card, bg="#1e293b")
        row2.pack(fill="x", pady=2)
        tk.Label(
            row2,
            text="ComfyUI 생성 엔진 (127.0.0.1:8188)",
            font=("Malgun Gothic", 9),
            fg="#94a3b8",
            bg="#1e293b",
        ).pack(side="left")
        self.comfy_status_lbl = tk.Label(
            row2,
            text="확인 중...",
            font=("Malgun Gothic", 9, "bold"),
            fg="#94a3b8",
            bg="#1e293b",
        )
        self.comfy_status_lbl.pack(side="right")

        btn_frame = tk.Frame(self.root, bg="#0f172a", padx=24)
        btn_frame.pack(fill="both", expand=True)

        self.launch_btn = tk.Button(
            btn_frame,
            text="🚀  WINDO 스마트 창문 바로 열기 (전용 창)",
            font=("Malgun Gothic", 11, "bold"),
            fg="#ffffff",
            bg="#2563eb",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief="flat",
            cursor="hand2",
            pady=11,
            command=lambda: self.start_and_open(mode="app"),
        )
        self.launch_btn.pack(fill="x", pady=(0, 8))

        self.fullscreen_btn = tk.Button(
            btn_frame,
            text="🖥️  전체화면 모드로 스마트 창문 열기",
            font=("Malgun Gothic", 10, "bold"),
            fg="#e2e8f0",
            bg="#334155",
            activebackground="#475569",
            activeforeground="#ffffff",
            relief="flat",
            cursor="hand2",
            pady=8,
            command=lambda: self.start_and_open(mode="fullscreen"),
        )
        self.fullscreen_btn.pack(fill="x", pady=(0, 8))

        self.browser_btn = tk.Button(
            btn_frame,
            text="🌐  일반 웹 브라우저 탭으로 열기",
            font=("Malgun Gothic", 10),
            fg="#cbd5e1",
            bg="#1e293b",
            activebackground="#334155",
            activeforeground="#ffffff",
            relief="flat",
            cursor="hand2",
            pady=8,
            command=lambda: self.start_and_open(mode="tab"),
        )
        self.browser_btn.pack(fill="x", pady=(0, 12))

        ctrl_frame = tk.Frame(btn_frame, bg="#0f172a")
        ctrl_frame.pack(fill="x")

        self.restart_btn = tk.Button(
            ctrl_frame,
            text="🔄 서버 재시작 (초기화)",
            font=("Malgun Gothic", 9, "bold"),
            fg="#38bdf8",
            bg="#1e293b",
            activebackground="#334155",
            activeforeground="#38bdf8",
            relief="flat",
            cursor="hand2",
            pady=7,
            command=self.restart_server,
        )
        self.restart_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.stop_btn = tk.Button(
            ctrl_frame,
            text="⏹️ 서버 종료",
            font=("Malgun Gothic", 9, "bold"),
            fg="#f87171",
            bg="#1e293b",
            activebackground="#334155",
            activeforeground="#f87171",
            relief="flat",
            cursor="hand2",
            pady=7,
            command=self.stop_server,
        )
        self.stop_btn.pack(side="right", fill="x", expand=True, padx=(6, 0))

        self.footer_lbl = tk.Label(
            self.root,
            text="준비 완료 · 버튼을 눌러 스마트 창문을 시작하세요",
            font=("Malgun Gothic", 8),
            fg="#64748b",
            bg="#0f172a",
            pady=8,
        )
        self.footer_lbl.pack(side="bottom")

    def _ensure_server_running(self) -> bool:
        if is_port_open(WINDO_PORT):
            return True
        if self.is_starting:
            for _ in range(30):
                if is_port_open(WINDO_PORT):
                    return True
                time.sleep(0.3)
            return is_port_open(WINDO_PORT)

        self.is_starting = True
        try:
            py_exe = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
            self.server_proc = subprocess.Popen(
                [py_exe, str(MAIN_SCRIPT)],
                cwd=str(BASE_DIR),
            )
            for _ in range(40):
                if is_port_open(WINDO_PORT):
                    return True
                time.sleep(0.25)
            return is_port_open(WINDO_PORT)
        finally:
            self.is_starting = False

    def start_and_open(self, mode: str = "app") -> None:
        self.footer_lbl.config(text="WINDO 스마트 창문 화면을 준비하고 있습니다...", fg="#38bdf8")

        def _worker() -> None:
            ok = self._ensure_server_running()
            if ok:
                if mode == "app":
                    open_app_window(fullscreen=False)
                elif mode == "fullscreen":
                    open_app_window(fullscreen=True)
                else:
                    webbrowser.open(APP_URL)
                self.root.after(
                    0,
                    lambda: self.footer_lbl.config(
                        text="✅ WINDO 스마트 창문이 실행되었습니다! (http://127.0.0.1:5001)",
                        fg="#4ade80",
                    ),
                )
            else:
                self.root.after(
                    0,
                    lambda: self.footer_lbl.config(
                        text="❌ 서버 구동 시간 초과. 다시 한 번 버튼을 눌러주세요.",
                        fg="#f87171",
                    ),
                )

        threading.Thread(target=_worker, daemon=True).start()

    def stop_server(self) -> None:
        self.footer_lbl.config(text="서버를 종료하는 중...", fg="#f87171")

        def _kill() -> None:
            if self.server_proc and self.server_proc.poll() is None:
                try:
                    self.server_proc.terminate()
                except Exception:
                    pass
            if os.name == "nt":
                try:
                    out = subprocess.check_output(
                        "netstat -ano | findstr :5001",
                        shell=True,
                        text=True,
                        stderr=subprocess.DEVNULL,
                    )
                    for line in out.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 5 and "LISTENING" in line:
                            pid = parts[-1]
                            if pid.isdigit() and int(pid) > 0:
                                subprocess.run(
                                    f"taskkill /F /PID {pid}",
                                    shell=True,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                except Exception:
                    pass
            self.root.after(
                0,
                lambda: self.footer_lbl.config(text="⏹️ WINDO 서버가 종료되었습니다.", fg="#94a3b8"),
            )

        threading.Thread(target=_kill, daemon=True).start()

    def restart_server(self) -> None:
        self.footer_lbl.config(text="🔄 서버를 재시작하는 중...", fg="#38bdf8")

        def _restart() -> None:
            self.stop_server()
            time.sleep(0.8)
            ok = self._ensure_server_running()
            msg = "✅ 서버 재시작 완료 (기본 대기 화면으로 초기화됨)!" if ok else "⚠️ 서버 재시작 대기 중..."
            self.root.after(0, lambda: self.footer_lbl.config(text=msg, fg="#4ade80"))

        threading.Thread(target=_restart, daemon=True).start()

    def _poll_status(self) -> None:
        windo_up = is_port_open(WINDO_PORT)
        comfy_up = is_port_open(COMFY_PORT)

        if windo_up:
            self.server_status_lbl.config(text="● 실행 중 (ON)", fg="#4ade80")
        elif self.is_starting:
            self.server_status_lbl.config(text="◐ 시동 중...", fg="#facc15")
        else:
            self.server_status_lbl.config(text="○ 정지됨 (OFF)", fg="#f87171")

        if comfy_up:
            self.comfy_status_lbl.config(text="● 연결됨 (RTX 5080)", fg="#38bdf8")
        else:
            self.comfy_status_lbl.config(text="○ 대기/오프라인", fg="#64748b")

        self.root.after(1500, self._poll_status)


if __name__ == "__main__":
    tk_root = tk.Tk()
    app = WindoLauncherApp(tk_root)
    tk_root.mainloop()

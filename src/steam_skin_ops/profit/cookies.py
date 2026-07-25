# -*- coding: utf-8 -*-
"""
本地 Cookie 提取工具
====================
通过 Chrome 远程调试协议（CDP）从浏览器中提取
buff.163.com 和 steamcommunity.com 的登录凭证，
写入 config/profit.yaml。

使用方法：
1. 关闭所有 Chrome 窗口
2. 运行本脚本（会自动以调试模式启动 Chrome）
3. 在自动打开的 Chrome 中确认已登录 BUFF 和 Steam
4. 回到本窗口按 Enter，脚本自动提取并写入 config/profit.yaml

完全在本机运行，不经过任何网络或 AI 中转。

依赖安装：
    pip install pyyaml requests
"""

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

# 强制 UTF-8 输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Chrome 调试端口
CDP_PORT = 9222
CDP_BASE = f"http://localhost:{CDP_PORT}"

# Chrome 可能的安装路径
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
]


def find_chrome() -> str | None:
    """查找 Chrome 可执行文件路径"""
    for p in CHROME_PATHS:
        if Path(p).exists():
            return str(p)
    # 从注册表查找
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
        )
        path, _ = winreg.QueryValueEx(key, "")
        if Path(path).exists():
            return path
    except Exception:
        pass
    return None


def launch_chrome_debug(chrome_path: str) -> subprocess.Popen:
    """以远程调试模式启动 Chrome"""
    # 使用独立的用户数据目录，避免与正在运行的 Chrome 冲突
    user_data_dir = Path(os.environ["TEMP"]) / "chrome_cookie_debug"
    user_data_dir.mkdir(exist_ok=True)

    cmd = [
        chrome_path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://buff.163.com",  # 首页打开 BUFF
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def wait_for_chrome(timeout: int = 15) -> bool:
    """等待 Chrome 调试接口就绪"""
    for _ in range(timeout):
        try:
            resp = requests.get(f"{CDP_BASE}/json/version", timeout=2)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def get_cookies_via_cdp(domain: str, cookie_names: list[str]) -> dict[str, str]:
    """
    通过 CDP Protocol 获取指定域名的 Cookie

    Returns:
        {cookie_name: cookie_value}
    """
    # 获取所有页面列表
    tabs = requests.get(f"{CDP_BASE}/json", timeout=5).json()
    if not tabs:
        raise RuntimeError("没有找到 Chrome 页面")

    # 使用第一个普通页面的 WebSocket 连接
    page = next((t for t in tabs if t.get("type") == "page"), tabs[0])
    ws_url = page.get("webSocketDebuggerUrl", "")
    if not ws_url:
        raise RuntimeError("无法获取 WebSocket 调试 URL")

    # 用 HTTP CDP 接口获取 Cookie（更简单，无需 WebSocket）
    # Chrome 提供了 /json/new 和 Network.getAllCookies via REST-like CDP
    # 实际上通过 WebSocket 调用 CDP
    import websocket  # type: ignore
    ws = websocket.create_connection(ws_url, timeout=10)

    # 发送 Network.getCookies 命令
    msg_id = 1
    ws.send(json.dumps({
        "id": msg_id,
        "method": "Network.getCookies",
        "params": {"urls": [f"https://{domain}", f"http://{domain}"]}
    }))

    result = {}
    deadline = time.time() + 10
    while time.time() < deadline:
        raw = ws.recv()
        data = json.loads(raw)
        if data.get("id") == msg_id:
            cookies = data.get("result", {}).get("cookies", [])
            for cookie in cookies:
                if cookie["name"] in cookie_names:
                    result[cookie["name"]] = cookie["value"]
            break

    ws.close()
    return result


def get_cookies_via_cdp_rest(domain: str, cookie_names: list[str]) -> dict[str, str]:
    """
    通过 CDP REST API + Storage.getCookies 获取 Cookie
    （不需要 websocket 库，用 urllib 实现简单 WebSocket 握手）
    """
    import socket
    import base64
    import hashlib
    import struct
    import threading

    tabs = requests.get(f"{CDP_BASE}/json", timeout=5).json()
    page = next((t for t in tabs if t.get("type") == "page"), tabs[0])
    ws_url = page.get("webSocketDebuggerUrl", "")

    # 解析 ws URL
    from urllib.parse import urlparse
    parsed = urlparse(ws_url)
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path

    # 手动 WebSocket 握手
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=10)
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
    sock.send(handshake)

    # 读取握手响应
    resp_buf = b""
    while b"\r\n\r\n" not in resp_buf:
        resp_buf += sock.recv(4096)

    def ws_send(sock, payload: str):
        data = payload.encode()
        mask = os.urandom(4)
        length = len(data)
        if length < 126:
            header = struct.pack("!BB", 0x81, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        sock.send(header + mask + masked)

    def ws_recv(sock) -> str:
        header = sock.recv(2)
        if len(header) < 2:
            return ""
        b1, b2 = header
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", sock.recv(8))[0]
        payload = b""
        while len(payload) < length:
            payload += sock.recv(length - len(payload))
        return payload.decode("utf-8", errors="replace")

    # 发送 CDP 命令
    ws_send(sock, json.dumps({
        "id": 1,
        "method": "Network.getCookies",
        "params": {"urls": [f"https://{domain}", f"https://.{domain}"]}
    }))

    result = {}
    deadline = time.time() + 10
    while time.time() < deadline:
        raw = ws_recv(sock)
        if not raw:
            break
        try:
            data = json.loads(raw)
            if data.get("id") == 1:
                for cookie in data.get("result", {}).get("cookies", []):
                    if cookie["name"] in cookie_names:
                        result[cookie["name"]] = cookie["value"]
                break
        except Exception:
            continue

    sock.close()
    return result


def write_config(config_path: Path, buff_session: str, steam_session_id: str,
                 steam_login_secure: str) -> None:
    """将提取的 Cookie 写入收益配置。"""
    example_path = Path("config/profit.example.yaml")

    if not config_path.exists():
        if example_path.exists():
            shutil.copy2(example_path, config_path)
            print(f"[OK] 已从模板创建 {config_path}")
        else:
            print("[错误] config/profit.example.yaml 不存在")
            return

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    if buff_session:
        config.setdefault("buff", {})["cookie"] = f"session={buff_session}"
    if steam_session_id:
        config.setdefault("steam", {})["session_id"] = steam_session_id
    if steam_login_secure:
        config.setdefault("steam", {})["steam_login_secure"] = steam_login_secure

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"[OK] Cookie 已写入 {config_path.resolve()}")


def main(config_path: Path = Path("config/profit.yaml")) -> None:
    print("=" * 60)
    print("  Chrome Cookie 提取工具（本地 CDP 模式）")
    print("=" * 60)
    print()

    # 检查 Chrome 是否已在调试模式运行
    chrome_already_running = False
    try:
        resp = requests.get(f"{CDP_BASE}/json/version", timeout=2)
        if resp.status_code == 200:
            chrome_already_running = True
            print(f"[OK] 检测到 Chrome 已在调试端口 {CDP_PORT} 运行")
    except Exception:
        pass

    chrome_proc = None
    if not chrome_already_running:
        chrome_path = find_chrome()
        if not chrome_path:
            print("[错误] 找不到 Chrome 浏览器，请手动指定路径")
            sys.exit(1)

        print(f"[1] 正在以调试模式启动 Chrome...")
        print(f"    {chrome_path}")
        print()
        print("    [!] 注意：这会打开一个新的 Chrome 窗口（独立配置）")
        print("    [!] 请在该窗口中登录 BUFF 和 Steam，然后回到这里")
        print()
        chrome_proc = launch_chrome_debug(chrome_path)

        print("[2] 等待 Chrome 启动...")
        if not wait_for_chrome(20):
            print("[错误] Chrome 启动超时，请手动以调试模式运行：")
            print(f'    chrome.exe --remote-debugging-port={CDP_PORT}')
            sys.exit(1)
        print("    [OK] Chrome 已就绪")
        print()
        print("[!] 请在 Chrome 中完成以下操作：")
        print("    1. 登录 https://buff.163.com")
        print("    2. 登录 https://steamcommunity.com")
    else:
        print("[!] 请确认当前 Chrome 中已登录 BUFF 和 Steam")

    print()
    try:
        input("完成后按 Enter 提取 Cookie...")
    except (EOFError, OSError):
        time.sleep(3)
    print()

    # 提取 BUFF Cookie
    print("[2/3] 提取 BUFF Cookie (buff.163.com)...")
    buff_session = ""
    try:
        buff_cookies = get_cookies_via_cdp_rest("buff.163.com", ["session"])
        buff_session = buff_cookies.get("session", "")
        if buff_session:
            print(f"      [OK] session = {buff_session[:8]}...（已截断）")
        else:
            print("      [!] 未找到 session（请确认已登录 BUFF）")
    except Exception as e:
        print(f"      [FAIL] {e}")

    # 提取 Steam Cookie
    print("[3/3] 提取 Steam Cookie (steamcommunity.com)...")
    steam_session_id = ""
    steam_login_secure = ""
    try:
        steam_cookies = get_cookies_via_cdp_rest(
            "steamcommunity.com", ["sessionid", "steamLoginSecure"]
        )
        steam_session_id = steam_cookies.get("sessionid", "")
        steam_login_secure = steam_cookies.get("steamLoginSecure", "")

        if steam_session_id:
            print(f"      [OK] sessionid = {steam_session_id[:8]}...（已截断）")
        else:
            print("      [!] 未找到 sessionid")
        if steam_login_secure:
            print(f"      [OK] steamLoginSecure = {steam_login_secure[:8]}...（已截断）")
        else:
            print("      [!] 未找到 steamLoginSecure（steamLoginSecure 是 HttpOnly，需用 CDP 专用方法）")
    except Exception as e:
        print(f"      [FAIL] {e}")

    print()

    # 关闭调试 Chrome
    if chrome_proc:
        chrome_proc.terminate()

    # 汇总
    missing = []
    if not buff_session: missing.append("BUFF session")
    if not steam_session_id: missing.append("Steam sessionid")
    if not steam_login_secure: missing.append("Steam steamLoginSecure")

    if missing:
        print(f"[!] 以下 Cookie 未能提取：{', '.join(missing)}")
        print(f"    请手动从浏览器 F12 -> Application -> Cookies 复制并填入 {config_path}")
        print()

    if buff_session or steam_session_id or steam_login_secure:
        print(f"正在写入 {config_path}...")
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            write_config(config_path, buff_session, steam_session_id, steam_login_secure)
        except Exception as e:
            print(f"[FAIL] 写入失败: {e}")
    else:
        print("[FAIL] 没有提取到任何 Cookie")

    print()
    print("完成！现在可以运行：python -m steam_skin_ops.profit sync")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
雪球登录助手：打开可见浏览器，用户手动登录后自动抓取 Cookie。
不代填账号密码，不绕过验证码。
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

import requests

_login_lock = threading.Lock()
_cancel_flag = threading.Event()
_worker: Optional[threading.Thread] = None
_browser_ref = None

_login_status: dict[str, Any] = {
    "status": "idle",  # idle | running | success | failed | cancelled
    "message": "尚未开始",
    "started_at": None,
    "finished_at": None,
    "cookie_preview": None,
}


def get_login_status() -> dict[str, Any]:
    return dict(_login_status)


def _set_status(status: str, message: str, cookie_preview: Optional[str] = None) -> None:
    _login_status["status"] = status
    _login_status["message"] = message
    if cookie_preview is not None:
        _login_status["cookie_preview"] = cookie_preview
    if status == "running" and not _login_status.get("started_at"):
        _login_status["started_at"] = datetime.now().isoformat(timespec="seconds")
    if status in ("success", "failed", "cancelled"):
        _login_status["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _xueqiu_cookie_map(cookies: list[dict]) -> dict[str, str]:
    by_name: dict[str, str] = {}
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if "xueqiu.com" not in domain:
            continue
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        if not name or not value:
            continue
        by_name[name] = value
    return by_name


def _cookie_header_from_map(by_name: dict[str, str]) -> str:
    preferred = [
        "xq_a_token",
        "xqat",
        "u",
        "device_id",
        "remember",
        "xq_r_token",
        "xq_id_token",
        "xq_is_login",
        "ssxmod_itna",
        "ssxmod_itna2",
        "acw_tc",
    ]
    parts: list[str] = []
    seen = set()
    for name in preferred:
        if name in by_name and by_name[name]:
            parts.append(f"{name}={by_name[name]}")
            seen.add(name)
    for name, value in by_name.items():
        if name not in seen and value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def cookie_has_required_fields(cookie: str) -> tuple[bool, str]:
    """检查 Cookie 字符串是否包含登录必需字段。"""
    text = cookie or ""
    if "xq_a_token=" not in text:
        return False, "缺少 xq_a_token"
    if not re.search(r"(?:^|[;\s])u=", text):
        return False, "缺少用户标识 u（未完整登录）"
    return True, "ok"


def validate_cookie_alive(cookie: str) -> tuple[bool, str]:
    """
    用真实接口验证 Cookie 是否可用。
    雪球未登录常见：HTTP 400 + error_code=400016
    """
    ok, reason = cookie_has_required_fields(cookie)
    if not ok:
        return False, reason

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": "https://xueqiu.com/",
        "Accept": "application/json, text/plain, */*",
    })
    # 必须写入 cookie jar；放 headers['Cookie'] 会被雪球判未登录(400016)
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            session.cookies.set(name, value, domain=".xueqiu.com", path="/")
    try:
        session.get("https://xueqiu.com/", timeout=15)
        resp = session.get(
            "https://xueqiu.com/cubes/rebalancing/history.json",
            params={"cube_symbol": "ZH000001", "count": 1, "page": 1},
            timeout=15,
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        err = str(payload.get("error_code") or "")
        if err == "400016" or resp.status_code in (401, 403):
            return False, "Cookie 无效或未登录（400016/403），请重新登录雪球"
        if resp.status_code == 400 and err and err != "400016":
            return True, "ok"
        if resp.status_code < 500:
            return True, "ok"
        return False, f"校验失败 HTTP {resp.status_code}"
    except Exception as e:
        return False, f"校验请求异常: {e}"
    finally:
        session.close()


def _looks_logged_in(by_name: dict[str, str], initial_token: Optional[str]) -> bool:
    token = (by_name.get("xq_a_token") or "").strip()
    uid = (by_name.get("u") or "").strip()
    if not token or not uid:
        return False

    flag = (by_name.get("xq_is_login") or "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if by_name.get("xq_r_token") or by_name.get("xq_id_token"):
        if initial_token is None or token != initial_token:
            return True
    if initial_token is not None and token != initial_token:
        return True
    return False


def _run_browser_login(timeout_sec: int, on_success: Callable[[str], None]) -> None:
    global _browser_ref
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _set_status(
            "failed",
            "未安装 playwright。请执行: pip install playwright && playwright install chromium",
        )
        return

    _set_status("running", "正在打开浏览器，请在弹出窗口中登录雪球…")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            _browser_ref = browser
            context = browser.new_context(
                locale="zh-CN",
                viewport={"width": 1280, "height": 860},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(2.0)
            if _cancel_flag.is_set():
                _set_status("cancelled", "已取消登录")
                browser.close()
                _browser_ref = None
                return

            initial_token = _xueqiu_cookie_map(context.cookies()).get("xq_a_token")
            _set_status("running", "请在浏览器中完成登录（账号密码 / 短信 / 扫码均可）…")

            deadline = time.time() + max(60, int(timeout_sec))
            while time.time() < deadline:
                if _cancel_flag.is_set():
                    _set_status("cancelled", "已取消登录")
                    break

                by_name = _xueqiu_cookie_map(context.cookies())
                if not _looks_logged_in(by_name, initial_token):
                    time.sleep(1.0)
                    continue

                # 再访问股票站，补齐跨域 Cookie
                try:
                    page.goto(
                        "https://stock.xueqiu.com/",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    time.sleep(1.0)
                except Exception:
                    pass

                by_name = _xueqiu_cookie_map(context.cookies())
                header = _cookie_header_from_map(by_name)
                ok, reason = cookie_has_required_fields(header)
                if not ok:
                    _set_status("running", f"登录未完成：{reason}，请继续在浏览器登录…")
                    time.sleep(1.0)
                    continue

                alive, alive_reason = validate_cookie_alive(header)
                if not alive:
                    _set_status("running", f"已检测到登录痕迹，但接口校验未通过：{alive_reason}")
                    time.sleep(1.5)
                    continue

                token = by_name.get("xq_a_token") or ""
                uid = by_name.get("u") or ""
                preview = f"xq_a_token={token[:6]}…{token[-4:]} ; u={uid}"
                try:
                    on_success(header)
                    _set_status("success", "登录成功，完整 Cookie 已写入并校验通过", preview)
                except Exception as e:
                    _set_status("failed", f"Cookie 校验通过但保存失败: {e}", preview)
                break
            else:
                if _login_status["status"] == "running":
                    _set_status(
                        "failed",
                        "登录超时：请确认已登录成功（需包含 xq_a_token 与 u）后重试",
                    )

            try:
                browser.close()
            except Exception:
                pass
            _browser_ref = None
    except Exception as e:
        _browser_ref = None
        if _cancel_flag.is_set():
            _set_status("cancelled", "已取消登录")
        else:
            msg = str(e)
            if "Executable doesn't exist" in msg or "BrowserType.launch" in msg:
                msg = "未安装 Chromium。请执行: playwright install chromium"
            _set_status("failed", f"登录失败: {msg}")


def start_login(on_success: Callable[[str], None], timeout_sec: int = 300) -> dict[str, Any]:
    """启动登录线程。若已在进行中则返回当前状态。"""
    global _worker
    with _login_lock:
        if _worker and _worker.is_alive():
            return get_login_status()
        _login_status["started_at"] = None
        _login_status["finished_at"] = None
        _login_status["cookie_preview"] = None
        _cancel_flag.clear()
        _set_status("running", "正在启动浏览器…")
        _worker = threading.Thread(
            target=_run_browser_login,
            args=(timeout_sec, on_success),
            name="xueqiu-login",
            daemon=True,
        )
        _worker.start()
        return get_login_status()


def cancel_login() -> dict[str, Any]:
    global _browser_ref
    _cancel_flag.set()
    browser = _browser_ref
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
        _browser_ref = None
    if _login_status.get("status") == "running":
        _set_status("cancelled", "已取消登录")
    return get_login_status()

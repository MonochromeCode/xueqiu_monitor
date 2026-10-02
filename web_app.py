#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
雪球组合监控 · Web 控制台
启动：python web_app.py
访问：http://127.0.0.1:8080
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import xueqiu_monitor as mon

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
STATE_FILE = BASE_DIR / "monitor_state.json"
ENV_FILE = BASE_DIR / ".env"

ENV_KEYS = (
    "XUEQIU_COOKIE",
    "DINGTALK_WEBHOOK",
    "MONITORED_CUBES",
    "CHECK_INTERVAL",
    "WEIGHT_CHANGE_THRESHOLD",
    "AT_ALL",
    "LOG_FILE",
)

app = FastAPI(title="雪球组合监控", version="1.0.0")

_check_lock = threading.Lock()
_config_lock = threading.Lock()
_env_mtime: float | None = None
_check_status: dict[str, Any] = {
    "running": False,
    "last_result": None,
    "last_finished_at": None,
    "error": None,
}


class CheckResponse(BaseModel):
    ok: bool
    message: str
    running: bool


class ConfigUpdate(BaseModel):
    xueqiu_cookie: str
    dingtalk_webhook: str
    monitored_cubes: str
    check_interval: int = 300
    weight_change_threshold: float = 1.0
    at_all: bool = False
    log_file: str = "xueqiu_monitor.log"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _mask_secret(value: str, keep: int = 6) -> str:
    if not value or len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}…{value[-keep:]}"


def _config_ok() -> bool:
    if "你的token" in mon.XUEQIU_COOKIE or not mon.XUEQIU_COOKIE:
        return False
    if "你的access_token" in mon.DINGTALK_WEBHOOK or not mon.DINGTALK_WEBHOOK:
        return False
    if not mon.MONITORED_CUBES or mon.MONITORED_CUBES == ["ZH123456"]:
        return False
    return True


def _parse_cubes(raw: str) -> list[str]:
    return [c.strip().upper() for c in (raw or "").split(",") if c.strip()]


def _escape_env_value(value: str) -> str:
    if any(ch in value for ch in (' ', '#', '"', "'", "=")) or value != value.strip():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _read_env_map() -> dict[str, str]:
    result: dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.rstrip("\n")
                stripped = raw.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                result[key] = value
    except Exception:
        pass
    return result


def _write_env_file(values: dict[str, str]) -> None:
    """更新 .env 中的已知配置项，尽量保留原有注释与其他键。"""
    lines: list[str] = []
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in values:
                new_lines.append(f"{key}={_escape_env_value(values[key])}")
                seen.add(key)
                continue
        new_lines.append(line)

    missing = [k for k in ENV_KEYS if k in values and k not in seen]
    if missing:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# 由 Web 控制台写入")
        for key in missing:
            new_lines.append(f"{key}={_escape_env_value(values[key])}")

    with open(ENV_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(new_lines).rstrip() + "\n")


def _apply_runtime_config(
    *,
    cookie: str,
    webhook: str,
    cubes: list[str],
    check_interval: int,
    weight_threshold: float,
    at_all: bool,
    log_file: str,
) -> None:
    mon.XUEQIU_COOKIE = cookie
    mon.DINGTALK_WEBHOOK = webhook
    mon.MONITORED_CUBES = cubes
    mon.CHECK_INTERVAL = check_interval
    mon.WEIGHT_CHANGE_THRESHOLD = weight_threshold
    mon.AT_ALL = at_all
    mon.LOG_FILE = log_file

    os.environ["XUEQIU_COOKIE"] = cookie
    os.environ["DINGTALK_WEBHOOK"] = webhook
    os.environ["MONITORED_CUBES"] = ",".join(cubes)
    os.environ["CHECK_INTERVAL"] = str(check_interval)
    os.environ["WEIGHT_CHANGE_THRESHOLD"] = str(weight_threshold)
    os.environ["AT_ALL"] = "true" if at_all else "false"
    os.environ["LOG_FILE"] = log_file


def _env_file_mtime() -> Optional[float]:
    try:
        return ENV_FILE.stat().st_mtime if ENV_FILE.exists() else None
    except OSError:
        return None


def _sync_runtime_from_env(force: bool = False) -> bool:
    """若 .env 有变更则重新加载到运行时。返回是否发生了同步。"""
    global _env_mtime
    mtime = _env_file_mtime()
    if not force and mtime is not None and _env_mtime == mtime:
        return False

    env_map = _read_env_map()
    cookie = env_map.get("XUEQIU_COOKIE", mon.XUEQIU_COOKIE)
    webhook = env_map.get("DINGTALK_WEBHOOK", mon.DINGTALK_WEBHOOK)
    cubes_raw = env_map.get("MONITORED_CUBES", ",".join(mon.MONITORED_CUBES))
    cubes = _parse_cubes(cubes_raw) or list(mon.MONITORED_CUBES)

    try:
        check_interval = int(env_map.get("CHECK_INTERVAL", mon.CHECK_INTERVAL))
    except ValueError:
        check_interval = mon.CHECK_INTERVAL
    try:
        weight_threshold = float(env_map.get("WEIGHT_CHANGE_THRESHOLD", mon.WEIGHT_CHANGE_THRESHOLD))
    except ValueError:
        weight_threshold = mon.WEIGHT_CHANGE_THRESHOLD

    at_all = str(env_map.get("AT_ALL", mon.AT_ALL)).lower() in ("1", "true", "yes", "on")
    log_file = env_map.get("LOG_FILE", mon.LOG_FILE or "")

    _apply_runtime_config(
        cookie=cookie,
        webhook=webhook,
        cubes=cubes,
        check_interval=check_interval,
        weight_threshold=weight_threshold,
        at_all=at_all,
        log_file=log_file,
    )
    _env_mtime = mtime
    return True


def _current_config() -> dict[str, Any]:
    _sync_runtime_from_env()
    mtime = _env_file_mtime()
    return {
        "xueqiu_cookie": mon.XUEQIU_COOKIE,
        "dingtalk_webhook": mon.DINGTALK_WEBHOOK,
        "monitored_cubes": ",".join(mon.MONITORED_CUBES),
        "check_interval": mon.CHECK_INTERVAL,
        "weight_change_threshold": mon.WEIGHT_CHANGE_THRESHOLD,
        "at_all": mon.AT_ALL,
        "log_file": mon.LOG_FILE or "",
        "config_ok": _config_ok(),
        "env_path": str(ENV_FILE),
        "mtime": mtime,
        "updated_at": datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else None,
    }


def _log_file_path() -> Optional[Path]:
    if not mon.LOG_FILE:
        return None
    log_path = Path(mon.LOG_FILE)
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path
    return log_path


def _token_info(state: dict) -> dict[str, Any]:
    token_state = state.get("_token", {})
    expiry = mon._get_token_expiry(mon.XUEQIU_COOKIE)
    remaining_hours: Optional[float] = None
    status = "unknown"

    if expiry is not None:
        remaining_hours = (expiry - time.time()) / 3600
        if remaining_hours <= 0:
            status = "expired"
        elif remaining_hours <= 24:
            status = "expiring"
        else:
            status = "ok"

    return {
        "status": status,
        "expiry": datetime.fromtimestamp(expiry).isoformat(timespec="seconds") if expiry else None,
        "remaining_hours": round(remaining_hours, 1) if remaining_hours is not None else None,
        "notified_expiry": bool(token_state.get("notified_expiry")),
    }


def _cube_summary(cube_id: str, data: dict) -> dict[str, Any]:
    positions = data.get("positions") or []
    total_weight = sum(float(p.get("weight") or 0) for p in positions)
    return {
        "id": cube_id,
        "name": (data.get("nav") or {}).get("name") or cube_id,
        "position_count": len(positions),
        "total_weight": round(total_weight, 2),
        "last_rb_id": data.get("last_rb_id"),
        "last_check": data.get("last_check"),
        "positions": sorted(positions, key=lambda p: float(p.get("weight") or 0), reverse=True),
    }


def _recent_changes(state: dict, limit: int = 30) -> list[dict]:
    history = state.get("_changes") or []
    if not isinstance(history, list):
        return []
    limit = max(1, min(int(limit), 100))
    return history[:limit]


@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/config")
def get_config():
    return _current_config()


@app.put("/api/config")
def update_config(body: ConfigUpdate):
    global _env_mtime
    cookie = (body.xueqiu_cookie or "").strip()
    webhook = (body.dingtalk_webhook or "").strip()
    cubes = _parse_cubes(body.monitored_cubes)
    check_interval = int(body.check_interval)
    weight_threshold = float(body.weight_change_threshold)
    log_file = (body.log_file or "").strip()

    if not cookie:
        raise HTTPException(status_code=400, detail="雪球 Cookie 不能为空")
    if not webhook:
        raise HTTPException(status_code=400, detail="钉钉 Webhook 不能为空")
    if not cubes:
        raise HTTPException(status_code=400, detail="至少填写一个监控组合代码")
    if check_interval < 10:
        raise HTTPException(status_code=400, detail="检查间隔不能小于 10 秒")
    if weight_threshold < 0:
        raise HTTPException(status_code=400, detail="仓位阈值不能为负数")

    values = {
        "XUEQIU_COOKIE": cookie,
        "DINGTALK_WEBHOOK": webhook,
        "MONITORED_CUBES": ",".join(cubes),
        "CHECK_INTERVAL": str(check_interval),
        "WEIGHT_CHANGE_THRESHOLD": str(weight_threshold),
        "AT_ALL": "true" if body.at_all else "false",
        "LOG_FILE": log_file,
    }

    with _config_lock:
        try:
            _write_env_file(values)
            _apply_runtime_config(
                cookie=cookie,
                webhook=webhook,
                cubes=cubes,
                check_interval=check_interval,
                weight_threshold=weight_threshold,
                at_all=bool(body.at_all),
                log_file=log_file,
            )
            _env_mtime = _env_file_mtime()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"保存配置失败: {e}") from e

    return {
        "ok": True,
        "message": "配置已保存并立即生效",
        "config": _current_config(),
    }


@app.get("/api/status")
def status():
    _sync_runtime_from_env()
    state = _load_state()
    cubes = []
    for cube_id in mon.MONITORED_CUBES:
        raw = state.get(cube_id, {})
        cubes.append(_cube_summary(cube_id, raw) if raw else {
            "id": cube_id,
            "name": cube_id,
            "position_count": 0,
            "total_weight": 0,
            "last_rb_id": None,
            "last_check": None,
            "positions": [],
        })

    return {
        "version": "2.2",
        "config_ok": _config_ok(),
        "monitored_cubes": mon.MONITORED_CUBES,
        "check_interval": mon.CHECK_INTERVAL,
        "weight_threshold": mon.WEIGHT_CHANGE_THRESHOLD,
        "at_all": mon.AT_ALL,
        "cookie_masked": _mask_secret(
            (mon.XUEQIU_COOKIE.split("xq_a_token=")[-1].split(";")[0] if "xq_a_token=" in mon.XUEQIU_COOKIE else mon.XUEQIU_COOKIE),
            keep=4,
        ),
        "webhook_configured": "你的access_token" not in mon.DINGTALK_WEBHOOK and bool(mon.DINGTALK_WEBHOOK),
        "token": _token_info(state),
        "check": _check_status,
        "cubes": cubes,
        "recent_changes": _recent_changes(state, 30),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/changes")
def list_changes(limit: int = 50):
    state = _load_state()
    items = _recent_changes(state, limit)
    return {"changes": items, "count": len(items)}


@app.get("/api/cubes")
def list_cubes():
    state = _load_state()
    result = []
    for cube_id in mon.MONITORED_CUBES:
        raw = state.get(cube_id, {})
        result.append(_cube_summary(cube_id, raw) if raw else {
            "id": cube_id,
            "name": cube_id,
            "position_count": 0,
            "total_weight": 0,
            "last_rb_id": None,
            "last_check": None,
            "positions": [],
        })
    # 状态文件里可能还有历史组合
    for cube_id, raw in state.items():
        if cube_id.startswith("_") or cube_id in mon.MONITORED_CUBES:
            continue
        if isinstance(raw, dict) and "positions" in raw:
            result.append(_cube_summary(cube_id, raw))
    return {"cubes": result}


@app.get("/api/cubes/{cube_id}")
def get_cube(cube_id: str):
    state = _load_state()
    cube_id = cube_id.upper()
    raw = state.get(cube_id)
    if not raw:
        raise HTTPException(status_code=404, detail=f"组合 {cube_id} 暂无快照数据")
    return _cube_summary(cube_id, raw)


@app.get("/api/logs")
def logs(lines: int = 80):
    _sync_runtime_from_env()
    lines = max(10, min(lines, 500))
    log_path = _log_file_path()
    if not log_path:
        return {"lines": [], "path": None, "mtime": None, "updated_at": None}
    if not log_path.exists():
        return {"lines": [], "path": str(log_path), "mtime": None, "updated_at": None}
    try:
        mtime = log_path.stat().st_mtime
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.readlines()
        return {
            "lines": [ln.rstrip("\n") for ln in content[-lines:]],
            "path": str(log_path),
            "mtime": mtime,
            "updated_at": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _run_check_once():
    global _check_status
    try:
        _sync_runtime_from_env()
        if not _config_ok():
            _check_status["error"] = "配置不完整，请检查 .env"
            _check_status["last_result"] = "config_error"
            return
        if not mon.MONITORED_CUBES:
            _check_status["error"] = "未配置监控组合"
            _check_status["last_result"] = "config_error"
            return
        client = mon.XueQiuClient(mon.XUEQIU_COOKIE, mon.MONITORED_CUBES[0])
        notifier = mon.DingTalkNotifier(mon.DINGTALK_WEBHOOK)
        mon.monitor_once(client, notifier)
        _check_status["last_result"] = "ok"
        _check_status["error"] = None
    except Exception as e:
        _check_status["last_result"] = "error"
        _check_status["error"] = str(e)
    finally:
        _check_status["running"] = False
        _check_status["last_finished_at"] = datetime.now().isoformat(timespec="seconds")
        _check_lock.release()


@app.post("/api/check", response_model=CheckResponse)
def trigger_check():
    if not _check_lock.acquire(blocking=False):
        return CheckResponse(ok=False, message="已有检查任务在运行", running=True)
    _check_status["running"] = True
    _check_status["error"] = None
    thread = threading.Thread(target=_run_check_once, daemon=True)
    thread.start()
    return CheckResponse(ok=True, message="已开始检查", running=True)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/config")
def config_page():
    return FileResponse(WEB_DIR / "config.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def _background_monitor():
    """定时监控循环：与 Web 控制台共用配置与检查锁。"""
    mon.logger.info("后台监控线程已启动")
    # 等 Web 服务起来后再做首检，方便前端先打开
    time.sleep(2)
    while True:
        _sync_runtime_from_env()
        interval = max(10, int(mon.CHECK_INTERVAL or 300))
        if _config_ok():
            if _check_lock.acquire(blocking=False):
                _check_status["running"] = True
                _check_status["error"] = None
                _run_check_once()
            else:
                mon.logger.info("已有检查任务在运行，本轮后台检查跳过")
        else:
            mon.logger.warning("配置不完整，跳过本轮监控（可在前端配置页补全）")
        time.sleep(interval)


def _open_browser(url: str, delay: float = 1.2):
    def _open():
        time.sleep(delay)
        try:
            import webbrowser
            webbrowser.open(url)
            mon.logger.info(f"已打开浏览器: {url}")
        except Exception as e:
            mon.logger.warning(f"自动打开浏览器失败: {e}，请手动访问 {url}")

    threading.Thread(target=_open, daemon=True).start()


def main():
    import uvicorn

    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "8080"))
    start_monitor = os.environ.get("START_MONITOR", "true").lower() != "false"
    open_browser = os.environ.get("OPEN_BROWSER", "true").lower() != "false"
    # 浏览器访问地址：0.0.0.0 时改用 localhost
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{browse_host}:{port}"

    print("=" * 60)
    print("  雪球组合监控 · 后台 + Web 控制台")
    print(f"  控制台: {url}")
    print(f"  后台监控: {'开启' if start_monitor else '关闭'}")
    print(f"  自动打开浏览器: {'是' if open_browser else '否'}")
    print("=" * 60)

    _sync_runtime_from_env(force=True)

    if start_monitor:
        threading.Thread(target=_background_monitor, name="xueqiu-monitor", daemon=True).start()

    if open_browser:
        _open_browser(url)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

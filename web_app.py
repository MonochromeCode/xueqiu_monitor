#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
雪球组合监控 · Web 控制台
启动：python web_app.py
访问：http://127.0.0.1:8080
"""

from __future__ import annotations

import hashlib
import json
import logging
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
import xueqiu_login as xq_login

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
    "TRADING_HOURS_ONLY",
    "OFF_HOURS_INTERVAL",
    "PRE_CLOSE_MINUTES",
    "PRE_CLOSE_INTERVAL",
    "MARKET_CLOSE",
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
    trading_hours_only: bool = True
    off_hours_interval: int = 1800
    pre_close_minutes: int = 15
    pre_close_interval: int = 60
    market_close: str = "15:00"


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
    trading_hours_only: bool = True,
    off_hours_interval: int = 1800,
    pre_close_minutes: int = 15,
    pre_close_interval: int = 60,
    market_close: str = "15:00",
) -> None:
    mon.XUEQIU_COOKIE = cookie
    mon.DINGTALK_WEBHOOK = webhook
    mon.MONITORED_CUBES = cubes
    mon.CHECK_INTERVAL = check_interval
    mon.WEIGHT_CHANGE_THRESHOLD = weight_threshold
    mon.AT_ALL = at_all
    mon.LOG_FILE = log_file
    mon.TRADING_HOURS_ONLY = trading_hours_only
    mon.OFF_HOURS_INTERVAL = off_hours_interval
    mon.PRE_CLOSE_MINUTES = pre_close_minutes
    mon.PRE_CLOSE_INTERVAL = pre_close_interval
    mon.MARKET_CLOSE = market_close

    os.environ["XUEQIU_COOKIE"] = cookie
    os.environ["DINGTALK_WEBHOOK"] = webhook
    os.environ["MONITORED_CUBES"] = ",".join(cubes)
    os.environ["CHECK_INTERVAL"] = str(check_interval)
    os.environ["WEIGHT_CHANGE_THRESHOLD"] = str(weight_threshold)
    os.environ["AT_ALL"] = "true" if at_all else "false"
    os.environ["LOG_FILE"] = log_file
    os.environ["TRADING_HOURS_ONLY"] = "true" if trading_hours_only else "false"
    os.environ["OFF_HOURS_INTERVAL"] = str(off_hours_interval)
    os.environ["PRE_CLOSE_MINUTES"] = str(pre_close_minutes)
    os.environ["PRE_CLOSE_INTERVAL"] = str(pre_close_interval)
    os.environ["MARKET_CLOSE"] = market_close


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
    trading_hours_only = str(
        env_map.get("TRADING_HOURS_ONLY", mon.TRADING_HOURS_ONLY)
    ).lower() in ("1", "true", "yes", "on")
    try:
        off_hours_interval = int(env_map.get("OFF_HOURS_INTERVAL", mon.OFF_HOURS_INTERVAL))
    except ValueError:
        off_hours_interval = mon.OFF_HOURS_INTERVAL
    try:
        pre_close_minutes = int(env_map.get("PRE_CLOSE_MINUTES", mon.PRE_CLOSE_MINUTES))
    except ValueError:
        pre_close_minutes = mon.PRE_CLOSE_MINUTES
    try:
        pre_close_interval = int(env_map.get("PRE_CLOSE_INTERVAL", mon.PRE_CLOSE_INTERVAL))
    except ValueError:
        pre_close_interval = mon.PRE_CLOSE_INTERVAL
    market_close = env_map.get("MARKET_CLOSE", mon.MARKET_CLOSE) or "15:00"

    _apply_runtime_config(
        cookie=cookie,
        webhook=webhook,
        cubes=cubes,
        check_interval=check_interval,
        weight_threshold=weight_threshold,
        at_all=at_all,
        log_file=log_file,
        trading_hours_only=trading_hours_only,
        off_hours_interval=off_hours_interval,
        pre_close_minutes=pre_close_minutes,
        pre_close_interval=pre_close_interval,
        market_close=market_close,
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
        "trading_hours_only": mon.TRADING_HOURS_ONLY,
        "off_hours_interval": mon.OFF_HOURS_INTERVAL,
        "pre_close_minutes": mon.PRE_CLOSE_MINUTES,
        "pre_close_interval": mon.PRE_CLOSE_INTERVAL,
        "market_close": mon.MARKET_CLOSE,
        "config_ok": _config_ok(),
        "env_path": str(ENV_FILE),
        "mtime": mtime,
        "updated_at": datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else None,
        "schedule": mon.get_schedule_info(),
        "token": _token_info(_load_state()),
        "login": xq_login.get_login_status(),
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

    issued = mon._get_token_issued_at(mon.XUEQIU_COOKIE)
    acquired_at = token_state.get("acquired_at")
    if not acquired_at and issued is not None:
        acquired_at = datetime.fromtimestamp(issued).isoformat(timespec="seconds")

    return {
        "status": status,
        "expiry": datetime.fromtimestamp(expiry).isoformat(timespec="seconds") if expiry else None,
        "remaining_hours": round(remaining_hours, 1) if remaining_hours is not None else None,
        "notified_expiry": bool(token_state.get("notified_expiry")),
        "acquired_at": acquired_at,
        "issued_at": datetime.fromtimestamp(issued).isoformat(timespec="seconds") if issued else None,
    }


def _record_cookie_acquired(cookie: str) -> None:
    """Cookie 更新时写入获取时间到 monitor_state.json。"""
    cookie = (cookie or "").strip()
    if not cookie:
        return
    state = _load_state()
    token_state = state.setdefault("_token", {})
    new_hash = hashlib.md5(cookie.encode()).hexdigest()
    if token_state.get("hash") and token_state["hash"] != new_hash:
        token_state.pop("notified_expiry", None)
    token_state["hash"] = new_hash
    issued = mon._get_token_issued_at(cookie)
    if issued is not None:
        token_state["issued_at"] = issued
        token_state["acquired_at"] = datetime.fromtimestamp(issued).isoformat(timespec="seconds")
    else:
        token_state["acquired_at"] = datetime.now().isoformat(timespec="seconds")
    mon._save_state(state)


def _cube_summary(cube_id: str, data: dict) -> dict[str, Any]:
    positions = data.get("positions") or []
    total_weight = sum(float(p.get("weight") or 0) for p in positions)
    cash = max(0.0, 100.0 - total_weight)
    return {
        "id": cube_id,
        "name": (data.get("nav") or {}).get("name") or cube_id,
        "position_count": len(positions),
        "total_weight": round(total_weight, 2),
        "cash": round(cash, 2),
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
    off_hours_interval = int(body.off_hours_interval)
    pre_close_minutes = int(body.pre_close_minutes)
    pre_close_interval = int(body.pre_close_interval)
    market_close = (body.market_close or "15:00").strip()

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
    if off_hours_interval < 60:
        raise HTTPException(status_code=400, detail="休市间隔不能小于 60 秒")
    if pre_close_interval < 10:
        raise HTTPException(status_code=400, detail="收盘前间隔不能小于 10 秒")
    try:
        hh, mm = market_close.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
    except Exception as exc:
        raise HTTPException(status_code=400, detail="收盘时间格式应为 HH:MM") from exc

    values = {
        "XUEQIU_COOKIE": cookie,
        "DINGTALK_WEBHOOK": webhook,
        "MONITORED_CUBES": ",".join(cubes),
        "CHECK_INTERVAL": str(check_interval),
        "WEIGHT_CHANGE_THRESHOLD": str(weight_threshold),
        "AT_ALL": "true" if body.at_all else "false",
        "LOG_FILE": log_file,
        "TRADING_HOURS_ONLY": "true" if body.trading_hours_only else "false",
        "OFF_HOURS_INTERVAL": str(off_hours_interval),
        "PRE_CLOSE_MINUTES": str(pre_close_minutes),
        "PRE_CLOSE_INTERVAL": str(pre_close_interval),
        "MARKET_CLOSE": market_close,
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
                trading_hours_only=bool(body.trading_hours_only),
                off_hours_interval=off_hours_interval,
                pre_close_minutes=pre_close_minutes,
                pre_close_interval=pre_close_interval,
                market_close=market_close,
            )
            _env_mtime = _env_file_mtime()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"保存配置失败: {e}") from e

    _record_cookie_acquired(cookie)

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
            "cash": 100.0,
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
        "login": xq_login.get_login_status(),
        "schedule": mon.get_schedule_info(),
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
            "cash": 100.0,
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


@app.get("/api/cubes/{cube_id}/live")
def get_cube_live(cube_id: str):
    """实时查询雪球原组合行情与持仓，并刷新本地快照。"""
    _sync_runtime_from_env()
    cube_id = cube_id.upper().strip()
    if not cube_id:
        raise HTTPException(status_code=400, detail="组合代码不能为空")
    if "你的token" in mon.XUEQIU_COOKIE or not mon.XUEQIU_COOKIE:
        raise HTTPException(status_code=400, detail="雪球 Cookie 未配置或无效")

    try:
        client = mon.XueQiuClient(mon.XUEQIU_COOKIE, cube_id)
        quote = client.get_cube_quote()
        positions = client.get_current_positions()
        latest_rb = client.get_latest_rebalancing()
        nav_info = mon.parse_nav_from_rebalancing(latest_rb) if latest_rb else {}
        if quote and quote.get("name"):
            nav_info["name"] = quote["name"]
        if not nav_info.get("name"):
            nav_info["name"] = cube_id

        rb_id = None
        if isinstance(latest_rb, dict):
            rb_id = latest_rb.get("id") or latest_rb.get("rebalancing_id")

        state = _load_state()
        mon._save_cube_state(state, cube_id, positions, nav_info, rb_id)
        mon._save_state(state)

        summary = _cube_summary(cube_id, state.get(cube_id) or {})
        summary["quote"] = quote
        summary["source"] = "live"
        summary["live_at"] = datetime.now().isoformat(timespec="seconds")
        if not positions and not quote:
            summary["warning"] = "未拉到持仓与行情，请检查 Cookie 或组合代码"
        return summary
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {e}") from e


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


@app.post("/api/logs/clear")
def clear_logs():
    """清空当前日志文件（截断，不删除文件）。"""
    _sync_runtime_from_env()
    log_path = _log_file_path()
    if not log_path:
        raise HTTPException(status_code=400, detail="未配置日志文件")

    truncated = False
    handlers = list(logging.getLogger().handlers) + list(mon.logger.handlers)
    for handler in handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        try:
            handler.acquire()
            stream = getattr(handler, "stream", None)
            if stream is not None:
                stream.seek(0)
                stream.truncate(0)
                stream.flush()
                truncated = True
        except Exception:
            pass
        finally:
            try:
                handler.release()
            except Exception:
                pass

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.truncate(0)
        truncated = True
    except Exception as e:
        if not truncated:
            raise HTTPException(status_code=500, detail=f"清空日志失败: {e}") from e

    mon.logger.info("日志已手动清空")
    return {"ok": True, "message": "日志已清空", "path": str(log_path)}


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


def _save_cookie_only(cookie: str) -> None:
    """只更新雪球 Cookie，保留其余配置。"""
    global _env_mtime
    cookie = (cookie or "").strip()
    ok, reason = xq_login.cookie_has_required_fields(cookie)
    if not ok:
        raise ValueError(reason)
    alive, alive_reason = xq_login.validate_cookie_alive(cookie)
    if not alive:
        raise ValueError(alive_reason)

    cfg = _current_config()
    cubes = _parse_cubes(cfg.get("monitored_cubes") or "")
    if not cubes:
        cubes = list(mon.MONITORED_CUBES) or ["ZH123456"]

    values = {
        "XUEQIU_COOKIE": cookie,
        "DINGTALK_WEBHOOK": cfg["dingtalk_webhook"],
        "MONITORED_CUBES": ",".join(cubes),
        "CHECK_INTERVAL": str(cfg["check_interval"]),
        "WEIGHT_CHANGE_THRESHOLD": str(cfg["weight_change_threshold"]),
        "AT_ALL": "true" if cfg["at_all"] else "false",
        "LOG_FILE": cfg.get("log_file") or "",
        "TRADING_HOURS_ONLY": "true" if cfg.get("trading_hours_only", True) else "false",
        "OFF_HOURS_INTERVAL": str(cfg.get("off_hours_interval", 1800)),
        "PRE_CLOSE_MINUTES": str(cfg.get("pre_close_minutes", 15)),
        "PRE_CLOSE_INTERVAL": str(cfg.get("pre_close_interval", 60)),
        "MARKET_CLOSE": cfg.get("market_close") or "15:00",
    }
    with _config_lock:
        _write_env_file(values)
        _apply_runtime_config(
            cookie=cookie,
            webhook=cfg["dingtalk_webhook"],
            cubes=cubes,
            check_interval=int(cfg["check_interval"]),
            weight_threshold=float(cfg["weight_change_threshold"]),
            at_all=bool(cfg["at_all"]),
            log_file=cfg.get("log_file") or "",
            trading_hours_only=bool(cfg.get("trading_hours_only", True)),
            off_hours_interval=int(cfg.get("off_hours_interval", 1800)),
            pre_close_minutes=int(cfg.get("pre_close_minutes", 15)),
            pre_close_interval=int(cfg.get("pre_close_interval", 60)),
            market_close=cfg.get("market_close") or "15:00",
        )
        _env_mtime = _env_file_mtime()
    _record_cookie_acquired(cookie)


@app.get("/api/xueqiu/login/status")
def xueqiu_login_status():
    return xq_login.get_login_status()


@app.post("/api/xueqiu/login/start")
def xueqiu_login_start(timeout_sec: int = 300):
    timeout_sec = max(60, min(int(timeout_sec), 900))

    def _on_success(cookie: str):
        _save_cookie_only(cookie)

    status = xq_login.start_login(_on_success, timeout_sec=timeout_sec)
    return {"ok": True, "login": status}


@app.post("/api/xueqiu/login/cancel")
def xueqiu_login_cancel():
    return {"ok": True, "login": xq_login.cancel_login()}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/config")
def config_page():
    return FileResponse(WEB_DIR / "config.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def _background_monitor():
    """定时监控循环：交易时段加密、休市降频。"""
    mon.logger.info("后台监控线程已启动")
    time.sleep(2)
    while True:
        _sync_runtime_from_env()
        sched = mon.get_schedule_info()
        interval = max(10, int(sched["interval"]))
        if _config_ok():
            # 休市也做低频检查，便于捕捉 Cookie 失效与隔夜调仓
            if _check_lock.acquire(blocking=False):
                _check_status["running"] = True
                _check_status["error"] = None
                mon.logger.info(
                    f"后台检查开始｜时段={sched['label']}｜下次间隔={interval}s"
                )
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

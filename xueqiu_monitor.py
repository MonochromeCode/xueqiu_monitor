#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
雪球组合变动监控 - 钉钉通知脚本 v2.1
──────────────────────────────────────
功能：
  · 定时拉取雪球组合持仓数据
  · 检测持仓新增/卖出/加减仓（按仓位权重对比）
  · 有变动时通过钉钉机器人发送 Markdown 通知
  · 记录上次调仓记录 ID，同一次调仓只推送一次（防重复）

依赖安装：
    pip install requests python-dotenv

Token 获取方式：
  1. 浏览器登录 https://xueqiu.com
  2. F12 → Network → 任意请求 → Request Headers → Cookie
  3. 复制完整 Cookie 字符串，填入 .env 文件
"""

import base64
import json
import os
import re
import sys
import time
import atexit
import logging
import hashlib
import traceback
from datetime import datetime
from typing import Optional, Any

import requests

# ─────────────────────────────────────────────
#  尝试加载 .env（可选，有 python-dotenv 才生效）
# ─────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv 未安装时直接读环境变量或使用下方硬编码配置

# ══════════════════════════════════════════════════════════════════
#  ⚙️  配置区（优先读取 .env，其次使用下方默认值）
# ══════════════════════════════════════════════════════════════════

# 雪球登录 Cookie（必填）
# .env 写法：XUEQIU_COOKIE=xq_a_token=xxx; u=xxxxxxxxxx
XUEQIU_COOKIE: str = os.environ.get(
    "XUEQIU_COOKIE",
    "xq_a_token=你的token;u=你的uid"   # ← 直接改这里，或用 .env
)

# 钉钉机器人 Webhook（必填）
DINGTALK_WEBHOOK: str = os.environ.get(
    "DINGTALK_WEBHOOK",
    "https://oapi.dingtalk.com/robot/send?access_token=你的access_token"
)

# 监控的雪球组合代码列表（必填）
# 多个组合用英文逗号分隔，如 "ZH123456,ZH654321"
_cubes_env = os.environ.get("MONITORED_CUBES", "")
MONITORED_CUBES: list[str] = (
    [c.strip() for c in _cubes_env.split(",") if c.strip()]
    if _cubes_env
    else ["ZH123456"]  # ← 直接改这里
)

# 检查间隔（秒），默认 5 分钟（交易时段内生效）
CHECK_INTERVAL: int = int(os.environ.get("CHECK_INTERVAL", "300"))

# 仓位权重变动阈值（百分点），超过此值才触发通知，默认 1%
WEIGHT_CHANGE_THRESHOLD: float = float(os.environ.get("WEIGHT_CHANGE_THRESHOLD", "1.0"))

# 是否 @所有人
AT_ALL: bool = os.environ.get("AT_ALL", "false").lower() == "true"

# 日志文件（空字符串=只输出到终端）
LOG_FILE: str = os.environ.get("LOG_FILE", "xueqiu_monitor.log")

# 是否启用交易时段智能调度（默认开启）
TRADING_HOURS_ONLY: bool = os.environ.get("TRADING_HOURS_ONLY", "true").lower() == "true"

# 非交易时段检查间隔（秒），默认 30 分钟
OFF_HOURS_INTERVAL: int = int(os.environ.get("OFF_HOURS_INTERVAL", "1800"))

# 收盘前加速窗口（分钟）与间隔（秒）
PRE_CLOSE_MINUTES: int = int(os.environ.get("PRE_CLOSE_MINUTES", "15"))
PRE_CLOSE_INTERVAL: int = int(os.environ.get("PRE_CLOSE_INTERVAL", "60"))

# 下午交易结束时间（HH:MM），A股 15:00；含港股可设 16:00
MARKET_CLOSE: str = os.environ.get("MARKET_CLOSE", "15:00")

# ══════════════════════════════════════════════════════════════════
#  📋  日志
# ══════════════════════════════════════════════════════════════════
_handlers = [logging.StreamHandler(sys.stdout)]
if LOG_FILE:
    _handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
#  ⏱  交易时段调度
# ══════════════════════════════════════════════════════════════════

def _parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = value.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return default


def get_schedule_info(now: Optional[datetime] = None) -> dict[str, Any]:
    """
    返回当前调度状态：
      in_trading / phase / interval / label
    交易时段默认：工作日 09:15-11:35、12:55-MARKET_CLOSE（默认 15:00）
    收盘前 PRE_CLOSE_MINUTES 使用更短间隔。
    """
    now = now or datetime.now()
    base_interval = max(10, int(CHECK_INTERVAL or 300))
    off_interval = max(base_interval, int(OFF_HOURS_INTERVAL or 1800))
    pre_close_interval = max(10, int(PRE_CLOSE_INTERVAL or 60))
    pre_close_minutes = max(0, int(PRE_CLOSE_MINUTES or 0))
    close_h, close_m = _parse_hhmm(MARKET_CLOSE, (15, 0))

    if not TRADING_HOURS_ONLY:
        return {
            "in_trading": True,
            "phase": "always",
            "interval": base_interval,
            "label": "全天候",
            "market_close": f"{close_h:02d}:{close_m:02d}",
        }

    # 周末
    if now.weekday() >= 5:
        return {
            "in_trading": False,
            "phase": "weekend",
            "interval": off_interval,
            "label": "周末休市",
            "market_close": f"{close_h:02d}:{close_m:02d}",
        }

    minutes = now.hour * 60 + now.minute
    morning = (9 * 60 + 15, 11 * 60 + 35)          # 09:15-11:35
    afternoon_start = 12 * 60 + 55                   # 12:55
    close_min = close_h * 60 + close_m
    afternoon_end = close_min + 5                    # 结束后缓冲 5 分钟
    pre_close_start = max(afternoon_start, close_min - pre_close_minutes)

    if morning[0] <= minutes <= morning[1]:
        return {
            "in_trading": True,
            "phase": "morning",
            "interval": base_interval,
            "label": "上午盘",
            "market_close": f"{close_h:02d}:{close_m:02d}",
        }
    if afternoon_start <= minutes <= afternoon_end:
        if pre_close_minutes and minutes >= pre_close_start:
            return {
                "in_trading": True,
                "phase": "pre_close",
                "interval": min(base_interval, pre_close_interval),
                "label": "收盘前加速",
                "market_close": f"{close_h:02d}:{close_m:02d}",
            }
        return {
            "in_trading": True,
            "phase": "afternoon",
            "interval": base_interval,
            "label": "下午盘",
            "market_close": f"{close_h:02d}:{close_m:02d}",
        }

    return {
        "in_trading": False,
        "phase": "off",
        "interval": off_interval,
        "label": "非交易时段",
        "market_close": f"{close_h:02d}:{close_m:02d}",
    }


def next_check_interval(now: Optional[datetime] = None) -> int:
    return int(get_schedule_info(now)["interval"])


# ══════════════════════════════════════════════════════════════════
#  🌐  雪球 API 客户端
# ══════════════════════════════════════════════════════════════════
_BASE_URL   = "https://xueqiu.com"
_STOCK_BASE = "https://stock.xueqiu.com"


def _alert_cookie_expired():
    """Cookie 失效时输出高优先级日志（带冷却，避免一轮降级刷屏）。"""
    now = time.time()
    last = getattr(_alert_cookie_expired, "_last", 0.0)
    if now - last < 60:
        return
    _alert_cookie_expired._last = now
    logger.critical("雪球 Cookie 已失效，请更新 XUEQIU_COOKIE！")


def _apply_cookie_to_session(session: requests.Session, cookie: str) -> None:
    """
    将 Cookie 字符串写入 session cookie jar。
    注意：不要用 headers['Cookie']，雪球接口会返回 400016。
    """
    session.headers.pop("Cookie", None)
    # 清掉旧的雪球登录相关 cookie，避免串台
    try:
        for c in list(session.cookies):
            if "xueqiu.com" in (c.domain or ""):
                session.cookies.clear(c.domain, c.path, c.name)
    except Exception:
        pass

    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        session.cookies.set(name, value, domain=".xueqiu.com", path="/")


# ─────────────────────────────────────────────
#  🔑  Token 过期检测
# ─────────────────────────────────────────────


def _get_token_expiry(cookie: str) -> Optional[float]:
    """从 xq_a_token / xq_id_token 中解析过期时间戳（秒）"""
    m = re.search(r'xq_a_token=([^;]+)', cookie)
    if m:
        token = m.group(1).strip()
        if '_' in token:
            parts = token.rsplit('_', 1)
            try:
                ts = int(parts[1])
                if ts > 1893456000000:  # 毫秒 → 秒
                    ts /= 1000
                if 1672531200 <= ts <= 1893456000:  # 2023 ~ 2030
                    return float(ts)
            except ValueError:
                pass

    # 回退：xq_id_token JWT 的 exp
    payload = _parse_xq_id_token(cookie)
    if payload and payload.get("exp"):
        try:
            return float(payload["exp"])
        except (TypeError, ValueError):
            pass
    return None


def _parse_xq_id_token(cookie: str) -> Optional[dict]:
    """解析 Cookie 中 xq_id_token 的 JWT payload（不校验签名）。"""
    m = re.search(r'xq_id_token=([^;]+)', cookie or "")
    if not m:
        return None
    token = m.group(1).strip()
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return None


def _get_token_issued_at(cookie: str) -> Optional[float]:
    """从 xq_id_token 解析签发/获取时间（秒）。优先 ctm，其次 iat。"""
    payload = _parse_xq_id_token(cookie)
    if not payload:
        return None
    ts = payload.get("ctm")
    if ts is None:
        ts = payload.get("iat")
    if ts is None:
        return None
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if ts > 1e12:  # 毫秒
        ts /= 1000.0
    if ts < 1e9:
        return None
    return ts


def _check_token_expiry(state: dict, notifier: Any) -> bool:
    """Token 过期前 1 天发送钉钉提醒。返回 True 表示 state 已变更。"""
    token_hash = hashlib.md5(XUEQIU_COOKIE.encode()).hexdigest()
    token_state = state.setdefault("_token", {})
    changed = False

    # token 被用户更新 → 重置通知状态，并记录新获取时间
    if token_state.get("hash") and token_state["hash"] != token_hash:
        token_state.clear()
        changed = True
    if token_state.get("hash") != token_hash:
        token_state["hash"] = token_hash
        changed = True

    issued = _get_token_issued_at(XUEQIU_COOKIE)
    if issued is not None and token_state.get("issued_at") != issued:
        token_state["issued_at"] = issued
        changed = True

    if not token_state.get("acquired_at"):
        if issued is not None:
            token_state["acquired_at"] = datetime.fromtimestamp(issued).isoformat(timespec="seconds")
        else:
            token_state["acquired_at"] = datetime.now().isoformat(timespec="seconds")
        changed = True

    expiry = _get_token_expiry(XUEQIU_COOKIE)
    if expiry is None:
        return changed

    if token_state.get("expiry") != expiry:
        token_state["expiry"] = expiry
        changed = True
    remaining = expiry - time.time()
    remaining_hours = remaining / 3600

    if 0 < remaining_hours <= 24 and not token_state.get("notified_expiry"):
        content = (
            f"## ⚠️ 雪球 Cookie 即将过期\n\n"
            f"> 过期时间：**{datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M')}**\n"
            f"> 剩余：**{remaining_hours:.1f} 小时**\n\n"
            f"请及时更新 `.env` 文件中的 `XUEQIU_COOKIE`，否则监控将失效。"
        )
        ok = notifier.send_markdown("雪球 Cookie 即将过期", content)
        if ok:
            token_state["notified_expiry"] = True
            return True
    return changed


class XueQiuClient:
    """
    雪球组合数据客户端（2025/2026 有效版）
    接口优先级：
      1. /cubes/rebalancing/current.json  （最新持仓，推荐）
      2. /v5/cube/rebalancing/current.json（备用，可能 404）
      3. /cubes/rebalancing/history.json  （降级方案）
    """

    def __init__(self, cookie: str, portfolio_id: str):
        self.portfolio_id = portfolio_id.upper()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://xueqiu.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        _apply_cookie_to_session(self.session, cookie)
        self._init_session()
        atexit.register(self.session.close)

    def set_cookie(self, cookie: str) -> None:
        """运行时更新 Cookie。"""
        _apply_cookie_to_session(self.session, cookie)

    def _init_session(self):
        try:
            self.session.get(_BASE_URL, timeout=10)
        except Exception as e:
            logger.warning(f"session 初始化失败 ({e})，继续运行")

    def _get(self, url: str, params: dict = None) -> Optional[dict]:
        try:
            resp = self.session.get(url, params=params, timeout=15)
            # 先尝试解析业务错误码（雪球未登录常返回 HTTP 400 + 400016）
            data = None
            try:
                data = resp.json()
            except Exception:
                data = None
            if isinstance(data, dict):
                err = str(data.get("error_code") or "")
                if err == "400016":
                    _alert_cookie_expired()
                    logger.error(f"HTTP {resp.status_code} error_code={err}: {url}")
                    return None
                # stock.xueqiu.com 的 403 常见于风控，不一定是 Cookie 失效
                if resp.status_code in (401, 403) and "xueqiu.com" in url and "stock.xueqiu.com" not in url:
                    _alert_cookie_expired()
                    logger.error(f"HTTP {resp.status_code} error_code={err}: {url}")
                    return None
            if resp.status_code in (401, 403) and "stock.xueqiu.com" in url:
                logger.error(f"HTTP {resp.status_code}: {url}")
                return None
            resp.raise_for_status()
            return data if isinstance(data, dict) else resp.json()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            logger.error(f"HTTP {status}: {url}")
            if status in (401, 403) and "stock.xueqiu.com" not in url:
                _alert_cookie_expired()
        except Exception as e:
            logger.error(f"请求异常: {url} -> {e}")
        return None

    def get_current_positions(self) -> list[dict]:
        """
        获取组合当前持仓，返回统一格式：
        [{'symbol': 'SH600519', 'name': '贵州茅台', 'weight': 30.5, 'prev_weight': 25.0, 'price': 1680.0}, ...]
        """
        # 方案1：新版接口
        url1 = f"{_BASE_URL}/cubes/rebalancing/current.json"
        params1 = {"cube_symbol": self.portfolio_id}
        data = self._get(url1, params1)
        source = "current"

        # 方案2：v5 备用接口
        if data is None:
            url2 = f"{_STOCK_BASE}/v5/cube/rebalancing/current.json"
            params2 = {"cube_symbol": self.portfolio_id, "count": 1, "page": 1}
            data = self._get(url2, params2)
            source = "v5"

        if data is None:
            data = self._get(
                f"{_BASE_URL}/cubes/rebalancing/history.json",
                {"cube_symbol": self.portfolio_id, "count": 1, "page": 1}
            )
            source = "history"

        if data is None:
            logger.warning(f"[{self.portfolio_id}] 持仓接口全部失败，返回空列表")
            return []

        positions = []
        try:
            if source in ("current", "v5"):
                last_rb = (
                    data.get("data", {}).get("last_rb")
                    or data.get("last_rb")
                    or {}
                )
                holdings = last_rb.get("holdings", [])
            elif source == "history":
                records = data.get("list", [])
                if records:
                    holdings = records[0].get("rebalancing_histories", [])
                else:
                    holdings = []
            else:
                holdings = []

            for item in holdings:
                weight = float(item.get("weight") or item.get("target_weight") or 0)
                if weight <= 0:
                    continue
                raw_code = str(item.get("stock_symbol") or item.get("symbol") or "").upper()
                positions.append({
                    "symbol":      raw_code,
                    "name":        item.get("stock_name") or item.get("name") or raw_code,
                    "weight":      weight,
                    "prev_weight": float(item.get("prev_weight") or 0),
                    "price":       float(item.get("price") or 0),
                })
        except (KeyError, TypeError, AttributeError) as e:
            logger.warning(f"持仓解析失败({e})，返回空列表")
            return []

        logger.info(f"[{self.portfolio_id}] 当前持仓 {len(positions)} 只 ({source})")
        return positions

    def get_cube_quote(self) -> Optional[dict]:
        """拉取雪球原组合行情摘要（净值/涨跌等）。"""
        data = self._get(
            f"{_BASE_URL}/cubes/quote.json",
            {"code": self.portfolio_id},
        )
        if data is None:
            return None

        raw = data.get(self.portfolio_id)
        if raw is None:
            raw = data.get("data") or data.get("quote")
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if not isinstance(raw, dict):
            # 有些响应直接把字段放顶层
            if "net_value" in data or "name" in data:
                raw = data
            else:
                return None

        def _num(key: str) -> Optional[float]:
            val = raw.get(key)
            if val is None or val == "":
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        return {
            "symbol": str(raw.get("symbol") or self.portfolio_id).upper(),
            "name": raw.get("name") or self.portfolio_id,
            "net_value": _num("net_value"),
            # 雪球 quote 接口的 gain 字段已是百分点（如 0.35 表示 +0.35%）
            "daily_gain": _num("daily_gain"),
            "monthly_gain": _num("monthly_gain"),
            "total_gain": _num("total_gain"),
            "annualized_gain_rate": _num("annualized_gain_rate")
            if raw.get("annualized_gain_rate") is not None
            else _num("annualized_gain"),
            "gain_unit": "percent",
            "follower_count": raw.get("follower_count"),
            "market": raw.get("market"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def get_latest_rebalancing(self) -> Optional[dict]:
        """获取最新调仓记录"""
        data = self._get(
            f"{_BASE_URL}/cubes/rebalancing/history.json",
            {"cube_symbol": self.portfolio_id, "count": 1, "page": 1}
        )
        if data is None:
            return None
        records = data.get("list", [])
        return records[0] if records else None


# ══════════════════════════════════════════════════════════════════
#  📤  钉钉通知
# ══════════════════════════════════════════════════════════════════
class DingTalkNotifier:
    """钉钉自定义机器人（无加签方式）"""

    def __init__(self, webhook: str):
        self.webhook = webhook
        self._last_send: dict[str, float] = {}  # cube_id -> timestamp
        self.cooldown = 60  # 同一组合 60 秒内不重复发送

    def _post(self, payload: dict) -> bool:
        try:
            resp = requests.post(
                self.webhook,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            result = resp.json()
            if result.get("errcode") == 0:
                return True
            logger.error(f"钉钉发送失败: {result}")
            return False
        except Exception as e:
            logger.error(f"钉钉请求异常: {e}")
            return False

    def send_markdown(self, title: str, content: str, cube_id: str = "") -> bool:
        now = time.time()
        if cube_id and now - self._last_send.get(cube_id, 0) < self.cooldown:
            logger.info(f"[{cube_id}] 冷却期内，跳过重复通知")
            return False
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": content},
            "at": {"isAtAll": AT_ALL},
        }
        ok = self._post(payload)
        if ok and cube_id:
            self._last_send[cube_id] = now
        return ok

    def send_text(self, content: str) -> bool:
        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {"isAtAll": AT_ALL},
        }
        return self._post(payload)


# ══════════════════════════════════════════════════════════════════
#  💾  状态持久化
# ══════════════════════════════════════════════════════════════════
_STATE_FILE = os.path.join(os.path.dirname(__file__), "monitor_state.json")
_CHANGE_HISTORY_LIMIT = 100


def _load_state() -> dict:
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存状态失败: {e}")


def _save_cube_state(state: dict, cube_id: str, positions: list, nav_info: dict, rb_id) -> None:
    """保存组合持仓快照和调仓记录ID"""
    state[cube_id] = {
        "positions": positions,
        "nav": nav_info,
        "last_rb_id": rb_id,
        "last_check": datetime.now().isoformat(),
    }


def format_change_detail(c: dict) -> str:
    """与钉钉通知一致的变动描述（不含 emoji / 加粗标记）"""
    change_type = c["type"]
    old_w = float(c.get("old_weight") or 0)
    new_w = float(c.get("new_weight") or 0)
    if change_type == "新增":
        return f"建仓 {new_w:.1f}%"
    if change_type == "卖出":
        return f"清仓（原仓位 {old_w:.1f}%）"
    if change_type == "加仓":
        return f"{old_w:.1f}% → {new_w:.1f}%（+{new_w - old_w:.1f}%）"
    return f"{old_w:.1f}% → {new_w:.1f}%（{new_w - old_w:.1f}%）"


def _append_change_history(
    state: dict,
    *,
    cube_id: str,
    nav_info: dict,
    changes: list[dict],
    rb_id,
    title: str,
    positions: Optional[list[dict]] = None,
) -> None:
    """记录已成功推送钉钉的持仓变动，供前端实时展示。"""
    now = datetime.now()
    total_weight = sum(float(p.get("weight") or 0) for p in (positions or []))
    cash = max(0.0, 100.0 - total_weight)
    event = {
        "id": f"{cube_id}-{rb_id or 'na'}-{int(now.timestamp() * 1000)}",
        "cube_id": cube_id,
        "cube_name": (nav_info or {}).get("name") or cube_id,
        "rb_id": rb_id,
        "time": now.isoformat(timespec="seconds"),
        "title": title,
        "notified": True,
        "total_weight": round(total_weight, 2),
        "cash": round(cash, 2),
        "changes": [
            {
                "type": c.get("type"),
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "old_weight": c.get("old_weight"),
                "new_weight": c.get("new_weight"),
                "price": c.get("price"),
                "detail": format_change_detail(c),
            }
            for c in changes
        ],
    }
    history = state.setdefault("_changes", [])
    history.insert(0, event)
    del history[_CHANGE_HISTORY_LIMIT:]


# ══════════════════════════════════════════════════════════════════
#  🔍  数据解析
# ══════════════════════════════════════════════════════════════════


def parse_nav_from_rebalancing(latest_rb: dict) -> dict[str, Any]:
    """从调仓记录中解析组合名称"""
    if not latest_rb:
        return {"name": ""}
    data = latest_rb if isinstance(latest_rb, dict) else {}
    histories = data.get("rebalancing_histories", [])
    cube_name = (
        histories[0].get("cube_name", "")
        if histories else data.get("cube_name", "")
    )
    return {"name": cube_name}


# ══════════════════════════════════════════════════════════════════
#  🔔  变动检测
# ══════════════════════════════════════════════════════════════════

def detect_changes(old: list[dict], new: list[dict]) -> list[dict]:
    """
    对比新旧持仓，以上次保存的 weight 为基准检测变动（而非 prev_weight）。
    这样可避免同一次调仓被多轮检查重复触发。

    返回变动列表，每项格式：
    {'type': '新增'/'卖出'/'加仓'/'减仓', 'symbol': ..., 'name': ...,
     'old_weight': ..., 'new_weight': ..., 'price': ...}
    """
    old_map = {p["symbol"]: p for p in old}
    new_map = {p["symbol"]: p for p in new}
    changes = []

    for sym, pos in new_map.items():
        curr_w = pos["weight"]
        if sym not in old_map:
            changes.append({
                "type": "新增",
                "symbol": sym,
                "name": pos["name"],
                "old_weight": 0,
                "new_weight": curr_w,
                "price": pos["price"],
            })
        else:
            # 使用上次保存的 weight（而非 API 的 prev_weight）进行对比，防止重复触发
            old_w = old_map[sym].get("weight", 0)
            delta = curr_w - old_w
            if abs(delta) >= WEIGHT_CHANGE_THRESHOLD:
                changes.append({
                    "type": "加仓" if delta > 0 else "减仓",
                    "symbol": sym,
                    "name": pos["name"],
                    "old_weight": old_w,
                    "new_weight": curr_w,
                    "price": pos["price"],
                })

    for sym, pos in old_map.items():
        if sym not in new_map:
            changes.append({
                "type": "卖出",
                "symbol": sym,
                "name": pos["name"],
                "old_weight": pos.get("weight", 0),
                "new_weight": 0,
                "price": pos["price"],
            })

    return changes


# ══════════════════════════════════════════════════════════════════
#  📨  通知内容构建
# ══════════════════════════════════════════════════════════════════

_TYPE_EMOJI = {"新增": "🟢", "加仓": "📈", "减仓": "📉", "卖出": "🔴"}


def build_markdown(
    cube_id: str,
    nav_info: dict,
    changes: list[dict],
    positions: Optional[list[dict]] = None,
) -> tuple[str, str]:
    """
    构建钉钉 Markdown 消息。
    返回 (title, content)
    """
    name = nav_info.get("name") or cube_id
    now = datetime.now().strftime("%m/%d %H:%M")

    title = f"雪球组合变动 · {name}"

    total_weight = 0.0
    if positions:
        total_weight = sum(float(p.get("weight") or 0) for p in positions)
    cash = max(0.0, 100.0 - total_weight)

    lines = [
        f"## 📈 {name} 持仓变动",
        f"> 组合代码：**{cube_id}**　｜　检测时间：{now}",
        f"> 持仓合计：**{total_weight:.1f}%**　｜　现金/未分配约：**{cash:.1f}%**",
        "",
        "### 📋 变动明细",
    ]

    for c in changes:
        emoji = _TYPE_EMOJI.get(c["type"], "•")
        change_type = c["type"]
        sym = c["symbol"]
        n = c["name"]
        price = c["price"]
        if change_type == "新增":
            detail_md = f"建仓 **{float(c['new_weight']):.1f}%**"
        elif change_type == "卖出":
            detail_md = format_change_detail(c)
        elif change_type == "加仓":
            old_w = float(c["old_weight"])
            new_w = float(c["new_weight"])
            detail_md = f"{old_w:.1f}% → **{new_w:.1f}%**（+{new_w - old_w:.1f}%）"
        else:
            old_w = float(c["old_weight"])
            new_w = float(c["new_weight"])
            detail_md = f"{old_w:.1f}% → **{new_w:.1f}%**（{new_w - old_w:.1f}%）"

        price_str = f"  当前价 ¥{price:.2f}" if price else ""
        lines.append(f"- {emoji} **{change_type}** {n}（{sym}）：{detail_md}{price_str}")

    content = "\n".join(lines)
    return title, content


# ══════════════════════════════════════════════════════════════════
#  🔄  单次监控执行
# ══════════════════════════════════════════════════════════════════

def monitor_once(client: XueQiuClient, notifier: DingTalkNotifier):
    """对所有监控组合执行一次检查"""
    state = _load_state()
    state_changed = False

    # Token 过期检查（仅首次检测到即将过期时发通知）
    if _check_token_expiry(state, notifier):
        state_changed = True

    for cube_id in MONITORED_CUBES:
        logger.info(f"─── 检查组合 {cube_id} ───")
        try:
            # 获取当前持仓
            client.portfolio_id = cube_id  # 切换组合
            new_positions = client.get_current_positions()
            logger.info(f"持仓数量: {len(new_positions)} 只")

            if not new_positions:
                logger.warning(f"[{cube_id}] 持仓列表为空，可能 Cookie 失效或组合不存在")

            # 获取最新调仓记录（含组合名称和调仓ID）
            latest_rb = client.get_latest_rebalancing()
            nav_info = parse_nav_from_rebalancing(latest_rb)
            if not nav_info.get("name"):
                nav_info["name"] = cube_id
            logger.info(f"组合名称: {nav_info['name']}")

            # ── 防重复推送：对比调仓记录 ID ──────────────────────────────
            rb_id = latest_rb.get("id") if latest_rb else None
            last_rb_id = state.get(cube_id, {}).get("last_rb_id")

            if rb_id and rb_id == last_rb_id:
                logger.info(f"[{cube_id}] 调仓记录未变化（ID={rb_id}），跳过通知")
                # 仅更新检查时间，不改变持仓快照
                if cube_id in state:
                    state[cube_id]["last_check"] = datetime.now().isoformat()
                    state_changed = True
                continue

            if rb_id:
                logger.info(f"[{cube_id}] 发现新调仓记录（ID={rb_id}，上次={last_rb_id}）")
            # ─────────────────────────────────────────────────────────────

            # 对比变动
            old_positions = state.get(cube_id, {}).get("positions", [])
            changes = detect_changes(old_positions, new_positions)

            if changes:
                logger.info(f"[{cube_id}] 检测到 {len(changes)} 项变动，准备发送通知")
                title, content = build_markdown(cube_id, nav_info, changes, positions=new_positions)
                ok = notifier.send_markdown(title, content, cube_id=cube_id)
                if ok:
                    logger.info(f"[{cube_id}] 通知发送成功")
                    # 推送成功 → 更新完整状态（含新 rb_id）+ 写入变动历史（与钉钉同步）
                    _save_cube_state(state, cube_id, new_positions, nav_info, rb_id)
                    _append_change_history(
                        state,
                        cube_id=cube_id,
                        nav_info=nav_info,
                        changes=changes,
                        rb_id=rb_id,
                        title=title,
                        positions=new_positions,
                    )
                    state_changed = True
                else:
                    logger.error(f"[{cube_id}] 通知发送失败，保留旧状态，下次重试")
                    # 推送失败 → 不更新状态，下次重试
            else:
                logger.info(f"[{cube_id}] 无持仓变动（阈值 {WEIGHT_CHANGE_THRESHOLD}%）")
                # 无变动 → 更新快照和 rb_id（避免下次重复对比）
                _save_cube_state(state, cube_id, new_positions, nav_info, rb_id)
                state_changed = True

        except Exception as e:
            logger.error(f"[{cube_id}] 异常: {e}")
            logger.debug(traceback.format_exc())
            # 发送错误通知（避免静默失败）
            notifier.send_text(f"⚠️ 雪球监控异常\n组合：{cube_id}\n错误：{e}")

        time.sleep(2)  # 避免请求过快

    if state_changed:
        _save_state(state)


# ══════════════════════════════════════════════════════════════════
#  ✅  启动自检
# ══════════════════════════════════════════════════════════════════

def _check_config() -> bool:
    """检查配置完整性，有问题直接打印并返回 False"""
    ok = True
    if "你的token" in XUEQIU_COOKIE or not XUEQIU_COOKIE:
        print("❌ 未配置雪球 Cookie！请在 .env 文件或脚本顶部填写 XUEQIU_COOKIE")
        ok = False
    if "你的access_token" in DINGTALK_WEBHOOK or not DINGTALK_WEBHOOK:
        print("❌ 未配置钉钉 Webhook！请在 .env 文件或脚本顶部填写 DINGTALK_WEBHOOK")
        ok = False
    if not MONITORED_CUBES or MONITORED_CUBES == ["ZH123456"]:
        print("❌ 未配置监控组合！请在 .env 文件或脚本顶部填写 MONITORED_CUBES")
        ok = False
    return ok


# ══════════════════════════════════════════════════════════════════
#  ▶  主程序（内置定时循环）
# ══════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  雪球组合变动监控 v2.1  ·  钉钉通知")
    print("=" * 60)

    if not _check_config():
        print("\n💡 配置方式（推荐使用 .env 文件）：")
        print("   在脚本同目录创建 .env 文件，内容：")
        print("     XUEQIU_COOKIE=xq_a_token=xxx;u=xxx")
        print("     DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx")
        print("     MONITORED_CUBES=ZH123456,ZH654321")
        print("     CHECK_INTERVAL=300")
        sys.exit(1)

    print(f"\n📌 监控组合: {', '.join(MONITORED_CUBES)}")
    sched = get_schedule_info()
    print(f"⏱  交易时段间隔: {CHECK_INTERVAL} 秒｜休市间隔: {OFF_HOURS_INTERVAL} 秒")
    print(f"📅 当前时段: {sched['label']}（下次间隔 {sched['interval']} 秒）")
    print(f"📊 仓位变动阈值: ≥ {WEIGHT_CHANGE_THRESHOLD}%")
    print(f"📅 启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # 初始化客户端（cookie 全局复用，portfolio_id 每次切换）
    client = XueQiuClient(XUEQIU_COOKIE, MONITORED_CUBES[0])
    notifier = DingTalkNotifier(DINGTALK_WEBHOOK)

    # 启动时立即执行一次
    logger.info("首次检查...")
    monitor_once(client, notifier)

    # 定时循环（智能调度）
    try:
        while True:
            sched = get_schedule_info()
            interval = int(sched["interval"])
            logger.info(
                f"当前时段「{sched['label']}」，等待 {interval} 秒后再次检查..."
            )
            time.sleep(interval)
            monitor_once(client, notifier)
    except KeyboardInterrupt:
        logger.info("监控已手动停止")
        print("\n👋 监控已停止")


if __name__ == "__main__":
    main()

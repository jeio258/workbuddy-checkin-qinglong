#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 宠物(Buddy)自动探险(派出)与积分自动领取（青龙面板版）

背景：
  在 WorkBuddy「成长空间 / 成长中心」网页里，可以把宠物派出去探险(travel)，
  一段时间后宠物归来(arrived)，需要手动点「领取积分」。本脚本把这条链路完全自动化：

    1) 查询宠物旅行状态 travel/status
       - state == "arrived"  -> 自动领取积分 (POST travel/claim)
       - state == "idle" 且 未达每日上限 -> 自动派出 (POST travel/depart)
       - state == "traveling" -> 还在路上，下一轮定时再查（不重复派出）
       - daily_limit_reached == true -> 今日已达派出上限，跳过
    2) 幂等安全：高频运行无副作用；idle 时调用 claim 返回 400 已处理，不会报错

特性（与 workbuddy_checkin.py 保持一致）：
  - Token 全部从环境变量读取，不写死在脚本里
  - 支持多账号：WB_ACCESS_TOKENS 用 "," 分隔
  - 输出兼容青龙面板（直接 print 即可被捕获并通知）
  - 接口失败不影响其它账号

环境变量：
  WB_ACCESS_TOKEN    单个账号 Bearer Token（必填其一）
  WB_ACCESS_TOKENS   多账号 Token，逗号分隔；支持 "uid:nickname:token" / "uid:token" / 纯 "token"（必填其一）
  WB_USER_ID         可选，X-User-Id；不填则尝试从 JWT 解析 sub
  WB_TRAVEL_LOCATION 可选，派出地点。填数字 id(如 1) 或 code(如 coffee)；不填则取配置里第一个地点
  WB_TRAVEL_AUTO_DEPART  可选，默认 1；设为 0 则【只领取、不自动派出】
  WB_PROXY           可选，代理；不填直连（避免系统 SOCKS 代理导致 socks 支持崩溃）
  WB_DOMAIN          可选，X-Domain 头；默认 www.codebuddy.cn
  QINGLONG_NOTIFY    可选，默认 1；设为 0 关闭青龙通知标记

本地调试（不依赖青龙）：
  WB_ACCESS_TOKEN=xxxx python3 workbuddy_buddy_travel.py
"""

import os
import sys
import json
import time
import base64

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAVE_REQUESTS = False

API_BASE = "https://copilot.tencent.com"
STATUS_EP = "/activity/growth/buddy/travel/status"
CONFIG_EP = "/activity/growth/buddy/travel/config"
DEPART_EP = "/activity/growth/buddy/travel/depart"
CLAIM_EP = "/activity/growth/buddy/travel/claim"

# 官方 CLI UA 与 X-Domain 头，与 workbuddy_checkin.py 保持一致；X-Domain 可用 WB_DOMAIN 覆盖
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
DOMAIN = os.environ.get("WB_DOMAIN", "").strip() or "www.codebuddy.cn"
NOTIFY = str(os.environ.get("QINGLONG_NOTIFY", "1")).lower() in ("1", "true", "yes")
AUTO_DEPART = str(os.environ.get("WB_TRAVEL_AUTO_DEPART", "1")).lower() in ("1", "true", "yes", "")
LOCATION_ARG = os.environ.get("WB_TRAVEL_LOCATION", "").strip()
# 代理：默认直连（避免系统 SOCKS 代理导致的 "Missing dependencies for SOCKS support" 崩溃）。
# 如需走代理，设置 WB_PROXY，例如 http://127.0.0.1:7897 或 socks5://127.0.0.1:7897
PROXY = os.environ.get("WB_PROXY", "").strip()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else {"http": None, "https": None}


_OUT = []


def log(msg):
    print(msg)
    _OUT.append(str(msg))


def notify(title, content):
    """通过青龙 QLAPI 发送通知（使用面板配置的 PushPlus 渠道）。"""
    if str(os.environ.get("QINGLONG_NOTIFY", "1")).lower() not in ("1", "true", "yes"):
        return
    try:
        QLAPI.systemNotify({"title": title, "content": content})
        print("📨 通知已推送")
    except Exception as e:
        print(f"⚠️ 通知发送失败: {e}")


def decode_jwt_sub(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
        return payload.get("sub") or ""
    except Exception:
        return ""


def decode_jwt_exp(token):
    """解码 JWT payload 的 exp（Unix 秒），用于过期预警。失败返回 0。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
        return int(payload.get("exp") or 0)
    except Exception:
        return 0


def get_accounts():
    """从环境变量读取账号列表，返回 [(uid, nickname, token), ...]。

    支持 "uid:nickname:token" / "uid:token" / 纯 "token" 三种格式，逗号分隔。
    """
    tokens_raw = os.environ.get("WB_ACCESS_TOKENS", "").strip()
    single = os.environ.get("WB_ACCESS_TOKEN", "").strip()
    tokens = []
    if tokens_raw:
        # token 为 base64url 不含冒号，从右拆出 token 安全；左侧再拆 uid:nickname（昵称可含冒号）
        for item in tokens_raw.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item and not item.startswith("eyJ"):
                head, _, tok = item.rpartition(":")
                uid, _, nick = head.partition(":")
                tokens.append((uid.strip(), nick.strip(), tok.strip()))
            else:
                tokens.append(("", "", item))
    if single:
        tokens.append(("", "", single.strip()))
    if not tokens:
        log("❌ 未检测到 Token：请设置环境变量 WB_ACCESS_TOKEN 或 WB_ACCESS_TOKENS")
        return []
    fixed = []
    for uid, nick, tok in tokens:
        if not uid:
            uid = decode_jwt_sub(tok)
        fixed.append((uid, nick, tok))
    return fixed


def http(method, url, token, uid, body="{}"):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid or "",
        "X-Domain": DOMAIN,
        "User-Agent": CLIENT_UA,
    }
    if HAVE_REQUESTS:
        resp = requests.request(method, url, headers=headers, data=body, timeout=20, proxies=PROXIES)
        return resp.status_code, resp.text
    else:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method=method)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler(PROXIES)).open(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")


def fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return str(ts)


def pick_location(config_data):
    """根据 WB_TRAVEL_LOCATION 选择地点；未指定则取配置中第一个。"""
    locations = (config_data or {}).get("locations") or []
    if not locations:
        return None
    if LOCATION_ARG:
        for loc in locations:
            if str(loc.get("id")) == LOCATION_ARG:
                return loc
            if loc.get("code") == LOCATION_ARG:
                return loc
        log(f"⚠️ 指定的地点 '{LOCATION_ARG}' 不在可选列表，回退到第一个地点")
    return locations[0]


def travel_one(uid, token):
    """对单个账号执行宠物旅行自动化，返回 (是否成功动作: bool, 消息: str)。"""
    # 1) 查状态
    st_code, st_text = http("GET", API_BASE + STATUS_EP, token, uid)
    if st_code == 401:
        return False, "登录态失效(401)，请在 WorkBuddy 客户端重新登录后更新 Token"
    try:
        st = json.loads(st_text)
    except Exception:
        return False, f"状态接口返回异常({st_code}): {st_text[:120]}"
    if st.get("code") != 0:
        return False, f"状态查询失败 code={st.get('code')} msg={st.get('msg','')}"
    data = st.get("data") or {}
    state = data.get("state")
    limit_reached = bool(data.get("daily_limit_reached"))

    if state == "arrived":
        # 2a) 已归来 -> 领取积分
        ck_code, ck_text = http("POST", API_BASE + CLAIM_EP, token, uid, "{}")
        if ck_code == 401:
            return False, "登录态失效(401)，请重新登录后更新 Token"
        try:
            ck = json.loads(ck_text)
        except Exception:
            return False, f"领取接口返回异常({ck_code}): {ck_text[:120]}"
        if ck.get("code") == 0:
            d = ck.get("data") or {}
            credit = d.get("credit") or d.get("reward_credit") or data.get("reward_credit") or 0
            return True, f"🎉 宠物已归来，自动领取积分 +{credit}"
        if ck.get("code") == 400 and "no unclaimed" in (ck.get("msg") or ""):
            return True, "宠物已归来，但本次无可领取奖励（可能已领取过）"
        return False, f"领取失败 code={ck.get('code')} msg={ck.get('msg','')}"

    if state == "traveling":
        arrive = data.get("arrive_at") or 0
        loc = data.get("location") or {}
        remain = max(0, int(arrive) - int(data.get("server_now") or int(time.time())))
        return True, f"🚀 宠物正在探险中（{loc.get('name','')}），预计 {fmt_time(arrive)} 归来（约 {remain//60} 分钟后），本轮不重复派出"

    if state == "idle":
        if limit_reached:
            return True, "😴 宠物空闲，但今日派出次数已达上限，明天再派"
        if not AUTO_DEPART:
            return True, "😴 宠物空闲（WB_TRAVEL_AUTO_DEPART=0，仅领取模式，未自动派出）"
        # 2b) 空闲且未达上限 -> 自动派出
        cfg_code, cfg_text = http("GET", API_BASE + CONFIG_EP, token, uid)
        try:
            cfg = json.loads(cfg_text) if cfg_code == 200 else {}
        except Exception:
            cfg = {}
        loc = pick_location(cfg.get("data") if isinstance(cfg, dict) else {})
        if not loc:
            return False, "未获取到可选的探险地点配置"
        dp_code, dp_text = http("POST", API_BASE + DEPART_EP, token, uid,
                                json.dumps({"location_id": loc.get("id")}))
        if dp_code == 401:
            return False, "登录态失效(401)，请重新登录后更新 Token"
        try:
            dp = json.loads(dp_text)
        except Exception:
            return False, f"派出接口返回异常({dp_code}): {dp_text[:120]}"
        if dp.get("code") == 0:
            dur = loc.get("duration_hours_max") or loc.get("duration_hours_min") or "?"
            return True, f"🐾 已自动派出宠物前往「{loc.get('name','')}」，约 {dur} 小时后归来并自动领取积分"
        return False, f"派出失败 code={dp.get('code')} msg={dp.get('msg','')}"

    return True, f"宠物状态未知: {state}"


def main():
    accounts = get_accounts()
    if not accounts:
        sys.exit(2)
    log(f"🐱 WorkBuddy 宠物自动探险开始，共 {len(accounts)} 个账号")
    ok, fail = 0, 0
    for idx, (uid, nickname, token) in enumerate(accounts, 1):
        # 展示名：优先昵称，其次 uid 前 8 位，都没有才用序号（多账号时区分谁是谁）
        tag = nickname or ((uid[:8] + "…") if uid else f"账号{idx}")
        # 过期预警：JWT exp 距现在 < 7 天时提示（不阻塞探险流程）
        exp = decode_jwt_exp(token)
        if exp:
            remain_days = (exp - time.time()) / 86400
            if remain_days < 0:
                log(f"⚠️ [{tag}] Token 已过期（JWT exp），接口将返回 401")
            elif remain_days < 7:
                log(f"⚠️ [{tag}] Token 将于约 {int(remain_days)} 天后过期，请提前更新")
        try:
            success, msg = travel_one(uid, token)
        except Exception as e:
            success, msg = False, f"异常: {e}"
        if success:
            ok += 1
            log(f"✅ [{tag}] {msg}")
        else:
            fail += 1
            log(f"❌ [{tag}] {msg}")
        time.sleep(1)
    log(f"🏁 完成：成功 {ok} / 失败 {fail} / 共 {len(accounts)}")
    notify(f"WorkBuddy 自动探险：成功 {ok} / 失败 {fail}", "\n".join(_OUT))
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

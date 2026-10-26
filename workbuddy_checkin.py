#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日自动签到（青龙面板版）

功能：
  - 调用官方签到接口，幂等（今日已签则跳过）
  - 签到活动未开启时跳过（active=false 不算失败）
  - 回传积分/套餐数据（get-user-resource，多套餐聚合）
  - 支持多账号：用 "," 分隔多个 Token
  - Token 不写死在脚本里，全部从环境变量读取
  - 输出结构与青龙面板兼容（直接 print 即可被青龙捕获并通知）

环境变量：
  WB_ACCESS_TOKEN   单个账号的 Bearer Token（必填其一）
  WB_ACCESS_TOKENS  多个账号 Token，逗号分隔（必填其一）；支持 "uid:nickname:token" / "uid:token" / 纯 "token"
  WB_USER_ID        可选，X-User-Id 头；不填则尝试从 JWT 解析 sub
  WB_PROXY          可选，代理；不填直连（避免系统 SOCKS 代理导致 socks 支持崩溃）
  WB_DOMAIN         可选，X-Domain 头；默认 www.codebuddy.cn
  QINGLONG_NOTIFY   可选，1/true 时打印青龙通知标记（默认开启，青龙自动捕获 stdout）

本地调试（不依赖青龙）：
  WB_ACCESS_TOKEN=xxxx python3 workbuddy_checkin.py
"""

import os
import sys
import json
import time
import base64

# 优先用 requests（青龙自带），否则回退到标准库 urllib
try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAVE_REQUESTS = False

API_BASE = "https://copilot.tencent.com/v2/billing/meter"
STATUS_EP = API_BASE + "/checkin-activity-status"
CHECKIN_EP = API_BASE + "/daily-checkin"
# 积分/套餐接口走 www.codebuddy.cn（Windows 版与参考项目均在此域名实测可用）
CREDIT_EP = "https://www.codebuddy.cn/v2/billing/meter/get-user-resource"
# 官方 CLI UA 与 X-Domain 头：get-user-resource 会校验请求合法性（缺则报 10085 请求不合法），
# 与 Windows 版/参考项目实测可用的请求头一致；X-Domain 可用 WB_DOMAIN 覆盖
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
DOMAIN = os.environ.get("WB_DOMAIN", "").strip() or "www.codebuddy.cn"
# 代理：默认直连（避免系统 SOCKS 代理导致的 "Missing dependencies for SOCKS support" 崩溃）。
# 如需走代理，设置 WB_PROXY，例如 http://127.0.0.1:7897 或 socks5://127.0.0.1:7897
PROXY = os.environ.get("WB_PROXY", "").strip()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else {"http": None, "https": None}

NOTIFY = str(os.environ.get("QINGLONG_NOTIFY", "1")).lower() in ("1", "true", "yes")


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
    """无需密钥，仅 base64url 解码 payload 取 sub（Uid）。失败返回空串。"""
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


def _bool(d, *keys):
    for k in keys:
        if k in d:
            v = d[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return v != 0
            if isinstance(v, str):
                return v.lower() in ("true", "1")
    return False


def _int(d, *keys):
    for k in keys:
        if k in d:
            try:
                return int(d[k])
            except (TypeError, ValueError):
                pass
    return 0


def _str(d, *keys):
    for k in keys:
        if k in d and isinstance(d[k], str):
            return d[k]
    return ""


def normalize_checkin_status(data):
    """归一化签到状态字段，兼容 snake_case 与 camelCase（接口两种写法都出现过）。"""
    if not isinstance(data, dict):
        return {}
    return {
        "active": _bool(data, "active", "Active"),
        "today_checked_in": _bool(data, "today_checked_in", "todayCheckedIn"),
        "streak_days": _int(data, "streak_days", "streakDays"),
        "daily_credit": _int(data, "daily_credit", "dailyCredit"),
        "today_credit": _int(data, "today_credit", "todayCredit"),
        "total_credits": _int(data, "total_credits", "totalCredits"),
        "week_checkin_days": _int(data, "week_checkin_days", "weekCheckinDays"),
        "activity_name": _str(data, "activity_name", "activityName"),
    }


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
    # 补全 uid
    fixed = []
    for uid, nick, tok in tokens:
        if not uid:
            uid = decode_jwt_sub(tok)
        fixed.append((uid, nick, tok))
    return fixed


def http_post(url, token, uid, body="{}"):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid or "",
        "X-Domain": DOMAIN,
        "User-Agent": CLIENT_UA,
    }
    if HAVE_REQUESTS:
        resp = requests.post(url, headers=headers, data=body, timeout=20, proxies=PROXIES)
        return resp.status_code, resp.text
    else:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler(PROXIES)).open(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")


def http_get(url, token, uid):
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid or "",
        "X-Domain": DOMAIN,
        "User-Agent": CLIENT_UA,
    }
    if HAVE_REQUESTS:
        resp = requests.get(url, headers=headers, timeout=20, proxies=PROXIES)
        return resp.status_code, resp.text
    else:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler(PROXIES)).open(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")


def checkin_one(uid, token):
    """对单个账号执行签到，返回 (成功: bool, 消息: str)。"""
    # 1) 查状态（注意：官方接口只接受 POST，GET 会返回 404 page not found）
    st_code, st_text = http_post(STATUS_EP, token, uid)
    if st_code == 401:
        return False, "登录态失效(401)，请在 WorkBuddy 客户端重新登录后更新 Token"
    try:
        st = json.loads(st_text)
    except Exception:
        return False, f"状态接口返回异常({st_code}): {st_text[:120]}"
    if st.get("code") != 0:
        return False, f"状态查询失败 code={st.get('code')} msg={st.get('msg','')}"
    data = normalize_checkin_status(st.get("data") or {})
    if not data.get("active"):
        return True, "签到活动未开启（跳过）"
    if data.get("today_checked_in"):
        return True, fmt_status(data, "今日已签到")
    # 2) 执行签到
    ck_code, ck_text = http_post(CHECKIN_EP, token, uid)
    if ck_code == 401:
        return False, "登录态失效(401)，请在 WorkBuddy 客户端重新登录后更新 Token"
    try:
        ck = json.loads(ck_text)
    except Exception:
        return False, f"签到接口返回异常({ck_code}): {ck_text[:120]}"
    code = ck.get("code")
    if code == 0:
        d = ck.get("data") or {}
        credit = d.get("credit", 0)
        streak = d.get("streak_days", 0)
        return True, f"签到成功 +{credit} 积分（连续 {streak} 天）"
    if code == 10001 or "已签" in (ck.get("msg") or ""):
        return True, "今日已签到（接口确认）"
    return False, f"签到失败 code={code} msg={ck.get('msg','')}"


def fmt_status(data, head):
    """把状态富字段拼成可读串：head（连续 N 天 | 本周 N 次 | 今日 N | 累计 N）。"""
    bits = []
    if data.get("streak_days"):
        bits.append(f"连续 {data['streak_days']} 天")
    if data.get("week_checkin_days"):
        bits.append(f"本周 {data['week_checkin_days']} 次")
    if data.get("today_credit"):
        bits.append(f"今日 {data['today_credit']}")
    if data.get("total_credits"):
        bits.append(f"累计 {data['total_credits']}")
    return f"{head}（{' | '.join(bits)}）" if bits else head


def fetch_user_resource(token, uid):
    """查询积分/套餐（多套餐包聚合）。返回 (summary_dict, 错误消息)。"""
    # 计费数据以北京时区计；青龙容器默认 UTC，手动偏移 8 小时避免范围偏差
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    body = json.dumps({
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now,
        "PackageEndTimeRangeEnd": "2126-12-31 23:59:59",
    })
    code, text = http_post(CREDIT_EP, token, uid, body)
    if code == 401:
        return None, "鉴权失败(401)"
    try:
        data = json.loads(text)
    except Exception:
        return None, f"积分接口返回异常({code}): {text[:120]}"
    if data.get("code") != 0:
        return None, f"积分查询失败 code={data.get('code')} msg={data.get('msg','')}"
    d = data.get("data") or {}
    resp = d.get("Response", d) if isinstance(d, dict) else {}
    resp_data = resp.get("Data", {}) if isinstance(resp, dict) else {}
    accounts = resp_data.get("Accounts", []) if isinstance(resp_data, dict) else []
    if not isinstance(accounts, list):
        accounts = []
    total_remain = total_used = total_size = 0
    packages = []
    for pkg in accounts:
        if not isinstance(pkg, dict):
            continue
        remain, used, size = _package_remain_used(pkg)
        total_remain += remain
        total_used += used
        total_size += size
        packages.append({"name": pkg.get("PackageName", ""),
                         "remain": remain, "used": used, "size": size})
    # 补全 used（无显式 used 时用 总量-剩余 推算）
    if total_size > 0:
        derived = total_size - total_remain
        if derived < 0:
            derived = 0
        if derived > total_used:
            total_used = derived
    dosage = resp_data.get("TotalDosage", 0)
    if dosage and dosage > total_size:
        total_size = dosage
        derived = total_size - total_remain
        if derived > total_used:
            total_used = max(derived, 0)
    return {"total_remain": total_remain, "total_used": total_used,
            "total_size": total_size, "pack_count": len(packages),
            "packages": packages}, None


def _package_remain_used(pkg):
    """提取单个套餐包的 remain/used/size（逻辑来自参考项目 billing.go 移植）。"""
    cycle_size = pkg.get("CycleCapacitySize", 0)
    if cycle_size and cycle_size > 0:
        remain = max(pkg.get("CycleCapacityRemain", 0), 0)
        if remain > cycle_size:
            remain = cycle_size
        used = cycle_size - remain
        explicit_used = pkg.get("CycleCapacityUsed", 0)
        if explicit_used > used:
            used = explicit_used
            if cycle_size >= used:
                remain = cycle_size - used
        return remain, used, cycle_size
    cycle_remain = pkg.get("CycleCapacityRemain", 0)
    cycle_used = pkg.get("CycleCapacityUsed", 0)
    if cycle_remain > 0 or cycle_used > 0:
        remain = max(cycle_remain, 0)
        used = max(cycle_used, 0)
        size = remain + used
        cap_size = pkg.get("CapacitySize", 0)
        if cap_size > size:
            size = cap_size
            if size >= remain:
                used = size - remain
        return remain, used, size
    remain = max(pkg.get("CapacityRemain", 0), 0)
    used = max(pkg.get("CapacityUsed", 0), 0)
    size = pkg.get("CapacitySize", 0)
    if size <= 0:
        size = remain + used
    if used == 0 and size > remain:
        used = size - remain
    return remain, used, size


def main():
    accounts = get_accounts()
    if not accounts:
        sys.exit(2)
    log(f"🔰 WorkBuddy 自动签到开始，共 {len(accounts)} 个账号")
    ok, fail = 0, 0
    for idx, (uid, nickname, token) in enumerate(accounts, 1):
        # 展示名：优先昵称，其次 uid 前 8 位，都没有才用序号（多账号时区分谁是谁）
        tag = nickname or ((uid[:8] + "…") if uid else f"账号{idx}")
        # 过期预警：JWT exp 距现在 < 7 天时提示（不阻塞签到）
        exp = decode_jwt_exp(token)
        if exp:
            remain_days = (exp - time.time()) / 86400
            if remain_days < 0:
                log(f"⚠️ [{tag}] Token 已过期（JWT exp），接口将返回 401")
            elif remain_days < 7:
                log(f"⚠️ [{tag}] Token 将于约 {int(remain_days)} 天后过期，请提前更新")
        try:
            success, msg = checkin_one(uid, token)
        except Exception as e:
            success, msg = False, f"异常: {e}"
        if success:
            ok += 1
            log(f"✅ [{tag}] {msg}")
        else:
            fail += 1
            log(f"❌ [{tag}] {msg}")
        # 积分/套餐回传：失败不阻塞签到，仅打日志
        try:
            credits, cerr = fetch_user_resource(token, uid)
        except Exception as e:
            credits, cerr = None, f"异常: {e}"
        if credits:
            log(f"📦 [{tag}] 总积分 {credits['total_size']} | 剩余 {credits['total_remain']} | 已用 {credits['total_used']}（{credits['pack_count']} 个套餐）")
        elif cerr:
            log(f"ℹ️ [{tag}] 积分查询未回传: {cerr}")
        time.sleep(1)  # 多账号之间稍作间隔，避免触发风控
    log(f"🏁 完成：成功 {ok} / 失败 {fail} / 共 {len(accounts)}")
    notify(f"WorkBuddy 自动签到：成功 {ok} / 失败 {fail}", "\n".join(_OUT))
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

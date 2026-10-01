# -*- coding: utf-8 -*-
"""VRC-OSC-Tester: VRChat OSC 功能状态检测工具

端口检测优先级（谁可靠听谁的）:
1. VRChat.exe 进程命令行中的 --osc=输入:IP:输出 参数
2. 最新 output_log 中的 "Advertising Service ... of type OSC on <端口>"（实际生效输入端口）
3. osc.json 配置文件（如存在）
4. 默认 9000 / 9001
"""

import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import scrolledtext, ttk

import psutil
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

VRCHAT_PROCESS_NAME = "VRChat.exe"
VRCHAT_DATA_DIR = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")),
    "AppData", "LocalLow", "VRChat", "VRChat",
)
OSC_CONFIG_PATH = os.path.join(VRCHAT_DATA_DIR, "osc.json")

DEFAULT_IN_PORT = 9000
DEFAULT_OUT_PORT = 9001

# ---------------- 文件日志 ----------------
# 日志目录: exe 同目录下的 logs 文件夹（脚本运行时为脚本所在目录）
_frozen = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable) if _frozen else os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = None
_file_lock = threading.Lock()


def _init_log_file():
    """创建 logs 目录并初始化本次运行的日志文件，返回日志路径或 None"""
    global LOG_FILE
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        LOG_FILE = os.path.join(LOG_DIR, f"osc_tester_{time.strftime('%Y%m%d_%H%M%S')}.log")
        with _file_lock, open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"===== VRC-OSC-Tester 运行日志  {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            f.write(f"运行模式: {'exe' if _frozen else 'python 脚本'}  目录: {BASE_DIR}\n")
            f.write(f"系统: {sys.platform}  Python: {sys.version.split()[0]}\n\n")
        return LOG_FILE
    except Exception as e:
        # 目录不可写等异常时降级为仅窗口显示
        try:
            fallback = os.path.join(os.environ.get("TEMP", "."), "vrc_osc_tester.log")
            with open(fallback, "a", encoding="utf-8") as f:
                f.write(f"[fallback] logs 目录创建失败({e})，降级写入此文件\n")
            LOG_FILE = fallback
        except Exception:
            LOG_FILE = None
        return None


def _write_file(line):
    if not LOG_FILE:
        return
    try:
        with _file_lock, open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# 全局监听服务引用
_listener_server = None
_listener_thread = None


def log(msg, tag="info"):
    """线程安全的日志输出：GUI 窗口 + logs 目录下的文件"""
    line = f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}"
    _write_file(line)

    def _append():
        txt_log.configure(state="normal")
        txt_log.insert(tk.END, msg + "\n", tag)
        txt_log.see(tk.END)
        txt_log.configure(state="disabled")
    root.after(0, _append)


def set_status(key, text, ok=None):
    def _update():
        labels[key].configure(text=text)
        color = {True: "#1a7f37", False: "#c62828", None: "#555555"}[ok]
        labels[key].configure(foreground=color)
    root.after(0, _update)


# ---------------- 端口检测 ----------------

def find_vrchat_process():
    """返回 (pid, cmdline 列表)；未运行返回 (None, None)"""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == VRCHAT_PROCESS_NAME.lower():
                return proc.info["pid"], proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None, None


def parse_osc_launch_arg(cmdline):
    """从命令行解析 --osc=输入端口:输出IP:输出端口"""
    for arg in cmdline:
        m = re.match(r"--osc=(\d+):([^:]+):(\d+)$", str(arg).strip())
        if m:
            return int(m.group(1)), m.group(2), int(m.group(3))
    return None


def get_latest_log_path():
    """返回最新的 output_log_*.txt 路径"""
    logs = glob.glob(os.path.join(VRCHAT_DATA_DIR, "output_log_*.txt"))
    return max(logs, key=os.path.getmtime) if logs else None


def parse_log_for_osc(log_path):
    """流式解析日志，提取 (osc_enabled, 实际输入端口, OSCQuery端口)"""
    enabled = None
    in_port = None
    oscquery_port = None
    re_enabled = re.compile(r"OSC enabled: (True|False)")
    re_osc = re.compile(r"Advertising Service \S+ of type OSC on (\d+)")
    re_oscquery = re.compile(r"Advertising Service \S+ of type OSCQuery on (\d+)")
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = re_enabled.search(line)
                if m:
                    enabled = m.group(1) == "True"
                m = re_osc.search(line)
                if m:
                    in_port = int(m.group(1))
                m = re_oscquery.search(line)
                if m:
                    oscquery_port = int(m.group(1))
    except Exception as e:
        log(f"解析日志失败: {e}", "err")
    return enabled, in_port, oscquery_port


def read_osc_config():
    """读取 osc.json（部分版本才存在，仅作参考）"""
    if not os.path.exists(OSC_CONFIG_PATH):
        return None
    try:
        with open(OSC_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = re.sub(r"//[^\n]*", "", f.read())
        return json.loads(raw)
    except Exception:
        return None


def query_oscquery(port):
    """请求 OSCQuery HTTP 服务，返回运行时配置（尽力而为）"""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/", headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data.get("HOST_INFO", {})
    except Exception:
        return None


def get_firewall_status():
    """检测 Windows 防火墙状态。

    返回 (profiles, rules, error):
      profiles: [(名称, 是否启用), ...]  域/专用/公用三个配置文件
      rules:    [(规则名, 方向, 操作, 是否启用, 程序路径), ...]  与 VRChat 相关的规则
    """
    profiles, rules = [], []
    cmd = (
        "Get-NetFirewallProfile | ForEach-Object { '{0}|{1}' -f $_.Name, $_.Enabled }; "
        "Get-NetFirewallApplicationFilter -Program '*VRChat*' -ErrorAction SilentlyContinue | "
        "ForEach-Object { $r = $_ | Get-NetFirewallRule; "
        "'{0}|{1}|{2}|{3}|{4}' -f $r.DisplayName, $r.Direction, $r.Action, $r.Enabled, $_.Program }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=60, errors="ignore")
        for line in r.stdout.splitlines():
            parts = [p.strip() for p in line.strip().split("|")]
            if len(parts) == 2 and parts[1] in ("True", "False"):
                profiles.append((parts[0], parts[1] == "True"))
            elif len(parts) == 5:
                rules.append((parts[0], parts[1], parts[2], parts[3] == "True", parts[4]))
        log(f"[调试] 防火墙查询完成: {len(profiles)} 个配置文件, {len(rules)} 条 VRChat 相关规则")
    except Exception as e:
        log(f"[调试] 防火墙查询失败: {e}", "err")
        return profiles, rules, str(e)
    return profiles, rules, None


def describe_firewall():
    """汇总防火墙判定，返回 (状态文字, ok, 详情日志已直接输出)"""
    profiles, rules, err = get_firewall_status()
    if err:
        return "防火墙：检测失败（可能无 PowerShell 权限）", None

    # 配置文件状态
    if profiles:
        ptxt = " / ".join(f"{n}{'开' if e else '关'}" for n, e in profiles)
        log(f"      防火墙配置文件: {ptxt}")
        if not any(e for _, e in profiles):
            log("      所有防火墙配置文件均为关闭状态，防火墙不可能是问题来源", "ok")
            return "防火墙：全部关闭（不会拦截）", True

    # VRChat 专用规则
    blocked = [r for r in rules if r[2] == "Block" and r[3]]
    allowed = [r for r in rules if r[2] == "Allow" and r[3]]
    for name, d, a, e, prog in rules:
        log(f"[调试] VRChat 防火墙规则: {name} | {d} | {a} | {'启用' if e else '禁用'} | {prog}")

    if blocked:
        names = "; ".join(f"{n}({d})" for n, d, a, e, p in blocked[:3])
        more = f" 等 {len(blocked)} 条" if len(blocked) > 3 else ""
        return f"防火墙：VRChat 被阻止规则拦截: {names}{more}", False
    if allowed:
        uniq = sorted({n for n, d, a, e, p in allowed})
        names = "; ".join(uniq[:3]) + (f" 等 {len(uniq)} 条" if len(uniq) > 3 else "")
        return f"防火墙：VRChat 已放行 ({names})", True
    return "防火墙：VRChat 无专用规则（本机回环不受影响）", None


def find_port_owners(port, exclude_pid=None):
    """用 netstat -ano 扫描指定端口（TCP + UDP），返回占用者列表。

    OSC 走 UDP，VRChat 只绑定 UDP 端口，所以必须同时看两种协议。
    返回 [(proto, local_addr, state, pid, proc_name), ...]
    """
    owners = []
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True,
                           text=True, timeout=15, errors="ignore")
        out = r.stdout
        log(f"[调试] netstat 执行完成，共 {len(out.splitlines())} 行输出")
    except Exception as e:
        log(f"[调试] netstat 执行失败: {e}", "err")
        return owners

    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] not in ("TCP", "UDP"):
            continue
        proto, local = parts[0], parts[1]
        pid_s = parts[-1]
        if not pid_s.isdigit():
            continue
        # 本地端口取地址最后一段（兼容 IPv6 形如 [::]:9000）
        if local.rsplit(":", 1)[-1] != str(port):
            continue
        state = parts[3] if (proto == "TCP" and len(parts) >= 5) else ""
        pid = int(pid_s)
        if exclude_pid and pid == exclude_pid:
            log(f"[调试] netstat 命中(排除自身): {' '.join(parts)}")
            continue
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = f"PID {pid}（无法读取进程名）"
        owners.append((proto, local, state, pid, name))
        log(f"[调试] netstat 命中: {' '.join(parts)}  ->  {name}", "recv")
    return owners


def describe_port_owners(port, exclude_pid=None):
    """返回 (vrchat_listening, other_names, owners)"""
    owners = find_port_owners(port, exclude_pid=exclude_pid)
    vrchat_listening = False
    other_names = []
    for proto, local, state, pid, name in owners:
        if name.lower() == VRCHAT_PROCESS_NAME.lower():
            vrchat_listening = True
        else:
            tag = f"{proto} {local}"
            other_names.append(f"{name} (PID {pid}, {tag})")
    return vrchat_listening, other_names, owners


# ---------------- 功能动作 ----------------

def do_refresh():
    """一键环境检测：进程 -> 启动参数 -> 日志 -> osc.json -> OSCQuery -> 端口占用"""
    log("=" * 46)
    log(f"开始环境检测 ... [{time.strftime('%H:%M:%S')}]")
    result = {"in": None, "out": None, "in_src": "", "out_src": ""}

    # 1. VRChat 进程 + 启动参数
    t0 = time.time()
    pid, cmdline = find_vrchat_process()
    running = pid is not None
    set_status("proc",
               f"VRChat 进程：运行中 (PID {pid})" if running else "VRChat 进程：未运行",
               running)
    log(f"[1/7] VRChat 进程检测 -> {'运行中 (PID %s)' % pid if running else '未运行'}"
        f"（耗时 {time.time()-t0:.1f}s）", "ok" if running else "warn")
    if running:
        log(f"[调试] 进程命令行: {' '.join(cmdline) if cmdline else '(读取失败)'}")
    else:
        log("[调试] psutil 未找到 VRChat.exe；若游戏已启动，可能是以管理员身份运行，"
            "可尝试以管理员身份运行本工具", "warn")

    osc_arg = parse_osc_launch_arg(cmdline) if running else None
    if osc_arg:
        in_p, ip, out_p = osc_arg
        set_status("launch", f"启动参数 --osc：输入 {in_p} / 输出到 {ip}:{out_p}（用户自定义）", True)
        log(f"      检测到启动参数 --osc={in_p}:{ip}:{out_p}", "recv")
        result["in"], result["out"] = in_p, out_p
        result["in_src"] = result["out_src"] = "--osc 参数"
    else:
        set_status("launch", "启动参数 --osc：未使用（走默认/配置文件端口）", None)
        log("      未使用 --osc 启动参数")

    # 2. 日志解析（最权威的"实际生效"来源）
    log_path = get_latest_log_path()
    if log_path:
        try:
            st = os.stat(log_path)
            log(f"[调试] 日志文件: {log_path}")
            log(f"[调试] 修改时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}, "
                f"大小 {st.st_size/1024/1024:.1f} MB")
        except OSError:
            pass
        log(f"[2/7] 解析日志: {os.path.basename(log_path)}")
        enabled, log_in_port, oscquery_port = parse_log_for_osc(log_path)
        if enabled is not None:
            state = "已启用" if enabled else "未启用"
            log(f"      日志记录 OSC 启用状态: {state}", "ok" if enabled else "warn")
        else:
            log("      日志中未找到 OSC 启用状态记录", "warn")
        if log_in_port:
            log(f"      日志记录实际输入端口: {log_in_port}", "recv")
            if not osc_arg:
                result["in"], result["in_src"] = log_in_port, "日志(实际生效)"
        if oscquery_port:
            log(f"      日志记录 OSCQuery 端口: {oscquery_port}（TCP，可用于查询运行时配置）")
            host = query_oscquery(oscquery_port)
            log(f"[调试] OSCQuery HTTP 原始响应: {json.dumps(host, ensure_ascii=False)[:300] if host else '(无响应)'}")
            if host:
                q_in = host.get("OSC_PORT") or host.get("oscPort")
                log(f"      OSCQuery 在线确认: 服务名 {host.get('NAME', '?')}, "
                    f"输入端口 {q_in}, OSC IP {host.get('OSC_IP', '?')}", "ok")
                if q_in and not osc_arg:
                    result["in"], result["in_src"] = int(q_in), "OSCQuery(实时)"
            else:
                log("      OSCQuery HTTP 查询失败（VRChat 可能已退出）", "warn")
    else:
        set_status("cfg", "未找到 VRChat 日志（可能从未运行过）", False)
        log("[2/7] 未找到 VRChat 日志文件", "warn")

    # 3. osc.json（参考）
    cfg = read_osc_config()
    log("[3/7] osc.json: " + ("不存在（部分版本仅在修改过配置时生成，不影响检测）" if cfg is None
                          else f"内容 {cfg}"))

    # 4/5. 端口占用检测（netstat，TCP + UDP 全查）
    my_pid = os.getpid()
    in_port = result["in"] or int(ent_in.get() or DEFAULT_IN_PORT)
    out_port = result["out"] or int(ent_out.get() or DEFAULT_OUT_PORT)
    log(f"[4/7] 最终判定 -> 输入端口 {in_port}（来源: {result['in_src'] or '默认/输入框'}）")
    log(f"[调试] 开始扫描输入端口 {in_port} 的占用情况（TCP + UDP）...")
    vrchat_l, others, owners = describe_port_owners(in_port)
    if vrchat_l:
        set_status("in_port", f"输入端口 {in_port}：VRChat 正在监听 ✓", True)
        log(f"[4/7] 输入端口 {in_port} -> VRChat 正在监听（UDP），输入方向正常", "ok")
    elif others:
        set_status("in_port", f"输入端口 {in_port}：被 {'; '.join(others)} 占用", False)
        log(f"[4/7] 输入端口 {in_port} -> 被 {'; '.join(others)} 占用，VRChat 无法绑定，"
            f"请结束占用程序或改端口", "err")
    else:
        set_status("in_port", f"输入端口 {in_port}：无任何程序监听", None)
        log(f"[4/7] 输入端口 {in_port} -> 无监听。若 VRChat 在运行，通常是 OSC 未在游戏内开启"
            f"（Action Menu → OSC → Enabled）", "warn")

    log(f"[调试] 开始扫描输出端口 {out_port} 的占用情况（TCP + UDP）...")
    _, out_others, out_owners = describe_port_owners(out_port, exclude_pid=my_pid)
    log(f"[5/7] 最终判定 -> 输出端口 {out_port}（来源: {result['out_src'] or '默认（输出端口由 VRChat 主动发送，不监听）'}）")
    if out_others:
        set_status("out_port", f"输出端口 {out_port}：被 {'; '.join(out_others)} 占用", False)
        log(f"[5/7] 输出端口 {out_port} -> 被 {'; '.join(out_others)} 占用。"
            f"监听前需先关闭占用程序，否则收不到 VRChat 数据", "err")
    else:
        set_status("out_port", f"输出端口 {out_port}：空闲，可监听", True)
        log(f"[5/7] 输出端口 {out_port} -> 空闲（本工具自身监听除外），可开始监听", "ok")

    # 6. 防火墙检测
    log("[6/7] 检测 Windows 防火墙状态 ...")
    fw_text, fw_ok = describe_firewall()
    set_status("firewall", fw_text, fw_ok)
    log("      说明: 127.0.0.1 本机回环流量不受防火墙限制，本机 OSC 测试一般与防火墙无关；"
        "跨设备发 OSC（如 --osc 指定局域网 IP）时防火墙才会成为影响因素", "recv")

    # 自动填充端口输入框，供发送/监听直接使用
    ent_in.delete(0, tk.END); ent_in.insert(0, str(in_port))
    ent_out.delete(0, tk.END); ent_out.insert(0, str(out_port))
    log(f"[7/7] 端口已自动填入下方输入框，可直接发送测试 / 开始监听。"
        f"（总耗时 {time.time()-t0:.1f}s）", "ok")
    return in_port, out_port


def start_listener():
    """监听 VRChat 的 OSC 输出端口"""
    def _run():
        out_port = int(ent_out.get() or DEFAULT_OUT_PORT)
        dispatcher = Dispatcher()
        dispatcher.set_default_handler(lambda addr, *args: log(
            f"收到 OSC  <-  {addr}  {list(args)}", "recv"))
        try:
            server = BlockingOSCUDPServer(("127.0.0.1", out_port), dispatcher)
        except OSError as e:
            log(f"监听失败（端口 {out_port} 可能被占用）: {e}", "err")
            set_status("listen", "监听状态：失败", False)
            return
        global _listener_server
        _listener_server = server
        log(f"开始监听 127.0.0.1:{out_port}，等待 VRChat 上报 OSC 数据 ...", "ok")
        set_status("listen", f"监听状态：监听中 (:{out_port})", True)
        try:
            server.serve_forever()
        except Exception:
            pass
        set_status("listen", "监听状态：已停止", None)
        log("监听已停止。")

    if _listener_thread and _listener_thread.is_alive():
        log("监听已在运行中。", "warn")
        return
    t = threading.Thread(target=_run, daemon=True)
    globals()["_listener_thread"] = t
    t.start()


def stop_listener():
    server = globals().get("_listener_server")
    if server:
        server.shutdown()


def send_chatbox_test():
    """向 VRChat 聊天框发送测试消息（地址 /chatbox/input）"""
    in_port = int(ent_in.get() or DEFAULT_IN_PORT)
    msg = ent_msg.get().strip() or "OSC 测试消息 ✅"
    try:
        client = SimpleUDPClient("127.0.0.1", in_port)
        client.send_message("/chatbox/input", [msg, True, True])
        log(f"已发送聊天框消息 -> 127.0.0.1:{in_port} /chatbox/input \"{msg}\"", "send")
        log("请在 VRChat 中查看聊天框是否显示该消息（需游戏内开启 OSC）。")
    except Exception as e:
        log(f"发送失败: {e}", "err")


# ---------------- GUI ----------------

_init_log_file()

root = tk.Tk()
root.title("VRC-OSC-Tester  |  VRChat OSC 检测工具")
root.geometry("720x600")
root.configure(bg="#f5f5f5")

frm_top = tk.Frame(root, bg="#ffffff", padx=12, pady=10)
frm_top.pack(fill=tk.X)

labels = {}
for i, key in enumerate(["proc", "launch", "in_port", "out_port", "firewall", "listen"]):
    labels[key] = tk.Label(frm_top, text="待检测", bg="#ffffff",
                           fg="#555555", font=("Microsoft YaHei", 10), anchor="w")
    labels[key].grid(row=i, column=0, sticky="w", pady=2)

frm_btn = tk.Frame(root, bg="#f5f5f5", padx=12, pady=8)
frm_btn.pack(fill=tk.X)

ttk.Button(frm_btn, text="🔄 一键环境检测",
           command=lambda: threading.Thread(target=do_refresh, daemon=True).start()).pack(side=tk.LEFT, padx=4)
ttk.Button(frm_btn, text="▶ 开始监听输出", command=start_listener).pack(side=tk.LEFT, padx=4)
ttk.Button(frm_btn, text="⏹ 停止监听", command=stop_listener).pack(side=tk.LEFT, padx=4)
ttk.Button(frm_btn, text="💬 发送聊天框测试", command=send_chatbox_test).pack(side=tk.LEFT, padx=4)

frm_in = tk.Frame(root, bg="#f5f5f5", padx=12)
frm_in.pack(fill=tk.X)

tk.Label(frm_in, text="输入端口:", bg="#f5f5f5", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
ent_in = tk.Entry(frm_in, width=7); ent_in.insert(0, str(DEFAULT_IN_PORT)); ent_in.pack(side=tk.LEFT, padx=(2, 10))
tk.Label(frm_in, text="输出端口:", bg="#f5f5f5", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
ent_out = tk.Entry(frm_in, width=7); ent_out.insert(0, str(DEFAULT_OUT_PORT)); ent_out.pack(side=tk.LEFT, padx=(2, 10))

frm_msg = tk.Frame(root, bg="#f5f5f5", padx=12, pady=4)
frm_msg.pack(fill=tk.X)
tk.Label(frm_msg, text="测试消息:", bg="#f5f5f5", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
ent_msg = tk.Entry(frm_msg); ent_msg.insert(0, "OSC 测试消息 ✅"); ent_msg.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

txt_log = scrolledtext.ScrolledText(root, font=("Consolas", 9), bg="#1e1e1e",
                                    fg="#d4d4d4", state="disabled")
txt_log.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
for tag, color in [("ok", "#4ec9b0"), ("warn", "#dcdcaa"), ("err", "#f48771"),
                   ("recv", "#c586c0"), ("send", "#569cd6"), ("info", "#d4d4d4")]:
    txt_log.tag_configure(tag, foreground=color)


def on_close():
    stop_listener()
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)
log("VRC-OSC-Tester 已启动。点击「一键环境检测」开始。", "ok")
log("检测优先级: --osc 启动参数 > 日志实际端口 > osc.json > 默认 9000/9001")
log(f"本次运行日志文件: {LOG_FILE or '（创建失败，仅窗口显示）'}", "recv")
log(f"日志目录: {LOG_DIR}")

if __name__ == "__main__":
    root.mainloop()

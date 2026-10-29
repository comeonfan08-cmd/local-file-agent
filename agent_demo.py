#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 工作原理演示（DeepSeek + 终端/CMD 工具）

一句话原理：
    Agent = 大模型 + 工具 + 一个 while 循环。

    模型本身不会去敲命令。它只会「说出来」自己想调用哪个工具、参数是什么；
    真正动手执行的是本地的 run_bash() 函数。
    执行完把结果塞回 messages 再问模型一次，模型就"看到"了世界发生的变化，
    然后决定下一步——如此往复，直到它认为任务完成（不再调用工具）。

运行方式：
    python3 agent_demo.py

依赖：只用 Python 标准库，不需要 pip install 任何东西。
"""

import getpass
import json
import os
import subprocess
import sys
from datetime import datetime

from urllib import error as urlerror
from urllib import request as urlrequest

# ======================= 1. 配置区（只改这里） =======================

# 你的 DeepSeek API Key。
# 留空即可：运行时会先读环境变量 DEEPSEEK_API_KEY，没有就提示你手动输入（输入内容不回显）。
API_KEY = ""

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"        # 想换模型改这里，例如 deepseek-chat / deepseek-reasoner

BASE_DIR = os.path.dirname(os.path.realpath(__file__))  # 脚本真实所在目录（即使通过软链接启动）
WORKDIR = BASE_DIR                                      # Agent 的默认工作目录（也是产物落盘位置）
LOG_PATH = os.path.join(BASE_DIR, "log.txt")            # 请求 / 响应日志

MAX_STEPS = 20                  # 最大循环轮数，防止模型无限调工具
REQUEST_TIMEOUT = 120           # 单次 HTTP 请求超时（秒）
TOOL_TIMEOUT = 120              # 单条终端命令超时（秒）
MAX_OUTPUT_CHARS = 4000         # 工具输出截断长度，避免把上下文撑爆

SYSTEM_PROMPT = (
    "你别着急一次把活干完，做好计划，一步一步来。"
    "我为你提供了调用本机终端/CMD 的工具，按任务需求使用工具来完成任务，我会帮你调用这些工具。"
    "我还会把你每一轮的决策、调用的工具、工具返回的结果都再次告诉你，不用担心忘记之前的信息。"
)

# ======================= 2. 工具定义（告诉模型"有什么工具可用"） =======================
# 注意：这里只是"菜单"，是给模型看的说明书。
# 模型看到它，才可能返回 tool_calls；真正执行在第 4 部分的 run_bash()。

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_terminal",
            "description": (
                "在本机执行一条终端命令（macOS/Linux 用 bash，Windows 用 cmd）。"
                "可以用来：在指定路径创建文件、写入内容、读取文件、列目录、运行 Python 脚本等。"
                "每次只执行一条命令；多条命令可用 && 或 ; 连接。"
                "返回执行后的退出码、标准输出和错误输出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的完整命令，例如：mkdir -p demo && echo hello > demo/a.txt",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "命令在哪个目录下执行。可以用绝对路径；"
                            "也可以用相对路径（相对于脚本所在目录）。不传则用脚本所在目录。"
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"命令超时时间（秒），默认 {TOOL_TIMEOUT}。",
                    },
                },
                "required": ["command"],
            },
        },
    }
]


# ======================= 3. 小工具函数 =======================

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hr(char="─", width=68):
    print(char * width)


def truncate(text, limit=MAX_OUTPUT_CHARS):
    """输出太长就截断，避免上下文爆炸（Agent 实践里的常见处理）。"""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n...[中间省略 {len(text) - limit} 字符]...\n{tail}"


def write_log(step, request_payload, response_obj=None, tool_record=None, note=""):
    """把本轮请求 / 响应 / 工具结果追加写入 log.txt。"""
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 78 + "\n")
        f.write(f"[{now_str()}] 第 {step} 轮{f'  {note}' if note else ''}\n")
        f.write("=" * 78 + "\n")

        f.write("\n---------- REQUEST ----------\n")
        f.write(json.dumps(request_payload, ensure_ascii=False, indent=2) + "\n")

        f.write("\n---------- RESPONSE ----------\n")
        if response_obj is None:
            f.write("(无响应：请求失败)\n")
        else:
            f.write(json.dumps(response_obj, ensure_ascii=False, indent=2) + "\n")

        if tool_record is not None:
            f.write("\n---------- TOOL RESULT（本地执行） ----------\n")
            f.write(json.dumps(tool_record, ensure_ascii=False, indent=2) + "\n")


# ======================= 4. 工具的本地实现 =======================

def run_bash(command, cwd=None, timeout=None):
    """
    真正执行命令的地方。
    Agent 的"手"就是这个函数——模型只能提出请求，代码决定要不要真的做。
    """
    workdir = cwd or WORKDIR
    if not os.path.isabs(workdir):
        workdir = os.path.join(WORKDIR, workdir)
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)          # 目标目录不存在就建出来

    timeout = int(timeout or TOOL_TIMEOUT)

    try:
        proc = subprocess.run(
            command,
            shell=True,                          # macOS/Linux -> /bin/sh；Windows -> cmd.exe
            cwd=workdir,
            capture_output=True,                 # 抓走 stdout / stderr，才能回传给模型
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        code = -1
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = f"命令执行超时（超过 {timeout} 秒），已被强制终止。"
    except Exception as e:                        # 兜底：别让一个异常把整个 Agent 打断
        code = -1
        out = ""
        err = f"执行命令时出现异常：{type(e).__name__}: {e}"

    return {
        "exit_code": code,
        "stdout": truncate(out),
        "stderr": truncate(err),
        "cwd": workdir,
    }


# ======================= 5. 调用大模型 =======================

def call_llm(messages, step, api_key):
    """发一次请求，返回解析后的 JSON；同时把请求与响应写进 log.txt。"""
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
        # 关闭深度思考，和官方示例保持一致；换 deepseek-reasoner 时可改成 {"type": "enabled"}
        "thinking": {"type": "disabled"},
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )

    try:
        with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        write_log(step, payload, None, note=f"HTTP {e.code}")
        raise RuntimeError(f"HTTP {e.code}：{detail}") from None
    except Exception as e:
        write_log(step, payload, None, note="网络异常")
        raise RuntimeError(f"请求失败：{type(e).__name__}: {e}") from None

    result = json.loads(raw)
    write_log(step, payload, result)
    return result


# ======================= 6. 把模型决策"演"给用户看 =======================

def show_decision(step, message, finish_reason):
    print()
    hr("═")
    print(f"第 {step} 轮  │  模型返回 finish_reason = {finish_reason}")
    hr()

    reasoning = (message.get("reasoning_content") or "").strip()
    if reasoning:
        print("【模型思考】")
        print(reasoning)
        print()

    content = (message.get("content") or "").strip()
    if content:
        print("【模型说】")
        print(content)
        print()

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        print("【决策】决定调用工具：" + str(fn.get("name")))
        print("        传入参数：" + str(fn.get("arguments")))


def show_tool_result_summary(result):
    """只告诉用户"执行了、成功与否"，不展示工具的具体输出。"""
    ok = "成功" if result["exit_code"] == 0 else f"失败（exit_code={result['exit_code']}）"
    print(f"【本地执行】{ok}，工作目录：{result['cwd']}")
    print("           （完整输出已回传给模型，这里不显示）")


# ======================= 7. Agent 主循环 =======================

def run_agent(question, api_key):
    """一次提问 = 一次完整的 Agent 循环。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    print()
    hr("═")
    print("收到问题：" + question.strip().replace("\n", " "))
    hr("═")

    for step in range(1, MAX_STEPS + 1):
        # (1) 发起请求：messages 里带着"历史和上一轮工具的输出"
        response = call_llm(messages, step, api_key)

        try:
            choice = response["choices"][0]
        except (KeyError, IndexError):
            print("接口返回格式异常，原始内容如下：")
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return

        finish_reason = choice.get("finish_reason")
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        # (2) 把模型的决策展示给用户
        show_decision(step, message, finish_reason)

        # (3) 把 assistant 这一轮说的话记进历史（要保留 tool_calls，否则下一轮对不上号）
        assistant_msg = {"role": "assistant", "content": message.get("content") or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        # (4) 循环终止条件：finish_reason 不是 tool_calls
        if finish_reason != "tool_calls" or not tool_calls:
            print()
            hr("═")
            print("任务结束 · 模型最终回答：")
            hr("═")
            print((message.get("content") or "(空)").strip())
            hr("═")
            return

        # (5) 执行模型要求的每一个工具调用
        for tc in tool_calls:
            tc_id = tc.get("id") or "call_unknown"
            fn = tc.get("function", {}) or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"

            print(f"【执行】正在运行 {name} ...")

            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                if not isinstance(args, dict):
                    raise ValueError("参数不是 JSON 对象")
                command = str(args.get("command") or "").strip()
                if not command:
                    raise ValueError("缺少 command 参数")
                result = run_bash(
                    command=command,
                    cwd=args.get("cwd"),
                    timeout=args.get("timeout"),
                )
                tool_payload = result
            except Exception as e:
                # 工具失败也要回传给模型，让它自己调整，而不是让程序崩掉
                tool_payload = {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"工具调用失败：{type(e).__name__}: {e}",
                    "cwd": WORKDIR,
                }

            show_tool_result_summary(tool_payload)

            # 工具结果顺手记一笔到日志，方便复盘
            write_log(step, {"tool": name, "arguments": raw_args},
                      {"tool_call_id": tc_id}, tool_record=tool_payload,
                      note=f"工具执行：{name}")

            # (6) 关键一步：以 role=tool 把结果塞回历史，下一轮模型就能"看到"结果
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": json.dumps(tool_payload, ensure_ascii=False),
            })

        # 回到 while 顶部，带着工具结果再问一次模型

    print()
    hr("═")
    print(f"已达到最大轮数 {MAX_STEPS}，循环停止（防止无限调用）。")
    hr("═")


# ======================= 8. 入口 =======================

def mask_key(key):
    """打码显示，方便确认到底读到了哪把钥匙，又不会把完整密钥打在屏幕上。"""
    if not key:
        return "(空)"
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def resolve_api_key():
    key = (API_KEY or "").strip()
    if key:
        print(f"已从脚本 API_KEY 读取到密钥：{mask_key(key)}")
        return key
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        print(f"已从环境变量 DEEPSEEK_API_KEY 读取到密钥：{mask_key(key)}")
        return key
    print("未配置 API Key。")
    key = getpass.getpass("请粘贴你的 DeepSeek API Key（输入时不显示）：").strip()
    return key


def banner():
    print()
    hr("═")
    print("  Agent 工作原理演示 · DeepSeek + 终端工具")
    hr("═")
    print(f"  模型      : {MODEL}")
    print(f"  工作目录  : {WORKDIR}")
    print(f"  日志文件  : {LOG_PATH}")
    print(f"  最大轮数  : {MAX_STEPS}")
    print()
    print("  输入你的任务，回车开始；输入 exit 或直接回车退出。")
    print("  例如：在当前目录建一个 hello.py，打印 1 到 5，然后运行它")
    hr("═")


def main():
    banner()
    api_key = resolve_api_key()
    if not api_key:
        print("没有 API Key，无法继续。请填写脚本顶部的 API_KEY 后重新运行。")
        return 1

    try:
        while True:
            print()
            try:
                question = input("你想让 Agent 做什么 > ").strip()
            except EOFError:
                break
            if not question or question.lower() in ("exit", "quit", "q", "退出"):
                print("再见。")
                break

            try:
                run_agent(question, api_key)
            except RuntimeError as e:
                print()
                print(f"请求出错：{e}")
                print("请检查：API Key 是否正确、网络是否可达、模型名是否存在。")
            except KeyboardInterrupt:
                print("\n已中断本轮任务。")

            print()
            print(f"（本轮完整请求/响应已写入 {os.path.basename(LOG_PATH)}）")
    except KeyboardInterrupt:
        print("\n已退出。")

    return 0


if __name__ == "__main__":
    sys.exit(main())

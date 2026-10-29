#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 网页版（Flask + SSE）—— 与 agent_demo.py 对照学习用

设计原则：agent_demo.py 一个字都不改。
    「脑子」call_llm 和「手」run_bash 直接 import 复用；
    「循环」在这里重写一份，唯一区别是 print() 换成了 yield 事件。

对照关系：
    agent_demo.run_agent()   → 本文件 stream_agent()   （终端版 / 网页版）
    agent_demo.show_decision()      → {"type": "reasoning"} / {"type": "content"} / {"type": "tool_call"}
    agent_demo.show_tool_result_summary() → {"type": "tool_result"}

启动：
    python3 web.py
    然后浏览器打开 http://127.0.0.1:8000

安全须知（重要）：
    run_bash 是 shell=True 的任意命令执行。本服务只绑定 127.0.0.1，
    仅供本机自己使用。绝对不要改成 0.0.0.0 或做端口映射到公网。
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime
from urllib import error as urlerror
from urllib import request as urlrequest

from flask import Flask, Response, render_template, request

import agent_demo as core

app = Flask(__name__)

# 正在运行的任务：run_id -> threading.Event（用于网页上的「停止」按钮）
STOPS = {}


# ======================= 1. 设置：API Key 与模型名 =======================
# 优先级：界面上保存的 > 环境变量 > agent_demo.py 里的常量
# 为什么会保存到文件？因为「保存」按钮如果只在内存里生效，一重启就白填了。
# 文件权限设成 600（只有本人可读写），文件名以点开头，不容易被误提交。

SETTINGS_PATH = os.path.join(core.BASE_DIR, ".agent_settings.json")
DEFAULT_MODEL = core.MODEL

# 模型清单不硬编码：官方 /models 接口才是唯一可信来源。
# 缓存 5 分钟，避免每次打开面板都打一次网络。
MODELS_URL = core.API_URL.replace("/chat/completions", "/models")
MODEL_CACHE_SECONDS = 300

# 拉不到时的兜底（2026-10 实测官方在线清单）
FALLBACK_MODELS = [
    {"id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash", "context_window": 1048576, "max_output_tokens": 393216},
    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro", "context_window": 1048576, "max_output_tokens": 393216},
]

# 这些名字官方已退役，请求会被路由到 Flash，标出来免得误选
RETIRED_NAMES = {"deepseek-chat", "deepseek-reasoner",
                 "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}

_settings_lock = threading.Lock()
_model_lock = threading.Lock()
_model_cache = {"at": 0.0, "data": [], "source": "", "error": ""}


def _fmt_tokens(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:g}M"
    if n >= 1024:
        return f"{n / 1024:g}K"
    return str(n)


def list_models(force=False):
    """返回 {"models": [...], "source": "live|fallback", "error": "..."}"""
    with _model_lock:
        if _model_cache["data"] and not force and (time.time() - _model_cache["at"]) < MODEL_CACHE_SECONDS:
            return {"models": _model_cache["data"], "source": _model_cache["source"],
                    "error": _model_cache["error"], "cached": True}

    key, _ = current_key()
    data, source, error = [], "fallback", ""

    if key:
        try:
            req = urlrequest.Request(MODELS_URL, headers={"Authorization": f"Bearer {key}"})
            with urlrequest.urlopen(req, timeout=15) as resp:
                obj = json.loads(resp.read().decode("utf-8", errors="replace"))
            for m in (obj.get("data") or []):
                mid = str(m.get("id") or "").strip()
                if not mid:
                    continue
                data.append({
                    "id": mid,
                    "name": str(m.get("name") or mid),
                    "ctx": _fmt_tokens(m.get("context_window")),
                    "max_out": _fmt_tokens(m.get("max_output_tokens")),
                    "retired": mid in RETIRED_NAMES,
                })
            if data:
                source = "live"
        except urlerror.HTTPError as e:
            error = f"HTTP {e.code}（多半是 Key 无效）"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
    else:
        error = "没有可用的 Key，无法拉取在线清单"

    if not data:
        data = [{"id": m["id"], "name": m["name"],
                 "ctx": _fmt_tokens(m["context_window"]),
                 "max_out": _fmt_tokens(m["max_output_tokens"]),
                 "retired": m["id"] in RETIRED_NAMES} for m in FALLBACK_MODELS]

    with _model_lock:
        _model_cache.update({"at": time.time(), "data": data, "source": source, "error": error})
    return {"models": data, "source": source, "error": error, "cached": False}


def _read_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        return {"api_key": "", "model": ""}
    return {
        "api_key": str(d.get("api_key") or "").strip(),
        "model": str(d.get("model") or "").strip(),
    }


def _write_settings(api_key, model):
    payload = {
        "api_key": api_key,
        "model": model,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)          # 原子替换，避免写一半断电留下坏文件
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass


SETTINGS = _read_settings()


def current_key():
    """返回 (key, 来源说明)，来源会显示在界面上，方便确认用的是哪把钥匙。"""
    with _settings_lock:
        key = (SETTINGS.get("api_key") or "").strip()
    if key:
        return key, "界面保存"
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key, "环境变量 DEEPSEEK_API_KEY"
    key = (core.API_KEY or "").strip()
    if key:
        return key, "agent_demo.py 的 API_KEY（不推荐）"
    return "", "未配置"


def current_model():
    with _settings_lock:
        m = (SETTINGS.get("model") or "").strip()
    return m or DEFAULT_MODEL


def mask(key):
    if not key:
        return "(空)"
    return f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "*" * len(key)


def looks_masked(key):
    """
    防止把界面上的脱敏串（形如 sk-abc...xyz）当成真 Key 存进去。

    这是「输入框预填脱敏值」这个交互的唯一风险点：
    用户没改直接点保存，就会把 10 个可见字符当成整把钥匙。
    前端已经用 dataset.state 拦了一道，这里是服务端的第二道。
    """
    return ("..." in key) or ("*" in key) or ("…" in key)


# ======================= 2. 核心：把 run_agent 改成事件流 =======================
# 函数体结构和 agent_demo.run_agent() 的 6 个步骤一一对应，
# 只是每一处 print 变成了一次 yield —— 这就是「终端 → 网页」的全部秘密。

def stream_agent(question, api_key, stop_event):
    messages = [
        {"role": "system", "content": core.SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    for step in range(1, core.MAX_STEPS + 1):
        # 每轮开头检查一次「停止」——只能在轮次之间生效，不能打断正在跑的命令
        if stop_event.is_set():
            yield {"type": "error", "text": "已在新一轮开始前停止"}
            return

        yield {"type": "step", "step": step}

        # (1) 请求模型
        try:
            response = core.call_llm(messages, step, api_key)
        except RuntimeError as e:
            yield {"type": "error", "text": str(e)}
            return

        try:
            choice = response["choices"][0]
        except (KeyError, IndexError):
            yield {"type": "error", "text": "接口返回格式异常，详见 log.txt"}
            return

        finish_reason = choice.get("finish_reason")
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        # (2) 展示决策（原来是 show_decision 打印，现在拆成三个事件）
        yield {"type": "decision", "finish_reason": finish_reason}

        reasoning = (message.get("reasoning_content") or "").strip()
        if reasoning:
            yield {"type": "reasoning", "text": reasoning}

        content = (message.get("content") or "").strip()
        if content:
            yield {"type": "content", "text": content}

        # (3) assistant 消息进历史，tool_calls 必须原样保留
        assistant_msg = {"role": "assistant", "content": message.get("content") or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        # (4) 终止条件
        if finish_reason != "tool_calls" or not tool_calls:
            if finish_reason == "length":
                yield {
                    "type": "warning",
                    "text": "finish_reason=length：回答因超长被截断，不是模型主动结束的",
                }
            yield {"type": "final", "text": (message.get("content") or "(空)").strip()}
            return

        # (5) 执行工具
        for tc in tool_calls:
            if stop_event.is_set():
                yield {"type": "error", "text": "已在工具执行前停止"}
                return

            tc_id = tc.get("id") or "call_unknown"
            fn = tc.get("function", {}) or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"

            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                if not isinstance(args, dict):
                    raise ValueError("参数不是 JSON 对象")
                command = str(args.get("command") or "").strip()
                if not command:
                    raise ValueError("缺少 command 参数")
                yield {"type": "tool_call", "name": name, "command": command,
                       "cwd": args.get("cwd") or ""}
                result = core.run_bash(
                    command=command,
                    cwd=args.get("cwd"),
                    timeout=args.get("timeout"),
                )
                tool_payload = result
            except Exception as e:
                tool_payload = {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"工具调用失败：{type(e).__name__}: {e}",
                    "cwd": core.WORKDIR,
                }

            # 网页版比终端版多给了一点：失败时把 stderr 也推给前端，
            # 免得像 show_tool_result_summary 那样只报「失败」还得去翻日志。
            yield {
                "type": "tool_result",
                "exit_code": tool_payload["exit_code"],
                "cwd": tool_payload["cwd"],
                "stderr_head": (tool_payload.get("stderr") or "").strip()[:300],
            }

            core.write_log(step, {"tool": name, "arguments": raw_args},
                           {"tool_call_id": tc_id}, tool_record=tool_payload,
                           note=f"工具执行：{name}")

            # (6) 结果回填，下一轮模型就能「看到」
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": json.dumps(tool_payload, ensure_ascii=False),
            })

    yield {"type": "error", "text": f"已达到最大轮数 {core.MAX_STEPS}，循环停止"}


# ======================= 3. HTTP 层 =======================

def sse(obj):
    """把事件打包成 SSE 格式：一条 data: 加上空行分隔。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@app.route("/")
def index():
    key, source = current_key()
    return render_template(
        "index.html",
        model=current_model(),
        workdir=core.WORKDIR,
        max_steps=core.MAX_STEPS,
        key_masked=mask(key),
        key_source=source,
        has_key=bool(key),
    )


# ======================= 4. 设置接口 =======================

def _settings_payload(extra=None):
    key, source = current_key()
    with _settings_lock:
        stored = (SETTINGS.get("api_key") or "").strip()
    models_info = list_models()
    out = {
        "model": current_model(),
        "models": models_info["models"],
        "models_source": models_info["source"],
        "models_error": models_info["error"],
        "key_masked": mask(key),
        "key_source": source,
        "has_key": bool(key),
        "has_saved_key": bool(stored),
        "settings_path": SETTINGS_PATH,
    }
    if extra:
        out.update(extra)
    return out


@app.route("/settings", methods=["GET", "POST"])
def settings_api():
    if request.method == "GET":
        return _settings_payload()

    global SETTINGS
    data = request.get_json(silent=True) or {}
    typed_key = str(data.get("api_key") or "").strip()
    clear_key = bool(data.get("clear_key"))
    new_model = str(data.get("model") or "").strip() or DEFAULT_MODEL

    if typed_key and looks_masked(typed_key):
        return {"ok": False, "msg": "这看起来是脱敏后的 Key（含 ... 或 *），请输入完整 Key；不想改就留空"}

    with _settings_lock:
        stored_key = (SETTINGS.get("api_key") or "").strip()

    if clear_key:
        final_key = ""                       # 明确要清除
    elif typed_key:
        final_key = typed_key
    else:
        final_key = stored_key               # 输入框留空 = 保持原样

    with _settings_lock:
        SETTINGS = {"api_key": final_key, "model": new_model}

    try:
        _write_settings(final_key, new_model)
    except OSError as e:
        return {"ok": False, "msg": f"写入设置文件失败：{e}"}

    extra = {"ok": True}                # 前端靠 d.ok 判断成败，必须显式带回来
    if clear_key:
        # 清掉文件里的 key 之后，环境变量/脚本常量可能还在兜底，必须说清楚
        key, source = current_key()
        extra["msg"] = ("已清除保存的 Key" if not key
                        else f"已清除保存的 Key，但 {source} 里还有一把，仍然生效")

    return _settings_payload(extra)


@app.route("/models")
def models_api():
    """force=1 可强制绕过 5 分钟缓存重新拉取。"""
    return list_models(force=request.args.get("refresh") == "1")


@app.route("/test", methods=["POST"])
def test_connection():
    """发一个极小的请求，验证 Key 和模型名能不能用。不写 log.txt，不进 messages。"""
    data = request.get_json(silent=True) or {}
    key = str(data.get("api_key") or "").strip()
    if key and looks_masked(key):
        return {"ok": False, "msg": "这看起来是脱敏后的 Key（含 ... 或 *），请输入完整 Key"}
    if not key:
        key, _ = current_key()
    model = str(data.get("model") or "").strip() or current_model()

    if not key:
        return {"ok": False, "msg": "没有可用的 API Key"}

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        core.API_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )

    t0 = time.time()
    try:
        with urlrequest.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "msg": f"HTTP {e.code}：{detail[:300]}"}
    except Exception as e:
        return {"ok": False, "msg": f"请求失败：{type(e).__name__}: {e}"}

    latency = int((time.time() - t0) * 1000)
    try:
        obj = json.loads(raw)
        reply = (obj["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, ValueError):
        return {"ok": False, "msg": "返回格式异常，可能 Key 无效或模型名不存在"}

    # 注意：响应里的 model 字段由网关归一化，实测总返回 deepseek-flash，
    # 所以这里显示的用「你请求的名字」，而不是响应里那个，否则会误导。
    return {"ok": True, "msg": f"连接成功 · {model} · {latency}ms",
            "latency": latency, "reply": reply[:40]}


@app.route("/run")
def run():
    question = (request.args.get("q") or "").strip()
    if not question:
        return Response(sse({"type": "error", "text": "问题为空"}), mimetype="text/event-stream")

    api_key, _ = current_key()
    model = current_model()
    if not api_key:
        return Response(
            sse({"type": "error", "text": "未配置 API Key，点右上角「模型设置」填一个"}),
            mimetype="text/event-stream",
        )

    # agent_demo.call_llm 读的是模块级全局 MODEL，所以这里直接改它。
    # 好处是不用动原脚本；代价是多人同时用不同模型会互相覆盖——本机自用无所谓。
    core.MODEL = model

    run_id = uuid.uuid4().hex[:8]
    stop_event = threading.Event()
    STOPS[run_id] = stop_event

    def generate():
        try:
            yield sse({"type": "start", "run_id": run_id,
                       "time": datetime.now().strftime("%H:%M:%S")})
            for event in stream_agent(question, api_key, stop_event):
                yield sse(event)
            yield sse({"type": "done"})
        except Exception as e:  # 兜底：任何意外都要让前端知道，而不是让连接悄悄断掉
            yield sse({"type": "error", "text": f"服务端异常：{type(e).__name__}: {e}"})
            yield sse({"type": "done"})
        finally:
            STOPS.pop(run_id, None)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.route("/stop")
def stop():
    run_id = (request.args.get("run_id") or "").strip()
    ev = STOPS.get(run_id)
    if ev is None:
        return {"ok": False, "msg": "任务不存在或已结束"}
    ev.set()
    return {"ok": True, "msg": "已发出停止信号，将在本轮工具执行前的检查点生效"}


if __name__ == "__main__":
    key, source = current_key()
    print()
    print("=" * 68)
    print("  Agent 网页版 · Flask + SSE")
    print("=" * 68)
    print(f"  模型      : {current_model()}")
    print(f"  工作目录  : {core.WORKDIR}")
    print(f"  API Key   : {mask(key)}  ← 来自 {source}")
    print(f"  最大轮数  : {core.MAX_STEPS}")
    print()
    print("  打开浏览器访问： http://127.0.0.1:8000")
    print("  只监听 127.0.0.1，本机以外访问不到（这是安全底线）")
    print("=" * 68)
    print()
    app.run(host="127.0.0.1", port=8000, threaded=True, debug=False)

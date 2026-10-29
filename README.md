# 本地文件 Agent

一个跑在你自己电脑上、能真的动手改文件的小型 Agent。

不联网执行、不上传文件、不需要账号——它就是你本机的一个程序，处理的是你本机的文件。

## 它是什么

一句话：**Agent = 大模型 + 工具 + 一个 while 循环。**

模型本身不会敲命令，它只会「说」自己想调用哪个工具、参数是什么；真正动手执行的是本机的一个函数。
执行完把结果塞回对话历史再问模型一次，模型就「看到」了世界发生的变化，然后决定下一步——
如此往复，直到它认为任务完成（不再调用工具）。

整套逻辑不到 500 行，两个入口共用同一套「脑子」和「手」。

## 两个入口

| 入口 | 启动方式 | 适合 |
|---|---|---|
| **网页版**（推荐） | `python3 web.py` → 浏览器打开 http://127.0.0.1:8000 | 日常使用，能看到每一轮的决策和执行过程 |
| **终端版** | `python3 agent_demo.py` | 想看清原理，代码里有完整注释 |

网页版和终端版共用 `call_llm()`（调用模型）和 `run_bash()`（执行命令），只是输出方式不同：
终端版 `print` 到屏幕，网页版通过 SSE 推给浏览器。

## 快速开始

**1. 准备 Python**（3.10 或更高）

```bash
python3 --version
```

**2. 安装依赖**

```bash
pip3 install -r requirements.txt
```

如果下载很慢，用国内镜像：

```bash
pip3 install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**3. 启动**

- macOS：双击 `start-mac.command`（首次可能需要右键 → 打开）
- Windows：双击 `start-win.bat`
- 或者手动：`python3 web.py`

启动后浏览器会自动打开 http://127.0.0.1:8000。

**4. 配置模型**

点页面右上角「模型设置」，粘贴你自己的 DeepSeek API Key（在 https://platform.deepseek.com 申请），
点「测试连接」确认能通，再点「保存」。

Key 也可以放在环境变量里：

```bash
export DEEPSEEK_API_KEY=sk-你的key
python3 web.py
```

## 它能干什么

- 按你的要求创建目录和文件、写入内容
- 执行终端命令、跑 Python 脚本，并把输出读回来判断结果
- 失败不会中断任务，它会看到报错自己换个办法重试（最多 20 轮）
- 每一轮的思考、决策、命令、执行结果都实时推送到页面上，随时可以点「停止」

## 安全说明

**请先读完这一节再用。**

- 服务只监听 `127.0.0.1`，局域网内其他设备访问不到。**不要**把 `host` 改成 `0.0.0.0`。
- 它会执行**真实的终端命令**，删除文件、修改配置都是真的。发送任务前请确认内容是你想要的。
- API Key 只保存在本机的 `.agent_settings.json`（权限 600，仅本人可读写），不会上传到任何地方。
- `log.txt` 会记录完整的请求和响应，包含你的对话原文。不要把它提交到 git 或分享出去。
- 这个程序**没有沙箱**。它等于给模型开了一个你本机用户权限的 shell。请只在你信任的机器上、只用于你确认过的任务。

## 文件说明

```
local-file-agent/
├── web.py                 网页版服务（Flask + SSE）
├── agent_demo.py          终端版 / 核心逻辑（call_llm、run_bash、主循环）
├── templates/index.html   前端页面（原生 JS，无 CDN 依赖）
├── requirements.txt       Python 依赖
├── start-mac.command      macOS 一键启动
├── start-win.bat          Windows 一键启动
├── .gitignore             挡住密钥和日志
├── .agent_settings.json   （运行后生成）你的配置，已被 gitignore
└── log.txt                （运行后生成）请求日志，已被 gitignore
```

## 常见问题

**端口被占用**
macOS 的「隔空播放接收器」会占用 5000 端口，本项目用 8000 就是为了避开它。
如果 8000 也被占用，改 `web.py` 最后一行的 `port=` 即可。

**测试连接返回 401**
Key 不对或已被吊销。去控制台确认一下，或者换一把。

**模型列表显示「在线拉取失败」**
下拉框里是内置的兜底清单，不影响使用。点「刷新列表」重试。

**页面打不开**
确认 `web.py` 的终端窗口还在运行，且没有报错。

## 许可

[MIT](LICENSE) © 2026 comeonfan08-cmd

- ✅ 自由使用、复制、修改、合并、发布、分发、再许可、销售
- ✅ 允许商业使用
- ✅ 只需保留版权声明和许可声明
- ⚠️ 软件按「现状」提供，不附带任何担保

这是 OSI 认可的标准开源协议，GitHub 会自动识别并显示 MIT 徽章。

## 致谢

用到的模型接口：DeepSeek（https://platform.deepseek.com）。

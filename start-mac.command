#!/bin/bash
# 本地文件 Agent · macOS 一键启动
# 双击运行本文件即可。关闭终端窗口即停止服务。

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有找到 python3。"
  echo "请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/"
  read -r -p "按回车键退出..."
  exit 1
fi

if ! python3 -c "import flask" >/dev/null 2>&1; then
  echo "缺少依赖 Flask，正在安装（使用清华镜像）..."
  python3 -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if [ $? -ne 0 ]; then
    echo "安装失败。请手动执行："
    echo "  python3 -m pip install -r requirements.txt"
    read -r -p "按回车键退出..."
    exit 1
  fi
fi

echo "正在启动，浏览器稍后会自动打开 http://127.0.0.1:8000"
echo "关闭这个窗口即停止服务。"
echo

( sleep 2; open "http://127.0.0.1:8000" >/dev/null 2>&1 ) &
exec python3 web.py

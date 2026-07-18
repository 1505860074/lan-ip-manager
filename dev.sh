#!/usr/bin/env bash
# ============================================================
# 开发模式一键启动脚本。
#
# 用法：
#   ./dev.sh              完整功能（会用 sudo，扫描/改本机网段都能用）
#   ./dev.sh --no-sudo    不提权，仅适合调前端和 ④SSH 命令台（扫描/改网段会失败）
#
# 特性：
#   - 自动确保虚拟环境 .venv/ 存在、依赖装好（第一次慢，之后秒开）；
#   - 开启 FLASK_DEBUG：改完 .py 自动重启；改 HTML/JS/CSS 直接刷新浏览器即可。
# ============================================================
set -e
cd "$(dirname "$0")"   # 切到脚本所在目录，保证在哪儿执行都对

# 1) 确保虚拟环境存在（只有第一次会真正创建）
if [ ! -d .venv ]; then
  echo "> 首次运行，正在创建虚拟环境 .venv/ ……"
  python3 -m venv .venv
fi

# 2) 确保依赖装好。-q 安静模式；--disable-pip-version-check 去掉“pip 可升级”的干扰提示。
#    已装好时这步很快，只做校验。
echo "> 检查依赖……"
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

PYTHON=.venv/bin/python
export FLASK_DEBUG=1   # 开发模式：改完 .py 自动重启（app.py 会读这个变量）

# 3) 启动。默认用 sudo 跑完整功能；传 --no-sudo 则不提权。
if [ "$1" = "--no-sudo" ]; then
  echo "> 无 root 模式：扫描 / 改本机网段会失败，仅适合调前端和 SSH 命令台。"
  exec "$PYTHON" app.py
else
  echo "> 完整模式：接下来若提示输入密码，是 sudo 在要你的登录密码。"
  # sudo -E 保留上面 export 的环境变量（否则 sudo 会把 FLASK_DEBUG 清掉）。
  # 用 .venv 里的 python 而不是系统 python3——否则找不到装在 .venv 里的 flask/scapy。
  exec sudo -E "$PYTHON" app.py
fi

#!/usr/bin/env bash
# ============================================================
# 打包脚本：把整个工具打成 dist/lan-ip-manager 这一个可执行文件。
#
# 打包后的成品：不需要再装 Python，也不需要 pip install 任何依赖，
#              直接 `sudo ./lan-ip-manager` 就能跑。
# 仍然需要的东西（无法打包进去，属于系统本身）：
#   1) root 权限（抓包、改网卡）
#   2) 系统自带的 `ip` 命令（所有 Linux 都有）
# ============================================================
set -e
cd "$(dirname "$0")"

# 1) 准备虚拟环境（不污染系统 Python），装依赖 + 打包工具 pyinstaller
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -r requirements.txt pyinstaller

# 2) 打包（用 venv 里的 pyinstaller，才能收集到 venv 里的依赖）
PYINSTALLER=.venv/bin/pyinstaller
#   --onefile        打成单个可执行文件
#   --add-data       把网页模板/静态资源一起打进去（Linux 用冒号 : 分隔）
#   --collect-all    把 scapy / paramiko 的子模块和数据文件全收进来，
#                    否则运行时常报「找不到某个子模块」
$PYINSTALLER --onefile \
  --name lan-ip-manager \
  --add-data "templates:templates" \
  --add-data "static:static" \
  --collect-all scapy \
  --collect-all paramiko \
  app.py

echo
echo "打包完成： $(pwd)/dist/lan-ip-manager"
echo "   运行方式： sudo ./dist/lan-ip-manager"

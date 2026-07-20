#!/usr/bin/env bash
# ============================================================
# 打包脚本：把整个工具打成 dist/lan-ip-manager 这一个可执行文件。
#
# 打包后的成品：不需要再装 Python，也不需要 pip install 任何依赖，
#              直接 `sudo ./lan-ip-manager` 就能跑。
# 仍然需要的东西（无法打包进去，属于系统本身）：
#   1) root 权限（抓包、改网卡）
#   2) 系统自带的 `ip` 命令（所有 Linux 都有）
#
# 【为什么用 conda 环境而不是普通 venv】
# PyInstaller 需要 Python 的“共享库”(libpython3.10.so) 才能把解释器嵌进成品。
# 本机自编译的那个 /usr/local python3.10 没加 --enable-shared 编，没有这个共享库，
# 直接拿它（或建在它上面的 .venv）打包会报：
#     ERROR: Python was built without a shared library ...
# 而 conda 自带的 Python 天生带共享库，所以这里用 conda 建一个 3.10 环境来打包。
# （deadsnakes PPA 那条路要连境外 launchpad.net，国内网络会卡死，故不采用。）
# ============================================================
set -e
cd "$(dirname "$0")"

# ---- 配置项 ----
CONDA_HOME="${CONDA_HOME:-$HOME/miniconda3}"            # miniconda 安装位置
ENV_NAME="lanip-pack"                                  # 专用于打包的 conda 环境名
PY_VER="3.10"                                          # 打包用的 Python 版本
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"  # 国内 pip 镜像

CONDA="$CONDA_HOME/bin/conda"
ENV_PY="$CONDA_HOME/envs/$ENV_NAME/bin/python"

if [ ! -x "$CONDA" ]; then
  echo "找不到 conda：$CONDA"
  echo "请先安装 miniconda，或用环境变量 CONDA_HOME 指定它的安装目录后重试。"
  exit 1
fi

# 1) 准备 conda 打包环境（已存在就跳过创建）；本地有缓存时创建很快
if [ ! -x "$ENV_PY" ]; then
  echo "==> 创建 conda 环境 $ENV_NAME (python $PY_VER) ..."
  "$CONDA" create -y -n "$ENV_NAME" "python=$PY_VER" pip
fi

# 2) 装依赖 + 打包工具 pyinstaller。
#    PYTHONNOUSERSITE=1：屏蔽 ~/.local 里的用户级包，强制所有依赖都装进本环境，
#    避免“环境里没有、却从 ~/.local 借用”导致打包混入外部旧包。
echo "==> 安装依赖与 pyinstaller ..."
PYTHONNOUSERSITE=1 "$ENV_PY" -m pip install -i "$PIP_MIRROR" \
  -r requirements.txt pyinstaller

# 3) 打包（用本环境的 pyinstaller，才能收集到本环境里的依赖）
#   --onefile        打成单个可执行文件
#   --clean          清掉上次的缓存，避免旧产物干扰
#   --add-data       把网页模板/静态资源一起打进去（Linux 用冒号 : 分隔）
#   --collect-all    把 scapy / paramiko 的子模块和数据文件全收进来，
#                    否则运行时常报「找不到某个子模块」
echo "==> 开始打包 ..."
PYTHONNOUSERSITE=1 "$ENV_PY" -m PyInstaller --onefile --clean --noconfirm \
  --name lan-ip-manager \
  --add-data "templates:templates" \
  --add-data "static:static" \
  --collect-all scapy \
  --collect-all paramiko \
  app.py

echo
echo "打包完成： $(pwd)/dist/lan-ip-manager"
echo "   运行方式： sudo ./dist/lan-ip-manager"

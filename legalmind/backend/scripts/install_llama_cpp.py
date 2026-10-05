"""在本机编译安装 llama-cpp-python（Windows）。

**为什么需要这个脚本**：本机拿不到 ``llama-cpp-python`` 的预编译 wheel——PyPI 上只有源码包，
解压会撞 Windows 260 字符路径上限；官方 wheel 索引可达但 wheel 本体托管在 **GitHub releases**，
本机不可达（502）。好在 VS 2022 Community 的 MSVC 与 CMake 都在，所以自己编。

脚本做两件事：

1. 从 ``vcvars64.bat`` 抓出 MSVC 环境变量（直接 ``call`` 它再跑 pip 会被 shell 引号搞坏）；
2. 把 ``TEMP`` / ``TMP`` 指到**短路径**（默认临时目录本身就长，加上 sdist 里
   ``vendor/llama.cpp/tools/ui/...`` 那串深目录就超 260 了），再交给 pip。

跑法::

    .venv/Scripts/python.exe scripts/install_llama_cpp.py
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
VCVARS = Path(
    r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
)
SHORT_TEMP = Path("D:/tmp")
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def msvc_environment() -> dict[str, str]:
    """``call vcvars64.bat && set`` 的输出就是配置好的环境；逐行取回。"""
    if not VCVARS.is_file():
        raise SystemExit(f"没找到 vcvars64.bat：{VCVARS}")
    completed = subprocess.run(
        ["cmd", "/c", "call", str(VCVARS), "&&", "set"],
        capture_output=True,
        text=True,
        check=True,
    )
    environment = dict(os.environ)
    for line in completed.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator and name:
            environment[name] = value
    return environment


def main() -> int:
    SHORT_TEMP.mkdir(parents=True, exist_ok=True)
    environment = msvc_environment()
    # 短临时目录：pip 在这里解压 sdist，路径长度必须留够余量
    environment["TEMP"] = str(SHORT_TEMP)
    environment["TMP"] = str(SHORT_TEMP)
    print(f"TEMP={environment['TEMP']}　开始编译（十几分钟，无输出是正常的）", flush=True)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "llama-cpp-python",
            "--no-cache-dir",
            "-i",
            MIRROR,
        ],
        env=environment,
        cwd=BACKEND_DIR,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

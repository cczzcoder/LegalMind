"""从 HuggingFace 镜像直链下载模型文件（设计 §9.5）。

**为什么不走 ``huggingface_hub``**：镜像会先 ``HEAD /resolve/...`` 拿到 307，跳到
``/api/resolve-cache/...``，而该路径返回 **403**，于是文件落成 **0 字节**（实测，
``hf_hub_download`` 与 ``snapshot_download`` 都一样）。``/resolve/`` 直链本身是通的，
所以这里按已知文件布局直接拉。

两条纪律：

- **写 ``.part`` 再改名**——中途断了不会留下一个「看起来完整」的半截权重。
- **404 跳过**——各仓库的文件名不完全一样（BGE-M3 只有 ``pytorch_model.bin``，
  text2vec 两者都有），缺哪个跳哪个；权重文件按给定顺序取**第一个下成功的**。
"""

import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

# 本机到镜像只有 ~0.6 MB/s，一个 2 GB 的权重没有进度会以为卡死
PROGRESS_BYTES = 100 * 1024 * 1024


def fetch(url: str, destination: Path, *, label: str = "") -> int:
    """下载单个文件；返回字节数。"""
    partial = destination.with_name(destination.name + ".part")
    partial.parent.mkdir(parents=True, exist_ok=True)  # 仓库里有 1_Pooling/ 这类嵌套目录
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    total = 0
    mark = 0
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            partial.open("wb") as handle,
        ):
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                total += len(chunk)
                if label and total - mark >= PROGRESS_BYTES:
                    mark = total
                    print(f"    … {label} {total / 1048576:.0f} MB", flush=True)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(destination)
    return total


def download_repo(
    repo: str,
    directory: Path,
    filenames: Iterable[str],
    *,
    endpoint: str,
    weights: Iterable[str] = (),
    force: bool = False,
) -> list[str]:
    """按名字逐个拉进 ``directory``；返回本次真正下载的文件名。

    ``weights`` 是权重文件的候选名，按顺序取第一个下成功的——已经有任何一个存在就不再下。
    """
    directory.mkdir(parents=True, exist_ok=True)
    base = f"{endpoint.rstrip('/')}/{repo}/resolve/main"
    downloaded: list[str] = []
    for filename in filenames:
        target = directory / filename
        if target.is_file() and target.stat().st_size > 0 and not force:
            continue
        try:
            size = fetch(f"{base}/{filename}", target, label=filename)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        downloaded.append(filename)
        print(f"  + {filename} ({size / 1048576:.1f} MB)", flush=True)

    candidates = list(weights)
    if candidates and not any((directory / name).is_file() for name in candidates):
        for filename in candidates:
            try:
                size = fetch(f"{base}/{filename}", directory / filename, label=filename)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    continue
                raise
            downloaded.append(filename)
            print(f"  + {filename} ({size / 1048576:.1f} MB)", flush=True)
            break
    return downloaded

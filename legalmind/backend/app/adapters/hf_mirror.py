"""从 HuggingFace 镜像直链下载模型文件（设计 §9.5）。

**为什么不走 ``huggingface_hub``**：镜像会先 ``HEAD /resolve/...`` 拿到 307，跳到
``/api/resolve-cache/...``，而该路径返回 **403**，于是文件落成 **0 字节**（实测，
``hf_hub_download`` 与 ``snapshot_download`` 都一样）。``/resolve/`` 直链本身是通的，
所以这里按已知文件布局直接拉。

两条纪律：

- **写 ``.part`` 再改名**——中途断了不会留下一个「看起来完整」的半截权重。
- **404 跳过**——各仓库的文件名不完全一样（BGE-M3 只有 ``pytorch_model.bin``，
  text2vec 两者都有），缺哪个跳哪个；权重文件按给定顺序取**第一个下成功的**。

⚠️ **「这个目录下全了没有」的判据是 `is_complete()`，不要各写各的**——见那里的注释。
"""

import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

# 本机到镜像只有 ~0.6 MB/s，一个 2 GB 的权重没有进度会以为卡死
PROGRESS_BYTES = 100 * 1024 * 1024


def _present(path: Path) -> bool:
    """文件在**且非空**。

    ⚠️ **0 字节要当成「不在」**：镜像那条 ``HEAD → /api/resolve-cache/`` 的老路会把文件落成
    0 字节（见模块开头），只判 ``is_file()`` 会把它当成下好了。
    """
    return path.is_file() and path.stat().st_size > 0


def is_complete(directory: Path, weights: Iterable[str]) -> bool:
    """``directory`` 里的模型**下全了吗**——判据是「配置在」**且**「至少一个非空权重在」。

    ⚠️⚠️ **别只看 ``config.json``**（实测踩到）：它是 ``download_repo`` 里**第一个**下的文件，
    而权重是**最后一个**、也最容易中断。一次被中断的首次下载会留下「config 在、权重不在」的
    目录；若就绪判据只看 config，之后**每次都会跳过下载**，然后在 transformers 深处报一个和
    「没下全」毫无关系的错（``no file named model.safetensors, or pytorch_model.bin``）——
    **而且永远不会自愈**，除非有人想到去手工删目录。
    """
    if not _present(directory / "config.json"):
        return False
    return any(_present(directory / name) for name in weights)


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
        if _present(target) and not force:
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
    # 有**非空**权重就跳过；0 字节的（镜像老路留下的）要重下，否则同样会「永不复发」
    if candidates and not any(_present(directory / name) for name in candidates):
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

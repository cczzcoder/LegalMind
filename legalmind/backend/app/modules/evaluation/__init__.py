"""生成质量评测（设计 §9.5「本地生成模型单独评测，达标后再接入正式问答」）。

这个包只放**纯逻辑**：不调用生成模型、不碰数据库，所以能被单元测试直接钉住。
真正跑模型的是 ``backend/scripts/evaluate_answering.py``，数据集在
``evaluations/datasets/generation_quality.json``。

**为什么不评「答案读起来好不好」**：那需要语义裁判，而本项目 §9.5 锁定本地模型、默认关闭外部
API。先用确定性指标把「可核验」这一层量出来（与 §9.3 第一层确定性校验同源）；语义核验
（§9.3 第二层）是另一件事，不混进来。
"""

from app.modules.evaluation import scoring

__all__ = ["scoring"]

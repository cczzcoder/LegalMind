"""法律语料：结构识别、元数据提取与版本落库（设计 §5.1、§5.2、§7、§8.3）。

- ``structure``：识别 编/章/节/条，供分块对齐到条（纯函数）
- ``metadata``：从前言与文件名提取法律元数据与效力状态（纯函数）
- ``service``：把解析结果挂到 ``legal_instruments`` / ``legal_versions`` /
  ``provision_identities`` / ``provision_versions``（调用方事务内）

条款关系（``provision_relations``）与适用性（``applicability_records``）尚未实现。
"""

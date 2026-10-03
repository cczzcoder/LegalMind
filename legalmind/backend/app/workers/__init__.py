"""按需启动的单进程 worker：领取数据库任务并处理（设计 §3.1、§12.2、§14.2）。

入口：``python -m app.cli run-worker``（``--once`` 只处理一个任务）。
"""

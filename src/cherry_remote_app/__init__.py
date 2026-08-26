"""Cherry Remote App —— 远程操控执行器（纯执行器，无任何 AI/LLM 逻辑）。

C 端组件：主动外连 B 端 AstrBot 插件的 WebSocket 服务，接收指令并执行，
回传原始结果。所有智能（指令生成、结果研判）均位于 B 端。
"""

__version__ = "1.3.0"

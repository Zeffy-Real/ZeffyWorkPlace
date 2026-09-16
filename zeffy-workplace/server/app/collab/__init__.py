"""P7-D2 多 Agent 协作深化（探索线）。

红线（审查强制）：
- **层间边界**：本包仅实现协作编排逻辑（共享上下文脱敏 / 并行子步派发 / 评审收敛门闸 /
  协作审计），数据经**公开产物/消息 API** 透传，**禁止直调 storage/governance/crypto/DB 内部**。
- **真相源**：并行仅子步骤级，**不产生独立 TaskNode**；子步骤并发结算收敛回调用方
  （AgentRunner），由主 TaskNode 唯一真相源统一写回/失败。
- **总闸短路**：``AGENT_COLLAB_ENABLED=false``（或总闸 ARTIFACT_META_ENABLED 关）→
  并行/脱敏/评审门闸全部不生效，行为与既有一致（零漂移）。
- **资源隔离**：并行独立 ``AGENT_COLLAB_WORKER_POOL`` 并发槽，不与主线 ARQ worker 竞争。
"""

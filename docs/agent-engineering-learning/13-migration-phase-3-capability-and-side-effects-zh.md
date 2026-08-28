# 迁移阶段 3：工具能力、审批与高副作用隔离

## 本阶段目标

阶段 3 解决“Agent 看见了工具，就可以调用工具”的问题。工具不是普通函数，而是带权限、成本、幂等性和副作用的能力。

目标是：

```text
系统注册什么
  ∩ Agent Profile 允许什么
  ∩ 当前身份有权使用什么
  ∩ 当前 Run 是否获批
  = 本次模型真正能看到的工具
```

## 1. Curated tools

Open SWE 将业务工具集中在 agent/tools 中，主 Agent 显式装配；Deep Agents 的内置文件工具由框架提供，不重复注册。

可迁移原则：

- 工具目录是受控注册表，不是自动扫描目录；
- Graph 只拿到当前 Profile 允许的工具；
- 工具名称、参数 schema 和权限在模型调用前确定；
- 新工具必须有调用方、错误语义和测试，不因为以后可能用就默认暴露。

ai-agent-platform 的 RuntimeRequestMiddleware 已支持按名称选择工具，但请求中的工具名称只能引用服务端注册目录。

## 2. Capability Profile

建议为每个 Assistant 定义能力画像：

```python
class AgentCapabilityProfile:
    assistant_id: str
    readable_tools: set[str]
    writable_tools: set[str]
    approval_required_tools: set[str]
    max_model_calls: int
    max_run_seconds: int
    supports_background_run: bool
    supports_interrupt: bool
```

最终选择工具时进行多重收缩：

```text
registered_tools
  ∩ assistant_profile
  ∩ project_policy
  ∩ RuntimeContext.permissions
  ∩ current_approval
```

不要只依赖 Prompt 告诉模型不要调用某工具。工具必须在 Graph/Middleware 层真正不存在或被拒绝。

## 3. 工具的副作用分类

每个工具都应标注：

| 属性 | 示例 |
| --- | --- |
| side_effect | read、write、external_write |
| idempotent | 是否重复调用结果一致 |
| project_scoped | 是否只能访问当前项目 |
| timeout | 单次工具 deadline |
| retry_policy | 不重试、幂等重试、人工确认 |
| audit_level | 参数摘要、完整参数、结果摘要 |
| approval | 无需、单步、整次 Run |

工具异常需要返回稳定错误码，而不是把第三方异常字符串直接暴露给模型。

## 4. 写工具不能盲目重试

Open SWE 的外部请求重试矩阵区分幂等和非幂等方法。迁移到平台时，创建工单、发送通知、写数据库、提交测试结果都要考虑：

```text
请求超时
  -> 上游可能已经成功
  -> 不能直接再发一次
  -> 使用 idempotency_key 查询或补偿
```

推荐写工具流程：

```text
生成幂等键
  -> 写入 intent/audit 记录
  -> 调用外部系统
  -> 保存 external_id
  -> 重试时先查询 external_id
```

没有查询确认能力的非幂等写工具，应默认为需要人工审批或不允许自动重试。

## 5. 审批不能只放在 UI

UI 的确认按钮不是安全边界。审批状态必须进入 Thread/Run 状态：

```text
waiting_approval
  -> approval_id
  -> approver
  -> approved_at
  -> expires_at
  -> approved action hash
```

Middleware 需要再次检查审批是否属于当前 Run、针对同一工具和参数摘要、没有过期或撤销，并且审批人有权限。

### 5.1 事件只能触发检查，不能直接授权自动动作

CI 失败、审查意见、监控告警或定时任务都只是触发信号，不能因为收到事件就直接执行写操作。自动动作开始前至少重新确认：

```text
目标资源仍可操作
当前状态仍满足触发条件
没有更新的人工操作覆盖该事件
同一资源没有等价 Run 正在执行
本轮未超过重试或循环上限
```

这套门槛不依赖 GitHub 或 Coding Agent：它同样适用于自动重试支付、同步 CRM、发送通知和修复数据。事件可能重复、延迟或过期；权限和真实状态必须以执行时的权威系统为准。

## 6. 高副作用能力必须独立

Open SWE 的 Sandbox、Git、GitHub Token、PR 和 CI Auto-fix 是 Coding Agent 的专用能力，不能进入所有 Runtime Agent 的默认工具集。

建议目录边界：

```text
runtime-service/runtime/
runtime-service/capabilities/
  knowledge/
  data_query/
  repo_execution/
```

普通 SQL Agent 不应该因为同一个 Runtime 进程存在 repo_execution 就能导入它。

## 7. Sandbox 生命周期的可迁移原则

如果以后接入 Coding Capability，可以借鉴 Open SWE 的三条原则：

1. thread metadata 保存 sandbox ID，进程内缓存只是加速层；
2. 已有 Sandbox 不可达时，默认报错，不静默替换空 Sandbox；
3. 只有明确允许替换的只读 Reviewer 场景才使用 allow_replacement。

这避免把“恢复成功”伪装成“创建了一个空工作区”。

## 8. 工具装配顺序

```text
加载 Agent Profile
  -> 读取 RuntimeContext 权限
  -> 解析项目策略
  -> 过滤工具目录
  -> 挂载审批 Middleware
  -> 挂载输入清洗和错误处理
  -> 暴露给模型
```

不要先把所有工具挂到模型，再靠运行时拒绝。

## 9. 分阶段实施

### 3.1 先做只读工具

只接入检索、查询、状态读取等低副作用工具，验证权限和错误码。

### 3.2 再接入审批写工具

要求幂等键、审计记录和人工确认，禁止默认重试。

### 3.3 最后接入 Workspace

只有 Coding Agent 需要时才创建 Sandbox、Git 和 PR Capability。

## 10. 验收测试

```text
无权限角色看不到写工具
工具名称不在注册表 -> Run 创建前失败
项目 A 的工具不能读取项目 B
审批过期 -> 工具调用被拒绝
相同幂等键重试 -> 不产生两次外部写入
Sandbox 失联 -> 明确失败，不替换为空环境
普通 Agent -> 无法导入 repo_execution
```

## 不要在阶段 3 做的事

- 不要把所有 Open SWE 工具复制到通用 Runtime。
- 不要把 GitHub Token 放进 RunnableConfig 或模型消息。
- 不要把 UI 审批状态当成唯一审批事实。
- 不要对未知是否成功的写操作自动重试。
- 不要在第一批改造里同时引入 Sandbox、PR 和 CI 自动修复。

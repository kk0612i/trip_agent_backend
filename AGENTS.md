# Python Agent开发规则

## 1. 工作方式与规范来源

- 默认使用中文沟通、编写业务注释和文档字符串。
- 修改前检查实际目录、依赖版本、入口、调用关系和相关测试，不凭教学示例推断现有实现。
- 先判断用户是在讨论、审查还是要求实现。用户要求先讨论时，不修改文件；明确要求实现后，完成范围内的工作与必要验证。
- 规范中的默认写法不得覆盖用户本次明确要求；已有项目的接口、数据和运行行为不可仅为风格调整而改变。

## 2. 包职责与文件命名

应用代码按职责分包，复杂 Agent 内部按功能分子包。以下用 `user`、`chat` 作为业务名示例，文件和子包按实际需要创建。

```text
项目根目录/
├── main.py                      # FastAPI 应用创建、生命周期、路由和异常处理器注册
├── app/
│   ├── __init__.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── user_router.py       # HTTP 参数、依赖、状态码和响应
│   │   ├── deps.py              # 请求依赖组装
│   │   └── errors.py            # 业务异常到 HTTP 的映射
│   ├── services/
│   │   ├── __init__.py
│   │   └── user_service.py      # 业务规则、操作协调和事务边界
│   ├── repository/
│   │   ├── __init__.py
│   │   └── user_repository.py   # SQL 查询、写入、更新和删除
│   ├── models/
│   │   ├── __init__.py
│   │   ├── user.py              # ORM 表映射
│   │   └── base.py              # 统一 Base 和公共 Mixin
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── user_schema.py       # 请求、响应、业务传输和模型结构化输出
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py            # 配置
│   │   ├── db.py                # 数据库引擎和会话工厂
│   │   ├── log.py               # 日志配置
│   │   ├── errors.py            # 异常定义
│   │   └── llm.py               # 大语言模型对象创建
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── chat_graph.py        # 图构建与编译
│   │   ├── chat_state.py        # 状态结构与 reducer
│   │   ├── chat_hook.py         # 钩子函数
│   │   ├── chat_nodes.py        # 节点函数
│   │   └── chat_edges.py        # 路由函数
│   ├── prompt/
│   │   ├── __init__.py
│   │   └── chat_prompt.py       # 提示词函数，例如 get_chat_system_prompt()
│   └── tools/                  # Agent 工具及业务调用适配，文件名不加 _tool 后缀
│       ├── __init__.py
│       ├── order.py
│       └── search.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # 使用 pytest 时存放跨业务共享的 fixture
│   ├── user/
│   │   ├── __init__.py
│   │   ├── test_user_service.py # 业务规则、失败分支和事务行为
│   │   └── test_user_api.py     # 请求校验、状态码和响应
│   ├── chat/
│   │   ├── __init__.py
│   │   ├── test_chat_nodes.py   # 节点行为及外部调用失败处理
│   │   ├── test_chat_edges.py   # 路由分支
│   │   └── test_chat_graph.py   # 工作流执行与最终结果
│   └── integration/            # 真实数据库、模型或外部服务的集成测试，内部按业务分包
│       └── __init__.py
├── scripts/                    # 维护脚本，按实际用途命名
└── docs/                       # 项目文档，按实际用途命名
```

- 普通 Python 包保留 `__init__.py`，初期可以为空。避免在其中集中导入所有模块，以免造成循环依赖。
- ORM Base 放在 `app/models/base.py`，数据库引擎和会话工厂放在 `app/core/db.py`，不另建重复的 `db/session.py`。
- Agent 的钩子、节点和路由函数分别放在 `<业务名>_hook.py`、`<业务名>_nodes.py` 和 `<业务名>_edges.py`。
- 提示词函数放在 `app/prompt/<业务名>_prompt.py`。系统提示词函数使用 `get_<业务名>_system_prompt()` 命名，返回提示词字符串。
- 测试文件和测试替身按业务放在 `tests/` 的子包中，根目录只保留包初始化文件和必要的共享配置。使用 pytest 时，业务专用的 fixture 放在对应子包的 `conftest.py` 中。集成测试放在 `tests/integration/` 下的业务子包中，单独运行。
- 私有实现就近放置，共享需求明确后再提取；不把无关逻辑集中到 `utils.py`、`common.py`。

## 3. 类、函数和依赖注入

- 类适合保存依赖并组织相关方法；独立逻辑、节点和路由优先使用函数，具体按实现需要选择。
- 依赖从外部传入，当前操作的输入通过参数传递。共享实例不保存本轮请求状态。
- Repository 依赖命名为 `xxx_repo`，Service 依赖命名为 `xxx_service`，数据库会话命名为 `session`。
- FastAPI 的依赖在 `api/deps.py` 或应用装配位置组装，业务层接收普通 Python 对象。

## 4. 类型、变量注释与文档字符串

- 类、函数和方法的文档字符串使用中文 Google 风格。先说明用途，再按需填写 `Args`、`Returns`、`Raises`；生成器使用 `Yields`。
- 变量和复杂逻辑按需添加中文注释，说明含义、用途或设计原因。
- 类型标注、注释和文档字符串与实际实现保持一致，修改代码时同步更新。

## 5. 同步、异步与并发

- 纯计算、字段转换、构造函数、条件路由和普通图构建、编译使用 `def`。
- 需要等待异步数据库、HTTP、模型或图调用时，使用 `async def` 和 `await`；异步流使用 `async for`。上层需要等待这些调用时，也使用异步。
- 异步函数可以直接调用短小的同步函数。同步阻塞 I/O 交给线程执行，耗时较长的 CPU 计算考虑进程或任务队列。
- 模型和 HTTP 等外部调用设置超时；Agent 明确本轮执行期限、工具调用或步骤上限。限制由代码执行，按业务要求配置。
- 重试明确可重试异常、最大次数和退避策略；涉及写操作时，重试策略与幂等策略一起确定。
- 调用取消或 SSE 断连时，明确未完成任务是取消还是交给后台继续，并按所选策略清理本轮资源。

## 6. 各层实现边界

### API

- Router 只负责请求校验、可信身份与依赖获取、Service 调用、HTTP/SSE 响应。
- 用户身份从服务端认证上下文获取，通过普通 Python 对象传给 Agent、Tool 和业务 Service；模型生成或客户端提交的身份字段不能直接作为可信身份。
- 声明输入输出 Schema 与状态码，不在 Router 编写 SQL、事务流程或完整 Agent 编排。
- 业务异常在 API 边界统一映射；不要每个路由捕获所有异常并返回 HTTP 200。

### Service

- 实现业务规则，协调多个 Repository，明确最外层业务工作单元的事务所有权。
- 不依赖 `Request`、`Depends`、`HTTPException`，不自行读取环境变量或创建隐藏的外部客户端。
- 入口 Service 可调用 Agent；Agent/Tool 可调用独立业务 Service。被调用的业务 Service 不反过来调用当前 Agent 或入口 Service。
- Tool 或独立业务 Service 在执行操作前校验当前调用者的权限和资源归属。读取会话历史、恢复图执行时，校验 `thread_id` 对应会话的归属和访问权限。
- 传出会话边界前按需要将 ORM 转为明确的业务或响应 Schema。

### Repository

- Repository 按 ORM 实体模型命名：文件使用 `<实体模型名的 snake_case>_repository.py`，类使用 `<实体模型名>Repository`。例如，`User` 对应 `user_repository.py` 中的 `UserRepository`。
- 单表操作优先放在对应实体的 Repository 中。联表查询和多表数据操作，可以放在主要实体对应的 Repository 中。
- Repository 负责 SQL 和数据读写；Service 负责业务规则和操作协调。
- 使用外部传入的 Session，事务提交和回滚由最外层业务操作负责。
- 单条查询未找到时返回 `None`，集合查询无结果时返回空列表；数据库异常正常传播。

### Models

- ORM 继承统一 `Base`，明确字段、可空性、约束和索引；精确金额使用 `Decimal`/`Numeric`。
- ORM 不负责连接、提交和外部请求；迁移配置导入全部模型后使用元数据，不在应用启动时自动修改表结构。

### Schemas

- 使用 Pydantic 定义数据结构，负责字段格式、范围和一致性校验；依赖数据库的业务检查交给 Service。
- 每个字段使用 `Field(description=...)` 添加中文描述，description 只说明字段含义。
- 字段类型、默认值和校验条件通过类型标注及 Field 参数表达。



## 7. Session、事务与生命周期

- 引擎、会话工厂和支持并发复用的模型客户端可在进程内复用。Session 及持有它的对象按请求或工作单元创建，由请求依赖或任务入口负责关闭。
- Session 和 AsyncSession 均不得在多个线程或异步任务中同时使用，只读操作也遵守此规则。独立并发任务分别创建和关闭自己的会话。
- 后台任务自行创建和关闭 Session，不复用请求的会话或持有该会话的 Repository、Service。
- 需要共同提交的数据操作使用同一个 Session 和事务，并顺序执行；提交成功后再返回写入成功。
- 查询也可能自动开启事务，开始新事务前确认调用链是否已有事务。
- 模型推理、人工暂停和长时间外部调用放在数据库事务之外。
- 应用生命周期负责可复用资源的初始化与关闭，模块导入和构造函数不执行外部 I/O。

## 8. 框架技能使用要求

实现、实质修改或审查下列相关代码前，必须按任务范围读取对应技能的 `SKILL.md`，并按技能要求读取相关参考文件。涉及多项能力时，读取所有适用技能。

- FastAPI 应用、路由、请求与响应模型、依赖注入、生命周期、异常处理和流式响应（含 SSE）：使用 `fastapi`。
- LangChain Agent、工具定义和 `create_agent`：使用 `langchain-fundamentals`。
- LangChain middleware、工具审批或结构化输出：使用 `langchain-middleware`。
- LangGraph 图、状态、节点、边和执行调用：使用 `langgraph-fundamentals`。
- LangGraph 状态持久化、检查点、历史和恢复：使用 `langgraph-persistence`。
- LangGraph 人工中断、审批和恢复执行：使用 `langgraph-human-in-the-loop`。
- 新增可能已有成熟实现的功能或公共机制：使用 `search-first`，先检查项目及框架已有能力。

执行要求：

1. 开始实现或审查前，简要说明已读取的技能、已核实的相关依赖版本，以及本次准备复用的框架能力。
2. 按项目实际依赖版本核对技能中的 API、参数和示例；使用新特性前确认当前版本支持，不直接照搬示例。
3. FastAPI 开发优先复用框架的参数校验、依赖注入、响应模型与生命周期管理，并遵守本文件的分层与事务规范。
4. 标准 Agent 循环优先评估 `create_agent`；自定义实现前说明现有能力无法满足的具体需求。
5. 图状态保存和执行恢复优先使用 Checkpointer；人工中断优先使用 `interrupt()` 和 `Command(resume=...)`。
6. 会产生业务写入或外部副作用的节点和 Tool，明确重试、恢复和重复请求下的幂等策略。同一业务操作的幂等键在各次尝试中保持一致，并通过数据库唯一约束或外部接口的幂等机制处理重复调用。
7. 恢复中断时，节点从开头重新执行，`interrupt()` 前的代码会再次运行。涉及审批时，将业务副作用放在审批之后或独立节点中；仍需处理副作用已成功、结果尚未保存到检查点时的重复执行。业务事务提交与检查点保存不能默认视为同一个原子操作。
8. 状态字段明确含义、更新方式和 reducer；节点返回局部更新，避免隐式修改共享状态。
9. 项目明确要求优先于技能中的通用建议；不因技能示例擅自升级依赖、更换 ORM、引入工具链或重构无关模块。
10. 技能或所需参考文件不可用时，如实说明缺失项及影响，不得声称已经读取或使用。

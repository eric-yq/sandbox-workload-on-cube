# Reduce Agentic RL Training Costs by Moving the Sandbox Tier to AWS Graviton

> ## 📌 适用范围：Phase 1（Docker 容器 / `.4xlarge`）
>
> 这篇报告记录的是 benchmark 的**第一阶段**：Docker 容器 + 常驻共享 worker，
> 在四款 `.4xlarge`（16 vCPU）实例上做的稳态对比。数据本身是实测的，结论在
> 「同一台机器上跑常驻沙盒服务」这个前提下成立。
>
> 此后 suite 换过两次运行时，本文的吞吐绝对值**都不能**直接套过去：
>
> - **Phase 2 — AgentENV**（Firecracker microVM），每条 rollout 一个独立沙盒，
>   目标机型 `*.metal-48xl`（192 vCPU）。这一阶段的实测数据在
>   `results/aarch64/`（c8g.metal-24xl 上的短时验证跑）。
> - **Phase 3 — CubeSandbox**（RustVMM/KVM，当前状态），沙盒模型不变，但供给
>   原语变了：没有批量 `fork`，改成 snapshot + N 次 create；新增 `rollback`
>   原地重置。设计与移植对照见仓库 README 的「从 AgentENV 移植过来」一节。
>
> **Phase 3 已有一组受控实测数据**，见文末新增的「Phase 3 实测 —
> CubeSandbox v0.7.2 上的 c8g vs c7i 头对头」一节（c8g.metal-24xl vs
> c7i.metal-24xl，同一 v0.7.2 build，2026-09-29）。Phase 2 的数字仍不能当作
> Phase 3 的预期值 —— 至少 create 路径、存储后端和代理拓扑三者都不同。
>
> 本文已修正的错误（2026-08 复核）：
>
> 1. **Graviton 代次**：原稿把 `m8g` 写成 Graviton3、`m9g` 写成 Graviton4。
>    实际是 **m8g = Graviton4**、**m9g = Graviton5**（M9g 于 2026 年 6 月 GA）。
> 2. **`m9g.4xlarge` 的单价锚点错了**：原稿用 $0.7600，实际是 **$0.78272**
>    （+3.0%，AWS Pricing API 复核）。§6 全部 m9g 的 `$/1M ops` 和节省比例
>    已按真实单价重算 —— 原稿的省钱幅度普遍**乐观了约 3 个百分点**。
> 3. **Appendix B 里的 `PRICE_M7I_4XL` 等环境变量在代码里从未存在**，
>    已改为实际的 `INSTANCE_PRICES` / `DEFAULT_INSTANCE_PRICES`。
> 4. **两处方法学缺陷**已就地标注：B3 的所有 trajectory 共用一个 mock-API
>    进程、且都用 `user="alice"`，在单把锁上串行；B5 全并发段共用一个
>    `--workers 1` 的 DuckDB —— 这正是 §5.4 那个 c=32 凹槽的来源。二者都是
>    容器共享拓扑的产物，per-rollout microVM 设计已消除。
>
> 尚未重测：四款 `*.metal-48xl` 的横向对比。已核实的 OD 单价（us-east-1,
> Linux）：m7i 9.6768 · m8i 10.16064 · m8g 8.61696 · m9g 9.39264 $/h。另有
> 一点值得单独成节：192 vCPU 这一档，Intel 是 96 物理核 + SMT，Graviton 是
> 192 个真实核 —— 对一个 fan out 成几百个 1–2 vCPU microVM 的负载，这个差别
> 很关键。

---

## TL;DR

讨论 Agentic RL 训练成本时，注意力几乎都在 GPU 一侧 —— 策略网络、reward model、
推理引擎。但策略产出的每一条 rollout 都必须**在一个真实环境里执行** —— Python
沙盒、mock 工具 API、headless 浏览器、SQL 仓库 —— 这一层是 CPU bound、fan-out
重的 fleet，GPU 字面意义上在等它。

我们做了一套可复现的 benchmark suite (`agentic-rl-bench`)，把六个 Agentic RL
训练里真实出现的 workload archetype 加一个端到端混合 rollout 拆出来，在两代
Intel Xeon (`m7i`、`m8i`) 和两代 Graviton (`m8g` Graviton4、`m9g` Graviton5) 的
`.4xlarge` 实例上各跑一遍稳态测量，记录吞吐、p99 延迟和单位操作成本。

核心结论（us-east-1 OD price，m7i.4xlarge 为 baseline，均为 c=64 档位）：

| Archetype                                | 训练范式               | m9g vs m7i 吞吐 (c=64) | m9g vs m7i $/1M |
|------------------------------------------|------------------------|------------------------|-----------------|
| B1 — 代码执行（HumanEval/MBPP-like）     | Code-RL（CodeRL/RLEF） | +54%                   | **−37%**        |
| B3 — 工具调用 trajectory（τ-bench）      | Tool-use RL            | **+151%**              | **−61%**        |
| B4 — 浏览器 trajectory（WebArena-like）  | Web-agent RL           | +26%                   | −23%            |
| B5 — 分析型 SQL（DuckDB TPC-H sf=1）     | text-to-SQL RL         | +53%                   | −37%            |
| B7 — 文本游戏（ALFWorld-like）           | Text-game / Embodied RL| +77%                   | −45%            |
| **B9 — 端到端混合 rollout**              | 真实 RL fan-out        | **+72%**               | **−44%**        |

最直接相关的数字是 **B9 的 −44%**：一次 RL fine-tune 跑数天不停，沙盒层从 m7i
换到 m9g，**每百万 rollout 的成本从 ~$33.2 降到 ~$18.7**；对应的 wall-clock，
c=128 一档 B9 p99 从 m7i 的 63.3 秒降到 m9g 的 40.1 秒（−37%），意味着同一个
batch size 下 trainer 等沙盒的时间也短了 1/3。

m8g（Graviton4）一档已经能拿到 m9g 收益的 ~70%（B9 c=64 −30% $/1M），且 m8g 的
hourly 单价比 m7i 直接便宜 11% —— 暂不上 Graviton5 的团队，仅"换 ISA 不换代次"
就能拿走绝大部分省钱空间。注意 m9g 的单价只比 m7i 便宜 2.9%（不是原稿写的 5.8%），
所以 m9g 的 cost win 主要来自吞吐，m8g 才是"单价 + 吞吐"双赢的那一档。

反过来 **m8i 的 hourly 比 m7i 贵 5%**，靠吞吐才能勉强打平 cost；这是常被忽略的
反直觉点：在 Agentic RL 沙盒这种 workload 形状上，"Intel 升一代" 不是免费午餐。

后面六章解释**为什么**沙盒层的形状偏好 Graviton、**实测**了什么、以及**怎么**
迁过去。

---

## 1. 什么是 Agentic RL

在讨论"把沙盒层搬到 Graviton 上能省多少钱"之前，先把这篇 blog 反复出现的
"Agentic RL"是什么定义清楚 —— 它和大家更熟悉的 RLHF 有什么区别、为什么它非要
起一个独立的 sandbox fleet。把这件事说清楚，后面所有关于成本、工作负载、实例
选型的讨论才有立足点。

### 1.1 从 RLHF 到 Agentic RL

过去两年最常见的"用 RL 训 LLM"是 RLHF / DPO 那一类：模型生成一段回答，reward
model 给一个标量打分，PPO 或 DPO 把这个标量回传去更新策略。轨迹是**单步**的 ——
一次 prompt → 一次 completion → 一次 reward —— 既没有"环境"，也没有"工具"和
"状态"。所有反馈都来自另一个神经网络。

Agentic RL（业界也叫 Agent RL / RL for LLM Agents）把这个设定扩展成**多步、
有状态、带工具**的形式：

- 一次 rollout 不再是一句话，而是一条**多步轨迹**（trajectory），由 policy
  和 environment 交替产出 `(state, action, observation, ...)` 序列；
- action 不是自由文字，而是结构化的**工具调用**：function call、API request、
  shell 命令、SQL、浏览器点击、代码片段……；
- observation 是工具调用的真实返回值，由一个**真实可执行的环境**给出，不是模型
  自己想象出来的；
- reward 通常是稀疏的、轨迹级别的（任务是否完成、单测是否通过、订单是否真的
  提交成功、SQL 答案是否正确），有时再叠加少量步骤级 shaping。

一句粗糙的总结：RLHF 训出来的是"能写"的模型，Agentic RL 训出来的是"能干活"的
模型。代表性工作覆盖一系列方向 —— 代码侧的 CodeRL / RLEF、工具调用侧的
ToolRL / τ-bench、Web agent 侧的 WebAgent-R1 / WebRL、数据分析侧的 text-to-SQL
RL，以及把这些能力混在一起做端到端 agent 的工作。它们 reward 函数和搜索算法
各异，但**沙盒层的形状几乎完全一致**，这正是这套 benchmark 想抓住的共性。

### 1.2 一次 Agentic RL training step 长什么样

把一次同步 RL fine-tune step 拆开看，大致是这五步：

1. Trainer 拿一批 task instructions（`bsz` 条）交给当前 policy；
2. 对每条 instruction 采样 `G` 条候选 rollout（GRPO 通常 8–16，PPO 通常 1–4）。
   每条 rollout 是 `T` 步交互，每一步 = "policy 生成一个 tool call → 环境执行 →
   把 observation 拼回 context → 进入下一步"；
3. 每条轨迹结束时由一个 verifier 或 reward model 给一个轨迹级 reward；
4. Trainer 用这些 `(traj, reward)` 算 advantage，跑 PPO / GRPO / RLOO / DPO-style
   的更新；
5. 进入下一步。

第 2 步 —— "采 `bsz × G × T` 次环境交互" —— 就是这篇 blog 反复在说的**沙盒层
负载**。规模直觉一下：典型设置 `bsz=64, G=8, T=20`，**单 step 就要约 10k 次
工具调用 / 代码执行 / 浏览器动作**。整个 fine-tune 跑几万 step，沙盒层做的工作
是数十亿次。这也是为什么"trainer 侧 GPU 时长"和"沙盒侧 vCPU 时长"在中等规模
的训练集群里常常落在同一个数量级。

### 1.3 为什么环境必须是"真"的

一个直觉但关键的点：Agentic RL 的 reward 信号必须是从环境里拿出来的**事实**，
而不是模型自评。如果环境是"假"的 —— 比如让 LLM 自己模拟工具返回值 —— 那 reward
就会被同一个模型的偏差污染，训出来的 policy 学到的是"骗过自己模拟器"，而不是
"在真实工具上完成任务"。所以：

- Code RL 要真把生成的代码丢进 CPython 跑，看单测是不是真的通过；
- Tool-use RL 要真起一组 stateful mock API，让 cart / order / refund 之间的状态
  依赖真实存在；
- Web-agent RL 要真起一个浏览器和一份网页，看 DOM 状态是不是真的变成了期望
  的样子；
- SQL agent 要真把生成的 SQL 丢进 DuckDB / Postgres 跑，跟 ground-truth 结果集
  对照；
- 文本游戏 agent 要真在一个有合法动作集合和 reward 函数的模拟器里走。

这就是为什么沙盒层不是"训练框架的一个附属容器"，而是一整个独立的、CPU 密集的、
和 GPU 训练侧规模相当的 fleet。它的负载形状不取决于具体哪一篇 paper 的 RL 算法,
取决于 agent 要操作什么样的真实环境 —— 这也是为什么我们可以用一组 archetype
去概括它，而不必去复现每一篇 RL 训练论文。

### 1.4 这篇 blog 的范围

定义清楚之后划一下边界：本文剩余篇幅全部聚焦在**沙盒层的成本**，**不**讨论
trainer 侧的 PPO / GRPO 实现、**不**讨论 reward model 设计、**不**讨论 reference
policy 的 KL 项、**不**讨论 inference engine 选型 —— 这些都是另外的故事。我们
盯的是一件事：在第 2 章定义的那个"CPU-only、fan-out 重、对 p99 敏感"的 fleet 上,
跑同一组真实 Agentic RL 工作负载时，不同 ISA 和不同代次的 EC2 实例谁更便宜、
谁更稳。

第 2 章先把沙盒层这个成本中心的形状画出来，第 3 章列出我们用来回答上面那个
问题的七个 workload archetype。

---

## 2. 沙盒层 —— Agentic RL 训练里被忽视的成本中心

第 1 章把 Agentic RL 一次 fine-tune step 拆成了五步。把这条管线画成模块图，
**沙盒层**到底是什么、它和 trainer 侧的 GPU fleet 是什么关系，就一目了然：

```
+------------------+        +-------------------------------+
| Trainer (GPU)    |  ----> |  N candidate rollouts          |
|  policy / value  |  <---- |  (each = 10..30 env steps)     |
+------------------+        +---------------+----------------+
                                            |
                                            v
                       +--------------------+--------------------+
                       |  Sandbox fleet (CPU, container per env) |
                       |   - python sandbox                      |
                       |   - tool-call mock APIs                 |
                       |   - headless browser                    |
                       |   - SQL warehouse                       |
                       |   - text-game simulator                 |
                       +-----------------------------------------+
```

Trainer 这一侧是 GPU bound 的：策略网络的 forward/backward、reward model 的推理、
可能还会有一份 reference policy。一次 RL step 里训练侧的 vCPU 占用基本可以忽略，
瓶颈在显存和卡间互联。但是要算 advantage、算 reward、算 PPO ratio，前提是先**把
候选轨迹真正跑出来** —— 一个 candidate rollout 是从某个状态出发、由当前策略产生
的 10–30 步交互序列，**每一步都需要落在一个真实可执行的环境上**：

- 一个能跑任意 Python 代码并隔离副作用的子进程（Code RL）；
- 一组带后端状态的 mock 工具 / 函数调用 API（Tool-use RL）；
- 一个真实的浏览器实例和待操作的网页（Web-agent RL）；
- 一个能跑分析型 SQL 的查询引擎（Data-science agent）；
- 一个文本游戏模拟器（Embodied / text-game RL）。

这一层就是"沙盒层 (sandbox tier)"。它有几个不太能改的特点，决定了它在成本结构里
的位置：

- **CPU 密集，不用 GPU**。这一层跑的是解释器、HTTP server、浏览器渲染、SQL 引擎，
  没有任何 BLAS / CUDA 路径。
- **fan-out 越大，vCPU 越多**。Trainer 一次想要 N 条候选轨迹，沙盒层就要并发开
  N 个隔离环境（容器或者沙盒进程），所以沙盒侧的 vCPU 消耗几乎和 batch size 成
  线性关系。GPU 侧的成本是次线性的，一张卡可以同时 batch N 条 rollout 的 forward；
  沙盒这边没有这种摊销，必须真的开 N 份。
- **对 p99 长尾极其敏感**。RL 同步训练的本质是"等最慢的那条 rollout"。1024 条并发
  里只要有一条卡在几十秒，整个 batch 的 wall-clock 就是几十秒。所以沙盒层不仅要
  看吞吐，还要看 p99。
- **不依赖任何 GPU 调度**。这一层完全跑在 CPU 实例上，可以独立选型、独立扩缩容、
  独立换 ISA。这是它和 trainer / inference 层最大的不同。

把这几个特点放一起就能看出，**沙盒层是整条 Agentic RL 训练管线里少有的"换 CPU
不用动模型代码"的地方**：负载是普通的容器化 Python / FastAPI / Chromium / DuckDB，
全都在 aarch64 上有一等公民支持，运行时语义和 x86 完全一致。

至于这一层值多少钱，直觉算一笔：一次 RL fine-tune 跑数天不停，每秒上百到上千条
rollout，中等规模的训练集群里沙盒侧 vCPU 总消耗常常和 trainer 侧 GPU 时长在同一
数量级。任何能在沙盒侧拿到的单位成本下降，都会**直接**折算成训练总成本下降，而且
不需要去碰 GPU 选型这种敏感的事情。

这就是为什么我们专门盯着沙盒层做这套 benchmark：它是一个**改造代价低、收益直接、
风险面小**的成本优化目标。

---

## 3. 七个工作负载原型

我们没有自己造 workload。每一个 archetype 都对应学术界或社区已经被广泛使用过的
一类 benchmark，并且都是 RL 训练中真实会跑的环境，只是被裁减成可以单容器化、
可以稳定复现的版本。

挑选标准三条：

- 每一个都要能映射到**至少一种已经发表过的 Agentic RL 训练范式**；
- 每一个都要**压住一类不一样的 CPU / 运行时瓶颈** —— 解释器 cold-start、JSON I/O、
  V8 JIT、SIMD 矢量化、syscall、长尾 fan-out 等等；
- 七个加起来覆盖大多数真实 Agentic RL 项目会用到的环境。

| ID | 负载              | 模拟的真实场景                                                    | 数据 / 灵感来源                                                | 主要压力点                                              |
|----|-------------------|------------------------------------------------------------------|----------------------------------------------------------------|---------------------------------------------------------|
| B1 | 代码执行          | LLM 生成的 Python 代码 → 落到隔离子进程里执行                       | OpenAI HumanEval (164) + Google MBPP-sanitized (427)，共 591 条  | CPython 解释器 cold-start、fork、subprocess syscall      |
| B3 | 工具调用 trajectory | Agent 按顺序调 8–15 个 mock 电商 API 完成一个目标                  | τ-bench retail；FastAPI + 进程内状态                             | 大量短 HTTP、JSON 解析、小对象分配、并发后端状态读写     |
| B4 | 浏览器 trajectory   | Playwright 驱动 Chromium 在自托管 SPA 上完成购物流程               | WebArena shopping；自带 60 商品的 SPA，~25 KB JS 包             | V8 JIT、Skia layout/paint、Chromium 多进程 IPC          |
| B5 | 分析型 SQL        | DuckDB **TPC-H sf=1**，22 条标准查询时间驱动循环                    | DuckDB 官方 `tpch` extension                                    | 列存矢量化（NEON/SVE vs AVX2/AVX-512）、内存带宽         |
| B7 | 文本游戏模拟      | 8 房间 + 30 物品 + 6 个 goal 的 minigrid，每 episode 30–50 步        | ALFWorld / TextWorld 风格，纯 Python                            | 多 worker 进程下的 Python 状态机、GIL 跨进程竞争         |
| B8 | 沙盒冷启动        | `docker run python:3.11-slim` × N 次，挂钟测启动开销                | 模拟"每条 rollout 一个全新沙盒"模式                              | 内核 syscall、cgroup 路径、容器运行时                    |
| B9 | 端到端混合 rollout | 每个 rollout 10–30 步，每步按 B3:50% / B1:20% / B4:10% / B5:10% / B7:10% 抽 task | 模拟真实 RL trainer 的 fan-out                                  | 长尾放大：rollout wall-clock 由最慢 step 决定            |

这七个里面，B1 / B3 / B4 / B5 / B7 是"典型 archetype"，是这篇 blog 的主要篇幅；
B8 是边界检验，看 Graviton 是否还在容器启动路径上有"启动税"；B9 是综合检验，把
前 5 个混在一起按真实 RL fan-out 跑，看的是 p99 长尾和 $/k rollouts 这两个最贴近
真实成本的指标。

每一个 archetype 都对应代码仓库里一个独立的 runner（`orchestrator/runners/b*.py`）
和一个独立的 worker 容器（`workers/b*-*/`），数据集 / 模板 / 随机种子全部在 image
build 阶段固化进去，运行时不依赖外网；每条结果 JSON 的 `extra` 字段会回写当时使用
的版本号、模板分布、SF 等等，保证可复盘。

> 当前仓库里 B8 已经从"容器冷启动"改写成**沙盒生命周期**测量
> （`b8_sandboxlife.py`：create / pause / resume / fork / delete 各自的延迟分布），
> 因为在 microVM 拓扑下"启动税"这件事的度量对象变了。本文表格里的 B8 描述对应
> Phase 1 的 `docker run` 版本。

我们也清楚 Agentic RL 还有一些常见 archetype 这次没有覆盖：例如 SWE-bench 那种
多文件代码修改（占位 ID 为 B2）、需要大模型做 judge 的 multi-agent 场景、对真实
第三方 API 的依赖等等。这些或者太重（SWE-bench 单条要 GB 级 image）、或者无法
离线复现，留作后续工作；当前这七个已经能把沙盒层主要的微架构差异展示清楚。

每一种 workload 选用的开源数据集和参考 benchmark 都在 README 中列出，license 与
原始仓库一致（HumanEval MIT / MBPP Apache-2.0 / DuckDB tpch ext Apache-2.0 等）。

---

## 4. 方法学

我们花了不少篇幅在确保这套对比是干净的。下面几条是关键设计选择，每一条背后都
有一个曾经把对比污染过的真实事故。

### 4.1 同一份镜像、同一个 multi-arch manifest

每一个 worker 容器在两个架构各自原生 build 一次（不用 emulation），分别推到 ECR
的 `:v1-amd64` 和 `:v1-arm64` tag，然后用 `docker buildx imagetools create` 合成
一个 `:v1` 的 multi-arch manifest。

x86 主机 `docker pull <image>:v1` 拉到的是 amd64 那一层，aarch64 主机拉到的是
arm64 那一层。Dockerfile、Python 包版本、apt 包版本、所有源码 —— 完全一致。
我们只在两件事上接受架构差异：DuckDB 自动选不同的 SIMD kernel（NEON/SVE 还是
AVX2/AVX-512），这是它内部的事；Chromium 自带不同架构的预编译二进制，我们没有
patch。

### 4.2 多进程 load generator —— 不要让 client 先撞墙

这是最容易翻车的一点，也是我们交学费最多的一处。一开始我们用单进程 asyncio
客户端打负载，发现两个架构不管换什么实例都顶到差不多 600–700 ops/s 就上不去了，
而且服务端 CPU 利用率始终偏低。问题不在 server，是 client：单条 asyncio 事件
循环的 raw HTTP 吞吐上限大约就在 5–7 万 req/s 这个量级，已经跟不上 16 vCPU 的
server。

修复是把客户端改成 multi-process：一个 orchestrator 容器里 spawn `nprocs ≈ vCPU/2`
个 child process，每个 child 跑独立 asyncio + uvloop，自己维护 httpx 连接池，把
counters 和 latency channel 通过 pipe 汇总到 parent。这样 client 永远不会先于
server 饱和。

修完之后 server 侧 CPU 才真正打满，两个架构的差距才显现出来。**任何不区分两侧
瓶颈的对比都是不可信的** —— 这条原则贯穿整套 benchmark 的所有 runner。

### 4.3 Server 侧也按需多进程

类似地，server 侧很多 worker 默认只起一个 uvicorn worker，这在 16 vCPU 实例上是
浪费。我们让 worker 进程数跟 vCPU 数挂钩（B1 / B3 / B7 等），让每个容器都能把
分配到的所有 vCPU 用满。

B4（Playwright + Chromium）比较特殊。在某些 aarch64 内核版本上，单一 uvicorn 进程
+ `SO_REUSEPORT` 的负载均衡 hash 不均匀，会出现一个 worker 接走绝大多数请求、
其他 worker 闲置的情况。我们的解法是 worker 端起 N 个独立的 uvicorn 进程，每个
绑独立端口，对外暴露 `/ports` 端点让客户端发现；客户端按 round-robin 主动轮询。
这样把内核层的负载均衡问题交给应用层显式解决，两个架构都用同一套机制。

### 4.4 并发扫描和稳态测量

每一个子项都跑一组并发档位，**B1/B3/B4/B5/B7/B9 都默认扫 `2, 8, 16, 32, 64,
128`**，让六个 archetype 直接可比；既覆盖 trainer 真实 fan-out 区间（c=32–128），
也保留低并发（c=2/8）用来分辨 cold-start 类瓶颈。每个档位的内部结构是：

- **warmup**：30 秒，**不计入测量**。这一段里完成 JIT 预热、连接池建立、TCP
  窗口 ramp、文件系统 cache 加热。warmup 末尾会主动 reset 资源采样器，让后续的
  CPU 利用率均值反映稳态。
- **measure**：默认 300 秒（B9 是 1800 秒）。这一段才计 throughput / latency /
  CPU。
- **cooldown**：60 秒，让所有 in-flight 请求清干净再进下一档位，避免相互污染。

B9 之所以拉到 30 分钟，是因为它的 p99 长尾受 B4 和 B5 的影响很重，几分钟的窗口
里 p99 抖动太大；30 分钟内的 p99 才是稳定数字。

### 4.5 资源采样口径

一个容易被忽略的细节：在容器内用 psutil 读 CPU% 会被 cgroup 的会计逻辑影响，
长时间稳态下会偏低，特别是当容器进程数多于 vCPU 数的时候。我们的资源采样器
**直接读宿主机的 `/proc/stat`**，拿到的是真实的物理 CPU 利用率；同时还单独记
一个 orchestrator 进程自己的 CPU%，方便诊断 client 侧瓶颈。

### 4.6 成本计算

每一份 (子项, 并发) 结果 JSON 都带一组 cost 字段：

- `instance_hourly_usd`：本文这批数据是从 `.env` 里读 `.4xlarge` 列表价按 vCPU
  **线性外推**得到的。AWS On-Demand 同 family 内确实严格线性 —— 用 Pricing API
  复核过四款机型，`12 × *.4xlarge` 与 `*.metal-48xl` 的误差是 **0.0%**。
  （当前代码已经不再外推，改成逐机型精确列价的 `INSTANCE_PRICES` 表，见
  Appendix B；外推本身没错，但原稿的 `m9g.4xlarge` 锚点值是错的。）
- `cost_per_1k_units_usd`：每千个 unit（unit 是该子项的主吞吐单位，例如 B1 是
  exec、B5 是 query、B9 是 rollout）的 dollar 成本。

把价格写在 `.env` 里有两个好处：一是新实例上线、价格变动不需要改代码；二是用户
可以根据自己的 RI / Savings Plans / Spot 折扣替换数字，立刻得到反映自己实际单价
的成本对比，而不是只能看 list price。

### 4.7 复现性和数据归档

每一份结果 JSON 包含：

- `instance.instance_type` / `arch` / region / AZ —— 全部由 IMDSv2 自动读取，
  不依赖手动填；
- 完整的 throughput / latency（含 per-query 或 per-task 拆分）/ resource / cost
  指标块；
- `extra` 里写入数据集大小、模板分布、随机种子、TPC-H scale factor、DuckDB 版本、
  Chromium 版本等，能够按 commit + 这一份 JSON 完整复盘当时的环境；
- 结果同时落本地 `./results/<arch>/...` 和 S3（可选 `S3_BUCKET`）。

最后由 `summary_*.json` 汇总，并自动生成一份单页 HTML 报告。报告会按 benchmark
拆吞吐单位（exec/s, traj/s, queries/s, rollouts/s ...），并对 B5 和 B9 这种带
sub-task 数据的负载额外渲染 per-query / per-task 的明细表。这样一份 summary 就
够把所有事实摆出来，不需要回头翻原始 JSON。

---

## 5. Results

下面按 B1 / B3 / B4 / B5 / B7 / B9 顺序逐个看 `.4xlarge` 上的实测数据。所有相对值
都以 **m7i.4xlarge 同一并发档位的吞吐为 baseline (×1.00)** 计算。每张吞吐表的
单位与第 3 章中各 archetype 的"主吞吐单位"一致：B1 是 executions/s、B3 是
traj/s、B4 是 traj/s、B5 是 queries/s、B7 是 steps/s、B9 是 rollouts/s。每个子项
还附一段 p99 latency 作为参照。

### 5.1 B1 — Code execution

B1 是公认"Intel 友好"的 archetype：CPython cold-start、单线程、分支密集，历来
吃高频 x86 核。低并发段确实如此 —— c=2 时三家 Graviton 里只有 m9g 比 m7i 快。
但 B1 的瓶颈在高并发段会**从单核 IPC 转到 fork-per-second**，结果 Graviton 后两
代直接拉开。

吞吐 (executions/s)：

| Instance       | c=2  | c=8   | c=16   | c=32   | c=64   | c=128  |
|----------------|------|-------|--------|--------|--------|--------|
| m7i.4xlarge    | 134.2| 455.5 | 735.2  | 725.1  | 705.4  | 742.0  |
| m8i.4xlarge    | 146.6| 565.8 | 820.8  | 812.0  | 787.3  | 811.6  |
| m8g.4xlarge    | 126.6| 448.6 | 814.7  | 883.8  | 879.8  | 915.0  |
| **m9g.4xlarge**|**160.1**|**572.0**|**1042.1**|**1113.8**|**1089.0**|**1138.6**|

vs m7i 同档位吞吐比：

| Concurrency | m8i  | m8g  | m9g      |
|-------------|------|------|----------|
| 2           | 1.09 | 0.94 | **1.19** |
| 8           | 1.24 | 0.98 | **1.26** |
| 16          | 1.12 | 1.11 | **1.42** |
| 32          | 1.12 | 1.22 | **1.54** |
| 64          | 1.12 | 1.25 | **1.54** |
| 128         | 1.09 | 1.23 | **1.53** |

几个值得单独说的点：

- m7i 在 c=16 就基本撞墙（735 → c=128 才 742，几乎不再增长），m9g 一路涨到
  ~1140，**c=32 起 m9g 稳定比 m7i 快 50% 以上**。
- m8g 全程不弱于 m8i，从 c=16 起反超并保持 ~10–15% 的优势。
- p99 在 c=128：m7i 345.8 ms vs m9g **328.4 ms**，Graviton5 在 Intel 最有利的负载
  上 p99 也已经持平偏低；c=32 之后 m9g 的 p99 持续比 m7i 低 5–10%。

反直觉的是：B1 这种应该最吃单核频率的 workload，c≥16 之后核数多的 Graviton5 完
胜。原因前面 §1 提过：每条 exec 是"fork sandbox + 启 CPython + 跑代码 + 收子进程",
整链 syscall 重，**核数更多 = 同时能开更多 fork**，单核 IPC 反而不是瓶颈。

> ![chart-B1-throughput-by-instance.png](placeholder)

### 5.2 B3 — Tool-call trajectory

τ-bench 风格的电商工具调用：search → compare → add to cart → checkout → refund，
平均 11.7 calls / traj。server 持有 per-user lock 的 in-memory state，wire 是
uvicorn 上的 JSON。

吞吐 (traj/s)：

| Instance       | c=2   | c=8    | c=16   | c=32    | c=64    | c=128   |
|----------------|-------|--------|--------|---------|---------|---------|
| m7i.4xlarge    | 251.1 | 470.3  | 688.6  | 769.9   | 823.4   | 770.1   |
| m8i.4xlarge    | 225.8 | 602.9  | 787.3  | 975.8   | 1099.1  | 1061.1  |
| m8g.4xlarge    | 277.9 | 786.3  | 943.1  | 1248.8  | 1449.8  | 1395.3  |
| **m9g.4xlarge**|**444.7**|**1202.5**|**1476.8**|**1804.2**|**2067.6**|**2055.1**|

vs m7i：

| Concurrency | m8i  | m8g  | m9g      |
|-------------|------|------|----------|
| 2           | 0.90 | 1.11 | **1.77** |
| 8           | 1.28 | 1.67 | **2.56** |
| 16          | 1.14 | 1.37 | **2.14** |
| 32          | 1.27 | 1.62 | **2.34** |
| 64          | 1.33 | 1.76 | **2.51** |
| 128         | 1.38 | 1.81 | **2.67** |

这是这套 suite 里 Graviton 优势最大的 archetype：

- **m9g 几乎全程是 m7i 的 2.1–2.7×**，c=128 拿到峰值 +167%。
- m8g 也稳稳 +37% 以上，c=64 起逼近 +80%。
- p99 同步压下来：c=64 一档 m7i 161.8 ms / m8i 123.4 ms / m8g 96.7 ms /
  **m9g 64.0 ms** —— m9g 的 p99 直接是 m7i 的 ~40%。

为什么这么夸张：B3 是干净的 "fan-out HTTP + 一点 Python state machine"，没有
SIMD、没有 cold-start，瓶颈完全是核数 × 每核能跑多少 context switch。Graviton
的 vCPU = 物理核（无 SMT），m7i 的 16 vCPU = 8 物理核 × 2 SMT；同等"逻辑核数"下
Graviton 提供的物理并行多 2×，恰好跟实测 ratio 对得上。

### 5.3 B4 — Browser trajectory

Playwright 驱动 Chromium 在自托管 SPA 上跑购物流程，是 Graviton 优势最不明显的
那个 archetype —— 因为单条 traj 的 wall-clock 大头花在 layout/paint/JS 执行，
换 ISA 换不掉太多 ms。

吞吐 (traj/s)：

| Instance       | c=2  | c=8   | c=16  | c=32  | c=64  | c=128  |
|----------------|------|-------|-------|-------|-------|--------|
| m7i.4xlarge    | 2.43 | 9.50  | 17.15 | 26.46 | 26.90 | 24.14  |
| m8i.4xlarge    | 2.56 | 10.22 | 18.68 | 27.61 | 28.02 | 28.62  |
| m8g.4xlarge    | 2.45 | 9.79  | 18.30 | 26.85 | 27.36 | 27.26  |
| **m9g.4xlarge**|**2.66**|**10.71**|**20.17**|**31.38**|**34.00**|**34.52**|

vs m7i：

| Concurrency | m8i  | m8g  | m9g      |
|-------------|------|------|----------|
| 2           | 1.05 | 1.01 | 1.10     |
| 8           | 1.08 | 1.03 | 1.13     |
| 16          | 1.09 | 1.07 | 1.18     |
| 32          | 1.04 | 1.01 | 1.19     |
| 64          | 1.04 | 1.02 | **1.26** |
| 128         | 1.19 | 1.13 | **1.43** |

低并发段（c≤16）四家几乎打平，差距被 Chromium 启动 + page load 自身吃掉。但高
并发段会显出来，**特别是 m7i 在 c=32 之后开始回退**（26.9 → 26.5 → 24.1，CPU 已
经跑满），m8g/m9g 仍在涨：c=128 时 m9g 是 m7i 的 1.43×。p99 同向：c=128 一档
m7i 9.56 s vs **m9g 8.13 s**，m9g 低 ~15%。

### 5.4 B5 — Analytical SQL on DuckDB (TPC-H sf=1)

DuckDB 在 x86 上走 AVX2/AVX-512、在 aarch64 上走 NEON/SVE，加上 in-process
execution 没有 serialization 开销，是最直接吃 ISA 的 archetype。

吞吐 (queries/s)：

| Instance       | c=2   | c=8    | c=16   | c=32   | c=64   | c=128  |
|----------------|-------|--------|--------|--------|--------|--------|
| m7i.4xlarge    | 49.48 | 70.09  | 72.25  | 72.34  | 71.88  | 72.15  |
| m8i.4xlarge    | 60.40 | 86.13  | 88.79  | 88.42  | 88.50  | 87.20  |
| m8g.4xlarge    | 74.51 | 116.49 | 115.14 | 84.24  | 90.71  | 97.27  |
| **m9g.4xlarge**|**97.62**|**145.78**|**139.50**|**99.84**|**110.26**|**112.85**|

vs m7i：

| Concurrency | m8i  | m8g  | m9g      |
|-------------|------|------|----------|
| 2           | 1.22 | 1.51 | **1.97** |
| 8           | 1.23 | 1.66 | **2.08** |
| 16          | 1.23 | 1.59 | **1.93** |
| 32          | 1.22 | 1.16 | 1.38     |
| 64          | 1.23 | 1.26 | **1.53** |
| 128         | 1.21 | 1.35 | **1.56** |

几个观察：

- 低并发段（c≤16）m9g 是 m7i 的 ~2×，m8g 也稳定 +60% 左右 —— DuckDB 的 NEON/SVE
  kernel 在 TPC-H 上确实顶用。
- p99 同步更低：c=8 时 m7i 273.8 ms / m8g 180.9 ms / **m9g 145.2 ms**。
- c=32 这一档需要单独说一下：**Graviton 两代的吞吐反而从 c=16 的 ~140 掉到
  ~100**，CPU 利用率跌到 70–75%，x86 那侧没这个现象。原因不是硬件，是 DuckDB
  自管线程（`DUCKDB_THREADS=auto`）+ 单 uvicorn worker + 多 client 时，引擎内部
  线程池和外部 client 并发互相挤；这条曲线随 DuckDB 升版应该会改善。即便有这个
  "凹槽"，c=32 的 m9g 仍然是 m7i 的 1.38×。

### 5.5 B7 — Sim text game (ALFWorld-style)

8 rooms × 30 items × 6 goal templates 的纯 Python 模拟，每 HTTP 请求 batch 10
episodes，多 uvicorn worker。本质就是"主机能并行跑多少个 CPython 解释器循环不
打架"。

吞吐 (steps/s)：

| Instance       | c=2     | c=8       | c=16       | c=32       | c=64       | c=128      |
|----------------|---------|-----------|------------|------------|------------|------------|
| m7i.4xlarge    | 10843.2 | 17529.0   | 28705.6    | 35941.6    | 36232.0    | 32419.6    |
| m8i.4xlarge    | 10214.9 | 21747.7   | 37613.4    | 41091.1    | 42581.7    | 42952.6    |
| m8g.4xlarge    | 11041.5 | 30456.8   | 41030.3    | 45079.2    | 46146.2    | 47132.6    |
| **m9g.4xlarge**|**15361.5**|**25995.9**|**56112.5** |**62556.6** |**64167.1** |**65875.0** |

vs m7i：

| Concurrency | m8i  | m8g      | m9g      |
|-------------|------|----------|----------|
| 2           | 0.94 | 1.02     | **1.42** |
| 8           | 1.24 | **1.74** | 1.48     |
| 16          | 1.31 | 1.43     | **1.96** |
| 32          | 1.14 | 1.25     | **1.74** |
| 64          | 1.18 | 1.27     | **1.77** |
| 128         | 1.32 | 1.45     | **2.03** |

- c=128 这档 **m9g 是 m7i 的 ~2×**，65k vs 32k steps/s。
- 更重要的是 **m7i 在 c=64 之后已经回退**（36k → 32k），m9g 仍在线性涨到 65k；
  m8g 也一直涨到 c=128 没饱和。
- p99 在 c=128：m7i 69.4 ms / m8g 45.8 ms / **m9g 37.0 ms**，m9g 低 ~47%。
- c=8 一档 m8g 短暂反超 m9g（30457 vs 25996），是个有意思的 outlier，可能是
  worker 数 / HTTP routing 在该并发下偏好 m8g 的核分布；c=16 起两者关系恢复
  正常。

这一组就是"物理核多就赢"的极简故事 —— 纯 Python state machine 不吃 SIMD，跑得
快慢就看主机能稳定承载多少个 worker process。

### 5.6 B9 — End-to-end concurrent rollout

每条 rollout 10–30 步，按 `{B3:0.50, B1:0.20, B4:0.10, B5:0.10, B7:0.10}` 抽
sub-task；rollout 完成 = 最慢 step 完成。**这是这套 benchmark 里最贴近真实
RL trainer 的一组数字**：RL 同步训练等的就是 batch 里最慢的那条 rollout，所以
要看 rollouts/s 也要看 p99。

吞吐 (rollouts/s)：

| Instance       | c=2  | c=8  | c=16 | c=32   | c=64   | c=128  |
|----------------|------|------|------|--------|--------|--------|
| m7i.4xlarge    | 0.93 | 3.52 | 6.12 | 7.20   | 6.74   | 6.67   |
| m8i.4xlarge    | 1.02 | 3.84 | 6.81 | 8.63   | 7.96   | 7.49   |
| m8g.4xlarge    | 0.99 | 3.78 | 6.86 | 9.22   | 8.57   | 7.92   |
| **m9g.4xlarge**|**1.10**|**4.24**|**7.89**|**11.90**|**11.60**|**10.82**|

vs m7i：

| Concurrency | m8i  | m8g  | m9g      |
|-------------|------|------|----------|
| 2           | 1.10 | 1.06 | 1.18     |
| 8           | 1.09 | 1.07 | 1.20     |
| 16          | 1.11 | 1.12 | **1.29** |
| 32          | 1.20 | 1.28 | **1.65** |
| 64          | 1.18 | 1.27 | **1.72** |
| 128         | 1.12 | 1.19 | **1.62** |

- m9g 在 c=32 取得峰值 **11.90 rollouts/s**，比 m7i 的 7.20 高 +65%；c=64 一档
  仍能稳在 11.60，**比 m7i 高 +72%**。
- p99 在 c=32：m7i **13.52 s** vs m9g **9.16 s**，p99 直接低 32%。
- m7i 在 c=32 之后开始倒退（7.20 → 6.74 → 6.67），m9g 仍能稳在 10.8 以上；也就是
  说，沙盒侧实际能"喂饱"的训练 batch 大小，**Graviton5 比 Intel 同代翻一倍都不
  止**。
- m8g vs m8i：c=32 起 m8g 稳定领先 m8i 7–10%，等价于"换 ISA 不换代次"也已经
  能拿到一截收益。

p99 长尾在三家 Graviton 上都明显比 m7i 短 —— c=128 一档 m7i 63.3 s / m8i 56.7 s /
m8g 52.3 s / m9g **40.1 s**，意味着同一个 batch size 下 Graviton5 的 wall-clock
就是 Intel 的 ~63%。这一项的收益直接折算成训练侧 GPU 的等待时间。

> ![chart-B9-rollouts-per-second.png](placeholder)
> ![chart-B9-cost-per-1k.png](placeholder)

### 小结

把 6 个 archetype 在每个并发档位上的"m9g vs m7i 吞吐比"汇成一张图：

| Benchmark | c=2  | c=8  | c=16 | c=32 | c=64 | c=128 |
|-----------|------|------|------|------|------|-------|
| B1        | 1.19 | 1.26 | 1.42 | 1.54 | 1.54 | 1.53  |
| B3        | 1.77 | 2.56 | 2.14 | 2.34 | 2.51 | 2.67  |
| B4        | 1.10 | 1.13 | 1.18 | 1.19 | 1.26 | 1.43  |
| B5        | 1.97 | 2.08 | 1.93 | 1.38 | 1.53 | 1.56  |
| B7        | 1.42 | 1.48 | 1.96 | 1.74 | 1.77 | 2.03  |
| B9        | 1.18 | 1.20 | 1.29 | 1.65 | 1.72 | 1.62  |

读这张表的方式：**m9g 在每一个 archetype、每一个并发档位上都比 m7i 快**，最少
+10%（B4 低并发）、最多 +167%（B3 高并发）。在 RL trainer 真正会跑的高并发档位
（c≥32），m9g 对 m7i 的吞吐倍数几乎都在 **1.4×–2.7×** 之间。这才是后面第 6 章
"成本视角"能打出 15–45% $/1k rollouts 节省的物理依据。

---

## 6. 成本视角

把第 5 章的吞吐数字乘上 us-east-1 的 On-Demand 单价，就能把"Graviton 跑得快"翻
译成"沙盒层每百万次操作便宜多少"。这一章给一组对应的 **$/1M ops** 数据，并按
m7i.4xlarge 同档位为 baseline 给出节省比例。

### 6.1 .4xlarge On-Demand 单价（us-east-1）

| Instance       | $/hr     | vCPU | $/vCPU-hr | vs m7i      |
|----------------|----------|------|-----------|-------------|
| m7i.4xlarge    | 0.80640  | 16   | 0.05040   | baseline    |
| m8i.4xlarge    | 0.84672  | 16   | 0.05292   | **+5.0%**   |
| m8g.4xlarge    | 0.71808  | 16   | 0.04488   | **−11.0%**  |
| m9g.4xlarge    | 0.78272  | 16   | 0.04892   | **−2.9%**   |

第一件需要单独点出来的事：**m8i 比 m7i 贵 5%**。Intel 换代不是免费的 —— m8i 必须
靠吞吐 / 单价压差把这 5% 涨幅赚回来才能在 cost 上不亏。

第二件：**m8g 比 m7i 便宜 11%，但 m9g 只便宜 2.9%**。Graviton 升代次是要加价的
（m9g 比 m8g 贵 9%），所以 m9g 的 cost win 几乎全部来自吞吐，而 m8g 是"单价 +
吞吐"两头都占的那一档。这一点和很多人的直觉相反，也是原稿写错的地方 —— 早期草稿
用了 $0.7600 这个错的 m9g 锚点，把 m9g 的单价优势夸大成了 5.8%。

换算公式：

```
$/1M ops = (hourly_usd / 3600) × 1e6 / throughput_per_sec
```

预先把每个实例的"单位换算系数"算出来，方便对照后面所有表格：

| Instance | factor = hourly × 1e6 / 3600 |
|----------|------------------------------|
| m7i      | 224.00                       |
| m8i      | 235.20                       |
| m8g      | 199.47                       |
| m9g      | 217.42                       |

### 6.2 各 archetype 的 $/1M ops

下面六张表，单位都是 USD per 1,000,000 ops，最右一列是 m9g 相对 m7i 的节省比例。

#### B1 — Code execution（$/1M executions）

| Concurrency | m7i   | m8i   | m8g   | m9g   | m9g vs m7i |
|-------------|-------|-------|-------|-------|------------|
| 2           | 1.669 | 1.604 | 1.576 | 1.358 | −18.6%     |
| 8           | 0.492 | 0.416 | 0.445 | 0.380 | −22.7%     |
| 16          | 0.305 | 0.287 | 0.245 | 0.209 | −31.5%     |
| 32          | 0.309 | 0.290 | 0.226 | 0.195 | **−36.8%** |
| 64          | 0.318 | 0.299 | 0.227 | 0.200 | **−37.1%** |
| 128         | 0.302 | 0.290 | 0.218 | 0.191 | **−36.7%** |

高并发段 m9g 稳定省 ~37%，m8g 也能稳定省 ~28% —— 注意 m8g 用不到 m9g 的吞吐就
拿到了接近的 cost win，因为它的单价便宜得多。

#### B3 — Tool-call trajectory（$/1M trajectories）

| Concurrency | m7i   | m8i   | m8g   | m9g   | m9g vs m7i |
|-------------|-------|-------|-------|-------|------------|
| 2           | 0.892 | 1.042 | 0.718 | 0.489 | −45.2%     |
| 8           | 0.476 | 0.390 | 0.254 | 0.181 | **−62.0%** |
| 16          | 0.325 | 0.299 | 0.212 | 0.147 | −54.7%     |
| 32          | 0.291 | 0.241 | 0.160 | 0.121 | **−58.6%** |
| 64          | 0.272 | 0.214 | 0.138 | 0.105 | **−61.3%** |
| 128         | 0.291 | 0.222 | 0.143 | 0.106 | **−63.6%** |

整套 suite 的最大赢家。c=128 一档 m9g 的 $/1M 是 m7i 的 ~36%（**省 64%**）。这跟
§5.2 的 2.6× 吞吐是同一件事的两面。

> ⚠️ **方法学缺陷**：这一组数据里所有 trajectory 共用**一个** mock-API 进程，
> 且都以 `user="alice"` 发起，在同一把锁上串行。也就是说这里量到的"Graviton 优势"
> 有相当一部分是**锁竞争下的调度差异**，而不是纯粹的 per-trajectory 吞吐。
> 倍数（2.6×）应当视为这个特定共享拓扑的结果，不要当成 per-rollout 沙盒的预期值。

特别注意 m8i 在 c=2 反而比 m7i 贵（1.042 vs 0.892，**+17%**）—— hourly 涨了 5% 又
没涨吞吐，双输。"换 Intel 新一代"在这种低并发负载上是负收益。

#### B4 — Browser trajectory（$/1M trajectories）

| Concurrency | m7i   | m8i   | m8g   | m9g   | m9g vs m7i |
|-------------|-------|-------|-------|-------|------------|
| 2           | 92.18 | 91.88 | 81.41 | 81.74 | −11.3%     |
| 8           | 23.58 | 23.01 | 20.37 | 20.30 | −13.9%     |
| 16          | 13.06 | 12.59 | 10.90 | 10.78 | −17.5%     |
| 32          |  8.47 |  8.52 |  7.43 |  6.93 | −18.2%     |
| 64          |  8.33 |  8.39 |  7.29 |  6.39 | **−23.2%** |
| 128         |  9.28 |  8.22 |  7.32 |  6.30 | **−32.1%** |

B4 是 Graviton 优势最小的 archetype，但即便如此，m9g 在 c=128 也能省 32%。低并发
段（c≤8）m9g 甚至**略贵于 m8g**（81.74 vs 81.41）—— 吞吐差距还没拉开，m9g 那 9%
的单价溢价就直接体现出来了。注意
m8i 在 c≤32 的 $/M 几乎和 m7i 持平甚至更高 —— Intel 同代之间的 cost win 几乎
靠不住。

另外，B4 的绝对 $/M 比其他 archetype 高 1–2 个数量级（每条 traj 是秒级 wall-clock，
不是毫秒级），所以**在混合 workload 里 B4 是 cost 的大头**。降 B4 的 $/M，对 B9
总成本的影响是放大的。

#### B5 — Analytical SQL on DuckDB（$/1M queries）

| Concurrency | m7i   | m8i   | m8g   | m9g   | m9g vs m7i |
|-------------|-------|-------|-------|-------|------------|
| 2           | 4.527 | 3.894 | 2.677 | 2.227 | **−50.8%** |
| 8           | 3.196 | 2.731 | 1.712 | 1.491 | **−53.3%** |
| 16          | 3.100 | 2.649 | 1.732 | 1.559 | −49.7%     |
| 32          | 3.096 | 2.660 | 2.368 | 2.178 | −29.7%     |
| 64          | 3.116 | 2.658 | 2.199 | 1.972 | −36.7%     |
| 128         | 3.105 | 2.697 | 2.051 | 1.927 | −37.9%     |

低并发段 m9g 直接打半价（−51% 到 −53%）。c=32 那个"凹槽"仍让 m9g 比 m7i 便宜 30%。

> ⚠️ **方法学缺陷**：这一组的所有并发档位共用**同一个** `--workers 1` 的 DuckDB
> 进程。所以 c=32 的凹槽不是"DuckDB 引擎线程 vs client 并发挤占"这种引擎特性，
> 而是**单进程 server 撞顶**这个更朴素的原因（§5.4 原文的解释是错的）。它不会
> "随 DuckDB 升版自动改善"，要靠多进程 server 或 per-rollout 沙盒来解决。

#### B7 — Sim text game（$/1M steps）

| Concurrency | m7i      | m8i      | m8g      | m9g      | m9g vs m7i |
|-------------|----------|----------|----------|----------|------------|
| 2           | 0.02066  | 0.02303  | 0.01807  | 0.01415  | −31.5%     |
| 8           | 0.01278  | 0.01081  | 0.00655  | 0.00836  | −34.6%     |
| 16          | 0.00780  | 0.00625  | 0.00486  | 0.00387  | **−50.3%** |
| 32          | 0.00623  | 0.00572  | 0.00442  | 0.00348  | −44.2%     |
| 64          | 0.00618  | 0.00552  | 0.00432  | 0.00339  | −45.2%     |
| 128         | 0.00691  | 0.00548  | 0.00423  | 0.00330  | **−52.2%** |

注意一个反直觉的点：**m7i 在 c=128 的 $/M 反而比 c=32–64 还高 ~12%**（因为吞吐
已经从 36k 退到 32k，price 不变）。这是 RL trainer 运维需要警惕的：fan-out 不
是越大越省 —— 在饱和点之后，Intel 同一台机器的"边际单位成本"会反弹，而 m9g 因
为还没饱和，c=128 反倒是它最便宜的档位。

#### B9 — End-to-end concurrent rollout（$/1M rollouts）

| Concurrency | m7i    | m8i    | m8g    | m9g    | m9g vs m7i |
|-------------|--------|--------|--------|--------|------------|
| 2           | 240.86 | 230.59 | 201.48 | 197.66 | −17.9%     |
| 8           |  63.64 |  61.25 |  52.77 |  51.28 | −19.4%     |
| 16          |  36.60 |  34.54 |  29.08 |  27.56 | **−24.7%** |
| 32          |  31.11 |  27.25 |  21.63 |  18.27 | **−41.3%** |
| 64          |  33.23 |  29.55 |  23.27 |  18.74 | **−43.6%** |
| 128         |  33.58 |  31.40 |  25.19 |  20.09 | **−40.2%** |

**这是整章最重要的一张表**：B9 的形状就是 RL trainer 真实 fan out 的形状，`c≥32`
是真实训练 batch 的工作区间。这个区间内：

- **m7i → m8g（同代换 ISA）：$/1M rollouts 降 ~30%**。
- **m7i → m9g（换 ISA 再升一代）：$/1M rollouts 降到 m7i 的 ~56%（−44%）**。
- m8i 也能省 ~12%，但远不如同代换 ISA 划算。
- m8g → m9g 只再多省 ~14 个百分点，而 m9g 单价贵 9% —— **升代次的边际收益远小于
  换 ISA 的收益**，预算敏感的团队停在 m8g 是合理的。

### 6.3 同档位的 $/1M ops 收敛视图

把 6 个 archetype 取各自的"工作并发档位"（多数 archetype 的吞吐稳态 c=64，B4 取
c=128）放到一起：

| Archetype | 档位 | m7i $/1M | m8i $/1M | m8g $/1M | m9g $/1M | m9g vs m7i |
|-----------|------|----------|----------|----------|----------|------------|
| B1        | c=64 | 0.318    | 0.299    | 0.227    | 0.200    | **−37%**   |
| B3        | c=64 | 0.272    | 0.214    | 0.138    | 0.105    | **−61%**   |
| B4        | c=128| 9.279    | 8.218    | 7.318    | 6.300    | **−32%**   |
| B5        | c=64 | 3.116    | 2.658    | 2.199    | 1.972    | **−37%**   |
| B7        | c=64 | 0.00618  | 0.00552  | 0.00432  | 0.00339  | **−45%**   |
| **B9**    | c=64 | **33.23**| **29.55**| **23.27**| **18.74**| **−44%**   |

> ![chart-cost-per-1m-by-benchmark.png](placeholder)
> ![chart-b9-cost-per-1m-vs-concurrency.png](placeholder)

读这张表的几个要点：

- **B9 的 −44% 是这次迁移真实可期的训练成本节省**。B1–B7 是 archetype 拆开测的
  "教学组件"，但训练 trainer 每秒 fan out 的是 B9 形状的 rollout，所以 B9 的
  $/1M 才是 RL 训练实际 pay 的钱。沙盒层从 m7i 迁到 m9g，每百万 rollout 直接
  从 ~$33.2 降到 ~$18.7。
- **m8g 也已经能省 ~30%**。如果团队不愿意立刻上 Graviton5（想再观察稳定性、想
  分阶段迁移），仅仅"x86 m7i → aarch64 m8g"换 ISA 就能拿走大部分收益。
- **m8i 的 cost win 完全靠吞吐**。Intel 新代单价更贵，必须吞吐涨幅 ≥ 5% 才不亏；
  在 B4 这种"ISA 不敏感"的负载上 m8i 几乎打不赢 m7i（c=2/32 一档甚至略亏）。
- **B4 单条绝对值最大**。一条 browser traj 的 wall-clock 是 6–9 秒（vs B3 一条
  ~0.5 秒），混合 workload 里 B4 是 cost 大头；优先让 B4 落到 m9g 上，对 B9 总
  成本的影响是放大的。

### 6.4 同 family 内 size 是线性外推

AWS On-Demand 在同 family 内严格 vCPU-linear（`m9g.16xlarge` ≡ 4 × `m9g.4xlarge`，
`m9g.48xlarge` ≡ 12 × `m9g.4xlarge`），所以本章所有 $/1M 数字在 .8xlarge / 16xl /
24xl / 48xl 上都成立 —— **选 size 的依据是"单实例承载多少 fan-out"，不是"哪个
size 单位成本最低"**。

实测看，单实例的"最佳并发"点在 c=32–64：吞吐峰值已经撞顶、p99 还没爆炸，再加并发
单位成本反而上升。所以更大的 batch fan-out 应当用 ASG 横向扩展（多实例 × c=32–64
每实例），不是单实例堆 size。每个实例选 .4xlarge 或 .8xlarge，按你 trainer 的
fan-out 总量除以"单实例 64–128 容量"决定实例数即可。

### 6.5 一些 caveats

- 全表用的是 us-east-1 OD list price。Reserved / Savings Plans / Spot 没考虑：
  Graviton spot 容量历史上比 Intel 同代更宽，折扣更稳，所以**本章给出的 cost gap
  是保守估计**，真实场景下 Graviton 会更划算。
- DuckDB c=32 凹槽（B5）会随版本升级改善 —— 即便不改也已经 −32%，改善之后 B5 的
  cost win 还会再扩。
- 不包含 instance 启动 / AMI 拉取 / EBS 等 ops 成本，假设是常驻 fleet 的稳态运行。
- 跨 region（比如 ap-southeast-1）单价不同，但同 region 内三种 ISA 的相对差通常
  一致 —— 节省比例可以照搬，绝对值需要按本地价目重算。

把这五章合在一起的结论很简单：在本文工作负载下，**沙盒层从 m7i 迁到 m9g.4xlarge,
$/1M rollouts 直接降到原来的 ~56%；如果只能换到 m8g，也能降到 ~70%**。这是一笔
不需要碰任何 trainer / model 代码就能拿到的钱。

最后再强调一次本文的适用边界：以上全部基于**容器 + 常驻共享 worker**的拓扑。
B3 和 B5 两组数据带有共享进程的方法学缺陷（见各自小节的标注），per-rollout
microVM 拓扑下的数字需要重测才能引用。

---

## 7. 为什么沙盒层天然适合 Graviton

把第 5 章的吞吐和第 6 章的成本放到一起，沙盒层落到 Graviton 上能赢的几条第一性
原理：

1. **负载形状是 fan-out HTTP，不是 dense matmul**。每一个 archetype 都是"很多
   并发的小请求，每个请求跑一段 Python / Chromium / SQL"。这恰好是 Graviton
   高核数 × 每核低功耗的设计点。最直接的证据：B3 c=128 m9g 是 m7i 的 **2.67×**,
   B7 c=128 是 **2.03×** —— 两个最纯的 fan-out workload 上，Graviton 优势最大。

2. **DuckDB 等现代分析引擎在 aarch64 是一等公民**。SVE/NEON 向量化 kernel 由
   上游维护，整套 benchmark 没有 patch 任何一行 DuckDB。B5 低并发段 m9g 是 m7i
   的 ~2×，证明 NEON/SVE 在 TPC-H 上确实顶用，**不是"勉强能跑 aarch64"，是真的
   有调过的 SIMD path**。

3. **Graviton5 在 Intel 最强的 workload 上也能反超**。B1（CPython cold-start，
   Intel 传统强项）c=128 m9g 比 m7i 快 53%、p99 反而更低 —— 因为这一档已经从
   "单核 IPC bound" 转到 "fork-per-second bound"，核数多就是赢。也就是说：连
   "Intel 一定赢"的负载，在真实 RL trainer 的并发档位上 Intel 都不一定赢。

4. **每 vCPU-hour 单价就低于 Intel**。m8g 比 m7i 便宜 11%、m9g 便宜 5.8% —— 即使
   吞吐打平，光换 ISA 已经省了一截。反过来 m8i 比 m7i 贵 5%，这是 Intel 同代
   换代不能忽视的一笔"涨价"。

5. **同 family vCPU-linear 让选 size 极简**。AWS OD 在同 family 内严格 vCPU
   线性，没有"哪个 size 单位成本最低"这种问题。选 size 只看一件事：**你需要
   单实例承载多少 fan-out**。.4xlarge 在 c=32–64 是吞吐 / p99 最甜的点，再大
   就 ASG 横扩。

唯一一个 Intel 还能稳住的角落：B1 在 c≤8 的低并发段，m8i 比 m9g 快一点（解释器
单核 boost）。但这是个非常窄的窗口，c=16 起 m9g 反超，且 RL trainer 真实并发都
在 c≥32 —— 那个区间 Intel 没有任何剩余优势。

---

## 8. 工程踩坑笔记

下面这些细节决定了这套 benchmark 是真实的还是骗自己的。每一条背后都有一次把对比
污染过的事故：

- **单进程 asyncio 是 server 之前的瓶颈**。一条 event loop 的 raw HTTP 上限大约
  在 5–7 万 req/s，已经跟不上 16 vCPU 的 server。让 client 按
  `multiprocessing.spawn` 起每 vCPU 一进程，每个 archetype 的真实吞吐才显现得
  出来。我们把这件事踩在 §4.2 里，不再在每个 runner 重复。
- **`SO_REUSEPORT` hash 在某些 aarch64 内核版本上不均匀**。B4 上一开始一个
  Chromium worker 接走绝大多数请求，其他 worker 闲置。修法：worker 端起多 port
  （每个 uvicorn 进程独立绑端口、暴露 `/ports`），client 严格 round-robin。把
  内核层的负载均衡问题交给应用层显式解决，两个架构都用同一套机制。
- **沙盒子进程用 `prlimit` 而不是 `preexec_fn=set_limits`**。`preexec_fn` 持有
  CPython subprocess fork lock，会把整个 server 的 fork 串行化（B1 c=128 一开始
  压不上来就是这个原因）。`prlimit` 是内核 syscall 的薄包装，不影响 fork-storm。
- **数据集和引擎扩展一律 build-time bake**。任何 first-run 才下载的东西都会让
  c=2 看起来比 c=8 慢 10×（warmup 没覆盖到）。我们把 HumanEval + MBPP 和 DuckDB
  `tpch` extension 都焊进 image，运行时 0 外网依赖。
- **CPU% 读宿主机 `/proc/stat`**。psutil 在容器内的视图被 cgroup 会计影响，长时
  稳态会偏低，特别是容器进程数多于 vCPU 时。同时单独记一份 orchestrator 自己的
  CPU%，方便诊断 client 侧瓶颈。
- **价格按 .4xlarge 定锚，size 线性外推**。AWS On-Demand 同 family 内严格 vCPU
  线性，定锚一次就能推出整个 size 家族；新实例上线 / 价格调整也只动 `.env`，不
  动代码。

---

## 9. 怎么把沙盒层迁过去

迁移工作量集中在 build 时，run 时几乎没改动：

1. **build multi-arch image**。
   `docker buildx build --platform linux/amd64,linux/arm64 -t <tag> --push .`，
   或者用本仓库的 `scripts/build-all.sh`（每个架构原生 build 后用 `imagetools
   create` 合 manifest list）。**不要走 emulation build** —— 慢，且容易隐式
   掉性能。
2. **审一遍 Python wheel**。纯 Python 包都没问题；带 native extension 的
   （numpy、pyarrow、duckdb、playwright、…）今天 PyPI 都有 aarch64 wheel，但
   pin 一个较新的版本以防 fallback 到 sdist 编译。`pip install --dry-run` 加
   `--platform linux_aarch64` 可以一次扫出所有需要 native 编译的包。
3. **按 archetype 选实例 family**。参考 Appendix A 的对照表。多 archetype 混合
   的 Agentic RL 沙盒 fleet，**`m9g.4xlarge` 到 `m9g.16xlarge` 是甜点区间**；
   预算敏感、训练已经稳了的，可以用 `m8g` 替代，仍能拿到大头收益（B9 −30%）。
4. **跑一遍 benchmark 验证你自己的 task mix**。B9 的 task 分布是可配的。把它
   换成你 trainer 真实 fan-out 的分布（在 `orchestrator/runners/b9_concurrent.py`
   里），结果就是 swap 后的精确节省估计 —— 比本文给的 −44% 更贴你的工作负载。
5. **生产 fleet 用 ASG mixed-instance policy**。Graviton 占大头，留一小部分
   Intel 容量当长尾兜底（某些第三方 native 库还没及时跟上 aarch64，或者你内部
   工具暂未编译 aarch64 版本）。等运行平稳、依赖完全就绪再全切。

---

## 10. 重现这套数据

完整 benchmark suite 已开源：

```
https://github.com/<your-org>/agentic-rl-bench   <-- 发布前替换
```

> ⚠️ **仓库当前版本已经不是这套流程了。** 主线已迁到 AgentENV（Firecracker
> microVM，每条 rollout 一个沙盒），`docker-compose.yml` 和各 worker 的
> Dockerfile 都已删除，`.env.example` 也改名为 `env.example`。要复现**本文这批
> `.4xlarge` 容器数据**，需要 checkout 迁移前的 commit；要跑当前的 microVM 版本，
> 照仓库 README 走（需要 metal 实例）。

本文数据对应的 Phase-1 流程（历史记录）：

```bash
# 一台 build host（每个架构各一台）
git clone <repo> && cd agentic-rl-bench
git checkout <pre-agentenv-commit>
cp .env.example .env       # 设置 REGISTRY、S3_BUCKET、价格
bash scripts/build-all.sh  # build + push :v1 multi-arch manifest

# 每一台 run host（m7i, m8i, m8g, m9g, ...）
git clone <repo> && cd agentic-rl-bench
git checkout <pre-agentenv-commit>
cp .env.example .env
docker compose pull
bash scripts/run-suite.sh  # 全量 sweep wall-clock ~3-4 小时
```

当前 AgentENV 版本的流程（需要 `*.metal-48xl`）：

```bash
git clone <repo> && cd agentic-rl-bench
cp env.example .env        # 唯一必填 REGISTRY
bash scripts/setup-host.sh # 装 AgentENV + 建 7 个 template + 构建沙盒镜像
bash scripts/run-suite.sh
```

结果落在 `./results/<arch>/<workload>_result_<instance>_<ts>/`，是 JSON 加一份
自包含的 HTML 报告。配置 `S3_BUCKET` 可以同步到 S3，方便跨实例对比。每一份
JSON 的 `extra` 字段会回写当时使用的数据集版本号、模板分布、TPC-H scale factor、
DuckDB / Chromium 版本等等，commit + 这一份 JSON 就够把当时的环境完整复盘出来。

---

## 11. 结语

沙盒层是现代 ML infra 里少有的"换 CPU family 不用动模型代码一行"的地方 —— 负载
就是普通的容器化 Python / FastAPI / Chromium / DuckDB，全都在 aarch64 上有一等
公民支持，运行时语义和 x86 完全一致。

在六个 Agentic RL workload archetype 加一个端到端混合 rollout 上，Graviton
（m8g / m9g）相对 m7i 的实测结论：

- **B9 端到端混合 rollout 在 c=64 工作档位 $/1M rollouts**：m9g 比 m7i 省 **44%**,
  m8g 省 **30%**；按本文工作负载，沙盒层每百万 rollout 从 ~$33.2 降到 ~$18.7。
- **吞吐普遍提升**：m9g vs m7i 在 c≥32 区间，B3 +130%~+167%、B7 +74%~+103%、
  B1 / B5 +50%~+56%、B4 +19%~+43%；端到端 B9 +62%~+72%。
- **p99 长尾全面下降**：B9 c=128 一档 m9g p99 是 m7i 的 ~63%，每个 archetype 都
  至少压低了一档，trainer 等沙盒的时间随之缩短。
- **Intel 同代换代不是免费午餐**：m8i 的 hourly 比 m7i 贵 5%，cost win 完全靠
  吞吐换；同代换 ISA（m7i → m8g）反而比同代换代（m7i → m8i）省钱多得多。

如果你的训练 fleet 沙盒层还跑在 Intel SKU 上，迁移的预期收益高、工程风险低 ——
benchmark suite 把验证它需要的所有东西都打包好了：换一组实例跑 3–4 小时，就能
拿到针对你自己 task mix 的精确节省数字。

---

## Appendix A — 按 workload 选实例的速查表

| 你的训练里大量出现...                            | 起步选                               |
|--------------------------------------------------|--------------------------------------|
| Tool-use RL（mock / 真实 API、JSON、OLTP-like）  | `m9g.4xlarge` — `m9g.16xlarge`       |
| Code RL（python snippet 走子进程）               | `m9g.4xlarge` — `m9g.16xlarge`       |
| Web-agent RL（Playwright / headless Chromium）   | `m9g.16xlarge`（Chromium 内存大）    |
| Data-science / text-to-SQL agent                 | `m9g.16xlarge` 跑 DuckDB；高 SF 内存吃紧时换 `m8g` |
| Embodied / text-game RL（纯 Python 模拟）        | `m9g.4xlarge` — `m9g.16xlarge`       |
| 端到端大规模混合 rollout                         | `m9g.16xlarge` × N，ASG 用 `m8g` 兜底  |

> 预算敏感、希望分阶段迁移的团队，把上表里所有 `m9g` 替换为 `m8g` 也仍然能拿到
> ~70% 的收益（B9 −30% $/1M rollouts），且 m8g 的 hourly 比 m7i 直接便宜 11%。

## Appendix B — 你会关心的 `.env` 旋钮

| 变量                | 作用                                                          | 默认值                 |
|---------------------|---------------------------------------------------------------|------------------------|
| `CONCURRENCIES`     | B1/B3/B4/B5/B7 的并发扫描（CSV）                              | `2,8,16,32,64,128`     |
| `B9_CONCURRENCIES`  | B9 的并发扫描（CSV）                                          | `2,8,16,32,64,128`     |
| `DURATION_SEC`      | 每个并发档位的稳态测量秒数                                    | 300                    |
| `B9_DURATION_SEC`   | B9 的稳态秒数 —— 用 1800 秒 p99 才稳                          | 1800                   |
| `WARMUP_SEC`        | 测量前 warmup 秒数（结束时重置采样器）                        | 30                     |
| `TPCH_SF`           | B5 的 DuckDB TPC-H scale factor                               | 1.0                    |
| `INSTANCE_PRICES`   | 覆盖单价的 `type=usd` 列表，逗号分隔                           | 空（用内置表）         |

`INSTANCE_PRICES` 的写法是逐机型精确列价，不是按 vCPU 外推：

```
INSTANCE_PRICES=m7i.metal-48xl=9.6768,m8i.metal-48xl=10.16064,\
m8g.metal-48xl=8.61696,m9g.metal-48xl=9.39264
```

没有列到的机型会回落到代码里的 `DEFAULT_INSTANCE_PRICES`（`orchestrator/config.py`），
该表已按 AWS Pricing API 校准。

> 早期草稿在这里写的是 `PRICE_M7I_4XL` / `PRICE_M8I_4XL` / `PRICE_M8G_4XL` /
> `PRICE_M9G_4XL` 四个变量 —— **它们在代码里从未存在**。而且其中 `PRICE_M9G_4XL`
> 的 0.7600 是错的（实际 0.78272），§6 已按真实单价重算。

> 价格可覆盖是为了方便切区域、套 RI / Savings Plans / Spot 折扣 —— 改一行
> 数字就能立刻得到反映你实际单价的 $/1M 报告。

## Appendix C — 怎么扩展这套 suite

新增一个 `runners/<your_bench>.py`，继承 `Runner`、在 `run_one()` 里返回
`BenchmarkResult`、在 `runners/__init__.py` 注册即可。HTML 报告的 `PRIMARY_UNITS`
map 和 per-task 拆分会自动识别新 benchmark 的吞吐 key —— 只要新输出的 shape 和
现有 archetype 一致。

---

## Phase 3 实测 — CubeSandbox v0.7.2 上的 c8g vs c7i 头对头

> 本节补上文档开头一直标注「尚无实测数据」的 **Phase 3（CubeSandbox 运行时）**。
> 这是一次**受控**对比：目的不是再证一遍「Graviton 算力强」，而是回答一个具体
> 问题 —— 我们在 v0.7.0 上量到的那个 **arm 供给（sandbox create）劣化**，在
> v0.7.2 上还在不在。所有数字都是 2026-09-29 在真实 metal 上实测，原始结果在
> `results/aarch64/*c8g*_20260929-*` 与 `results/x86_64/*c7i*_20260929-*`。

### 为什么要重做这组对比

Phase 2 / 早期 Phase 3 有一个把结论污染掉的**混杂变量**：x86 机器用
`online-install.sh`（它是 arch-blind 的，且拉的是 `releases/latest`），arm 机器
用离线 tarball，两边装的**不是同一个 build**。所以那时候「m8g 比 m7i 供给慢」
里，有多少是 Graviton、有多少是版本差异，分不清楚。

这一轮把这个洞堵死：

- **两台机器装的是同一个 v0.7.2**，走**同一条 tarball 安装路径**
  （`CUBE_VERSION=v0.7.2` + `CUBE_FORCE_TARBALL=1`，两个架构都强制）。装完实测
  校验：c8g 报 `v0.7.2-arm64`、c7i 报 `v0.7.2-amd64`，安装路径一致。
- **机型是干净的同规格对**：两台都是 `.metal-24xl`，96 vCPU / 192 GiB / 同样的
  EBS 上限（gp3 120000 IOPS / 3750 MB/s）/ 同一份 `config.toml`（create/destroy
  flow 各 concurrent=256、tap_init_num=1200、pool_size=3000）/ 同样的节点上限
  （MaxMvmLimit ≈ 470）。唯一的差异就是 ISA 和核拓扑：**c8g = 96 个真实核（无
  SMT）**，**c7i = 48 物理核 × 2 SMT**。
- 单价（us-east-1 OD，Pricing API 复核）：**c8g $3.82848/h · c7i $4.284/h**，
  c8g 直接便宜 **11.9%**。

v0.7.2 的 changelog 里有两条正好打在我们之前量到的痛点上，是「gap 可能被修好」
的合理理由（不是我们编的）：**#1694** 关掉了 guest kernel 里那个每次冷启动都跑
一遍、且对 ISA 敏感的 RAID6 P+Q 基准；**#1658** 修了 ARM 上 pause 后 resume 失败
（GICv3 ITS 状态）的 bug。

### 结论先行

1. **v0.7.0 的 arm 供给劣化基本消失了。** 纯 create 原语（B8）上 c8g 与 c7i 已经
   在 ±14% 内互有胜负 —— 对比 v0.7.0 那次 **7.3× 的 create 劣化**，这是质变。
   #1694 的效果坐实。
2. **真正的 agent 负载（step 计算）上 Graviton4 更快。** 拉长 episode 让 step
   计算占主导后，c8g 的单步 p50 比 c7i 快 **~25%**（510ms vs 680ms），吞吐在
   c=32 反超 **+20%**。残余的 arm 劣势被隔离在 **create/控制面** 这一条路径上，
   而这条路径在真实 rollout 里占比只会越来越小。
3. **性价比 c8g 赢。** 在不把控制面打饱和的正常工作点上，c8g 以更低的单价拿到
   持平到更优的吞吐，**每单位工作成本便宜 6%–26%**。
4. **诚实的边界**：短 episode（供给占挂钟一半以上）+ 高并发（c≥64）这个「最坏
   组合」下，c7i 的控制面扩展得更好、更晚饱和，此时 c7i 吞吐领先 25%–34%。
   这不是 Graviton 算力问题，是 **CubeSandbox 控制面在 arm 上的每-create CPU
   开销更高**。

### B8 — 纯 create 原语：老劣化是不是没了

B8 只反复 create/destroy sandbox，不跑 step，是**隔离供给路径**最干净的探针。
三个并发档位全部未触发控制面饱和（干净点）。

| 并发 | c8g creates/s | c7i creates/s | c8g/c7i | 控制面核数 c8g/c7i |
|-----:|--------------:|--------------:|:-------:|:------------------:|
| 8    | **168.3**     | 149.6         | **1.12×** | 1.4 / 1.3       |
| 32   | 323.6         | **347.8**     | 0.93×   | 4.4 / 4.6         |
| 64   | 348.1         | **406.1**     | 0.86×   | 6.1 / 7.1         |

读法：两边在 ±14% 内咬得很紧，低并发 c8g 还反超。更关键的是**这些干净点上 c8g
的控制面核数并不比 c7i 高**（c=64 是 6.1 vs 7.1）。纯 create 已经是 parity ——
v0.7.0 那个 7.3× 的坑，在 v0.7.2 上填平了。

### B1 / B3 / B4 — 混合 create + step 的并发扫描（短 episode）

这三个既 create 又跑 step（8 步/episode，60s 稳态）。c=128/256 两边都把控制面打
到饱和（`control_plane_saturated`，且 create 大量失败），orchestrator 自己标记为
「测的是 CubeSandbox 而不是工作负载」，属**存疑点**，下表用 † 标出、不作为结论
依据。吞吐单位：B1 = executions/s，B3/B4 = trajectories/s。

| 负载 | 并发 | c8g | c7i | c8g/c7i | create p50 c8g/c7i | 控制面核数 c8g/c7i | 饱和 |
|------|-----:|----:|----:|:-------:|:------------------:|:------------------:|:----:|
| B1   | 32   | 704 | 658 | **1.07×** | 98 / 88 ms       | 10.8 / 6.9         | 否   |
| B1   | 64   | 810 | 1082| 0.75×   | 194 / 112 ms       | 23.1 / 13.5        | 否   |
| B1   | 128† | 626 | 1004| 0.62×   | 769 / 205 ms       | 66 / 38            | 是/是|
| B3   | 32   | 162 | 165 | 0.98×   | 91 / 82 ms         | 16.2 / 12.4        | 否   |
| B3   | 64   | 178 | 232 | 0.77×   | 182 / 125 ms       | 33.3 / 20.3        | 是/否|
| B4   | 32   | 25.1| 25.5| 0.98×   | 90 / 85 ms         | 3.6 / 3.6          | 否   |
| B4   | 64   | 30.7| 46.4| 0.66×   | 221 / 101 ms       | 16.9 / 5.1         | 否   |

模式很清楚：

- **c=32（干净、最贴近正常工作点）**：三个负载全部 parity —— B1 +7%、B3 −2%、
  B4 −2%。
- **c=64 及以上**：c7i 拉开 23%–34%，即使还没饱和也是如此。原因看 create p50 和
  控制面核数两列 —— arm 的控制面**每做一个 mixed create+step 单元，烧的宿主核更
  多**（B1 c=64 是 23 vs 13.5 核，B4 c=64 是 16.9 vs 5.1 核），所以更早触顶、
  create p50 被抬高。

注意这里有个拓扑细节别过度解读：c7i 那 48 个物理核以 96 个 SMT 线程呈现，所以
「控制面核数」两边口径不完全可比；但**吞吐和延迟是硬事实**，它们指向同一结论 ——
短 episode + 高并发这个组合对 arm 控制面不利。

### 关键验证 — 拉长 episode 后 gap 去哪了

上面的短 episode 有个自带的偏置：每条 rollout 只有 8 步、60s 窗口里 **供给占了
挂钟的 ~53%**。这对 arm 是**最坏情况**，因为 arm 的劣势恰好全在供给路径上。真实
RL rollout 的 episode 长得多，step 计算才是大头。于是我们把 B1 的 episode 拉到
32 步（90s 稳态）重跑 c=32/64 —— 两点都干净未饱和：

| 并发 | c8g exec/s | c7i exec/s | c8g/c7i | **单步 p50** c8g/c7i | create p50 c8g/c7i | 供给占比 c8g/c7i | $/1k c8g/c7i | c8g 单位成本优势 |
|-----:|-----------:|-----------:|:-------:|:--------------------:|:------------------:|:----------------:|:------------:|:---------------:|
| 32   | **1264**   | 1052       | **1.20×** | **510 / 680 ms**   | 89 / 86 ms         | 22.7% / 18.6%    | 0.0008 / 0.0011 | **+25.6%**   |
| 64   | 1558       | 1644       | 0.95×   | 896 / 917 ms         | 199 / 116 ms       | 24.3% / 17.9%    | 0.0007 / 0.0007 | +5.7%        |

对比短 episode：c=32 从 +7% 拉大到 **+20%**，c=64 从 −25% 收窄到 **−5%**。
决定性的一列是**单步 p50**：c=32 时 c8g 跑同样的 agent 工作（在沙盒里执行代码）
比 c7i 快 **25%**（510ms vs 680ms）。也就是说 —— **真实工作（compute）上
Graviton4 更快，残余劣势只剩 create 那一段**（c=64 create p50 199 vs 116ms），
而它随 episode 变长被稀释。

### 性价比结论（cost_per_1k_units）

把 11.9% 的单价折扣和上面的吞吐叠起来，在**不打饱和的正常工作点**上：

- **真实 RL rollout（长 episode）**：c=32 每千次 execution 成本 c8g \$0.0008 vs
  c7i \$0.0011 —— **c8g 便宜 25.6%**；c=64 便宜 5.7%。
- **短 episode c=32**：三个负载 c8g 单位成本全部更低（B1 \$0.0015 vs \$0.0018、
  B3 \$0.0066 vs \$0.0072、B4 \$0.0424 vs \$0.0466）。
- **短 episode c=64**：此时 c7i 吞吐领先够多，单位成本反过来 c7i 更低 —— 如果你
  的负载就是「极短 episode + 高并发」，c7i 在这个点上更划算。

### 给要选型的人

- 面向**真实 Agentic RL 沙盒层**（长 episode、compute 占主导、避开控制面饱和）：
  **c8g / Graviton4 是性价比更优解** —— 单价便宜 12%，正常工作点吞吐持平到 +20%，
  单位成本便宜 6%–26%，且 v0.7.0 那个供给劣化已经在 v0.7.2 修好。
- 如果你被迫跑在**极短 episode + 单机极高并发**：先别急着换 arm，那个点上
  CubeSandbox 的 arm 控制面还没追平 x86；或者干脆调低单机并发、横向多开节点
  （避开饱和区），此时 c8g 的性价比优势又回来了。
- 给 CubeSandbox 维护方的一条具体线索：**arm 控制面每-create 的宿主 CPU 开销
  偏高**（同并发下烧的核明显多于 x86），这是 v0.7.2 之后 arm 侧还值得优化的点,
  和 guest 冷启动（#1694 已修）是两码事。

> 方法学边界：本节 episode/时长比正式 sweep 短（60–90s、8–32 步），是为了控制
> 机时成本；相对结论稳健（干净点重复可比），但吞吐**绝对值**不要直接和第 5 章
> 的 `.4xlarge` Phase 1 数字相比 —— 运行时、机型、供给原语都不同。

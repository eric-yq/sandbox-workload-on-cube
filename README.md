# Agentic-RL Sandbox Benchmark on CubeSandbox

针对 AWS EC2 metal 实例在 Agentic-RL 沙盒典型负载下的 benchmark，运行时是
[CubeSandbox](https://github.com/TencentCloud/CubeSandbox)（腾讯云开源，基于 RustVMM +
KVM 的 microVM 沙盒平台，E2B API 兼容）。

跑测目标是真实 RL rollout 链路上**每秒能完成多少 rollout**、**P99 长尾**、
**每千次/百万次操作成本**，以及 **环境供给占 rollout 墙钟的比例**。

> **这个 repo 是从 AgentENV 版本移植过来的。** 移植的动机、逐项对照、以及
> 「哪些旧结论作废」都在下面的 [从 AgentENV 移植过来](#从-agentenv-移植过来) 一节。
> **七个子项已在 c7g.metal + CubeSandbox v0.7.0 上全部跑通**（低并发短跑，功能验证），
> 但**四款目标机型一台都还没跑**，所以还没有可比的性能基线 —— 见
> [验证状态](#验证状态)。

对比实例（us-east-1 On-Demand，已核对 AWS Pricing API）：

| 实例 | vCPU | **物理核** | SMT | 内存 | 主频 | 网络 | $/h | vs m7i |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| m7i.metal-48xl | 192 | **96** | 2 | 768 GiB | 3.2 GHz | 50 Gb | 9.6768 | — |
| m8i.metal-48xl | 192 | **96** | 2 | 768 GiB | 3.9 GHz | 75 Gb | 10.1606 | +5.0% |
| m8g.metal-48xl (Graviton4) | 192 | **192** | 1 | 768 GiB | 2.7 GHz | 50 Gb | 8.6170 | −11.0% |
| m9g.metal-48xl (Graviton5) | 192 | **192** | 1 | 768 GiB | 3.3 GHz | 100 Gb | 9.3926 | −2.9% |

> **读物理核，不要读 vCPU。** 同样 192 vCPU，Intel 是 96 核 + SMT，Graviton 是 192 个真核。
> 这套 benchmark 的负载形状恰好是"几百个单 vCPU microVM 并行"，这个差异是一阶的，不是二阶的。

必须用 metal：EC2 只在 `*.metal*` 实例上暴露 `/dev/kvm`。Cube 有一条 PVM 路径可以在普通云
主机上造出 KVM，但 **PVM 不支持 ARM64**，而且引入一个自定义宿主内核对 benchmark 是多余变量，
所以这里不用。

---

## 核心模型：一个 rollout 一个沙盒

三层嵌套关系：

```
训练 step (一次梯度更新)
 └── rollout × (bsz × G)          ← 一批轨迹，彼此独立、可并行
      └── 环境 step × T           ← 一条轨迹内的交互步，严格串行、有状态
           └── 一次工具调用 / 代码执行 / 浏览器动作   ← 沙盒真正干的活
```

**rollout 是天然的隔离边界，环境 step 不是**：一条 rollout 内的 step 必须共享
购物车 / 文件系统 / 浏览器 session，而 rollout 之间必须完全隔离。所以

- 一个 rollout = 一个 CubeSandbox microVM，从模板快照恢复出来
- 并发档位（`CONCURRENCIES`）的含义是**同时存活的 rollout 沙盒数**，不是"打向共享 worker 的
  在途 HTTP 请求数"
- 每条 rollout 的墙钟被拆成 `sandbox_create` / `steps` / `sandbox_release` 三段上报

细节见 `orchestrator/rollout.py` 顶部。

派生出的头号指标是 `provisioning_share_p50 = (create_p50 + release_p50) / rollout_p50`：
**环境供给占 rollout 墙钟的比例**。超过 50% orchestrator 会告警 —— 说明 episode 太短、
沙盒开销没被摊薄。

---

## 子项一览

| ID | 名称 | Workload（未改动） | 一个 rollout 是什么 |
| --- | --- | --- | --- |
| B1 | CodeExec | HumanEval 164 + MBPP-sanitized 427 = **591 个 Python snippet**，每条 fork `prlimit … python3 -c` 子进程 | `B1_EPISODE_STEPS`(=8) 次代码执行（写码→跑测→读报错→改） |
| B3 | ToolCall | τ-bench 风格 mock 电商 API，**7 模板 → 200 条 trajectory**（avg 11.7 步） | 1 条 trajectory ✅ 天然对齐 |
| B4 | Browser | Playwright + Chromium headless，**8 模板 → 80 条 trajectory**，跑在同沙盒内的 nginx SPA | 1 条 trajectory ✅ 天然对齐 |
| B5 | SQLExec | DuckDB in-process **TPC-H sf=1**，22 条标准 query | `B5_EPISODE_STEPS`(=5) 次 query（写 SQL→看错→改） |
| B7 | Sim-TextGame | ALFWorld/TextWorld 风格 minigrid（8 房间 + 30 物品 + 6 goal 模板），每 episode 30–50 步 | `B7_EPISODES_PER_ROLLOUT`(=1) 个 episode |
| B8 | **microVM 生命周期** | create / pause / resume / delete / snapshot / clone / rollback | 不是 workload，测的是供给原语本身 |
| B9 | Concurrent-Rollout | 端到端综合：10–30 步 mini-episode，按 B3:50% / B1:20% / B5:10% / B4:10% / B7:10% 抽 task | 1 条 rollout ✅ 天然对齐 |

B2 (SWE-Build) / B6 (DataSci) 从未实现，也没在 `ALL_RUNNERS` 里注册占位。

各子项的 trajectory / corpus / 数据集来源见 runner 模块顶部 docstring。**workload 代码一行
没改** —— 移植只动了沙盒供给层和观测层，这是跨运行时对比能成立的前提。

---

## 架构

单机，无 docker-compose。宿主上跑的是 Cube 的全套组件加一个 orchestrator 进程：

```
宿主 (metal-48xl, 192 vCPU / 768 GiB)
├── CubeAPI      :3000   ← E2B 兼容控制面（create/delete/snapshot/rollback）
├── CubeProxy    :80     ← 数据面反向代理（OpenResty，host networking）
├── CubeMaster   :8089   ← 调度器
├── Cubelet      :9998   ← 节点 agent，内嵌 CubeVS(eBPF) 与网络运行时
│    └── containerd-shim-cube-rs × N   ← 每条 rollout 一个，VMM 在进程内
├── CubeOps      :3010   ← 节点注册表（沙盒数 / 容量 / 健康）
├── CubeTemplateCenter :8090          ← 模板构建
├── MySQL / Redis / CoreDNS / WebUI(:12088)   ← Docker 容器
└── orchestrator (host venv)          ← 压测端 + 指标 + 报告
     └── N 个 load-gen 子进程
```

orchestrator 跑在宿主而不是沙盒里：要打满 192 vCPU、要直接读 `/proc/stat` 和 Cube 各组件的
cgroup、以及 `direct` 传输要连只在宿主网络命名空间可见的沙盒 IP。

### 控制面与数据面是分开的进程

这是相对 AgentENV 拓扑最重要的结构差异。AgentENV 只有一个 Axum 进程同时做控制面和反向代理，
所以它坐在**每一个被测请求**的关键路径上，饱和了整批数据就废了。Cube 把两者拆开：CubeProxy
扛数据面，CubeAPI 扛 create/delete，一边饱和不会直接拖慢另一边。

但 CubeProxy 仍然在数据面关键路径上，所以每个结果里都有 `resource.control_plane`，
按组件给出占用核数，以及一个 `control_plane_saturated` 判定。三个触发条件：

- 控制面合计（CubeProxy + CubeAPI + CubeMaster + Cubelet + CubeOps）> 25% 宿主核
- **create 失败数 > 0**
- 节点撞到沙盒上限（CubeOps 报的 `MaxMvmLimit`，是**按节点资源算出来的**，不是配置里的
  3000；c7g.metal / 125 GiB 上实测只有 **313**）

饱和的档位会在日志里告警、在 HTML 报告里标红 —— **那些点测的是 Cube 而不是沙盒负载，而且
如果一个架构饱和另一个没饱和，跨架构对比直接失效。**

逃生口是 `CUBE_TRANSPORT=direct`，把 CubeProxy 从被测路径上摘掉，见下。

### 统一沙盒镜像

7 个 template 全部来自**同一个镜像** `workers/arl-sandbox`，只靠 entrypoint 参数区分启动
哪些服务：

| template | args | 服务 | vCPU / 内存 | 探针端口 |
| --- | --- | --- | --- | --- |
| arl-b1 | `b1` | b1:8001 | 2 / 1024 | 8001 |
| arl-b3 | `b3` | b3:8003 | 2 / 1024 | 8003 |
| arl-b4 | `b4 static` | b4:8004 + nginx:80 | 2 / 2048 | **8010** |
| arl-b5 | `b5` | b5:8005 | 2 / 3072 | 8005 |
| arl-b7 | `b7` | b7:8007 | 2 / 1024 | 8007 |
| arl-b8 | `b3` | b3:8003 | 2 / 1024 | 8003 |
| arl-b9 | `b1 b3 b4 b5 b7 static` | 全部 | 4 / 4096 | **8010** |

为什么用一个胖镜像而不是六个瘦的：B4 需要 SPA 在同一个 VM 里，B9 需要五套栈在同一个 VM 里
（否则"共享 worker 争用"会以"共享服务"的形式重新出现）。既然这两个必须合并，再维护四个
近乎相同的瘦镜像只是多几条构建和 provisioning 路径。

镜像大不是问题：Cube 把模板构建成本地磁盘上的内存快照，沙盒通过 reflink（`FICLONE`）
写时复制恢复 —— 没改过的页是共享而不是复制的，**只有 rollout 真正写过的页才花真实内存和
磁盘**。胖模板 + 窄工作集正是这个运行时的设计目标，这也是它宣称的单沙盒开销与模板大小
基本无关的原因。

顺带一个免费红利：**B5 的快照打在 `CALL dbgen(sf=1)` 之后**，所以每条 rollout 恢复出来时
TPC-H 数据已经在内存里，不用重跑 30 秒 dbgen。靠的是下面这个机制。

### 模板探针：为什么多了一个 :8010

Cube 决定何时冻结模板快照的方式是**轮询一个 HTTP 端点**（`probePort` + `probePath`）
直到 2xx，然后把此刻运行着的一切冻进快照。这比 AgentENV 的 shell `readyCmd` 更省事，
但它只探**一个**端点，而我们有两个模板要跑多个服务。

探错了不会报错，会**静默地在服务还没起完的时候冻结快照** —— 于是每条 rollout 都要重付
Chromium 启动或 dbgen 的钱，而且是间歇性的首请求失败而不是构建失败。

所以：多服务模板探 `workers/arl-sandbox/readyz.py` 提供的聚合端点（:8010），它只在被要求
启动的**每一个**服务都答 2xx 时才返回 200。单服务模板直接探自己那个端口，压根不启动这个
聚合进程 —— 否则 c=1024 的单服务 sweep 要为 1024 个多余的 Python 解释器付内存，而这正是
这套 benchmark 要报的数。

注意 runner 客户端侧探的仍然是**各自的 workload 端口**（B1→8001 等），不是 :8010：Cube 的
就绪承诺只覆盖被探的那个端口，而我们要的数是"trainer 多久能发出第一个 tool call"。

### 三种数据面传输

`CUBE_TRANSPORT` 选一个：

| 值 | 形式 | 说明 |
| --- | --- | --- |
| `path`（默认） | `{proxy}/sandbox/<id>/<port><path>` | 不用改 Host、不用 DNS，裸 IP 就能跑 |
| `host` | `Host: <port>-<id>.<domain>` | E2B 兼容形式，SDK `getHost()` 给的就是这个。CubeProxy 只正则匹配 Host 开头的 `<port>-<id>`，所以域名后缀是装饰，**不需要真实 DNS 记录** |
| `direct` | `http://<sandbox-ip>:<port><path>` | 完全绕开代理 |

`direct` 不是黑魔法：**CubeProxy 自己在调用方与沙盒同宿主时就是直连这个地址的**。沙盒 IP
不在任何 API 响应里，它在 CubeMaster 写、CubeProxy 读的 Redis 路由表
（`cube:v1:shared:sandbox:proxy:<id>` 的 `SandboxIP` 字段），所以 orchestrator 读同一张表
不是绕路，而是用了和代理相同的事实来源。代价是它绕过了 auto-resume 网关，所以一个被 pause
的沙盒会拒连而不是被唤醒 —— 对本 benchmark 无影响（我们不用 auto-pause），但值得知道。

---

## 四种沙盒获取策略

`CUBE_STRATEGY` 选一个。这里和 AgentENV 版本差异最大，因为原语不一样。

| 策略 | 做法 | 隔离性 | 用途 |
| --- | --- | --- | --- |
| `fresh`（默认） | create → 跑一条 rollout → delete | 最强 | RL trainer 什么都不信任时的做法，主结论用这个 |
| `pool` | 预建 `concurrency` 个沙盒跨 rollout 复用，**不重置状态** | ✗ 漏状态 | "可达吞吐上限"参照线，不适合当主结论 |
| `clone` | 养一个 golden 沙盒，snapshot 一次，每条 rollout 从该快照 create | 强 | GRPO 从同一状态采 G 条候选的形状 |
| `rollback` | 预建池 + 每个成员各 snapshot 一次，rollout 归还时**原地重置** | 强 | Cube 独有：池子的吞吐 + 不牺牲隔离 |

**`rollback` 是这次移植新增的能力**，AgentENV 没有对应原语。它让"复用的沙盒池"重新变得
状态隔离：归还时 `POST /sandboxes/{id}/rollback` 把文件系统和内存重置回快照，沙盒 ID 不变，
省掉一对 create/delete。`pool` 是这四个里**唯一**会把状态从一条 rollout 漏到下一条的，
结果文件里的 `extra.sandbox.state_reset_between_rollouts` 明确标了这一点。

### clone 不是 fork，形状不一样

AgentENV 有 `POST /sandboxes/{id}/fork {count:N}`：一次调用把运行中的沙盒克隆成 N 个继承
完整内存+文件系统状态的子沙盒，父沙盒只 checkpoint 一次，所以摊到每个 child 约 5ms，
**比单独 create 快一个数量级**。

Cube 没有服务端 fan-out。等价操作是两步：

```
POST /sandboxes/{id}/snapshots        -> snapshotID
POST /sandboxes  × N   (templateID = snapshotID)    ← 快照和模板共用一个 ID 命名空间
```

所以**每个 child 是一次完整的 create 往返，成本随 N 线性增长**，不会像批量 fork 那样被摊薄。
`clone` 策略因此并发发起这 N 个 create（SDK 自带的 `clone()` 默认是串行循环，扛不住这个
并发量，这也是不用 SDK 的原因之一）。B8 把 `snapshot` 和 `clone_fanout` 分开计时，
`clone_per_child` 就是回答"克隆到底比从模板 create 便宜吗"的那个数 —— 预期它随 N 增大
**收敛到 create**，而不是像批量 fork 那样甩开它。

另外 B8 里 `snapshot` 阶段会先打一个丢弃不用的快照再开始计时：Cube 把 soft-dirty 页表重置
推迟到一个沙盒的**首次** snapshot 里做，不预热的话每个分布前面都会挂一个几百毫秒的离群点。

---

## 磁盘：必须是 XFS + reflink

**这是在 Ubuntu 上最容易踩的坑，而且它的表现是服务起不来。**

Cube 默认的存储后端 cubecow 用 `FICLONE` ioctl 实现快照 / 克隆 / 回滚，**ext4 不支持**，
而 Ubuntu / Debian / WSL 默认都是 ext4。Cubelet 在非 reflink 文件系统上直接拒绝启动。

`scripts/ensure-cube.sh` 在下载任何东西之前就拦下来。而 `setup-host.sh` 的第 2 步
（`scripts/ensure-data-disk.sh`）会自动处理掉它：找到那块空闲数据盘，建 XFS + reflink，
挂好并写进 fstab。

```bash
DRY_RUN=1 bash scripts/ensure-data-disk.sh     # 先看它打算做什么
sudo bash scripts/ensure-data-disk.sh          # setup-host.sh 第 2 步就是这个
```

它只格**确认为空**的整盘（无文件系统、无挂载点、无持有运行系统的子分区、非根盘），
按设备属性而不是按名字识别（Nitro 上 NVMe 名字不稳定），空闲盘多于一块时拒绝自己挑，
要求你用 `DATA_DEVICE=` 指定。已经准备好的盘是 no-op；盘上已有带 reflink 的 XFS 会被
直接接管而不是重新格。

> 卷要挂在 **`/data`**，不是 `/data/cubelet`。CubeMaster 在 `/data/CubeMaster` 暂存
> rootfs 产物，转换后的模板 rootfs 镜像（每个约 3 GiB × 7 个）默认落在
> `/usr/local/services/cubetoolbox/cubebox_os_image`，也就是根卷。挂低一层会把这两样
> 都留在根卷上。镜像目录的 bind mount 由 `setup-host.sh` 在装完 Cube 之后建立——不能
> 更早，因为一键安装器拒绝一个非空但没有自己标记的安装根目录。

上游 FAQ：[CubeSandbox#311](https://github.com/TencentCloud/CubeSandbox/issues/311)

**四款目标实例都没有实例存储**（EC2 API 确认 `InstanceStorageInfo` 为空），所以
`/data/cubelet`（装着解包后的 rootfs、每个模板一份内存快照、每个活沙盒一层可写 CoW、
shim 日志）的全部 I/O 都走 EBS。

实例侧的 EBS 天花板很高，不会是瓶颈：

| 实例 | EBS 带宽 | EBS IOPS |
| --- | --- | --- |
| m7i.metal-48xl | 5,000 MiB/s | 240,000 |
| m8i.metal-48xl | 7,500 MiB/s | 240,000 |
| m8g.metal-48xl | 5,000 MiB/s | 240,000 |
| m9g.metal-48xl | 9,000 MiB/s | 288,000 |

单个 gp3 卷的上限是 **80,000 IOPS / 2,000 MiB/s**，约为实例能力的 1/3。对本负载单卷足够，
不需要条带或 io2。

### 500 GiB gp3 怎么配

gp3 的两条规则：**IOPS** 基线 3,000，可加配到 500 IOPS/GiB、上限 80,000（要拿满需卷 ≥160
GiB）；**吞吐** 基线 125 MiB/s，可加配到 0.25 MiB/s per provisioned IOPS、上限 2,000 MiB/s
（要拿满需 ≥8,000 IOPS）。500 GiB 时两个最大值都可用。

us-east-1 月成本（存储 $0.08/GB、加配 IOPS $0.005/IOPS、加配吞吐 $0.04/MiB/s，均为
Pricing API 实取）：

| 方案 | IOPS | 吞吐 | 月成本 | 8 小时折算 |
| --- | --- | --- | --- | --- |
| 基线（不加配） | 3,000 | 125 MiB/s | $40.00 | $0.44 |
| 中档 | 40,000 | 1,000 MiB/s | $260.00 | $2.85 |
| **推荐：拉满** | **80,000** | **2,000 MiB/s** | **$500.00** | **$5.48** |

**而且这点钱是噪声。** 实例本身 $8.6–10.2/小时，一次 8 小时压测约 $70–82；磁盘从中档换到
拉满只多 $2.63。既然目标是"不让磁盘成为瓶颈"，就别在这里省。

- 单独挂给 `/data/cubelet`，别用默认 root 卷，且必须 `mkfs.xfs -m reflink=1`
- `ensure-cube.sh` 在可用空间 < 50 GB 时直接报错，< 500 GB 时告警
- **快照是计费存储且生命周期独立于沙盒**：`clone` / `rollback` 策略和 B8 都会建快照，
  orchestrator 在 teardown 时显式删除（`with` 语义不会帮你删），但一次被 kill -9 的跑测
  可能留下孤儿快照，值得偶尔 `cubemastercli` 查一下

### 看 await，不要看 util

对网络挂载的卷，`util` 只表示"至少有一个 I/O 在飞"，一个并行服务多请求的卷可以长期高 util
而远未触及上限。把 util>90% 当饱和会频繁误报。

EBS 的限流方式是**排队**而不是拒绝，所以真正的信号是**服务时间上升而吞吐不再上升**。
gp3 正常 ~1 ms。因此每个结果的 `resource.disk` 里都带 `await_ms_p95` /
`queue_depth_p95` / `util_p95`，以及两级判定：`disk_saturated`（util>90% **且**
await>10 ms）和 `disk_pressure`（await>5 ms 或队列深度>8，仅提示）。

---

## 快速开始

### 0. 前置

- 一台 `*.metal-48xl` 实例（m7i / m8i / m8g / m9g），`/dev/kvm` 可用
- **Ubuntu 24.04**（Cube 官方 ✅ Tested；OpenCloudOS 9 / TencentOS 4 是官方推荐，
  默认就是 XFS）。硬要求是 **glibc ≥ 2.31**，因为 Cube 的二进制在 Ubuntu 20.04 上构建
- **`/data/cubelet` 必须是 XFS + reflink** —— 见上一节
- **EBS 卷：gp3 ≥ 500 GiB，加配 IOPS 和吞吐**，单独挂给 `/data/cubelet`
- aarch64 需要 CubeSandbox ≥ v0.5.0（原生 ARM64 支持是那个版本进的：构建链、guest 内核、
  UEFI 启动路径、seccomp 过滤器都做了适配）。注意 `online-install.sh` 一键脚本目前只自动
  发现 x86_64 包，所以 ARM 走 release 里的 `-arm64.tar.gz`，`ensure-cube.sh` 会自动处理
- 只有走 ECR 才需要 IAM 权限：instance profile 含 `AmazonEC2ContainerRegistryPowerUser`，
  要上传结果再加 `s3:PutObject`
- Docker 不需要预装，`setup-host.sh` 会装。注意 Docker 在这里是**双重依赖**：既是镜像
  builder，也是 Cube 一键部署跑 MySQL / Redis / CubeProxy / CoreDNS / WebUI 的运行时

### 1–4. 全流程

已经有一台裸金属实例的话：

```bash
git clone <repo> && cd sandbox-workload-on-cube

cp env.example .env
vim .env                      # 最少要填 REGISTRY

sudo bash scripts/setup-host.sh

bash scripts/run-suite.sh     # B1 B3 B4 B5 B7 B8 B9
```

**连实例都还没有**，就用 `tools/` —— 从建实例到出结果全自动，在你的工作机上执行，
包括 EBS 的规格、文件系统和挂载点（这些配错会直接毁掉一轮测试）：

```bash
cp tools/config.env.example tools/config.env && vim tools/config.env

bash tools/00-preflight.sh              # 预检
bash tools/01-create-instance.sh        # 建实例 + EBS
bash tools/02-prepare-disks.sh          # 文件系统 + 挂载
bash tools/03-setup-env.sh              # 装环境
bash tools/04-provision-templates.sh    # 建模板
bash tools/05-run-benchmark.sh          # 跑测 + 取回结果
bash tools/99-teardown.sh               # 用完销毁，别忘
```

细节见 [`tools/README.md`](tools/README.md)。

装完先开 Web 控制台看一眼：`http://<host>:12088`（Overview 确认节点 Ready，
Templates 确认 7 个 `arl-b*` 都是 READY）。

> **宿主机重启过就得重建 template。** Cubelet 把节点本地的镜像登记放在 tmpfs 上，重启
> 一次就没了，但 template 定义还在 MySQL 里、`GET /templates` 依旧报 `READY` —— 看起来
> 一切正常，只有 create 会失败。重启后先跑：
>
> ```bash
> sudo bash scripts/ensure-cube.sh          # 幂等，确认平台健康（不会重装）
> bash scripts/provision-templates.sh       # 重建 7 个 template，实测约 3 分 44 秒
> .venv/bin/python orchestrator/smoke_test.py --all
> ```
>
> 不确定的时候直接跑 `smoke_test.py --all`：它对每个 template 真的 create 一次，
> 所以能查出这种"状态是 READY 但用不了"的情况，而只看 template 列表查不出来。

`setup-host.sh` 是幂等的八步：

1. **Docker** — 镜像 builder + Cube 依赖容器的运行时。**必须在第 3 步之前**
2. **数据盘** — 自动找到那块空闲 EBS 数据卷，建 XFS + `reflink=1`，挂到 `/data` 并按
   UUID 写进 fstab。**必须在第 3 步之前**：第 3 步在非 reflink 文件系统上会直接拒绝，
   而新装的 Ubuntu 一定是 ext4。已经准备好的主机是 no-op
3. **CubeSandbox 运行时** — 校验 arch / `/dev/kvm` / glibc / Docker / **XFS reflink** / 磁盘，
   装一键包，调 Cubelet 配置，改 sysctl 和 systemd 的 `LimitNOFILE`，起服务并验证
   CubeAPI `/health` + CubeProxy 在听 + CubeOps 节点注册；装完再把模板镜像目录
   bind mount 到数据卷上（不能更早，装之前建这个目录会让一键安装器拒绝运行）
   3b. **AWS CLI（按需）** — 只在 `REGISTRY` 指向 ECR 或设了 `S3_BUCKET` 时安装
4. **数据集** — HumanEval + MBPP 到 `orchestrator/datasets/`
5. **构建并推送** 统一沙盒镜像（本机原生架构，无需 QEMU）
6. **建 7 个 template** — 走 HTTP API，见下
7. **orchestrator venv**
8. **冒烟测试** — 起一个沙盒跑一步删掉，再单独验一遍 snapshot / clone / rollback

迭代时可以跳过重建：

```bash
sudo bash scripts/setup-host.sh --skip-build          # 只重建 template
sudo bash scripts/setup-host.sh --skip-templates      # 只重建镜像
sudo bash scripts/setup-host.sh --skip-disk           # 磁盘自己准备好了
```

单项调试：

```bash
bash scripts/run-single.sh B9
DURATION_SEC=60 B9_DURATION_SEC=60 bash scripts/run-single.sh B9

.venv/bin/python orchestrator/smoke_test.py --all
.venv/bin/python orchestrator/reap.py         # 清掉跑测留下的沙盒
```

### 跑一次要多久

默认配置下 `run-suite.sh` 的测量部分是 **约 3 小时 50 分**（26 个数据点）。这是配置决定的
确定值，不是估计：每个时间驱动的点固定花 `WARMUP_SEC + DURATION_SEC + COOLDOWN_SEC`。

| 子项 | 点数 | 并发档位 | 每点时长 | 小计 |
| --- | --- | --- | --- | --- |
| B1 | 4 | 32,128,512,1024 | 30+300+60 = 390s | 26 min |
| B3 | 4 | 32,128,512,1024 | 390s | 26 min |
| B4 | 4 | 16,64,128,256 | 390s | 26 min |
| B5 | 4 | 32,128,256,512 | 390s | 26 min |
| B7 | 4 | 32,128,512,1024 | 390s | 26 min |
| B8 | 3 | 8,32,64 | 按 trial 数（500） | 4 min |
| **B9** | **3** | **64,128,256** | **30+1800+60 = 1890s** | **95 min** |
| 各子项 warmup | 7 | — | 启一个沙盒 + smoke test | 2 min |
| | | | | **≈ 230 min** |

**B9 一个人吃掉 42% 的时间**，而它只有 3 个点 —— 因为 `B9_DURATION_SEC=1800`。混合 rollout
的 p99 长尾要跑够久才稳，这是有意的；但如果你只想先看吞吐趋势，它是第一个该砍的
（`SKIP=B9` → 2.2 h）。

冒烟验证不要跑全套：

```bash
DURATION_SEC=30 B9_DURATION_SEC=30 COOLDOWN_SEC=10 WARMUP_SEC=10 \
CONCURRENCIES=32 B4_CONCURRENCIES=16 B8_CONCURRENCIES=8 B9_CONCURRENCIES=64 \
  bash scripts/run-suite.sh          # 七个子项各一个点，约 7 分钟
```

**首次 `setup-host.sh` 是另一笔一次性开销**，主要是构建那个 ~2 GB 的统一沙盒镜像（含 arm64
Chromium 和 DuckDB tpch）再转成 7 个 template。按经验在 metal 实例上大约 **20–40 分钟**，
其中镜像构建和 7 次模板构建各占大头（每次模板构建都要把镜像转成 rootfs 再启一个临时
microVM 等探针）。第二次跑就只剩几十秒的校验。

### 为什么 provisioning 走 HTTP API 而不是 cubemastercli

`cubemastercli tpl create-from-image` 能干这事，但 REST API 是 orchestrator 本来就在说的
协议、不需要 CLI 在 PATH 上、而且能直接拿到 job/status 对象来轮询。而且**一次调用同时
完成 create 和 build**（AgentENV 需要两次）：

```
POST /templates          -> templateID, jobID (202)   含 cpu/memory/exposedPorts/probePort
GET  /templates/{tid}    -> 轮询 status 到 READY
```

几个容易踩的字段差异：

- **`cpu` 的单位是 millicore**，不是整数 vCPU。2 vCPU 要写 `2000`
- **`exposedPorts` 必须声明**所有可能被代理进去的端口
- `command` 映射 ENTRYPOINT、`args` 映射 CMD，我们用 `["/app/start.sh"]` + 服务列表
- 私有 registry 凭据直接内联在请求里（`registryUsername` / `registryPassword`）——
  比 AgentENV 那套"把 docker config 拷进服务账号 home"干净得多，不落盘

---

## 结果产物

每个 (子项, 并发档位) 一个 JSON：

```json
{
  "benchmark": "B9",
  "instance": {
    "instance_type": "m8g.metal-48xl", "arch": "aarch64",
    "vcpus": 192, "physical_cores": 192, "threads_per_core": 1, "smt": false,
    "kernel": "6.8.0-xx-aws", "cpu_model": "0xd4f"
  },
  "concurrency": 128,
  "duration_sec": 1800,
  "throughput": {"rollouts_per_sec": 41.2, "steps_per_sec": 823.5},
  "latency_ms": {
    "rollout":  {"p50": 2980, "p95": 7100, "p99": 11200},
    "sandbox": {
      "sandbox_create":  {"p50": 62,   "p99": 210},
      "steps":           {"p50": 2870, "p99": 10900},
      "sandbox_release": {"p50": 35,   "p99": 110}
    },
    "per_task": {"B1": {...}, "B3": {...}, "B4": {...}, "B5": {...}, "B7": {...}}
  },
  "resource": {
    "cpu_util_avg": 0.91, "mem_peak_gb": 402.3, "host_ncpu": 192,
    "orch_cpu_util_avg": 12.4,
    "control_plane": {
      "cube_proxy_cores_p95": 6.4, "cube_api_cores_p95": 1.1,
      "cube_master_cores_p95": 0.9, "cubelet_cores_p95": 3.2,
      "control_plane_cores_p95": 11.8,
      "control_plane_host_share_p95": 0.061,
      "sandbox_fleet_cores_p95": 158.4,
      "sandbox_shims_max": 128,
      "sandbox_count_max": 128, "max_sandboxes": 3000,
      "create_fails": 0,
      "node_at_sandbox_capacity": false,
      "control_plane_saturated": false
    }
  },
  "cost": {"instance_hourly_usd": 8.61696,
           "cost_per_1k_units_usd": 0.0581, "cost_per_1m_units_usd": 58.1},
  "extra": {
    "sandbox": {"strategy": "fresh", "transport": "cubeproxy/path",
                "one_sandbox_per_rollout": true,
                "state_reset_between_rollouts": true,
                "provisioning_share_p50": 0.0325},
    "all_stacks_in_one_sandbox": true
  }
}
```

HTML 报告除了主表，还有：rollout 生命周期拆分、控制面健康度（按组件分列，含 SATURATED
标红）、磁盘、B5 的 Q01–Q22 分查询表、B9 的分任务表、B8 的分阶段表。

命名约定不变：`<workload>_result_<instance-type>_<ts>/c0128.json` + `rollup.json`，
以及 `summary_*.json` / `report_*.html`；S3 路径 `s3://${S3_BUCKET}/${S3_PREFIX}/<arch>/…`。

---

## 从 AgentENV 移植过来

这个 repo 的上一版跑在 [AgentENV](https://github.com/kvcache-ai/AgentENV)（Firecracker +
overlaybd + ublk）上。**workload 层一行没改**：7 个子项的语料、trajectory 模板、请求体、
worker app、多进程压测驱动、成本模型、报告骨架全部原样保留。改动集中在沙盒供给层和观测层。

### 逐项对照

| 关注点 | AgentENV | CubeSandbox |
| --- | --- | --- |
| 控制面 | `:8000`，单进程兼做反向代理 | CubeAPI `:3000`，代理是独立的 CubeProxy `:80` |
| 认证 | `X-API-Key`；`/nodes` 另需 `X-Admin-Token` | `Authorization: Bearer` 或 `X-API-Key`；**都不配就是无认证**（一键默认） |
| 数据面寻址 | `x-agentenv-sandbox-id` + `x-agentenv-target-port` 两个头 | 路径前缀 / Host 前缀 / 直连沙盒 IP，三选一 |
| 沙盒 IP | 不返回，需要 `[custom_extension]` hook 才能拿到（**未验证**） | 不在 API 里，但在 CubeProxy 自己读的 Redis 路由表里（`SandboxIP`） |
| VM 尺寸 | 只能在 template 建时设，`cpuCount` 整数 | 同样只在 template 建时设，但 **`cpu` 是 millicore** |
| 模板构建 | 两步（create + start build），`startCmd` + shell `readyCmd` | 一步（`POST /templates` 即 202），`command`/`args` + **HTTP 探针** |
| 快照就绪门控 | `readyCmd` 可以探多个端口 | `probePort` 只探**一个** → 多服务模板需要 `readyz.py` 聚合 |
| GRPO fan-out | `POST /{id}/fork {count:N}`，父沙盒 checkpoint 一次，~5ms/child | **无。** snapshot 一次 + N 次独立 create。**实测 194ms/child**（= 从模板 create 的 3.5 倍），且并发发出也基本被串行化 |
| 原地重置 | 无 | `POST /{id}/rollback` → 新增 `rollback` 策略 |
| 冷启动 | `POST /sandboxes-cold`（按沙盒） | **无。** OCI→rootfs 转换只在模板构建时发生 |
| 节点观测 | `GET /nodes` + `X-Admin-Token` | CubeOps `:3010/internal/v1/nodes`（无认证，PascalCase，`omitempty`） |
| 活沙盒数 | `sandboxCount` from `/nodes` | `mvm_num`；CubeAPI `/health` 的 `sandboxes` 是**硬编码 0** |
| create 失败计数 | `createFails` 累积计数器 | **全平台都没有** → 压测端自己数 |
| 控制面 CPU 归属 | 一个 systemd cgroup 装下全部（含 microVM） | 五个 unit cgroup + CubeProxy 的 docker scope；沙盒在独立的 `cube_sandbox{,_v2}` 树 |
| 每沙盒进程 | `firecracker` | `containerd-shim-cube-rs`（VMM 在进程内），**comm 截断成 `containerd-shim`** |
| 存储要求 | `$AENV_HOME` 任意 fs，overlaybd + ublk | **`/data/cubelet` 必须 XFS + reflink**（cubecow 用 FICLONE） |
| 内核陷阱 | Ubuntu 6.17 云内核 `ublk_drv` Oops，template build 永久挂死 | 不适用（不用 ublk）。只要求 glibc ≥ 2.31 |
| 沙盒 TTL 默认 | 上游 15s（会在测量中途 pause） | `-1`，即**永不回收**（泄漏的沙盒会一直占配额） |
| 密集度上限 | `[pool].high_watermark` | `MaxMvmLimit`，**按节点资源推导**（配置默认 3000 不生效；c7g.metal/125GiB 实测 313） |

### 移植时新增的东西

- **`rollback` 策略** —— 池的吞吐 + 不牺牲隔离。AgentENV 没有对应原语
- **`workers/arl-sandbox/readyz.py`** —— 聚合就绪端点，补上单探针的表达力缺口
- **压测端 create 失败计数** —— 平台不提供，但"创建失败了多少个"直接决定一个档位是否有效。
  一条 rollout 的 counters 带出来，`base.fold_create_fails` 折进 `resource.control_plane`，
  并强制把该档位判为饱和
- **节点容量判定** —— 撞到 `node_max_mvm_num` 的档位没有真正测到你要的并发，和代理饱和
  是同一类失效，所以也进 `control_plane_saturated`
- **`orchestrator/reap.py`** —— 独立的清理入口。Cube 默认不回收空闲沙盒，被中断的跑测
  留下的沙盒不会被任何东西收走

### 哪些旧结论作废

**AgentENV 上那批数（`results/aarch64/*_20260819-*`）不能和 Cube 的数
（`results/aarch64/*_20260911-*`）放在一张表里比。** 除了
运行时完全不同，还有几个具体的不可比点：

1. **供给成本的构成变了。** AgentENV 上 B3/B7 的供给占比高达 85–89%（单步只 1–2ms，
   而一次 create+delete 约 100ms）。Cube 上实测是 **B3 88%、B7 93%、B1 50%、B5 42–49%**
   —— 结论方向一致（短 episode 被供给吃掉），但具体数字必须重测而不能外推
2. **fork → clone 的形状变了。** AgentENV 的 `fork` 批量摊薄到 ~5ms/child；Cube 每个 child
   是完整 create，**实测 194ms/child**，比从模板 create（54.8ms）还贵 3.5 倍。任何基于
   "fork 很便宜"的容量结论都要重算 —— 实测下来 `clone` 相对 `fresh` 只快约 10%
3. **代理不再和控制面共享进程。** AgentENV 的饱和判定盯一个进程，Cube 盯五个组件。
   同一个 `control_plane_saturated=false` 在两边不是同一个含义
4. 旧数据是 c8g.metal-24xl（96 核 / 188 GiB）上 30s 短跑、只压到 c=32 得到的

`results/aarch64/*_20260819-*` 下的 AgentENV 结果**故意保留**，作为历史记录和 workload
保真度的参照。这个检查点已经用上了 —— 移植后 Cube 上的 workload 层数字和旧的对得上：

| workload 指标 | AgentENV（旧） | CubeSandbox（新） |
| --- | --- | --- |
| B1 语料条数 | 591 | 591 |
| B3 平均步/trajectory | 11.7 | 11.7 |
| B5 dbgen | `tpch_sf=1.0` / 6.29s | `tpch_sf=1.0` / 7.69s |
| B7 平均步/episode | 39.94 | 40.0 |
| B9 平均步/rollout | 20.09 | 21.23 |

也就是说**跑的是同一个 workload，换的只是沙盒供给层** —— 这正是移植的目标。

---

## 关键配置开关

完整列表见 `env.example`（有注释）。最需要注意的：

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `REGISTRY` | 镜像仓库（唯一必填） | — |
| `CUBE_API_URL` / `CUBE_PROXY_URL` | 控制面 / 数据面，**分开配** | `:3000` / `:80` |
| `CUBE_API_KEY` | 空 = 无认证（一键默认） | 空 |
| `CUBE_TRANSPORT` | `path` / `host` / `direct` | `path` |
| `CUBE_STRATEGY` | `fresh` / `pool` / `clone` / `rollback` | `fresh` |
| `CUBE_SANDBOX_TIMEOUT_SEC` | 每个沙盒的空闲 TTL（**秒**） | 3600 |
| `CONCURRENCIES` | 同时存活的 rollout 沙盒数 | `32,128,512,1024` |
| `B4_CONCURRENCIES` | B4 单独收窄（每沙盒要跑 Chromium） | `16,64,128,256` |
| `B9_CONCURRENCIES` / `B9_DURATION_SEC` | B9 单独档位 / 时长 | `64,128,256` / 1800 |
| `B1_EPISODE_STEPS` / `B5_EPISODE_STEPS` | 一条 rollout 里几个环境 step | 8 / 5 |
| `SANDBOX_VCPU_*` / `SANDBOX_MEM_MB_*` | per-template VM 尺寸（整数 vCPU，脚本转 millicore） | 见 `env.example` |
| `THINK_TIME_MS` / `THINK_TIME_PAUSE` | 模拟 policy 生成延迟；可在空闲期 pause 沙盒 | `0` / `0` |
| `CUBE_TAP_INIT_NUM` | Cubelet 预分配 TAP 数（上游 500 会卡 c=1024） | 1200 |
| `CUBE_WORKFLOW_CONCURRENT` | Cubelet 并发 create/destroy 工作流（上游 100） | 256 |
| `CLIENT_NPROCS_MAX` | 压测端子进程上限 | 64 |
| `INSTANCE_PRICES` | 按实例类型的真实价目表 | 内置四款 metal |

### think time 缺口

默认 `THINK_TIME_MS=0`，即环境 step 背靠背连打——测的是沙盒容量上限。但真实 rollout
大部分墙钟在等 policy 生成，沙盒是**空闲**的，而这恰好是 microVM 沙盒声称的价值所在
（Cube 的 AutoPause/AutoResume、单沙盒 <5MB 常驻开销）。把 `THINK_TIME_MS` 调上去、再配
`THINK_TIME_PAUSE=1`，测的就变成"同样 768 GiB 下，空闲沙盒被挂起后能撑多少条并发
rollout"——这是容器结构上做不到的对比。

一个 Cube 特有的坑：**默认情况下被 pause 的沙盒仍然占着调度配额**（Cubelet 的
`host.quota.paused_resource_release_ratio` 默认 `0`）。所以"pause 掉 N 个再多建 N 个"
不会成功，除非把这个比例调上去 —— 而调上去之后 resume 变成 best-effort，可能被 409 拒绝。
这个取舍本身就值得单独测一档。

### 成本模型

`hourly_price()` 先按**完整实例类型精确匹配** `INSTANCE_PRICES`，再退回旧的 4xlarge
线性锚点（会打 warning）。线性外推本身是对的 —— 用 Pricing API 核过四款机型，
`12 × *.4xlarge` 与 `*.metal-48xl` 的误差是 0.0%。真正会出错的是**锚点值写错**，
所以这里坚持逐机型精确列价。

---

## 目录结构

```
├── env.example                    # 配置模板（唯一必填 REGISTRY）
├── tools/                         # ★ 从零到一：建实例 → 配盘 → 装环境 → 建模板 → 跑测
│   ├── config.env.example         #   唯一需要编辑的文件
│   ├── 00-preflight.sh            #   预检（只读，不改任何东西）
│   ├── 01-create-instance.sh      #   建 EC2 + 两块 EBS（启动时挂载）
│   ├── 02-prepare-disks.sh        #   XFS+reflink、挂 /data、bind mount
│   ├── 03-setup-env.sh            #   同步 repo、生成 .env、装平台
│   ├── 04-provision-templates.sh  #   建 7 个模板并验证真的能用
│   ├── 05-run-benchmark.sh        #   跑 benchmark、拉回结果
│   ├── 99-teardown.sh             #   销毁（实例 + 两块卷）
│   ├── lib.sh                     #   共用：state、SSH、同步、守卫
│   └── remote/setup-disks.sh      #   在实例上执行的磁盘逻辑（02 上传它）
├── scripts/
│   ├── setup-host.sh              # ★ 一键初始化（七步，幂等）
│   ├── ensure-cube.sh             # ★ 装/校验 CubeSandbox + 宿主调优（含 XFS reflink 门禁）
│   ├── ensure-builder.sh          # Docker：镜像 builder + Cube 依赖容器的运行时
│   ├── provision-templates.sh     # 建 7 个 template（POST /templates，一步完成 build）
│   ├── build-all.sh               # 构建统一沙盒镜像并推 registry
│   ├── tune_config.py             # TOML 键级幂等编辑（含 containerd 风格带引号 section）
│   ├── fetch-datasets.sh          # HumanEval + MBPP
│   ├── run-suite.sh / run-single.sh
│   └── _lib.sh
├── orchestrator/                  # 宿主进程（Python async）
│   ├── main.py                    # 入口：sweep + 汇总 + HTML 报告
│   ├── cube.py                    # ★ CubeSandbox 控制面客户端 + 三种传输 + Redis IP 解析
│   ├── rollout.py                 # ★ rollout→沙盒生命周期（fresh/pool/clone/rollback）
│   ├── reap.py                    # 按 arl_bench 标记清理泄漏的沙盒
│   ├── smoke_test.py              # 端到端自检（含 snapshot/clone/rollback）
│   ├── config.py / metrics.py / instance_meta.py / s3_uploader.py
│   ├── datasets/
│   └── runners/
│       ├── base.py                # Runner 契约 + 成本模型 + create 失败折算
│       ├── multiproc.py           # 多进程压测驱动
│       ├── b1_codeexec.py + b1_corpus.py
│       ├── b3_toolcall.py  + b3_trajectories.py
│       ├── b4_browser.py   + b4_trajectories.py
│       ├── b5_sqlexec.py
│       ├── b7_textgame.py
│       ├── b8_sandboxlife.py      # microVM 生命周期（含 snapshot/clone/rollback）
│       └── b9_concurrent.py
├── workers/                       # 见 workers/README.md
│   ├── arl-sandbox/               # ★ 唯一被构建的镜像（Dockerfile + start.sh + readyz.py）
│   ├── b1-codeexec/app.py         # 以下目录只提供源码，没有自己的 Dockerfile，
│   ├── b3-mock-api/app.py         #   由 arl-sandbox 的 Dockerfile COPY 进去
│   ├── b4-playwright/app.py
│   ├── b4-webarena-static/html/
│   ├── b5-sql-runner/app.py
│   └── b7-textgame/app.py
└── results/aarch64/
    ├── *_20260911-*              # ★ CubeSandbox 真机结果（c7g.metal，见「验证状态」）
    └── *_20260819-*               # AgentENV 时代的历史结果，保留作参照（见上）
```

---

## 验证状态

分两层：**离线自检**（任何机器都能跑）和**真机验证**（c7g.metal + CubeSandbox v0.7.0-arm64）。

### 真机验证 ✅

环境：**c7g.metal**（Graviton3 / Neoverse-V1 / **64 物理核** / 125 GiB / Ubuntu 24.04.4 /
kernel 7.0.0-1012-aws / CubeSandbox **v0.7.0-arm64** / Docker 29.8.0 / ECR）+ 一块
200 GiB gp3（6000 IOPS / 500 MiB/s）做 XFS reflink。

> c7g.metal 不在目标四款机型里，而且下面所有数字都是 **c=4/8 的 30 秒短跑**，机器
> 的 `MaxMvmLimit` 只有 313。所以这是**功能验证，不是性能基线**。

**七个子项全部在真机跑通**，`create_fails` 和 `errors` 全为 0，跑完没有残留的沙盒或
快照，7 个 template 保持 READY。四种获取策略（`fresh`/`pool`/`clone`/`rollback`）和两种
传输（`path`/`host`）都实测过。

平台侧已跑通：

| 项 | 结果 |
| --- | --- |
| `ensure-builder.sh` 装 Docker | aarch64 上正常，Docker 29.8.0 + buildx v0.37.0 |
| `ensure_aws_cli()` | aarch64 上装出 aws-cli/2.36.42 |
| **XFS reflink 门禁** | 正确拦下 ext4，`FICLONE` 在真实卷上验证通过 |
| `ensure-cube.sh` 安装 CubeSandbox | arm64 一键包自动发现 v0.7.0 → 下载 → 安装 → 调优 → 健康检查全通 |
| Cubelet 配置调优（`tune_config.py`） | 在**真实** `config.toml` 上生效：`tap_init_num`、workflow 并发；带引号的 containerd 插件段名解析正确，`tomllib` 校验 + 备份 + 保留 uid/gid |
| sysctl + systemd `LimitNOFILE` drop-in | 三个 unit 的 `99-arl-bench.conf` 实际落盘 |
| ECR 全链路 | 自动建 repo → login → 构建 arm64 镜像 → push → 模板构建内联凭据拉取 |
| **7 个 template 全部 READY** | 冷构建 **3 分 43 秒**（7 个），含 arl-b5（TPC-H dbgen 7.69s，在 30s 探针预算内）和 arl-b9（五套栈 + nginx，靠 readyz 聚合门控） |
| 模板请求体 | `cpu:2000`（millicore 换算）、`args:["b4","static"]`、`probePort=8010`、`exposedPorts` 全部按预期到达服务端 |
| **readyz 聚合探针** | 按设计工作：`{"ok":false,"services":{"b1":"ok","static":"URLError"}}`，并且正确阻止了快照在服务没起完时冻结 |
| **两种传输** | `path`（`/sandbox/<id>/8001/healthz` → 200，未就绪端口 → 502）和 `host` 都跑通，且**吞吐无可测差异** |
| 重启后自愈 | fstab 按 UUID 挂 `/data` + bind mount，`cube-sandbox-control.target` 拉起全部 14 个服务 |
| `smoke_test.py --all` | 7 个 template 各一步 + snapshot/clone/rollback 全通，**3.5 秒** |
| orchestrator venv | `python3.12-venv` 自动安装，依赖装齐 |
| 结果产出 | 逐档 JSON + 每子项 `rollup.json` + `summary_*.json` + `report_*.html`，全套跑完 **10 分钟**（`run-suite.sh`，rc=0） |

这一轮的原始结果就在 repo 里：`results/aarch64/*_20260911-*`，其中
`summary_c7g.metal_20260911-011626.json` 和 `report_c7g.metal_20260911-011626.html`
是那次完整 `run-suite.sh` 的产物（七个子项、`fresh`、`path`）。

### 真机实测结果

`fresh` 策略 + `path` 传输，`DURATION_SEC=30`，`WARMUP_SEC=10`：

| 子项 | c | 吞吐 | rollout p50 | p95 | create p50 | release p50 | steps p50 | 供给占比 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B1 codeexec | 4 | 83.5 exec/s | 318 ms | 347 | 84.6 | 75.6 | 156.9 | 0.50 |
| B1 | 8 | 160.3 exec/s | 338 ms | 369 | 88.4 | 83.1 | 163.9 | 0.51 |
| B3 toolcall | 4 | 22.8 traj/s（268 calls/s） | 178 ms | 192 | 80.5 | 75.2 | 20.2 | 0.88 |
| B3 | 8 | 42.4 traj/s（492 calls/s） | 192 ms | 209 | 84.2 | 84.2 | 21.4 | 0.87 |
| B5 sqlexec | 4 | 18.8 q/s | 1042 ms | 1470 | 79.2 | 355.1 | 598.4 | 0.42 |
| B5 | 8 | 33.7 q/s | 1174 ms | 1676 | 83.7 | 490.2 | 621.5 | 0.49 |
| B7 textgame | 4 | 23.8 ep/s（950 steps/s） | 169 ms | 182 | 79.8 | 76.5 | 11.8 | 0.93 |
| B7 | 8 | 43.9 ep/s（1757 steps/s） | 184 ms | 199 | 83.5 | 86.5 | 12.8 | 0.93 |
| B4 browser | 2 | 1.63 traj/s（18.9 act/s） | 1071 ms | 2053 | 109.0 | 153.9 | 800.3 | 0.25 |
| B4 | 4 | 2.87 traj/s（33.0 act/s） | 1118 ms | 3075 | 112.9 | 162.9 | 834.2 | 0.25 |
| B9 concurrent | 4 | 1.47 roll/s（31.1 steps/s） | 2583 ms | 6765 | 134.0 | 409.5 | 2021.5 | 0.21 |

workload 保真度也一起确认了：B1 加载满 591 条语料、pass_rate 0.982；B3 平均 11.7
步/trajectory；B5 `tpch_sf=1.0` + dbgen 7.69s 且 Q01–Q22 逐条延迟都在；B7 平均 40.0
步/episode、平均 reward 1.15；B4 `selector_miss_rate` 0.055–0.093 且 SPA 与沙盒同置；
B9 平均 21.2 步/rollout、五套栈同处一个沙盒。

B1 的 rollout p99（3648ms）是 p95 的 10 倍，来自 steps p99 3487ms —— 但
`snippet_timeout_sec` 是 10s，所以这不是超时，就是 HumanEval/MBPP 里少数本身很慢的题，
**属于 workload 特性，不是 Cube 的问题**。

注意 B4 / B9 的**供给占比只有 0.21–0.25**，是七个子项里最低的：它们的 episode 足够长
（steps p50 0.8–2.0 秒），供给成本被摊薄了。这和 B7 的 0.93 是同一个坐标轴的两端 ——
**per-rollout microVM 划不划算，取决于 episode 干了多少活**，这正是这套 benchmark 要
回答的问题。

**B8 单次原语成本**（`latency_ms.phases`，已预热；create/delete/pause/resume n=40，
snapshot/clone/rollback n=10）：

| 原语 | p50 |
| --- | --- |
| create（从 **template**） | 54.8 ms |
| delete | 81.5 ms |
| **pause** | **343.2 ms** |
| resume | 61.9 ms |
| snapshot | 140.9 ms |
| clone fan-out（4 个 child） | 777.3 ms |
| **clone 摊到每个 child** | **194.3 ms** |
| rollback（原地重置） | 79.2 ms |

两个值得注意的地方：

1. **从快照 create 比从模板 create 贵 3.5 倍**（194.3ms vs 54.8ms），而且一次 fan-out
   4 个 child 花了 777ms ≈ 每个 194ms —— 也就是说并发发出的 create **在服务端基本被
   串行化了**。这正是"Cube 没有服务端批量 fan-out"的代价。注意这组数据无法区分
   "从快照 create 本身就要 194ms" 和 "本来 55ms 但被串行了 4 次"，要分清得再跑一次
   `count=1`。
2. **`pause`(343ms) 比 `resume`(62ms) 贵 5 倍。** 所以拿 pause 去省空闲期的资源，只有
   在空闲窗口明显长于 ~400ms 时才划算，短 think time 下 `THINK_TIME_PAUSE=1` 是亏的。

**四种策略 + 两种传输**（都用 B3，因为它供给占比最高，差异最明显）：

| 方案 | c=4 | c=8 | create p50 (c=8) | release p50 (c=8) | 供给占比 | 状态隔离 |
| --- | --- | --- | --- | --- | --- | --- |
| `fresh`（基线） | 22.8 | 42.4 traj/s | 82.9 | 82.7 | 0.87 | ✅ |
| **`pool`** | 275.7 | **545.8 traj/s** | 0.0 | 0.0 | 0.0003 | ❌ |
| `clone` | 26.3 | 47.0 traj/s | 27.9 | 97.8 | 0.81 | ✅ |
| `rollback` | 25.6 | 46.4 traj/s | 38.4 | 111.3 | 0.87 | ✅ |
| `fresh` + `host` 传输 | 22.6 | 42.4 traj/s | 82.6 | 83.5 | 0.88 | ✅ |

这张表是这轮验证里信息量最大的一张：

- **`pool` 快 12.9 倍**（545.8 vs 42.4），但它 `state_reset=False`，即放弃状态隔离。
  这个数字的价值在于**给出了上界**：B3 的开销里有 92% 以上纯粹是供给，不是 workload。
- **`clone` 和 `rollback` 保住了隔离，但只快 ~10%。** 原因看 create/release 两列就清楚：
  它们把成本**从 acquire 挪到了 release**，而不是消掉。`clone` 的 create 从 82.9ms 降到
  27.9ms（因为 child 是按 `CUBE_CLONE_BATCH` 提前批量造好的，acquire 只是取一个），
  但 release 从 82.7ms 涨到 97.8ms；`rollback` 更直接，重置就发生在 release 里。
- **`host` 传输和 `path` 传输没有可测差异**（42.4 vs 42.4，p50 都是 189.6ms）。所以
  传输方式可以按部署便利性选，不影响测量。

平台侧资源（c=8，`fresh`）：Cubelet 是控制面开销的**绝对主项**，1.7–3.4 核，而
CubeMaster 0.06–0.42、CubeProxy 0.01–0.14、CubeAPI ~0.02、CubeOps ~0.007。整个控制面
占 64 核的 2%–6%。B5 是唯一一个沙盒本身占大头的子项（`sandbox_fleet_cores_p95` 10.99）。

### 真机验证暴露并已修的问题

1. **`ensure-cube.sh` 的报错文案会毁掉机器。** reflink 门禁在没有备用盘时，用
   `findmnt --target` 猜出的设备正好是**根文件系统**，于是打印
   `sudo mkfs.xfs -f /dev/nvme0n1p1` —— 照着做就没了。改了两轮：第一版又建议了
   `/dev/nvme0n1`（整块根盘，它自己没有 FSTYPE 但分区挂着系统）。最终只推荐
   **整棵设备树里没有任何文件系统和挂载点**的盘，否则明说"没有备用盘"并给出挂卷/
   loopback 两条路。
2. **nginx 在 microVM 里根本没起来**，于是 arl-b4 / arl-b9 构建失败。Debian 默认站点有
   `listen [::]:80`，而 **Cube 客机内核没有 IPv6**，nginx 直接
   `[emerg] socket() [::]:80 failed (97: Address family not supported by protocol)`。
   `start.sh` 是后台起 nginx 的，所以失败完全静默 —— 表现只是"模板构建探针超时"。
   修法：自带 IPv4-only 站点配置（`nginx-spa.conf`），并在 `start.sh` 里先 `nginx -t`
   把配置错误变成显式失败。**注意这个问题在 Docker 里复现不出来**（sysctl 关掉 IPv6
   之后 `AF_INET6` 仍然存在，socket() 照样成功），只有真客机会暴露。
3. **磁盘检查查错了文件系统。** 原来只看 `CUBE_DATA_DIR`（196 GiB 空闲），而构建真正
   吃的是**根卷**：`$CUBE_TOOLBOX/cubebox_os_image` 每个模板约 **3 GiB**，加上
   `/data/CubeMaster` 的暂存。结果根卷到 89%，B5 以
   `insufficient disk space: available=4 GiB, estimated_required=5 GiB` 失败，而那块
   200 GiB 的卷只用了 4 GiB。修法：三个文件系统一起查、按模板数量放大要求、并且在
   "重的目录在根卷而大卷挂在别处"时直接给出改挂载的命令。**卷要挂在 `/data`，不是
   `/data/cubelet`**，再把 `cubebox_os_image` bind-mount 过去；实测根卷 89% → 26%。
4. **`set -o pipefail` + `grep -q` 静默反转判断。** `grep -q` 命中即退出，上游进程吃
   SIGPIPE，pipefail 把"找到了"变成"失败了"。后果有两个：`cube_installed()` 永远返回
   false，**每次重跑都重新下载并重装 400 MB 的一键包**；以及 systemd `LimitNOFILE`
   drop-in 的 unit 存在性检查永远为假，**限额调优从来没被应用过**。加了
   `out_matches()`（先捕获再匹配 here-string），修了 4 处。
5. **把安装器的假失败当成致命错误。** Cube 自己的 quickcheck 会报
   `expected socket not ready: /data/cubelet/cubelet.sock`，因为 **Cubelet 跑在自己的
   mount namespace 里**（它给 `/data/cubelet/state` 挂了 tmpfs），socket 在宿主
   namespace 看不见 —— `ss -xlp` 能看到监听、`nsenter --mount` 能看到文件、节点注册
   正常。原先 setup 就此中止。改成：记下来，然后由我们自己的健康检查（CubeAPI /health
   + CubeProxy 在听 + CubeOps 节点已注册）说话。
6. **节点沙盒上限不是 3000。** `MaxMvmLimit` 是按节点资源**算出来**的，这台机器上是
   **313**。原先 README 写的 3000 是 CubeMaster 的配置默认值，不是生效值。现在
   `ensure-cube.sh` 会把它和配置的最高并发档位对比并告警。
7. **template 活不过宿主机重启，但 API 仍然报 READY。** Cubelet 启动时把自己的 state
   目录挂成 **tmpfs**（`mounted state tmpfs /data/cubelet/state size=1024m`），而节点本地
   的镜像登记（`io.cubelet.internal.v1.images`）就在里面。重启一次全没了，可是 template
   定义在 MySQL 里活得好好的、`GET /templates` 照样是 `READY` —— 于是**列表看起来完全
   健康，只有 create 会失败**，报 `template ... is not available locally`。现在
   `cube.py` 把这个错误单独抬成 `TemplateNotLocalError`，直接在报错里写清原因和
   "重跑 `provision-templates.sh`"。
8. **standalone 跑 `provision-templates.sh` 会撞上过期的 ECR token。** 只有
   `setup-host.sh` 会先调 `export_registry_credentials`，可**重启后的恢复路径恰恰是单独
   跑 provision**。不带凭据时 Cube 的 native export 会回落到宿主机的 docker 凭据库，
   那里的 ECR token 超过 12 小时就废了，于是几分钟后报
   `authorization token has expired`，而且算在 template 头上。现在 provision 自己取
   凭据（幂等），`_lib.sh` 里取不到 token 也从 warn 改成直接 `die`。
9. **构建失败的 template，详情路由是 404。** CubeMaster 会把失败 template 的 definition
   行删掉，但在集合里留一条 `status=FAILED` 且带 `lastError` 的记录 —— 也就是说
   `GET /templates/{tid}` 对**最需要识别的情况**答 404。原来的轮询只看详情路由，永远
   等不到 FAILED，会把整个 `BUILD_TIMEOUT`（1800s）烧完。现在改成从列表里取状态
   （新的 `template_entry()`），顺带把满屏的 `curl: (22) ... 404` 噪音也去掉了。
10. **快照的删除顺序错了，每跑一次漏一个。** Cube 有引用计数：删一个还有活沙盒依赖的
    快照会失败，报 `130409 ... snapshot X still has N active runtime ref(s)`（HTTP **500**）。
    实测确认的规则是：**给沙盒拍快照不产生引用，但"从快照 create"和"rollback 到快照"
    都产生**。原来三处都错了 —— `smoke_test.py` 在删 parent **之前**删快照（这就是昨天
    那个孤儿快照的由来）、B8 每轮在 golden 还挂着的时候删快照、以及 `rollout.py` 的
    `release()` 在 rollback 失败退役成员时**把 reset point 从唯一的记账里 pop 掉却不删
    快照**（无条件泄漏）。而这些失败全被 `except Exception: pass` 吞掉了。现在统一成
    "先删依赖再删快照"，加了 `_delete_snapshots()`（`sync=true` + 失败必告警并打出手工
    清理命令），B8 改成 pending 列表 + golden 删掉后重试。
11. **`cubelet_cores` 一直是 0.0。** Cubelet 启动后把自己的进程**挪出 systemd unit 的
    cgroup**，搬进 `/sys/fs/cgroup/cube_sandbox/cubelet`。unit cgroup 依然存在、
    `cpu.stat` 依然可读，只是停在它搬走前烧掉的那 7.8 秒上（真 cgroup 那边已经 171.9
    秒）。因为路径存在与否判断不出来，改成按"这个 cgroup 里有没有进程"来选
    （新的 `_cgroup_has_procs()`）。**影响不小**：c=8 时 `control_plane_cores_p95` 从
    0.304 变成 2.141，之前把控制面开销低报了约 7 倍，而 Cubelet 恰恰是控制面的主项。
12. **`sandbox_shims_max` 一直是 0（psutil 看不见短命进程）。** 一开始以为是 comm 被内核
    截断到 15 字符（`containerd-shim`）导致匹配不上，但真正的原因更麻烦：拿一个跑着的
    B3 负载做并排采样，**同一时刻**裸 `/proc` 扫描能看到 max=4 / avg=2.53，而
    `psutil.process_iter()` 一个都看不到；把沙盒改成常驻不再churn，同一个 psutil 调用
    又能正确返回 4。也就是说 `process_iter` 对存活 ~100ms 的进程不可靠，而
    `fresh` 策略的沙盒恰好就是这个量级 —— 正是这个指标要测的东西。现在直接扫 `/proc`：
    先读 `comm`（内核截断形式）粗筛，再读 `cmdline` 确认。修完之后
    `sandbox_shims_max` 和 CubeOps 的 `sandbox_count_max` **每个子项都严格相等**。
13. **内存只统计沙盒池会低报。** 客机内存和 rootfs I/O 大部分记在 **Cubelet** 头上，
    不在沙盒自己的 cgroup 里：只开一个 1 GiB 沙盒时，池子是 6.3 GB 而 Cubelet 是
    16.6 GB。现在两个都采（`cubelet_mem_gb_max` / `sandbox_fleet_mem_gb_max`），否则
    "一台机器能塞多少 rollout" 这个最关键的结论会算错。

### 真机确认的几个事实（和代码里的假设一致）

- `GET /health` 的 `sandboxes` **确实是硬编码 0**
- CubeOps 的 `mvm_num` 在为 0 时**整个字段消失**（omitempty）—— 代码里的默认值处理是必需的
- Cube 在 64 核机器上报 `CpuTotal=128`、`MemMBTotal=160658`（比物理内存大），所以
  **容量判断要用 CubeOps 的数，不要用自己算的**
- 模板构建的探针预算是服务端固定的 **~30s**（`failure_threshold:60 × period_ms:500`），
  `POST /templates` 没有可调字段 —— 所以慢启动的服务必须靠快照预热，而不是靠放宽探针
- 每个沙盒**确实**有一个独立的 `containerd-shim-cube-rs` 进程
  （`/data/cubelet/root/component_versions/cube-shim/v0.7.0/bin/`），它住在预先建好的
  编号 cgroup 槽位 `/sys/fs/cgroup/cube_sandbox/sandbox/<N>` 里（`CUBE_CGROUP_POOL_SIZE`
  决定槽位数，实测 0..3000 共 3001 个）。Cubelet 是**兄弟** cgroup
  （`/cube_sandbox/cubelet`），不是沙盒的父节点，所以两边分别采样不会重复计数
- 删掉 template / 快照后，`/data/storage/xfs/snapshots/` 下的目录会留着，但每个只有
  **112 KB**（元数据，不是 rootfs），不构成实际的磁盘泄漏

### 离线自检 ✅

无需 CubeSandbox，任何机器可跑：

```bash
python3 scripts/selfcheck.py      # 101 项，全通过
```

内建假的 CubeAPI + CubeProxy + CubeOps，用**真的**客户端跑：三种传输的 URL/header
构造、create（含请求体断言）、就绪探测重试、snapshot/clone/rollback、pause/connect、
delete 的 503+Retry-After 重试、`x-next-token` 分页、按 metadata 回收（并断言不碰别人的
沙盒）、create 失败计数链路、四种策略端到端、readyz 聚合逻辑、HTML 报告渲染、
metrics 采样器在无 Cube 环境下的降级。

另外 `tune_config.py` 的 TOML 编辑同时在上游仓库的真实
`Cubelet/config/config.toml` 上验证过（只改预期的行、`tomllib` 校验通过），API 契约
则是逐条对照 `openapi.yml`、`CubeAPI/src/routes.rs`、`middleware/auth.rs`、
`CubeProxy/lua/*`、`Cubelet/config/config.toml`、`CubeOps/.../node.go` 得出的。

### 仍未验证 ⚠️

- **高并发**：这台机器的 `MaxMvmLimit` 是 **313**，且只有 125 GiB，所以默认的 512/1024
  档位在这里根本跑不了。上面所有数字都是 c=2/4/8。`tap_init_num` / workflow 并发这两个
  调优值是否合适，也只有在高并发下才看得出来
- **`direct` 传输**：Redis 路由表的 key 与 `SandboxIP` 字段名是从 CubeProxy 的 Lua 源码
  读出来的，未在活节点上核对（`path` 和 `host` 都已实测）
- **`THINK_TIME_PAUSE=1` 的实际收益**：B8 量出 pause 要 343ms，说明短 think time 下必然
  是亏的，但没有跑一遍带 think time 的完整对比
- **本地 plain-HTTP registry 路径**：这轮走的是 ECR。`build-all.sh` 会先探测
  CubeTemplateCenter 走 skopeo 还是本地 Docker 再告警，但明文 registry 本身没实测
- **长跑**：最长的单次测量窗口是 30 秒。快照回收、fd 上限、cgroup 池耗尽这些问题要
  几十分钟的连续压测才会显形
- **四款目标实例本身**（m7i/m8i/m8g/m9g.metal-48xl）：一台都没跑过，所以没有可比的
  性能基线，也没有 x86 侧的数据

---

## 参考

- **运行时**: [TencentCloud/CubeSandbox](https://github.com/TencentCloud/CubeSandbox) (Apache-2.0)
- **上一版运行时**: [kvcache-ai/AgentENV](https://github.com/kvcache-ai/AgentENV) (MIT)
- **B1**: [openai/human-eval](https://github.com/openai/human-eval) (MIT) +
  [google-research/mbpp](https://github.com/google-research/google-research/tree/master/mbpp) (Apache-2.0)，sanitized 子集
- **B3**: trajectory 模板 inspired by [sierra-research/tau-bench](https://github.com/sierra-research/tau-bench) retail 任务
- **B4**: trajectory 模板 inspired by [WebArena](https://github.com/web-arena-x/webarena) shopping 任务，自托管 SPA
- **B5**: DuckDB 官方 [`tpch` extension](https://duckdb.org/docs/extensions/tpch.html) (Apache-2.0)，TPC-H 标准 22 查询

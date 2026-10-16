# Agentic-RL Sandbox Benchmark on CubeSandbox

针对 AWS EC2 metal 实例在 Agentic-RL 沙盒典型负载下的 benchmark，运行时是
[CubeSandbox](https://github.com/TencentCloud/CubeSandbox)（腾讯云开源，基于 RustVMM +
KVM 的 microVM 沙盒平台，E2B API 兼容）。

跑测目标是真实 RL rollout 链路上**每秒能完成多少 rollout**、**P99 长尾**、
**每千次/百万次操作成本**，以及 **环境供给占 rollout 墙钟的比例**。

本文讲这套 benchmark 测什么、为什么这么设计。**要直接开跑看
[RUNBOOK.md](./RUNBOOK.md)**，查结果字段和配置开关看 [REFERENCE.md](./REFERENCE.md)。

> **七个子项已在 c7g.metal + CubeSandbox v0.7.0 上全部跑通**（低并发短跑，功能验证），
> 但**四款目标机型一台都还没跑**，所以还没有可比的性能基线 —— 见
> [REFERENCE.md → 验证状态](./REFERENCE.md#验证状态)。

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

各子项的 trajectory / corpus / 数据集来源见 runner 模块顶部 docstring。

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

Cube 把两者拆成独立进程：CubeProxy 扛数据面，CubeAPI 扛 create/delete，一边饱和不会直接
拖慢另一边。这对 benchmark 是结构性的好事 —— 如果同一个进程既做 create/delete 又坐在
**每一个被测请求**的关键路径上，它一饱和整批数据就废了。

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
直到 2xx，然后把此刻运行着的一切冻进快照。但它只探**一个**端点，而我们有两个模板要跑
多个服务。

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

`CUBE_STRATEGY` 选一个。

| 策略 | 做法 | 隔离性 | 用途 |
| --- | --- | --- | --- |
| `fresh`（默认） | create → 跑一条 rollout → delete | 最强 | RL trainer 什么都不信任时的做法，主结论用这个 |
| `pool` | 预建 `concurrency` 个沙盒跨 rollout 复用，**不重置状态** | ✗ 漏状态 | "可达吞吐上限"参照线，不适合当主结论 |
| `clone` | 养一个 golden 沙盒，snapshot 一次，每条 rollout 从该快照 create | 强 | GRPO 从同一状态采 G 条候选的形状 |
| `rollback` | 预建池 + 每个成员各 snapshot 一次，rollout 归还时**原地重置** | 强 | Cube 独有：池子的吞吐 + 不牺牲隔离 |

**`rollback` 是 Cube 独有的原语。** 它让"复用的沙盒池"重新变得
状态隔离：归还时 `POST /sandboxes/{id}/rollback` 把文件系统和内存重置回快照，沙盒 ID 不变，
省掉一对 create/delete。`pool` 是这四个里**唯一**会把状态从一条 rollout 漏到下一条的，
结果文件里的 `extra.sandbox.state_reset_between_rollouts` 明确标了这一点。

### clone 没有服务端 fan-out

Cube 没有"一次调用克隆出 N 个子沙盒"的原语。等价操作是两步：

```
POST /sandboxes/{id}/snapshots        -> snapshotID
POST /sandboxes  × N   (templateID = snapshotID)    ← 快照和模板共用一个 ID 命名空间
```

所以**每个 child 是一次完整的 create 往返，成本随 N 线性增长**，不会被批量摊薄。
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

## 更多文档

- **[RUNBOOK.md](./RUNBOOK.md)** —— 跑测手册：从一台空实例到一份完整结果的命令序列，
  含 [B9 hosting 模式](./RUNBOOK.md#b9-hosting-模式)（长驻池子 + SLO 密度）
- **[REFERENCE.md](./REFERENCE.md)** —— 结果 JSON schema、配置开关全表、目录结构、
  验证状态（含真机实测结果和已修问题）、数据集与出处
- **[workers/README.md](./workers/README.md)** —— 沙盒镜像与七个 worker 服务
- **[tools/README.md](./tools/README.md)** —— 实例创建 / 磁盘 / 内核切换等运维脚本
- `env.example` —— 全部配置项，带注释

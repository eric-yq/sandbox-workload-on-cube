# 参考手册

[← 回到 README](./README.md)

README 讲的是这套 benchmark 测什么、怎么设计的、以及怎么跑起来。这里放的是跑起来之后
要查的东西：结果 JSON 长什么样、有哪些配置开关、代码在哪、以及目前验证到了什么程度。

跑测流程本身见 [RUNBOOK.md](./RUNBOOK.md)，B9 的 hosting 模式见
[RUNBOOK.md → B9 hosting 模式](./RUNBOOK.md#b9-hosting-模式)。

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

命名约定：`<workload>_result_<instance-type>_<ts>/c0128.json` + `rollup.json`，
以及 `summary_*.json` / `report_*.html`；S3 路径 `s3://${S3_BUCKET}/${S3_PREFIX}/<arch>/…`。

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
    └── *_20260911-*              # ★ CubeSandbox 真机结果（c7g.metal，见「验证状态」）
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
- **B1**: [openai/human-eval](https://github.com/openai/human-eval) (MIT) +
  [google-research/mbpp](https://github.com/google-research/google-research/tree/master/mbpp) (Apache-2.0)，sanitized 子集
- **B3**: trajectory 模板 inspired by [sierra-research/tau-bench](https://github.com/sierra-research/tau-bench) retail 任务
- **B4**: trajectory 模板 inspired by [WebArena](https://github.com/web-arena-x/webarena) shopping 任务，自托管 SPA
- **B5**: DuckDB 官方 [`tpch` extension](https://duckdb.org/docs/extensions/tpch.html) (Apache-2.0)，TPC-H 标准 22 查询

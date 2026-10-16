# 跑测手册

[← 回到 README](./README.md)

从一台空的 metal 实例到一份完整结果，按顺序执行。为什么这么测、架构和沙盒策略见
[README](./README.md)；结果字段、配置开关全表和验证状态见
[REFERENCE.md](./REFERENCE.md)。

---

## 0. 前置

- 一台 `*.metal-48xl` 实例（m7i / m8i / m8g / m9g），`/dev/kvm` 可用
- **Ubuntu 24.04**（Cube 官方 ✅ Tested；OpenCloudOS 9 / TencentOS 4 是官方推荐，
  默认就是 XFS）。硬要求是 **glibc ≥ 2.31**，因为 Cube 的二进制在 Ubuntu 20.04 上构建
- **`/data/cubelet` 必须是 XFS + reflink** —— 见
  [README → 磁盘](./README.md#磁盘：必须是-xfs--reflink)
- **EBS 卷：gp3 ≥ 500 GiB，加配 IOPS 和吞吐**，单独挂给 `/data/cubelet`
- aarch64 需要 CubeSandbox ≥ v0.5.0（原生 ARM64 支持是那个版本进的：构建链、guest 内核、
  UEFI 启动路径、seccomp 过滤器都做了适配）。注意 `online-install.sh` 一键脚本目前只自动
  发现 x86_64 包，所以 ARM 走 release 里的 `-arm64.tar.gz`，`ensure-cube.sh` 会自动处理
- 只有走 ECR 才需要 IAM 权限：instance profile 含 `AmazonEC2ContainerRegistryPowerUser`，
  要上传结果再加 `s3:PutObject`
- Docker 不需要预装，`setup-host.sh` 会装。注意 Docker 在这里是**双重依赖**：既是镜像
  builder，也是 Cube 一键部署跑 MySQL / Redis / CubeProxy / CoreDNS / WebUI 的运行时

## 1–4. 全流程

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

## 跑一次要多久

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

## B9 hosting 模式

B9 能模拟两种 archetype，用的是同一套 task mix、同一个 runner，差别全在环境变量：

| | `rl`（默认） | `hosting` |
| --- | --- | --- |
| 沙盒 | 每条 rollout 一个全新沙盒 | 长驻池子，一个沙盒 = 一个 agent 会话 |
| `CUBE_STRATEGY` | `fresh` | `pool` |
| 会话长度 | 10–30 步 | 60–100 步 |
| think time | 0 | 非 0 |
| 创建开销 | 属于被测负载，占 rollout 墙钟约 15% | 一次性 fixture，单独报在 `extra.pool_fill` |
| 状态跨步保留 | 是污染 | 是产品本身 |
| 头条指标 | `rollouts_per_sec` | `steps_per_sec` + SLO 密度 |

### 跑它

```bash
bash scripts/run-b9-hosting.sh

# 自定义档位（上限见下面的准入天花板）
B9_CONCURRENCIES=64,96,117 bash scripts/run-b9-hosting.sh
```

脚本会设好整套 profile，并在开跑前查三件事：模板存在且 READY、并发不超过节点准入上限、
think time 不会让这次跑测失去意义。三者任一不满足时都会**照样产出一份看起来正常的结果
文件**，所以值得前置检查。完整 profile 和各开关语义见 `env.example` 第 6b 节。

### 前置：先建 arl-b9-small

hosting 要的是密度，而 `arl-b9` 是 4 vCPU，节点只收得下 48 个。`arl-b9-small` 是同一个镜像、
同样五个服务，只把 vCPU 砍到 1（内存必须留在 4096，见下），用自己的 alias 构建，不会覆盖
`arl-b9`：

```bash
CONF=/usr/local/services/cubetoolbox/CubeMaster/conf.yaml
sudo cp -a $CONF $CONF.bak
sudo sed -i 's/^  common_timeout_insec: 30$/  common_timeout_insec: 180/' $CONF
sudo systemctl restart cube-sandbox-cubemaster.service && sleep 12

bash scripts/provision-templates.sh B9S

sudo cp -a $CONF.bak $CONF
sudo systemctl restart cube-sandbox-cubemaster.service
```

那个 timeout 必须临时抬高：1 vCPU 的 guest 冷启动五个 stack 超过 Cube 默认给快照门禁的
30 秒，构建会在 `CREATING_TEMPLATE` 阶段超时（x86 报 `context deadline exceeded`，
arm64 报 `statuscode:503`，同一阶段同一原因）。它只约束模板构建 —— 从建好的快照 create
沙盒是 84–103 ms —— 所以建完就该还原，测量跑在出厂配置上。

**内存不能降到 4096 以下。** 2048 能构建、`smoke_test.py` 也能过，然后真实会话下
约 98% 的 rollout 失败：B5 的 DuckDB 查询中途被杀，CubeProxy 对 `:8005` 开始返 502。
构建门禁和 smoke test 各自只碰每个服务一次，都看不到一个「只在 TPC-H 开始分配内存后才死」
的服务。独立的 `arl-b5` 单给 DuckDB 就是 3072 MiB，B9 还要带 Chromium。

### 并发上限

节点准入是 `min(QuotaCpu/cpu, QuotaMem/mem, MaxMvmLimit)`。在 192000m / 约 482 GiB 的
24xl 上：

```
arl-b9        4 vCPU / 4096 MiB -> min(48, 117)  =  48   （CPU 受限）
arl-b9-small  1 vCPU / 4096 MiB -> min(192, 117) = 117   （内存受限）
```

所以 hosting 的天花板是 **117**，而且再砍 vCPU 没有收益 —— 要再往上只能缩小 workload
（比如降 `TPCH_SF`），不是缩小模板。超过上限不会干脆失败：CubeMaster 用 1 秒前的陈旧
指标做准入，那一档会半满并照样报一个吞吐数字。

### think time 是占空比旋钮，不是真实思考时间

默认 200 ms，不是真实 agent 那种数秒的停顿，这是故意的。闭环会话的步速上限是
`1/think_time`，所以只有当 `并发/(think+工作)` 超过主机的步处理能力时，主机才会真正吃上
负载。实测对比：

| think time | 并发 | 结果 |
| --- | --- | --- |
| 2000 ms | 48 | 29 steps/s，主机 CPU **3.9%**，两台机器吞吐**逐位相同**（5260/5260 步） |
| 200 ms | 32→96 | 119→233→344 steps/s，CPU 12%→24%→35%，0 错误 |

2000 ms 那组里 `cost_per_1k_units` 的 arm/x86 比值恰好等于实例价格比 0.894x —— 看起来像
结论，其实只是价格表。脚本会算出这次跑测要向主机索取多少 steps/s 并在明显偏低时告警，
`run_one` 也会在日志里打印同样的天花板。

### 看什么

```bash
python3 -c "
import json; d=json.load(open('results/<arch>/<run>/rollup.json'))
sd=d['slo_density']
print('mode        :', d['mode'])
print('knee        :', sd['knee_sessions'], '会话 @', round(sd['knee_steps_per_sec'],1), 'steps/s')
print('max passing :', sd['max_passing_sessions'], ' 扫完还没拐点:', sd['sweep_exhausted'])
for lv in sd['levels']:
    print(' ', lv['sessions'], lv['met'], lv['violations'] or '')
"
```

- **`rollup.json` → `slo_density`** —— hosting 的头条。`knee_sessions` 是第一次违约前撑住的
  会话数（保守口径），`max_passing_sessions` 是乐观口径；`sweep_exhausted=true` 说明每档都
  过了，真实容量在扫描范围之外，`knee` 只是下限。预算默认是相对的：每个 stack 的单步 p95
  不得超过它自己在最低档 p95 的 `SLO_DEGRADE_FACTOR` 倍（锚点在同一台机器上测，跨架构
  比较不会偷偷带入在其中一台上选的常数）。有真实产品 SLO 时用 `SLO_STEP_P95_MS=b1=40,b3=25`
  覆盖成绝对值。
- **`c<N>.json` → `latency_ms.per_task`** —— 按 stack 的单步延迟分布，hosting 真正要看的数。
- **`extra.pool_fill`** —— 池子建起来多久（扩容延迟），含 `creates_per_sec` 和 create 分位数。
- **`cost.cost_per_1k_steps_usd`** —— 用这个，不要用 `cost_per_1k_units_usd`：hosting 下一个
  unit 是一整个会话，所以后者随会话长度和 think time 变，不随机器变。
- **`latency_ms.rollout.errors`** —— 先看这个再看吞吐。它很大而 `total_rollouts` 很小时，
  完成的那些 rollout 的 p50/p95 会显得很健康（幸存者偏差）。

## 为什么 provisioning 走 HTTP API 而不是 cubemastercli

`cubemastercli tpl create-from-image` 能干这事，但 REST API 是 orchestrator 本来就在说的
协议、不需要 CLI 在 PATH 上、而且能直接拿到 job/status 对象来轮询。而且**一次调用同时
完成 create 和 build**：

```
POST /templates          -> templateID, jobID (202)   含 cpu/memory/exposedPorts/probePort
GET  /templates/{tid}    -> 轮询 status 到 READY
```

几个容易踩的字段差异：

- **`cpu` 的单位是 millicore**，不是整数 vCPU。2 vCPU 要写 `2000`
- **`exposedPorts` 必须声明**所有可能被代理进去的端口
- `command` 映射 ENTRYPOINT、`args` 映射 CMD，我们用 `["/app/start.sh"]` + 服务列表
- 私有 registry 凭据直接内联在请求里（`registryUsername` / `registryPassword`），不落盘


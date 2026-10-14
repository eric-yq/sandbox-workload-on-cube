# tools/cube-bench/ —— 沙盒创建性能的重复采样与汇总

`cube-bench-sweep.sh` 把 CubeSandbox 自带的 `examples/cube-bench`（Go）包成一轮可重复、可汇总的扫描：按并发矩阵逐点跑，**每一次 run 之后都把节点清空**，最后把所有轮次聚合成一张表。

它解决的是手工跑 cube-bench 的两个问题：单次结果噪声大到无法比较，以及两轮之间清理不彻底会静默污染下一轮。

**这个脚本跑在 Cube 节点上**，不是跑在执行 `00`–`05` 的工作机上。它直连 `127.0.0.1:3000`（CubeAPI）和 `127.0.0.1:3010`（CubeOps），并调用节点上的 `cubemastercli`。

## 前置条件

- 节点上已部署 CubeSandbox，且至少有一个 `READY` 模板
- `cubemastercli` 在 `PATH` 上（一键安装会建 `/usr/local/bin` 软链）
- `cube-bench` 已编译出 `bin/cube-bench`（默认在 `/home/ubuntu/CubeSandbox/examples/cube-bench`，`make` 即可）
- `python3`（只用标准库）

## 用法

```bash
# 默认：三个点各 10 次
bash cube-bench-sweep.sh

# 自定义矩阵和轮数
REPS=5 MATRIX="1:20 10:200 20:300 50:500" bash cube-bench-sweep.sh
```

`MATRIX` 的格式是空格分隔的 `并发:请求数`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MATRIX` | `1:20 10:200 20:300` | 并发:请求数，空格分隔 |
| `REPS` | `10` | 每个点重复几次 |
| `WARMUP` | `3` | 传给 cube-bench 的 `-w` |
| `BENCH_DIR` | `/home/ubuntu/CubeSandbox/examples/cube-bench` | cube-bench 所在目录 |
| `BENCH_BIN` | `./bin/cube-bench` | 相对 `BENCH_DIR` |
| `CUBE_TEMPLATE_ID` | 自动探测 | 留空则取第一个 `READY` 模板 |
| `E2B_API_URL` | `http://127.0.0.1:3000` | CubeAPI |
| `CUBE_OPS_URL` | `http://127.0.0.1:3010` | CubeOps，用来读节点配额 |
| `E2B_API_KEY` | `e2b_000000` | 本地部署用任意占位串 |
| `SETTLE_TIMEOUT` | `240` | 等节点归零的上限，超时告警但继续 |
| `RUN_GAP` | `2` | 归零之后再多等几秒 |
| `OUTDIR` | `~/cube-bench-sweep-<时间戳>` | 输出目录 |

## 输出

```
~/cube-bench-sweep-20261014-061607/
├── meta.json      实例类型、模板规格、节点配额、算出的稳态上限
├── report.txt     下面那张表（屏幕上也会打印）
├── report.csv     同样内容，可直接导表格
├── report.json    聚合结果 + 每一轮的原始值
└── runs/          每一轮的原始 cube-bench 报告
    ├── c1_n20_rep01.json
    └── ...
```

`report.txt` 长这样（m8g.metal-24xl 实测）：

```
instance        : m8g.metal-24xl (nproc=96)
template        : tpl-b63b1b8ef05443299ead4dc8  (cpu=2000m per sandbox)
node quota      : QuotaCpu=192000 milli, MaxMvmLimit=942
steady-state cap: 96 sandboxes

实例类型        并发  请求数  successful  total_time_s  throughput_qps  avg    min    p95    p99    max    单沙箱均摊  reps  succ_min  succ_max  succ_std
--------------  ----  ------  ----------  ------------  --------------  -----  -----  -----  -----  -----  ----------  ----  --------  --------  --------
m8g.metal-24xl  1     20      20.0        0.999         20.02           49.90  47.02  52.54  53.44  53.44  49.95       2     20        20        0.0
m8g.metal-24xl  20    300     300.0       1.186         253.05          77.18  59.20  88.38  91.47  92.97  3.95        2     300       300       0.0
```

列的含义：

- `avg / min / p95 / p99 / max` —— **单次 create 的延迟**（毫秒），取自 cube-bench 报告的 `create` 段，跨轮次求平均
- `单沙箱均摊` —— `total_time_s × 1000 / successful`，即每交付一个沙盒花掉的**墙钟**毫秒。c=1 时它和 `avg` 基本相等；c=20 时降到 3.95 ms，这个差值就是并发摊薄的效果
- `succ_min / succ_max / succ_std` —— `successful` 在各轮之间的离散度。**这三列比 `successful` 的均值重要**，理由见下

## 注意事项

### 1. `successful` 超过稳态上限之后就不是容量指标了

这是用这个脚本最容易得出错误结论的地方。

CubeMaster 的准入由 `cpu` / `mem` 两个 filter 把关，算式是 `free = 配额 − 节点上报的用量`，不够就返回 `130597 no more resource`。节点配额是推导出来的（`host.quota.*` 默认全 0），在 96 核机器上是 `QuotaCpu = 192000` 毫核；模板默认 `cpu=2000m`。所以：

```
稳态上限 = QuotaCpu / 模板 cpu = 192000 / 2000 = 96
```

脚本会自动算出这个数并打在报告头部。把创建放慢到 1.5 秒一个，**严格在第 96 个被拒**，这一点在 c8gd.metal-24xl 和 m8g.metal-24xl 上都验证过。

这条算式有一个前提：`scheduler.ignore_redis_allocation` 为 `false`（代码默认值，一键部署的 `conf.yaml` 里不写这个键，所以默认就是它）。打开之后整个配额门禁会失效，上限变成物理闸门，见下面第 5 节。

而 `node_status_update_frequency = "1s"`——闸门看的是 1 秒前的陈旧指标。突发创建时速率可达 250–600 个/秒，从用量越过配额到下一次上报落地之间，所有请求都被放进来：

```
successful ≈ 96 + 创建速率 × U(0,1)
```

实测在同一台机器、同一条 `-c 50 -n 500`、每轮前都确认 `QuotaCpuUsage=0 / mvm_num=0 / listed=0` 的情况下，拿到过 **222 / 263 / 266 / 272 / 330 / 345 / 478 / 493 / 500**。

所以当 `请求数 > 稳态上限` 时，`successful` 测的是「突发能抢在闸门关上前挤进去多少」，不是机器能力。报告会对这些点自动打 CAVEAT 并列出 spread。**跑完务必先看 `succ_std`**：不为 0 就别用 `successful` 的均值，此时只有延迟列（`avg/p95/p99`）是有意义的。

要可复现的容量数，就把 `请求数` 压到稳态上限以内（例如 `MATRIX="1:20 10:90 20:90"`），或者直接按配额算。

顺带一提，这个行为值得给 Cube 报 issue：准入依赖 1 秒前的指标，意味着一次突发可以超发节点配额 5 倍以上（500 vs 96），真实多租户部署里等于短时间接纳了 5 倍于预期的租户。

### 2. 吞吐数字是反向误导的

`throughput_qps = successful / total_time_s`，而被拒的请求是**瞬间**返回的，会把分母压小。于是失败越多的 run，吞吐看起来越好：

```
222 成功 → 0.81s → 273 sandboxes/sec
478 成功 → 2.43s → 196 sandboxes/sec
```

只看吞吐会得出完全相反的结论。要判断一轮是否健康，看 `successful` 和 `errors`，不要看 qps。

### 3. 清理之间必须轮询，不能固定 sleep

`cubemastercli list --all -q | wc -l` 返回 0 **不代表**节点上报的配额已经回落。上一轮的用量还在上报窗口里时就开跑，结果会被静默污染。

脚本因此在每轮之前轮询到 `listed=0 且 QuotaCpuUsage=0 且 mvm_num=0` 才继续，日志里每行 `pre:` 都应该是三个 0：

```
06:16:44  [c=20  n=300  rep  2/2 ] pre: listed=0 QuotaCpuUsage=0 mvm_num=0
```

如果看到非 0，说明 `SETTLE_TIMEOUT` 到了仍未归零，那一轮的数据不可信。

注意节点注册表里每个数值字段都是 `omitempty`，完全空闲的节点会**直接不返回** `mvm_num` 而不是返回 0 —— 脚本里取值都带了默认 0，手工 `grep` 时则是「grep 不到才对」。

### 4. create-only 不回收，而且集群默认永不超时

`-m create-only` 建完就不管了，而 CubeSandbox 集群默认 `default_timeout_insec: -1`（永不回收）。手工跑 cube-bench 时沙盒会一直挂着占配额，直到你手动清。脚本每轮都清，但**如果你中途 Ctrl-C，节点上会留下一批**。手动收尾：

```bash
cubemastercli list --all -q | xargs -r -n 25 -P 4 cubemastercli --timeout 300s cubebox destroy
cubemastercli list --all -q | wc -l
```

### 5. 官方文档那个「成功率 100%」，是因为配额门禁被关掉了

[官方性能基准文档](https://cubesandbox.com/zh/blog/posts/2026-06-01-cubesandbox-perf-benchmark)在 96 逻辑核 / 375 GiB 的机器上报告 `1:20`、`10:200`、`20:300`、`50:500` 四档**成功率均为 100%**，沙箱规格同样是 2 vCPU / 2 GiB，模式同样是 `create-only`。照着默认部署复现不出来：`50:500` 需要突破配额 5.2 倍。

差别在 `CubeMaster/conf.yaml` 的一个开关：

```go
// CubeMaster/pkg/base/config/config.go
func (s *SchedulerConf) EffectiveAllocated(usage int64) int64 {
	if s.ShouldIgnoreRedisAllocation() {
		return 0        // ← 已分配量当成 0
	}
	return usage
}
```

`cpufilter` / `memfilter` 算的是 `配额 − EffectiveAllocated(上报用量)`，所以打开后 `quotaCpuFree` 恒等于 `QuotaCpu`，**filter 永不拒绝**。

```yaml
# /usr/local/services/cubetoolbox/CubeMaster/conf.yaml
scheduler:
  priority_select_num: 1
  metric_update_timeout: 300s
  local_metric_update_timeout: 300s
  ignore_redis_allocation: true     # ← 加这一行
  filter:
    enable_filters: [...]
```

**开启**（先备份，再插到 `local_metric_update_timeout` 后面，重启生效）：

```bash
CONF=/usr/local/services/cubetoolbox/CubeMaster/conf.yaml
sudo cp -a "$CONF" "${CONF}.bak.$(date +%Y%m%d-%H%M%S)"
sudo sed -i '/^  ignore_redis_allocation:/d' "$CONF"          # 先去重，避免重复插入
sudo sed -i '/^  local_metric_update_timeout:/a\  ignore_redis_allocation: true' "$CONF"
sudo systemctl restart cube-sandbox-cubemaster.service
```

**关闭**（删掉那一行即可，不依赖备份是否存在）：

```bash
CONF=/usr/local/services/cubetoolbox/CubeMaster/conf.yaml
sudo sed -i '/^  ignore_redis_allocation:/d' "$CONF"
sudo systemctl restart cube-sandbox-cubemaster.service
```

**确认当前状态**：

```bash
CONF=/usr/local/services/cubetoolbox/CubeMaster/conf.yaml
sudo grep -n 'ignore_redis_allocation' "$CONF" || echo "未设置 -> 走代码默认 false（门禁生效）"
systemctl is-active cube-sandbox-cubemaster.service
sudo python3 -c "import yaml;print(yaml.safe_load(open('$CONF'))['scheduler'].get('ignore_redis_allocation'))"
```

两点提醒：第一条 `sed` 的去重是必要的，`a\` 是纯追加，重复执行会插入多行同名键；YAML 解析器只取其中一个，但配置文件会变得难读。另外第一次开启前的 `cp -a` 备份建议留着 —— 真正的还原路径是 `sudo cp -a "${CONF}.bak.<时间戳>" "$CONF"` 再重启。

重启后用下面这条验证门禁是否真的失效（默认配置下应在第 96 个被拒，开启后应该一路建到几百个）：

```bash
REPS=3 MATRIX="50:500" bash cube-bench-sweep.sh
```

m8g.metal-24xl 上的 A/B（只改这一个开关，各 3 次）：

| 档位 | 开关打开 | 默认（关闭） |
| --- | --- | --- |
| 10:200 | 200 / 200 / 200 | 161 / 129 / 145 |
| 20:300 | 300 / 300 / 300 | 200 / 177 / 171（另有跑满的轮次） |
| 50:500 | 500 / 500 / 500 | 222 / 478 / 500 |
| `succ_std` | **0.0** | 16.0 |

打开后 `10:200` 这档和文档几乎逐项重合（文档 / 实测）：avg 88.7 / 96.7 ms，p95 116.9 / 113.3 ms，max 119.1 / 118.5 ms，吞吐 101.4 / 102.6 个/s，单沙箱均摊 9.9 / 9.75 ms。五项都在噪声内，所以文档的测试环境应该就是这个配置（或等价设置）。

**它关掉的是什么**：按模板声明规格记账那一层。默认每个沙箱扣 2000m 配额，96 个扣满；打开后调度器不再累计已分配量，一直收到物理闸门为止 —— `CpuUtil >= 80%`（`NodeMaxCpuUtil` 默认值）、`loadMemFree = MemMBTotal − MemUsage` 低于「请求量 + 保留量」、以及 `MaxMvmLimit=942`。所以**不会 OOM**，这几道物理保护是 #525 刻意保留的。

代价是失去「每个沙箱真能拿到声明的 2 vCPU」这个保证：500 个沙箱声明了 1000 vCPU，物理只有 96 核。**压测冷启动延迟和部署密度时这是你要的；真实多租户生产环境里等于放弃资源隔离承诺。**

两个容易误解的点：

- **不影响单次创建成本。** 打开后 `10:200` 的 avg 从 60 ms 变成 96 ms，看着像开关的代价，但还原配置重启后重测仍是 94.3 ms —— 那是这台机器在几千次创建测试后自身的漂移（`/data/cubelet` 的 CoW 层与存储元数据累积），和开关无关。
- **调度层超卖比已经没了。** `scheduler.overcommit_ratio`（2026-06-11 #525 引入，默认 CPU=3/Mem=2）在 2026-09-04 #1575 被移除，v0.7.x 里标记 deprecated 且忽略，启动会打 warning。写了也不生效。cubelet 侧那个 `defaultHostQuotaCPUOvercommitFactor = 2` 自首次开源就没变过，所以 `QuotaCpu` 恒等于 `2 × nproc × 1000`。

### 6. 想真的提高容量，要改模板规格

`--cpu` / `--memory` 是**建模板时**的参数，建完不能改。官方 quickstart 的命令两个都没传，所以拿到的是默认 `2000m / 2000Mi`：

```bash
cubemastercli tpl create-from-image \
  --image cube-sandbox-int.tencentcloudcr.com/cube-sandbox/sandbox-code:latest \
  --writable-layer-size 1G --expose-port 49999 --expose-port 49983 --probe 49999 \
  --cpu 500 --memory 512
```

在 96 核 / 377 GiB 上，`500m/512Mi` 让 CPU 允许 384 个、内存允许 941 个，瓶颈变成 `MaxMvmLimit=942`，稳态能到约 384。

另一条路是抬节点配额 —— `/usr/local/services/cubetoolbox/Cubelet/dynamicconf/conf.yaml` 里的 `host.quota.mcpu_limit` / `mem_limit` / `mvm_limit` 现在都是 0（取推导默认，CPU 是 `2 × nproc`）。**这条未实测**，改完建议用放慢节奏的方式验证拒绝点是否按预期上移。

三条路的区别：改模板规格**保留**按规格记账的语义，只是把每个沙箱改小，适合想真实压高密度；抬 `host.quota.*` 是等比放大配额；而第 5 节那个开关是直接**关掉记账**，适合复现官方文档或只测创建延迟。

### 7. 脚本会主动 `unset E2B_DEBUG`

`E2B_DEBUG=true` 会把 e2b SDK 的**数据面**指到 `localhost:49983`。对 cube-bench 本身没影响（它只调创建接口），但避免从你的 shell 带进来之后污染同一个会话里的其它操作。

## 实测参考值

m8g.metal-24xl（96 vCPU / 377 GiB），模板 `cpu=2000m,mem=2000Mi`：

默认配置（`ignore_redis_allocation` 未设置）：

| 并发 | 请求数 | create avg | 单沙箱均摊 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | 20 | 49.9 ms | 49.95 ms | 低于稳态上限，稳定可复现 |
| 10 | 200 | 60.2 ms | 6.17 ms | 超过上限，依赖超发 |
| 20 | 300 | 77.2 ms | 3.95 ms | 超过上限，依赖超发 |

`ignore_redis_allocation: true`（见第 5 节，全部 100% 成功、`succ_std=0`）：

| 并发 | 请求数 | create avg | p95 | 单沙箱均摊 | 吞吐 |
| --- | --- | --- | --- | --- | --- |
| 10 | 200 | 96.7 ms | 113.3 ms | 9.75 ms | 102.6 个/s |
| 20 | 300 | 148.6 ms | 196.3 ms | 7.52 ms | 133.0 个/s |
| 50 | 500 | 405.0 ms | 895.6 ms | 8.33 ms | 120.3 个/s |

两张表的延迟不可直接对比：后者是这台机器跑过几千次创建之后测的，基线已经漂移（同一档位还原配置后重测是 94.3 ms，不是 60 ms）。

节点配额：`QuotaCpu=192000` 毫核（`2 × nproc`）、`QuotaMem=482443` MiB（约 `1.25 × RAM`）、`MaxMvmLimit=942`、稳态上限 96。

320 个沙盒实测只占用约 17 GiB 内存（每个约 54 MiB），**内存从来不是这里的瓶颈**，CPU 配额才是。

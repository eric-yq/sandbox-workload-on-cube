# tools/ —— 从零到一跑完一轮 benchmark

在**你的工作机**上执行，通过 SSH 驱动一台裸金属实例。全程不需要手工登录那台机器。

```bash
cp tools/config.env.example tools/config.env
$EDITOR tools/config.env          # 至少填 KEY_NAME / SSH_KEY_PATH / IAM_INSTANCE_PROFILE / REGISTRY

bash tools/00-preflight.sh             # 预检，什么都不改
bash tools/01-create-instance.sh       # 建实例 + 两块 EBS（启动时挂载），记下私网 IP
bash tools/02-prepare-disks.sh         # XFS+reflink、挂 /data、bind mount
bash tools/03-setup-env.sh             # 同步 repo、生成 .env、装平台
bash tools/04-provision-templates.sh   # 建 7 个模板并验证真的能用
bash tools/05-run-benchmark.sh         # 跑 benchmark，结果拉回本地
bash tools/99-teardown.sh              # 销毁（实例 + 两块卷）
```

## 快档（BENCH_PROFILE）

`config.env` 里的 `BENCH_ENV_OVERRIDES_QUICK` 是一组短跑参数（约 12 分钟跑完七项，而不是几小时）。两种用法：

```bash
# 只影响这一次跑测，不改实例上的 .env —— 推荐
BENCH_PROFILE=quick bash tools/05-run-benchmark.sh
BENCH_PROFILE=quick bash tools/05-run-benchmark.sh B8

# 烧进实例的 .env（之后每次跑都用它）
BENCH_PROFILE=quick bash tools/03-setup-env.sh --skip-build
```

**优先用 05 那种。** 03 会顺带重装平台并重建容器镜像，只为改几个并发数就跑一遍不值得；加 `--skip-build` 能省掉镜像那步，但仍然比 05 慢得多。

`BENCH_PROFILE` 的取值只有 `quick` 和 `default`（`default` 对应 `BENCH_ENV_OVERRIDES`，默认是空的，即完全用 `env.example` 的值）。写错会**在连接实例之前**就被拒绝。

三条优先级，从高到低：

| 来源 | 例子 |
| --- | --- |
| 命令行内联变量 | `DURATION_SEC=60 BENCH_PROFILE=quick bash tools/05-run-benchmark.sh` → 用 60 |
| `BENCH_PROFILE` 的 override 块 | quick 档里的 `DURATION_SEC=30` |
| 实例上 `.env` 的值 | `env.example` 里的 `DURATION_SEC=300` |

per-benchmark 的键（`B1_CONCURRENCIES`、`B4_CONCURRENCIES`、`B8_TRIALS`、`B9_DURATION_SEC`…）也在转发范围内 —— 否则 quick 档只会生效一半。override 块里格式不对的行会被忽略并告警，不会被拼进远端命令。

## 每个脚本做什么

| 脚本 | 在哪跑 | 做什么 | 幂等 |
| --- | --- | --- | --- |
| `00-preflight.sh` | 本地 | 预检：本地工具、AWS 凭据、key 权限、机型在哪些 AZ 有货、AMI 能否解析、IAM profile、EBS 参数是否越界、成本提示 | 是（只读） |
| `01-create-instance.sh` | 本地 | 解析 Ubuntu AMI → 选 AZ/子网/安全组 → `RunInstances`（两块卷在 block device mapping 里）→ 等 running → 记录私网 IP 和 volume ID | 否（会再建一台，且会先问） |
| `02-prepare-disks.sh` | 本地 → SSH | 按 volume-id 定位设备 → `mkfs.xfs -m reflink=1` → 按 UUID 挂 `/data` → bind mount `cubebox_os_image` → 验证 FICLONE | 是（已有 reflink XFS 就保留数据） |
| `03-setup-env.sh` | 本地 → SSH | rsync repo → 生成 `.env` → `setup-host.sh --skip-templates` | 是 |
| `04-provision-templates.sh` | 本地 → SSH | `ensure-cube.sh` → `provision-templates.sh` → `smoke_test.py --all` | 是（每次重建模板） |
| `05-run-benchmark.sh` | 本地 → SSH | `run-suite.sh` 或 `run-single.sh`（nohup，可断开）→ 拉回结果 → 查残留 | 是 |
| `99-teardown.sh` | 本地 | 销毁实例 + 确认两块卷已删；`--stop` 只停机保留磁盘 | 是 |

`03-setup-env.sh`–`05-run-benchmark.sh` 刻意只是**薄封装**，真正的安装/供给/压测逻辑全在 `scripts/` 里 —— 那些脚本已经在真机上验证过，复制一份只会让两边慢慢走偏。

## EBS 为什么是这个配置

默认给 **根卷 100 GiB gp3** + **数据卷 1 TiB gp3 / 40,000 IOPS / 1,000 MiB/s**，依据都是 c7g.metal 上的实测：

- **根卷 100 GiB**：默认 38 GiB 会被撑到 89%，因为模板 rootfs 镜像每个约 3 GiB 落在根卷上，第四个模板构建就以 `insufficient disk space` 失败，而那块 200 GiB 的数据卷几乎是空的。`02-prepare-disks.sh` 的 bind mount 把这部分挪到了 `/data`，100 GiB 是"万一 bind mount 没生效也不至于翻车"的余量。
- **必须 XFS + reflink**：Cube 的 cubecow 后端用 `FICLONE` 实现 snapshot/clone/rollback，ext4 不支持，Cubelet 会直接拒绝启动。Ubuntu 默认 ext4，所以 `02-prepare-disks.sh` 必须自己建文件系统。
- **卷挂在 `/data`，不是 `/data/cubelet`**：挂低一层会让 `CubeMaster` 暂存和 `cubebox_os_image` 留在根卷上。
- **40,000 IOPS**：实测七个子项里 create 最密集的 B8 在 **c=4** 就打到 5,229 IOPS p95；按每 rollout/s 约 26 IOPS 外推到这台机器能达到的吞吐，大致需要 31,000。留了余量。
- **gp3 而不是 io2**：实测 gp3 的 `await` 只有 0.79–1.05 ms，而一次 create 是 55–135 ms —— 盘的延迟不在关键路径上，io2 的 sub-500µs 买不到任何东西。
- **IOPS 可以在线改**（Elastic Volumes），所以宁可先给 40,000 再看指标。跑完看结果 JSON 里的 `resource.disk.iops_p95` 和 `disk_saturated`；要加的话注意两次修改之间有 6 小时冷却期。

gp3 上限：80,000 IOPS（需 ≥160 GiB）、2,000 MiB/s。`m8g.metal-48xl` 侧的上限是 240,000 IOPS / 5,000 MB/s，所以单卷不会先撞到机型天花板。`00-preflight.sh` 会替你校验这些比例关系。

## 三个反直觉的地方

**1. 模板活不过重启。** Cubelet 把 state 目录挂成 tmpfs，节点本地的镜像登记就在里面。重启后模板定义还在 MySQL、`GET /templates` 照样报 `READY`，**看起来完全健康，只有 create 会失败**。所以：

- `04-provision-templates.sh` 不以 "status: READY" 为准，它跑 `smoke_test.py --all`，对每个模板真的建一次沙盒
- `05-run-benchmark.sh` 开跑前先做一次 smoke 预检，免得 20 分钟后才发现白跑
- `99-teardown.sh --stop` 之后再开机，**必须重跑 `04-provision-templates.sh`**

**2. `.env` 里的覆盖项必须放在最前面。** `scripts/_lib.sh` 的 `load_env()` 只在变量尚未设置时才赋值，所以文件里**第一次出现的才生效**。`03-setup-env.sh` 因此是把覆盖项**前置**写入的 —— 追加到末尾会静默失效。

**3. 数据卷不能按设备名找。** Nitro 上 NVMe 的枚举顺序取决于设备响应发现的顺序，既不是 block device mapping 的顺序，也不保证跨重启稳定。`02-prepare-disks.sh` 用 `lsblk -o +SERIAL` 把 EBS volume ID 和 NVMe 序列号对上，并且在动手前拒绝任何"是根盘"或"已有挂载点"的设备 —— 猜错设备名的代价是格式化根文件系统。

## 状态与安全

- `tools/.state` 记录 instance-id、私网 IP、公网 IP、两个 volume-id，让 01–05 可以分别执行、重跑、中断后续跑。**已 gitignore**，因为它指向真实资源。
- `tools/config.env` 同样已 gitignore。
### 网络：默认继承**执行脚本这台机器**的位置

`VPC_ID` / `SUBNET_ID` / `AVAILABILITY_ZONE` / `SECURITY_GROUP_ID` 全部留空时，benchmark 实例会被放到**和你运行这些脚本的机器完全相同的 VPC、子网、安全组**里（从它自己的 instance metadata 读出来）。这样可达性是**构造上保证**的：安全组里那条自引用入站规则（VPC default 组唯一的规则就是它）之所以放行，正是因为两端都在这个组里。

**脚本从不创建任何网络资源。** 它自己造出来的网络很容易落在工作机连不到的地方 —— 这个默认行为就是为了避免那种失败。

优先级（从最具体到最兜底）：

| 项 | 顺序 |
| --- | --- |
| VPC | `SUBNET_ID` 所属 VPC → `VPC_ID` → 工作机的 VPC → 默认 VPC |
| 子网 | `SUBNET_ID` → `AVAILABILITY_ZONE` → 工作机的子网 → 同 VPC 内其他子网（容量兜底） |
| 安全组 | `SECURITY_GROUP_ID` → 工作机的安全组 → VPC default 组 |

裸金属 48xl 不是每个 AZ 都有货，所以 AZ 是**按顺序试**的：**工作机自己的 AZ 排第一**（同 AZ 省掉跨区流量费，SSH 和结果传输也更快），没货再退到同 VPC 的其他 AZ —— 换子网仍然连得通，VPC 的 local 路由和共享的安全组两者都还成立。

工作机不是 EC2 实例（读不到 IMDS）时，退回默认 VPC + default 组，并明确警告：那种组合**大概率不放通来自 AWS 之外的 SSH**，这时必须显式填 `SUBNET_ID` 和 `SECURITY_GROUP_ID`。

**不假设，直接查。** 共享安全组只有在那个组**真的有**自引用规则时才管用 —— VPC default 组有，手工建的组经常没有。所以 `00-preflight.sh` 和 `01-create-instance.sh` 都会在开跑前用 `sg_reachable.py` 拿真实规则算一遍"这台机器能不能 SSH 进去"，能就打印是哪条规则放行的，不能就在**实例还没开始计费之前**说清楚。否则这个错误的表现形式是几分钟后一个没有解释的 SSH 超时。

- **安全组的实际含义**：VPC default 组组内互通是**全端口**，不只 22。也就是说同组的其他实例能访问这台机器上**无认证的 CubeAPI（:3000）**和 Web 控制台（:12088）。但互联网侧进不来 —— default 组没有任何基于 CIDR 的入站规则，即使实例有公网 IP 也一样。如果你账号里 default 组还挂着别的东西、这种共享暴露不可接受，就显式指定一个安全组。

### 公网 IP：默认会有，而且是需要的

`ASSOCIATE_PUBLIC_IP=auto`（默认）表示继承子网的 `MapPublicIpOnLaunch`。**默认 VPC 的子网这个属性是开的**，所以按默认配置跑出来的实例**会有公网 IP**。`01-create-instance.sh` 会把实际结果打出来并写进 state，不让它成为意外。

这个公网 IP 是**有用途的**：`03-setup-env.sh` 要下载 Docker 包、CubeSandbox 一键包（约 400 MB，从 GitHub）、HumanEval/MBPP 数据集，还要访问 ECR。设成 `no` 之前必须先有别的出网路径（NAT Gateway，或者 S3/ECR 的 VPC Endpoints），否则 `03-setup-env.sh` 会卡在第一个下载上。`00-preflight.sh` 会提前替你查 IGW/NAT 并在两种情况下都拦住你。

要点是：**决定安全边界的是安全组，不是有没有公网 IP。** 入站只有 22 且限 VPC 内，所有脚本也一律连**私网**地址。但只要这台机器有公网 IP，任何一次"临时把端口开到 0.0.0.0/0 调试一下"都会立刻把无认证的 CubeAPI 暴露出去。

| 想要 | 配置 | 前提 |
| --- | --- | --- |
| 默认（有公网 IP，走 IGW 出网） | `ASSOCIATE_PUBLIC_IP=auto` | VPC 有 IGW |
| 强制要公网 IP | `ASSOCIATE_PUBLIC_IP=yes` | 同上 |
| 不要公网 IP | `ASSOCIATE_PUBLIC_IP=no` | **必须**有 NAT 或 VPC Endpoints |
- SSH key 不是 600 时，`lib.sh` 会在 `~/.ssh/` 下留一份 600 的私有副本，而不是改你原来的文件权限。
- 两块卷都是 `DeleteOnTermination=true`，`99-teardown.sh` 还会再确认一遍确实删掉了。

## 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `01-create-instance.sh` 报没有容量 | 裸金属 48xl 不是每个 AZ 都有货 | 脚本会自动换 AZ 重试；全都没有就换 region 或申请容量预留 |
| `03-setup-env.sh` 卡在 apt 锁 | Ubuntu 首启在跑 unattended-upgrades | 已内置等待（`wait_for_cloud_init`）；直接重跑即可 |
| 镜像构建 401 / `authorization token has expired` | 没有 IAM instance profile，或 ECR token 过期 | 填 `IAM_INSTANCE_PROFILE`；`provision-templates.sh` 现在会自己取新 token |
| `create ... is not available locally` | 重启过，模板没了 | `bash tools/04-provision-templates.sh` |
| 模板构建超时 | 镜像里的服务没在 Cube 固定的 ~30s 探针预算内答 2xx | 看 `/data/log/template/<tid>_0/` 下的构建日志 |
| `02-prepare-disks.sh` 说设备已有文件系统 | 卷上有数据 | 确认要erase 再 `FORCE_MKFS=1 bash tools/02-prepare-disks.sh` |
| `00-preflight.sh` 报 `no inbound rule permits port 22` | 安全组里没有放行本机的规则（共享的组没有自引用规则，或者工作机不在那个组里） | 给组加一条 22 入站，或把 `SECURITY_GROUP_ID` 指向一个有的组 |
| `01-create-instance.sh` 警告工作机不是 EC2 实例 | 从笔记本直接跑，读不到 IMDS | 显式填 `SUBNET_ID` + `SECURITY_GROUP_ID`，且该组要放通你的出口 IP |

# CADFlow Lite

CADFlow Lite 是面向 EDA / CAD 运维团队的轻量级 LSF 与 FlexNet 可观测门户。它将作业、用户、队列、计算节点和 License 用量汇聚到一个 Web 界面，适合在 Rocky Linux 上以原生 Python + systemd 方式部署。

不依赖 Docker、Podman、Docker Compose 或 Maven/POM；服务启动后直接访问 `http://服务器IP:8080`。

## 功能概览

| 模块 | 解决的问题 |
| --- | --- |
| 总览 | 快速查看运行/等待/异常作业、Slots、资源利用率与告警 |
| 作业 | 按用户、状态筛选作业；查看提交时间、缩略作业名、提交/执行节点、资源申请与运行时长 |
| 用户 | 汇总各用户的作业数、运行 Slots 与资源占用，并可搜索过滤 |
| 队列 | 查看 LSF 队列的状态、优先级、并发与等待情况 |
| 节点 | 查看 Slots、CPU 利用率、Load、可用内存、`/tmp` 与 Swap 等 LSF 主机资源 |
| License | 读取 FlexNet `lmstat`，按 Vendor、特征名和状态筛选许可证使用情况 |
| 配置 | 在 Web 中分别配置 LSF 与 FlexNet 采集源，并通过只读 LSF 预检后启用真实采集 |

## 产品界面

以下图片来自当前 CADFlow Web 界面。截图中的数据为演示数据，仅用于展示布局与交互；生产数据由现场 LSF 与 FlexNet 命令只读采集。

### 1. 集群总览

一屏汇总作业状态、Slots、资源利用率、活跃告警及队列负载趋势，适合日常值守。

![CADFlow 集群总览](docs/screenshots/overview.png)

### 2. 作业

支持按用户和作业状态过滤；每个作业显示提交时间、缩略作业名称、提交节点、执行节点、申请资源与运行时长。

![CADFlow 作业视图](docs/screenshots/jobs.png)

### 3. 用户

按 LSF 用户汇总运行、等待、异常作业与 Slots 使用，便于定位资源占用来源。

![CADFlow 用户视图](docs/screenshots/users.png)

### 4. 队列

展示队列优先级、开放/激活状态、作业数、等待数和运行数，辅助判断调度瓶颈。

![CADFlow 队列视图](docs/screenshots/queues.png)

### 5. 节点

展示节点可用 Slots、CPU、Load、可用内存、`/tmp` 与 Swap；数据直接对应 LSF 的 `bhosts` 与 `lsload` 输出。

![CADFlow 节点视图](docs/screenshots/hosts.png)

### 6. License

汇总 FlexNet 特征许可的总数、已用数、剩余数和风险状态，提供关键字、Vendor 与状态筛选。

![CADFlow License 视图](docs/screenshots/licenses.png)

### 7. 采集源配置

LSF 与 FlexNet 独立配置：LSF 使用固定白名单命令采集；FlexNet 指定 `lmstat`、License Server 与一个或多个 Vendor。保存时会执行 LSF 只读预检。

![CADFlow 采集源配置](docs/screenshots/config.png)

## 数据采集方式

```text
LSF 命令 (bjobs / bqueues / bhosts / lsload) ─┐
                                               ├─> CADFlow 采集器 ─> SQLite ─> Web / API / Prometheus
FlexNet 命令 (lmstat -a) ──────────────────────┘
```

- 采集命令固定为只读白名单：`bjobs`、`bqueues`、`bhosts`、`lsload`、`lmstat`。
- LSF 和 FlexNet 失败彼此隔离：License 暂不可用时，LSF 的作业、队列和节点数据仍可继续采集。
- 采集结果保存在 SQLite（WAL 模式）；`/metrics` 可供 Prometheus 抓取。

## Rocky Linux 一键安装

在 Rocky 10.2（或兼容环境）中执行：

```bash
git clone https://gitee.com/raychade/cadflow-lite.git
cd cadflow-lite
sudo bash deploy/install-rocky10.sh
```

安装完成后，打开：

```text
http://ROCKY_IP:8080
```

默认以 Demo 模式启动，便于立即验证页面。真实 LSF 环境请在左侧 **配置** 页面填写 LSF 和 FlexNet 参数后保存。

常用目录：

```text
/opt/cadflow-lite/current                 当前运行版本（软链接）
/opt/cadflow-lite/releases/<timestamp>    各版本发布目录
/etc/cadflow-lite/cadflow.env             持久化服务配置
/var/lib/cadflow-lite/cadflow.db           SQLite 数据和 Web 配置
```

如果不希望安装脚本调整防火墙：

```bash
sudo bash deploy/install-rocky10.sh --no-firewall
```

## 配置真实 LSF / FlexNet

在 **配置** 页面填写：

1. 运行模式选择 **LSF**，并填写集群名称。
2. 填写 LSF Bin 目录，例如 `/eda/lsf/10.1/linux2.6-glibc2.3-x86_64/bin`。
3. 在 LSF 环境变量中填写 `LSF_ENVDIR`、`LSF_SERVERDIR`、`LSF_LIBDIR`、`LSF_BINDIR` 等现场变量。
4. 填写 `lmstat` 的完整路径和 License Server，例如 `27000@cad01`。
5. License Vendor 可填写多个名称，使用英文逗号分隔，例如：`snpslmd,cdslmd,mgcld`。
6. 点击 **保存配置并预检 LSF**。预检通过后，后续采集即使用现场真实命令。

建议先以服务账号验证现场命令：

```bash
sudo -u cadflow bash -lc '
  source /eda/lsf/conf/profile.lsf
  bjobs -u all -a
  bqueues -w
  bhosts -w
  lsload -w
  /eda/license/flexlm/lmstat -a -c 27000@cad01
'
```

## 运行、日志与升级

```bash
# 服务状态与日志
sudo systemctl status cadflow-lite --no-pager
sudo journalctl -u cadflow-lite -f

# 重启服务
sudo systemctl restart cadflow-lite

# 从 Gitee master 一键升级，失败自动回滚
sudo bash /opt/cadflow-lite/current/deploy/upgrade-rocky10.sh
```

服务由 systemd 管理，并在安装时启用开机自启；服务器重启后 CADFlow 会自动恢复。

## API

| API | 用途 |
| --- | --- |
| `GET /api/health` | 采集健康、部分失败信息、陈旧状态和快照年龄 |
| `GET /api/summary` | 集群总览 |
| `GET /api/jobs` | 作业列表，支持状态/用户/队列过滤 |
| `GET /api/queues`、`/api/hosts`、`/api/licenses` | 队列、节点、License 数据 |
| `GET /api/config`、`PUT /api/config` | Web 配置 |
| `POST /api/collect` | 立即触发一次采集 |
| `GET /metrics` | Prometheus 指标 |

## 开发验证

```bash
python3 -m pytest -q
```

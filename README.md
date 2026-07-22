# CADFlow Lite

面向芯片公司 CAD/LSF 运维的轻量监控门户。它运行在 Rocky Linux 原生 Python + systemd 环境中，不使用 Docker、Podman、Docker Compose 或 Maven/POM。

## 产品效果

### 集群总览

![CADFlow 集群总览](docs/screenshots/overview.png)

### 用户资源使用

![CADFlow 用户资源使用](docs/screenshots/users.png)

### LSF / FlexNet 配置

![CADFlow 采集源配置](docs/screenshots/config.png)

## 功能

- LSF 作业、队列、主机与 FlexNet License 只读采集
- Web 首次配置：集群名、LSF Client 路径、License Server 与环境变量
- SQLite WAL 历史数据、陈旧状态、Prometheus `/metrics`
- 固定命令白名单：`bjobs`、`bqueues`、`bhosts`、`lsload`、`lmstat`
- Rocky 10.2 一键安装、systemd 开机自启和自动升级/回滚

## Rocky 10.2 一键安装

从 Gitee 获取源码后，在源码目录执行：

```bash
git clone https://gitee.com/raychade/cadflow-lite.git
cd cadflow-lite
sudo bash deploy/install-rocky10.sh
```

安装位置：

```text
/opt/cadflow-lite/current                 当前运行版本（软链接）
/opt/cadflow-lite/releases/<timestamp>    各版本发布目录
/etc/cadflow-lite/cadflow.env             持久化服务配置
/var/lib/cadflow-lite/cadflow.db           SQLite 数据与 Web 配置
```

默认以 Demo 模式监听 `0.0.0.0:8080`，并放行 Rocky 防火墙的 TCP/8080。浏览器打开：

```text
http://ROCKY_IP:8080
```

若不希望脚本修改防火墙：

```bash
sudo bash deploy/install-rocky10.sh --no-firewall
```

## Web 配置真实 LSF

在页面左侧进入 **配置**，填写：LSF Bin 目录、`lmstat` 路径、License Server、Vendor 和必要的 `LSF_*` 环境变量。保存时会执行固定的只读预检；只有预检成功，配置才会切换为 LSF 采集。

预检前，先使用将来运行服务的账号确认现场命令支持以下字段：

```bash
bjobs -u all -a -noheader -o "jobid user stat queue from_host exec_host job_name submit_time slots max_mem run_time proj_name delimiter='|'"
bqueues -w
bhosts -w
lsload -w
lmstat -a -c 27000@license01
```

## 运行、日志与重启

```bash
sudo bash deploy/run-rocky10.sh
sudo systemctl status cadflow-lite --no-pager
sudo journalctl -u cadflow-lite -f
sudo systemctl restart cadflow-lite
```

服务由 systemd 管理，已启用开机自启；Rocky 重启后无需重新运行项目命令。

## 一键自动升级

默认从 Gitee `master` 获取最新版本，构建新的原生 Python 发布目录，备份 SQLite，切换 `current` 软链接并进行健康检查。失败会自动回滚到上一版本。

```bash
sudo bash deploy/upgrade-rocky10.sh
```

也可使用已下载的本地源码升级：

```bash
sudo bash deploy/upgrade-rocky10.sh --source /path/to/cadflow-lite
```

升级不会覆盖 `/etc/cadflow-lite/cadflow.env` 或 `/var/lib/cadflow-lite/cadflow.db`。

## API

| API | 用途 |
|---|---|
| `GET /api/health` | 采集健康、陈旧状态与快照年龄 |
| `GET /api/summary` | 集群总览 |
| `GET /api/jobs` | 作业列表，支持状态/用户/队列过滤 |
| `GET /api/queues`、`/api/hosts`、`/api/licenses` | 资源状态 |
| `GET /api/config`、`PUT /api/config` | Web 配置 |
| `POST /api/collect` | 手工触发采集 |
| `GET /metrics` | Prometheus 指标 |

## 验证

```bash
python3 -m pytest -q
```

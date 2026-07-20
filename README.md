# CADFlow Lite

面向芯片公司 CAD/LSF 运维的轻量监控 MVP。它提供统一 Web 门户，用于观察 LSF 作业、队列、计算节点和 FlexNet License，并暴露 Prometheus 指标。

## 已实现

- 集群总览：RUN/PEND/EXIT、Slot、CPU、内存和活动告警
- 作业视图：用户、项目、队列、节点、内存效率和CPU效率
- 队列视图：最大Slot、运行、等待和挂起作业
- 节点视图：状态、Slot、CPU、内存和Load
- License视图：Feature容量、使用率和风险级别
- SQLite WAL存储和历史趋势
- `/metrics` Prometheus端点
- Demo与真实LSF两种采集模式
- 只读命令白名单，拒绝任意Shell
- 可选管理Token保护手工采集接口
- Docker Compose、健康检查和非root容器

## 立即体验

### Docker Compose

```bash
export CADFLOW_ADMIN_TOKEN='replace-with-a-long-random-token'
docker compose up --build -d
```

浏览器访问 `http://SERVER_IP:8080`。默认使用Demo数据，不需要LSF环境。

### Python虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
CADFLOW_MODE=demo uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## 接入真实LSF

在能够运行LSF客户端命令的只读采集节点上配置：

```bash
export CADFLOW_MODE=lsf
export CADFLOW_CLUSTER_NAME=production-lsf
export CADFLOW_DB_PATH=/var/lib/cadflow/cadflow.db
export CADFLOW_ADMIN_TOKEN='replace-with-a-long-random-token'
export CADFLOW_LMSTAT_PATH=/opt/flexnet/bin/lmstat
export CADFLOW_LICENSE_SERVERS='27000@license01,27001@license02'
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

当前适配器使用以下只读命令：

- `bjobs -u all -a -noheader -o ...`
- `bqueues -noheader -o ...`
- `bhosts -noheader -o ...`
- `lmstat -a -c <server>`

不同LSF版本的 `-o` 字段支持可能不同。生产接入前，应在测试集群验证命令输出，并根据实际IBM Spectrum LSF版本调整字段映射。

## API

| API | 用途 |
|---|---|
| `GET /api/health` | 采集健康状态 |
| `GET /api/summary` | 总览指标 |
| `GET /api/jobs` | 作业列表，支持status/user/queue过滤 |
| `GET /api/queues` | 队列状态 |
| `GET /api/hosts` | 节点状态 |
| `GET /api/licenses` | License容量 |
| `GET /api/alerts` | 内置规则告警 |
| `GET /api/history` | 历史趋势 |
| `POST /api/collect` | 手工触发采集，使用`X-Admin-Token` |
| `GET /metrics` | Prometheus文本指标 |

交互式API文档位于 `/docs`。

## 安全边界

- 采集器使用参数数组执行命令，不使用 `shell=True`。
- 只允许 `bjobs/bqueues/bhosts/lsload/lmstat`。
- 容器默认使用非root用户、只读根文件系统和`no-new-privileges`。
- 管理Token应通过环境变量或Secret管理，不写入代码仓库。
- 轻量版尚未提供用户SSO和细粒度RBAC；不应直接暴露到互联网。
- 推荐由Nginx/企业网关提供TLS、访问控制和审计。

## 测试

```bash
pytest -q
```

## 下一阶段

- 对接AD/LDAP/OIDC和角色权限
- PostgreSQL/TimescaleDB存储
- 多集群Collector/Server分离
- PEND/EXIT根因分类与作业资源推荐
- License拒绝日志、到期时间和容量预测
- Alertmanager、飞书/邮件通知
- 存储I/O和作业性能关联
- AI只读诊断助手

## License

本MVP为独立实现，没有复制`lsfMonitor`源代码。正式开源或商业化前，请根据组织策略补充许可证文件。

# CADFlow Lite 代码交付规范

每次功能开发和代码提交遵循以下检查清单：

1. 功能行为、配置、API、安装或升级方式发生变化时，同步更新 `README.md`。
2. README 保留清晰的产品功能介绍，并维护总览、作业、用户、队列、节点、License、配置等主要界面的 Demo 截图。
3. 新用户应能通过 `sudo bash deploy/install-rocky10.sh` 完成安装，并直接访问 `http://服务器IP:8080`。
4. 已安装实例应能通过 `sudo cadflow-update` 一键升级；升级必须备份数据库、执行健康检查，并在失败时自动回滚。
5. 提交前至少执行：

```bash
python3 -m pytest -q
bash -n deploy/install-rocky10.sh
bash -n deploy/upgrade-rocky10.sh
bash -n deploy/cadflow-update
```

Windows 环境只能完成代码与静态测试；Rocky Linux、systemd、真实 LSF/FlexNet 的最终结果必须在目标服务器验证。

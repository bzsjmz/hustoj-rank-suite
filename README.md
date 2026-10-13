# HUSTOJ Rank Suite

HUSTOJ 排行榜采集、历史统计、图片渲染与 AstrBot 查询套件。使用 Playwright 持久浏览器进行已授权访问，WebVPN/CAS 登录由管理员人工完成。

## 架构与边界

采集器完整分页校验后写入 SQLite，并生成不可变当前数据版本，通过 current-data 原子切换。AstrBot 只读消费共享文件，通过 Unix socket 请求独立渲染器；提交记录查询通过采集端的有界请求服务完成。浏览器认证、SQLite 和学生数据均留在运行存储，不进入 Git。

主循环串行，默认等待间隔 300 秒，实际周期还包含采集与发布耗时；额外年级每日错峰更新。图片默认只预渲染前三页，其余按需。

## 部署准备

适用于 Linux/systemd。需要 Python 3.12、Chromium 运行依赖，以及 Xvfb、Fluxbox、x11vnc、noVNC、websockify 和 xdpyinfo。AstrBot 另行部署，插件当前声明支持 aiocqhttp。

```bash
git clone https://github.com/bzsjmz/hustoj-rank-suite.git
cd hustoj-rank-suite
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p data logs share
PLAYWRIGHT_BROWSERS_PATH="$PWD/data/ms-playwright" .venv/bin/python -m playwright install chromium
cp .env.example .env
chmod 600 .env
```

先填写获授权的 WebVPN/OJ 入口、年级前缀和学号布局；示例 URL 不可直接使用。OJ_MAJOR_LABELS_FILE 可指向私有专业名称 JSON，格式见 examples/major-labels.json。两院划分按 COLLEGE_SPLIT_SNAPSHOT_ID 的冻结名单执行，不是按当前专业编码自动推断；默认值须由部署者核对。命令名称仍保留两院业务约定。

确认实际数据盘块设备后生成并审查 unit：

```bash
bash scripts/install_systemd.sh --io-device /dev/DEVICE --render-dir /tmp/oj-rank-units
# 审查后，以 root 安装；此操作 enable 采集、渲染、监控服务，不启动服务。
sudo bash scripts/install_systemd.sh --io-device /dev/DEVICE
sudo systemctl start oj-rank.service oj-rank-render.service oj-rank-resource-monitor.service
```

脚本按当前项目路径生成 unit；模板使用系统服务权限，需要先审查资源配额与主机布局。不要覆盖已有生产部署。备份 timer 独立安装，见 [备份说明](docs/DATABASE_BACKUP.md)。

## 人工认证

在本地电脑执行 `ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:16080:127.0.0.1:6080 USER@SERVER`，打开 `http://127.0.0.1:16080/vnc.html` 完成登录。VNC/noVNC 仅监听回环，不开放公网。认证失效后服务等待管理员，禁止保存或自动填写账号密码。

## AstrBot 接入

将 plugins/astrbot_plugin_oj_rank 作为独立插件目录安装到 AstrBot 插件路径；本仓库为套件，不能假设通过整个仓库 URL 自动发现子目录插件。给容器挂载项目 share 到 /oj-rank-share（只读），run 到 /oj-rank-render（只读），并核对 socket 权限。不要挂载 data 或认证 profile。

普通命令、管理员统计和配置见 [插件说明](plugins/astrbot_plugin_oj_rank/README.md)。allowed_users 与 allowed_groups 均为空时允许所有会话，正式使用前明确配置访问范围。

## 验证与运维

```bash
.venv/bin/python -B -m unittest discover -s tests -v
(cd plugins/astrbot_plugin_oj_rank && ../../.venv/bin/python -B -m unittest discover -s tests -v)
.venv/bin/python -B -m app.publication_audit --verify-snapshot
python3 -B scripts/check_public_release.py --history
```

测试使用隔离临时数据；发布诊断须在已配置的运行目录执行。进程 active 不等于业务健康，必须核对认证、新成功快照、完整产物和插件加载。

- [当前数据发布协议](docs/DATA_PUBLICATION.md)
- [发布诊断与恢复](docs/PUBLICATION_RECOVERY.md)
- [安全边界](SECURITY.md)
- [公开发布检查](docs/PUBLIC_RELEASE.md)

SQLite、历史 gzip、当前数据版本和图片仍是不同提交边界。完整数据恢复需单独规划 DB/share 对齐，不能直接覆盖数据库。

## 历史单日新增总榜

新增 `/历史单日榜 [页码]`：按历史每个用户每天新增 AC 排名，同一人可多次上榜，支持图片分页与文字备用。历史快照自动回填；统计按北京时间，以零点前 30 分钟内快照为端点，仅纳入已结束且基准可靠的日期。详见 [统计口径与升级说明](docs/HISTORICAL_DAILY.md)。

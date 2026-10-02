# 统筹耐心资本的联合承诺与退出约束基础平台

本项目是一套可离线运行的 Python 服务端平台，供创新药企业的研发管理、转化医学、商务拓展和基金运营团队管理候选药实验记录、研发证据、协作中心资源、尽调交接通道、交易风险告警与跟进任务，并为早期生物医药项目的母基金、市场化基金和产业方保存联合资本承诺、条件出资、跟投权、用途限制、现金流与承诺争议。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/portfolio_ops/：研发中心、尽调通道、研究资源、交接计划和商业情景；
- src/discovery_lab/：研究协议、实验记录、异常排除、分析任务租约和候选结论；
- src/licensing_ops/：管线信息、交易风险告警、跟进工单和资源分配；
- src/capital_ops/：轮协议版本、多方承诺确认、出资窗口、条件先后、跟投权、用途限制、调用与实缴、返还分配、争议冻结与周期结算；
- fixtures/：离线验收使用的研究协议与结构化实验记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 联合资本承诺与结算（capital_ops）

- **仅追加事件账本**：参与方登记、每轮协议版本、承诺、出资窗口、条件、跟投权、用途限制、调用、实缴、分配返还、争议与项目调整都以带哈希链的不可变事件保存；当前余额与周期对账单只是事件投影。
- **多方分别确认**：母基金、市场化基金、产业方只能确认自己名下的承诺；未确认或前置条件未满足的资金不得被调用。
- **条件先后**：窗口级、轮级、承诺级条件可声明依赖顺序，依赖未满足不得勾选。
- **争议部分冻结**：争议只冻结相关承诺、调用单、分配或窗口及金额，无争议部分继续执行；争议解决只追加解冻事件。
- **项目调整**：阶段失败等调整通过追加核减/恢复/取消决定并留下原因，历史事件不改写。
- **周期重算**：对账单可指定 `as_of` 从有效事件重新计算；每笔投入与返还都能经接口追溯到调用单、窗口条件、用途限制与分配来源。
- **重启恢复**：进程重启后重放事件即可恢复全部尚未解决的承诺冲突与冻结金额，哈希链用于校验账本完整性。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.acceptance --workspace .
PYTHONPATH=src python3 -m discovery_lab.acceptance --workspace .
PYTHONPATH=src python3 -m licensing_ops.acceptance
PYTHONPATH=src python3 -m capital_ops.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析、交易风险处置，以及联合资本的多方确认、条件出资、争议部分冻结、追加调整、周期重算与重启恢复，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m capital_ops.api --database capital.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。联合资本服务的主要接口包括：

- `POST /parties`、`POST /rounds/versions`、`POST /commitments`、`POST /windows`、`POST /conditions`、`POST /restrictions`、`POST /follow_ons`；
- `POST /commitments/confirmations`（参与方分别确认自己的承诺）、`POST /follow_ons/{id}/exercise`；
- `POST /capital_calls`（未满足条件返回 409 及结构化原因）、`POST /receipts`；
- `POST /distributions`、`POST /distributions/{id}/payments`；
- `POST /disputes`、`POST /disputes/{id}/resolve`、`GET /disputes/unresolved`；
- `GET /rounds/{id}/statement?as_of=...`、`GET /commitments/{id}`、`GET /commitments/{id}/eligibility`、`GET /receipts/{id}/explain`、`GET /distributions/{id}/explain?commitment_id=...`、`GET /audit/chain`。

除 `/health` 外所有接口都需要 `X-Actor-Id` 请求头。

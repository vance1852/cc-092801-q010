# 统筹耐心资本的联合承诺与退出约束基础平台

本项目是一套可离线运行的 Python 服务端平台，供创新药企业的研发管理、转化医学、商务拓展和基金运营团队管理候选药实验记录、研发证据、协作中心资源、尽调交接通道、交易风险告警、跟进任务，以及早期生物医药项目的联合资本承诺与结算。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/portfolio_ops/：研发中心、尽调通道、研究资源、交接计划和商业情景；
- src/discovery_lab/：研究协议、实验记录、异常排除、分析任务租约和候选结论；
- src/licensing_ops/：管线信息、交易风险告警、跟进工单和资源分配；
- src/capital_ops/：联合资本协议版本、出资窗口、先决条件、跟投权、用途与退出限制、资本调用、现金流、争议部分冻结与周期核算；
- fixtures/：离线验收使用的研究协议与结构化实验记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

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

四条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析、交易风险处置，以及一轮联合资本承诺：多方分别确认条款、未满足条件的出资被阻断、争议仅冻结相关承诺金额、重启后恢复未决冲突并从有效现金流事件重算周期台账，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m capital_ops.api --database capital.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。资本结算服务的主要接口：

- `POST /projects`、`POST /agreement_revisions`：建立项目并逐轮保存协议版本（承诺、出资窗口、先决条件、跟投权、用途/退出限制）；追加版本必须给出 `adjustment_kind` 与 `reason`，旧版本自动置为 superseded 并保留追加决定；
- `POST /projects/{id}/confirmations`：参与方用户（仅本机构）分别确认整份承诺或单个窗口/条件/权利/限制条款；
- `POST /projects/{id}/conditions`：风控将先决条件标记为 satisfied、waived 或 failed；
- `POST /capital_calls`：按窗口开放日期、先决条件状态与争议冻结切分 callable / blocked / partial，未满足条件的金额 callable 为 0；
- `POST /cash_events`：登记 contribution 或 return；出资必须落在开放窗口、条件均已满足/豁免且不超过争议冻结后的可调用额度，返还要给出按出资窗口冲减净投入的依据，可带用途类目做用途限制校验；
- `POST /disputes`、`POST /disputes/{id}/resolve`：争议只冻结相关承诺/窗口的金额（`dispute_freezes` 精确到调用条目份额），其他参与方继续执行；
- `GET /projects/{id}/commitments/{party_id}`、`/projects/{id}/ledger`、`/projects/{id}/cash_events`：窗口可调用性、争议冻结、从 booked 事件重算的周期台账与逐笔依据；
- `GET/POST /recover`：进程重启后恢复未决争议、冻结条目、未结清调用与未确认承诺。

所有写接口要求 `X-Actor-Id` 头，并按 manager / party / risk / auditor 角色鉴权；资本调用与现金流支持幂等键。

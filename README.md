# 带参与选择与出价机制差异的鲜果竞价保留价分析

本地分析工作台保留已有业务资料与预览能力，增加了独立分析资料的校验、HTTP 预览与保留价推断。`data/study` 保存本地合成资料，`app/study.py` 解释资料结构；`app/reserve_analysis.py` 与 `app/reserve_service.py` 提供保留价分析，经验弱假设层给可核查区间、结构强假设层给情景外推并明确标注，信息不足时返回 null 与诊断而不是编造结论。

数据均为本地合成，不含真实姓名、电话号码或生产系统凭据。`standard` 是常规资料，`edge` 用于检查信息不足或约束冲突。它们只描述观测，不预设分析结论。

## 本地使用

Python 3.11，依赖版本固定在 `requirements.txt`。NumPy/SciPy 是分析工作台的数值工具；预览逻辑主要使用标准库。准备阶段安装依赖，仓库不在启动或测试时下载任何资源。

支持 Python 3.11 系列的补丁版本；Docker 基础镜像通过 SHA256 固定为 Python 3.11.14，依赖清单保持精确版本。相同依赖还需在非 root 的 Python 3.11.2 Linux 环境中验证，应用不依赖特定补丁版本。

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m app.demo
python -m app.server
```

服务监听 `127.0.0.1:8080`，也可通过 `PORT` 调整。预览请求由 `app/client.py` 发起；`app.demo` 在临时端口启动真实服务并完整打印现有业务输出与 `/reserve-analysis` 分析结果，不是录制日志的回放。服务接口和数据文件的可见性约定不能互相替代。

## 可复现环境

```sh
docker build -t local/fruit-price-index:baseline .
docker run --rm --network none local/fruit-price-index:baseline python -m unittest discover -s tests -v
docker run --rm --network none local/fruit-price-index:baseline
```

准备阶段允许构建镜像和预装依赖；正式演示不进行构建或安装。默认演示只显示已有功能。新能力完成后，请使演示通过实际 HTTP 消费调用展示输入口径、分析结果和限制，保留完整输出；小样本预期在 90 秒内结束。功能测试启用方式：`RUN_ACCEPTANCE=1 python -m unittest tests.test_acceptance -v`，覆盖新接口的字段与信息不足契约；完整测试（含可手工核对的单元测试）见 `tests/test_reserve_analysis.py`。

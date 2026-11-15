# 运行与验证

现有业务预览、资料校验及真实 HTTP 传输由 `python -m unittest discover -s tests -v` 验证；`python -m app.demo` 临时启动服务，通过消费端读取旧业务和新资料的描述输出。固定依赖的安装和容器准备方法见 README。

`RUN_ACCEPTANCE=1 python -m unittest tests.test_acceptance -v` 验证新分析消费接口的字段契约、信息不足处理、heldout 只核查不调参、manager 场次排除、双机制情景与坏请求拒绝。该测试不是算法正确性的证明；`tests/test_reserve_analysis.py` 补充小规模可手工核对计算（Wilson/t 区间、参与与流拍计数、信息截止修订回归、集团关联、ascending 与混合机制分流、零出价与全流拍健壮性）。`python -m app.demo` 通过真实 HTTP 调用 /reserve-analysis 展示输入口径、分析结果与限制，不按资料名返回静态结论；standard+edge 两个资料的演示约 12 秒，在 90 秒预算内。结构层若未通过 heldout 收入覆盖核查，会在 diagnostics 以 structural_interval_failed_heldout_revenue_check 明示，其外推区间不得当作已验证的提价收益。

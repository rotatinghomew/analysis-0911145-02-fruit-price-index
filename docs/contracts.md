# 产品数据与调用约定

sessions 是实际竞价场次，reserve_cents 与 bid_cents 均为每箱整数分。first_price 是最高有效出价者按自身出价成交；ascending 的退出记录只提供估值界限，不得当密封报价。invited 是参加机会，不出价不等于估值零。reserve_assignment 的 rotation 与 manager 表示不同选择机制，不保证经理定价可识别。bids 以 sale/bidder 为逻辑键，revision 只在 known_at_week 不晚于 as_of_week 时生效；同 buyer_groups 表示集团关联。heldout 场次不能用于调参。新消费接口 POST /reserve-analysis 接收 {case, reserve_candidates_cents?, minimum_sell_probability?}，返回 {snapshot, mechanism, candidates, validation, diagnostics}。candidates 的价格、收入/售出概率或边界及支持情况必须有单位和假设；mechanism 交代估值、参与和关联假设。资料不足不要求返回唯一最优值。

现有 GET /health、GET /cases 和 POST /preview 保留原有资料预览。新增 GET /study-cases 和 POST /study-preview {case} 只校验与描述 data/study 的资料。推断消费路由 POST /reserve-analysis 已接通：app.client.analysis 指向该接口，请求 {case, reserve_candidates_cents?, minimum_sell_probability?}，返回 {snapshot, mechanism, candidates, recommendation, validation, diagnostics, provenance}。所有 snapshot 都是相应原始资料字节的 SHA-256，输出仅使用有限 JSON 数值，无法判断用明确状态与 null 表达，不得返回 NaN。资料只供本地分析，不连接外部服务。

candidates 是记录列表，逐项含 reserve_cents、sell_probability_interval、revenue_interval_cents（每箱整数分口径的区间，单位 cents_per_box）；后两项为有含义的上下界或 null，不能把外推伪装成观测支持。顶层区间只来自经验弱假设层（rotation 场次），观测保留价范围外一律为 null 且 support=unsupported；结构层情景放在每行 structural.{grouped,independent} 内，跨范围结果标 support=extrapolation。

机制口径：不同 format（first_price/ascending）分别估计不混估，ascending 退出记录只作估值界限、不套密封一价结构模型；rotation 场次才进入估计，manager 场次排除并在 diagnostics 说明，heldout 只用于 validation 不调参。grouped 把同 buyer_groups 视为一个协调竞争单位，independent 按名义买家独立竞争，两套假设的建议差异见 recommendation.structural_sensitivity。minimum_sell_probability 为业务底线，floor_decision 只在区间下界不低于底线时才判 meets_floor；收入优势仅在弱假设层区间严格不重叠时才宣称，否则保留意见。provenance 给出邀请名单、报价记录（含 revision 汇总）与集团表的独立 SHA-256，可追溯所用版本。

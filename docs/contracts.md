# 产品数据与调用约定

catalog 中 sku/version 的 valid_from 为含端点周，valid_to 为不含端点周；null 表示持续有效。标价不代表成交价。transactions 的 gross_cents 含运费 shipping_cents；adjustments 以 recorded_week 决定发布时可见性，refund_cents 和 returned_kg 分别扣金额和净重。gift_kg 是免费实物，必须与商品净重一起解释成交单位。请求 POST /index {case, release_week, base_week}，返回 {snapshot, series, decomposition, diagnostics}。series 的每点含 week、group、index（可为 null）及 support。指数基期为 100；无法构成可比链接时须区分不可比与价格未变。decomposition 的单位和加法/乘法约定需记录。版本读取使用库中的 cut，不覆写原始交易。

当前 GET /cases、POST /preview 和 GET /health 已可用；client.analysis 是待接入的业务消费调用，新分析路由尚不存在。原始数据通过 app.store 读取，snapshot 为输入字节的 SHA-256。新增接口须保留现有预览能力和 JSON 有限数值约定。

series 是记录列表，group 标识总体或商户规模切片；货币全局重定标不改变指数。有支撑的基期值为 100。scaled 是全币值同比例重定标的资料，不是新的交易群。release_week 既限定可见交易，也限定退款版本。

# AI 校准系统 — 完整设计

## 系统定位

不替代任何现有模块（选词V3、选品V3、货源查找）。在现有流水线的**上游**和**下游**各加一层 AI：

```
═══════════════════════════════════════════════════════════════
                        AI 校准系统
═══════════════════════════════════════════════════════════════

  上游：搜索词飞轮（角色③）              下游：评分进化（角色②）
  ┌──────────────────────┐          ┌──────────────────────┐
  │ 种子词 → 标题收集     │          │ 预测 → 采集 → 货源   │
  │   ↓                  │          │   ↓                  │
  │ AI提取候选词          │          │ 实际利润 vs 预测评分  │
  │   ↓                  │          │   ↓                  │
  │ 阶段B：分析已有数据    │          │ AI 分析偏差模式       │
  │ 阶段C：轻量搜索验证    │          │   ↓                  │
  │   ↓                  │          │ 按品类调整评分权重     │
  │ pass入队 / 素材库     │          │   ↓                  │
  └──────────────────────┘          │ 回测验证 → 更新配置    │
           │                        └──────────────────────┘
           │ 扩充的词库                       │
           ▼                                 ▼
  ╔══════════════════════════════════════════════════╗
  ║              现有采集流水线（不修改）               ║
  ║                                                  ║
  ║  词库 → 行情采集 → 选词评分 → 选品采集 → 货源查找  ║
  ╚══════════════════════════════════════════════════╝
                    │
                    ▼ 每次流水线产出的数据
            ┌──────────────┐
            │  预测日志     │ ← 角色②的燃料
            │  实际产出     │
            └──────────────┘
```

---

# 第一部分：角色③ — 搜索词飞轮（上游）

## 阶段B：AI分析已有数据（0次额外搜索）

### 输入

```
一轮飞轮的输入：
  1. 父级搜索词列表（上一轮 pass 的活跃词）
  2. 每个父级词的搜索结果（20条/词，已采集）
  3. 已有词库（用于判断边际贡献）
```

### Prompt 模板

```
你是闲鱼选品关键词分析师。以下是飞轮本轮的数据，请完成候选词的分流和评分。

══════════════════════════════════════
第一部分：上下文
══════════════════════════════════════

本轮父级搜索词及结果摘要：
{parent_search_summary}
格式：每个父级词，列出其搜索结果中出现的候选品类词

已有词库（用于判断边际贡献）：
{existing_word_library}

══════════════════════════════════════
第二部分：候选词列表（从标题中提取）
══════════════════════════════════════

{candidates}

══════════════════════════════════════
第三部分：任务 — 对每个候选词，按以下流程处理
══════════════════════════════════════

步骤1：类型分流

判断候选词属于哪种类型：

"search_word"  → 能定位到一种商品 → 进入步骤2评分
"title_boost"  → 不能搜索但能提升标题点击率 → 进入步骤3归类
"noise"        → 无价值 → 淘汰

判断标准（重要）：
- search_word = 包含品牌/型号/品类+参数/独立品类名
  例："捷安特ATX830""26寸山地车""麻将凉席""电饭煲"
- title_boost = 描述卖家状态/成色/交易方式/信任信号
  例："闲置自用""99新""包邮""年会奖品""正品保证"
- noise = 不包含任何商品信息或完全无意义
  例："好物推荐""哈哈哈哈""DDDD"

步骤2：对 search_word 评分（5维度，基于已有数据）

不要凭常识猜。评分依据来自父级搜索结果中该候选词出现的数据：

【商品指向性】(0-5)
  基于词的结构判断：
  品牌+型号+参数 → 5分
  品牌+品类或品类+参数 → 4分  
  品牌+品类 → 3分
  品类+属性 → 2分
  纯品类词 → 1分
  无法定位商品 → 0分

【需求信号】(0-5) — 基于父级搜索结果中的数据
  在父级搜索结果的20条商品中：
  - 有多少条标题包含这个候选词？
  - 这些商品中有soldCount的比例是多少？
  - wantNum的平均值是多少？
  
  评分依据（重要：数据说话，不凭常识）：
  - 如果该候选词在父级结果中根本没出现 → 标记为"signal_insufficient"
    不强行打分，进入步骤4
  - 出现≥3条且有soldCount → 从数据中提取需求强度
  - 出现但无soldCount → 3分（有曝光无成交，中性）
  
  参考锚定：
  - 出现且soldCount>0的比例>50% → 5分，需求信号强
  - 出现但soldCount=0 → 3分，有曝光无验证成交
  - 未出现 → 不评分，标记signal_insufficient

【竞争预估】(0-5) — 基于父级搜索结果中的数据
  从父级搜索结果中分析：
  - 这个候选词在20条父级结果中出现了几次？
  - 出现的商品中，有soldCount的比例？
  
  逻辑：出现少但soldCount高 = 卖家少需求强 = 蓝海
  
  参考锚定：
  - 出现≤2条，且有soldCount → 5分，蓝海信号
  - 出现3-5条，部分有soldCount → 4分，低竞争  
  - 出现6-10条 → 3分，中等竞争
  - 出现>10条 → 2分，竞争偏高
  - 出现但全部无soldCount → 3分，有卖家无成交，中性

【货源可得性】(0-5) — 基于词的结构 + 品类常识
  品牌+型号 → 5分（标品，拼多多一定有货）
  品牌+品类 → 4分（有品牌锚定）
  品类+参数 → 3分（品类成熟但无品牌，能找到同品类）
  纯品类 → 2分（太宽泛，货源匹配难）
  孤品/手工/DIY信号 → 1分（"自组""手作""定制"等）
  
  这个维度部分依赖常识，但范围限定在"词结构→供应端可得性"的判断上，
  不是凭空猜品类利润。

【利润空间】(0-5) — 基于父级搜索结果中的价格数据 + 品类常识
  从父级搜索结果中：
  - 包含这个候选词的商品，平均标价是多少？
  - 如果该候选词在父级结果中未出现，则不评分，标记signal_insufficient
  
  高价格基准（>2000元）= 潜在高利润 → 4-5分
  中等价格（500-2000元）= 中等利润空间 → 3-4分
  低价格（100-500元）= 需要走量 → 2-3分
  极低价格（<100元）= 利润薄 → 1-2分
  
  结合品类常识修正：数码产品利润薄（减1分），日用百货利润薄（减1分），
  品牌耐用品利润厚（加1分），小众品类利润厚（加1分）

【词库边际贡献】(0-5) — 与已有词库比对
  与已有词库中每个词的相似度比对：
  - 全新品类/细分，词库无任何近似 → 5分
  - 已有品类的新细分 → 4分
  - 已有词的近义词，搜索结果重叠度预估<50% → 3分
  - 已有词的近义词，搜索结果重叠度预估>50% → 2分
  - 几乎完全相同 → 1分

步骤3：对 title_boost 归类

按使用场景归入：
- 真实感：闲置、自用、搬家出、年会奖品、前男友送的、退坑
- 成色：99新、几乎全新、仅拆封、未使用、九成新
- 信任：正品保证、支持验货、假一赔十、实体店同款
- 交易：包邮、可小刀、同城面交、信用极好
- 紧迫：急出、最后一天、马上搬家、清仓价

步骤4：标记 signal_insufficient

对于 signal_insufficient 的词：
- 如果词的结构有"品牌+型号"或"品类+精确参数"特征 → 标记为 "pending_verify"
  （进入阶段C轻量搜索验证）
- 如果只是宽泛品类词 → 标记为 watch
  （不进入阶段C，保留观察）

══════════════════════════════════════
第四部分：输出格式
══════════════════════════════════════

{
  "search_words": [
    {
      "word": "瓜车",
      "merged_with": ["gravel", "砾石公路车"],
      "scores": {
        "specificity": 4,
        "demand_signal": 4,
        "demand_evidence": "自行车结果中出现3次，2条有soldCount(23,65)，售出率67%",
        "competition": 5,
        "competition_evidence": "仅出现3次，供给端稀缺",
        "supply_access": 3,
        "supply_reason": "自组装为主，非品牌标品，需阶段C验证",
        "profit_potential": 4,
        "profit_evidence": "3条均价3850元，品牌件利润空间大",
        "marginal_value": 4,
        "marginal_reason": "词库无此细分品类"
      },
      "composite": 8.4,
      "status": "pass",
      "needs_phase_c": false
    },
    {
      "word": "死飞倒刹",
      "merged_with": [],
      "scores": {
        "specificity": 4,
        "demand_signal": "signal_insufficient",
        "demand_evidence": "在父级搜索结果中未出现",
        "competition": "signal_insufficient",
        "supply_access": 2,
        "supply_reason": "小众玩法，配件非常规",
        "profit_potential": "signal_insufficient",
        "marginal_value": 5,
        "marginal_reason": "词库完全无此品类"
      },
      "status": "pending_verify",
      "needs_phase_c": true,
      "phase_c_reason": "品类型号精确，信号不足但值得验证"
    }
  ],
  "title_materials": {
    "真实感": [
      {"text": "闲置自用", "source_count": 15, "use_case": "配合品牌型号增加真实感"}
    ],
    "成色": [...],
    "信任": [...],
    "交易": [...],
    "紧迫": [...]
  },
  "noise": [...],
  "summary": {
    "total_candidates": 150,
    "search_words": 40,
    "pass": 8,
    "watch": 12,
    "pending_verify": 20,
    "discard": 0,
    "title_materials_new": 15
  }
}
```

---

## 阶段C：轻量搜索验证

### 什么时候触发

阶段B标记为 `pending_verify` 的词，且满足以下条件之一：
- 词的结构有"品牌+型号"或"品类+精确参数"（值得验证）
- 阶段B认为品类多样且边际贡献高，但信号不足

### 执行

对每个 pending_verify 词，搜索1页（20条），取搜索结果摘要数据：

```python
# 轻量搜索（不是完整行情采集）
result = bridge.search(keyword, page=1, page_size=20)
# 只取：
# - numFound
# - 每条商品的 soldCount, wantNum, price, title, serviceTags

# 加随机间隔，降低风控
time.sleep(random.uniform(12, 18))
```

### 阶段C评分 Prompt

```
以下是轻量搜索验证的结果，请更新候选词的评分。

══════════════════════════════════════
搜索数据
══════════════════════════════════════

关键词: "瓜车"
numFound: 47

搜索结果摘要（20条商品的统计数据，非完整列表）：
- 总商品数: 20
- 有soldCount的商品: 12条 (60%)
- soldCount分布: 1-10(7条), 10-50(4条), 100+(1条)
- 平均wantNum: 6.3
- 平均价格: ¥3850
- 有serviceTag"包邮": 8条
- 有serviceTag"百分百好评": 5条
- 有serviceTag"已售": 12条

卖家结构：
- 有userFishShopLabel（鱼铺）: 12/20 (60%)
- 个人卖家: 8/20 (40%)

标题中高频词：
铝合金(15), 碳纤维(6), 油碟(8), 砾石(10), 组装(5)

══════════════════════════════════════
已有父级搜索中的信号（阶段B已有）：
在自行车结果中出现3次，2条有soldCount(23,65)
══════════════════════════════════════

请更新以下维度的评分（只更新需要改的维度）：

1. 需求信号：基于搜索验证数据重新评分
   numFound=47，60%售出率，有"已售100+" → 需求真实且强

2. 竞争预估：numFound=47 → 在售仅47件 → 竞争极低

3. 货源可得性：60%是专业卖家（鱼铺）、标题高频词含"组装" 
   → 部分DIY、部分成品，需要进一步确认，暂维持3分

4. 利润空间：均价3850，已有soldCount的商品均价3850 vs 
   无soldCount的商品均价4200 → 定价合理才能卖出去，维持4分

请输出更新后的完整评分 JSON（格式同阶段B）。
同时指出这个搜索验证数据中"你觉得最重要的一个发现"。
```

### 阶段C后处理

```python
def phase_c_postprocess(verified_words: list) -> dict:
    passed = []
    watch = []
    
    for word in verified_words:
        composite = calculate_composite(word)
        word["composite_final"] = composite
        
        if composite >= 7.0:  # 阶段C阈值比纯阶段B低0.5（因为已验证）
            word["status"] = "pass"
            passed.append(word)
        elif composite >= 5.5:
            word["status"] = "watch"
            watch.append(word)
        else:
            word["status"] = "discard"
    
    return {"passed": passed, "watch": watch}
```

---

## 飞轮调度

### 串行模式（起步推荐）

```
    采集流水线跑完
         │
         ▼
    飞轮阶段B（AI处理，1-2秒）
         │
         ▼
    飞轮阶段C（轻量搜索，~5分钟）
         │
         ▼
    词库更新 + 素材库更新
         │
         ▼
    下一轮采集（使用扩充后的词库）
```

### 飞轮触发条件

```python
FLYWHEEL_TRIGGERS = {
    "manual":        "用户手动触发",
    "after_pipeline": "每次采集流水线完成后自动触发",
    "queue_low":      "待采集词 < 15 时自动触发（保持词库不枯竭）",
    "scheduled":      "每3天自动触发一次（保持词库新鲜度）",
}
```

---

# 第二部分：角色② — 评分模型自进化（下游）

## 数据来源

每次采集流水线跑完，自然产生预测 vs 实际数据：

```
选词V3预测: "自行车" S级85分 → 预计能找到好品
选品V3预测: "捷安特ATX830" A级78分 → 预计利润30%
货源查找实际: 拼多多找到同款 ¥650 → 闲鱼行情均价 ¥850 → 利润23%

这是一条完整的"预测→实际"对照记录。
```

## 预测日志结构

```json
{
  "pipeline_run_id": "2026-05-19_1430",
  "timestamp": "2026-05-19T14:30:00",
  "records": [
    {
      "keyword": "自行车",
      "keyword_scores": {
        "total_100": 85, "grade": "S",
        "scores": {"demand_scale": 18, "deal_efficiency": 25, "deal_quality": 16,
                   "profit_certainty": 12, "competition": 8, "trend_signal": 6},
        "avg_price": 850, "no_bargain_rate": 0.62, "num_found": 2300
      },
      "products": [
        {
          "item_id": "123456",
          "title": "捷安特ATX830 27速铝合金山地车",
          "product_scores": {
            "total_100": 78, "grade": "A",
            "scores": {"demand_signal": 16, "price_advantage": 18,
                       "seller_verification": 18, "timeliness": 12,
                       "supply_attribute": 10, "item_quality": 4},
            "sold_count_from_search": 65
          },
          "supply_result": {
            "found": true,
            "platform": "拼多多",
            "supply_price": 650,
            "market_avg_price": 850,
            "profit_margin_pct": 23.5,
            "supply_title": "捷安特ATX830同款 铝合金山地车 27速"
          }
        }
      ]
    }
  ]
}
```

## AI 分析周期

不是每条记录都分析。积累到一定量再触发：

```python
ANALYSIS_TRIGGERS = {
    "per_category_min_samples": 5,  # 单个品类积累5条记录 → 触发分析
    "global_min_samples": 15,       # 全局积累15条 → 触发全局分析
    "max_age_days": 7,              # 超过7天的记录自动触发分析（不等待）
}
```

## 分析 Prompt

```
你是闲鱼选品模型的校准分析师。以下是一个品类的预测 vs 实际对照数据。

══════════════════════════════════════
品类：自行车
样本数：7条
══════════════════════════════════════

每个样本包含：
- 选品模型各维度得分（demand_signal, price_advantage, seller_verification, 
  timeliness, supply_attribute, item_quality）
- 实际货源利润（profit_margin_pct）
- 是否找到货源（supply_found）

数据：
{sample_data}

══════════════════════════════════════
分析任务
══════════════════════════════════════

1. 【维度相关性分析】

计算每个评分维度与实际利润的相关性。不是统计回归，是定性判断：

  看评分vs实际的对照：
  - 价格优势维度得分高的商品，实际利润是否也高？
  - 需求信号维度得分高的商品，是否真的更容易找到货源？
  - 有没有某个维度得分和实际结果完全不对齐？

输出：
  - 最相关的维度（评分高 → 实际利润高）: _____
  - 最不相关的维度（评分和实际利润无关或负相关）: _____
  - 最被高估的维度（评分偏高但实际差）: _____
  - 最被低估的维度（评分中等但实际好）: _____

2. 【品类特异性发现】

这个品类的数据有什么和全局模式不同的地方？

  例（基于7条自行车数据可能发现）：
  - 自行车的supply_attribute得分普遍偏低（非标品多），
    但实际找到货源的成功率不低 → 这个维度对自行车品类可能不重要
  - 价格在1500-3000元的商品利润最稳定，
    低于1500元利润薄，高于3000元卖得慢

3. 【预测偏差模式】

选品模型的评分和实际利润之间，有没有系统性的偏差方向？

  例：
  - 7条中5条的预测利润 > 实际利润 → 模型整体偏高
  - 偏差集中在"高价商品"（>3000元）→ 模型对高价商品的利润预估不准
  - 偏差没有明显模式 → 模型在当前样本上表现OK

4. 【权重调整建议】

基于以上分析，建议以下调整（每个调整都要给出数据支撑）：

  - 权重调整：demand_signal从20调到___，原因：___
  - 维度调整：是否有维度应该新增/删除/合并？
  - 阈值调整：品类特有参数是否需要覆盖全局默认值？

  约束：
  - 调整幅度不超过当前值的±30%（保守调整，避免过拟合）
  - 调整必须有至少3条样本支持
  - 权重总和不变（仍为100分制）

══════════════════════════════════════
输出 JSON
══════════════════════════════════════

{
  "category": "自行车",
  "sample_count": 7,
  "analysis": {
    "most_correlated_dim": "price_advantage",
    "most_correlated_reason": "得分高的商品实际利润确实高(5/7一致)",
    "least_correlated_dim": "supply_attribute",
    "least_correlated_reason": "标品/非标品判断与实际利润无关联(2/7一致)",
    "overestimated_dim": "seller_verification",
    "overestimated_reason": "卖家销量高不等于单品利润高(3/7预测偏高)",
    "underestimated_dim": "timeliness", 
    "underestimated_reason": "新上架商品实际利润比模型预估的好(4/7偏低)",
    "systematic_bias": "高价商品(>3000元)利润被高估15-20%"
  },
  "proposed_changes": [
    {
      "type": "weight_adjust",
      "dimension": "demand_signal",
      "current_weight": 20,
      "proposed_weight": 25,
      "reason": "需求信号强实际利润也高，当前权重偏低",
      "supporting_samples": 5,
      "risk": "低，5条样本一致"
    },
    {
      "type": "category_override",
      "dimension": "supply_attribute",
      "global_weight": 15,
      "category_weight": 8,
      "reason": "自行车品类非标/标品判断对利润无影响",
      "supporting_samples": 7,
      "risk": "中，7条样本中部分可能是巧合"
    }
  ],
  "do_not_change": [
    {
      "dimension": "price_advantage",
      "reason": "当前权重合理，与实际利润高度一致"
    }
  ]
}
```

## 回测验证

AI 提出调整建议后，用历史样本回测验证：

```python
def backtest(proposed_changes: list, historical_samples: list) -> dict:
    """
    用新权重重新计算历史样本的评分，对比旧排序和新排序。
    """
    old_scores = [score_with_old_weights(s) for s in historical_samples]
    new_scores = [score_with_new_weights(s, proposed_changes) for s in historical_samples]
    
    # 按实际利润排序（ground truth）
    actual_ranking = sorted(historical_samples, key=lambda x: x["actual_profit"], reverse=True)
    
    # 按旧评分排序
    old_ranking = sorted(zip(historical_samples, old_scores), key=lambda x: x[1], reverse=True)
    
    # 按新评分排序
    new_ranking = sorted(zip(historical_samples, new_scores), key=lambda x: x[1], reverse=True)
    
    # 比较：新排序的前N个是否更接近实际排序的前N个
    top_n = min(5, len(historical_samples))
    old_match = len(set(r[0]["item_id"] for r in old_ranking[:top_n]) & 
                     set(r["item_id"] for r in actual_ranking[:top_n]))
    new_match = len(set(r[0]["item_id"] for r in new_ranking[:top_n]) & 
                     set(r["item_id"] for r in actual_ranking[:top_n]))
    
    return {
        "old_top5_match_actual": old_match,
        "new_top5_match_actual": new_match,
        "improvement": new_match - old_match,
        "pass": new_match >= old_match,  # 至少不能比旧的差
        "recommendation": "approve" if new_match > old_match else "reject"
    }
```

回测通过（新排序比旧排序更接近实际结果）→ 自动更新配置。不通过 → 保持原配置，记录原因。

## 配置更新

```python
def apply_changes(changes: list, config_path: str):
    """回测通过后，更新 models_config.json"""
    config = json.load(open(config_path))
    
    for change in changes:
        if change["type"] == "weight_adjust":
            dim = change["dimension"]
            config["scoring"]["product_dimensions"][dim]["weight"] = change["proposed_weight"]
        
        elif change["type"] == "category_override":
            dim = change["dimension"]
            cat = change["category"]
            if "category_overrides" not in config["scoring"]:
                config["scoring"]["category_overrides"] = {}
            if cat not in config["scoring"]["category_overrides"]:
                config["scoring"]["category_overrides"][cat] = {}
            config["scoring"]["category_overrides"][cat][dim] = {
                "weight": change["category_weight"],
                "applied_at": datetime.now().isoformat(),
                "based_on_samples": change["supporting_samples"]
            }
    
    # 备份旧配置
    backup_path = config_path + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy(config_path, backup_path)
    
    # 写新配置
    json.dump(config, open(config_path, 'w'), indent=2, ensure_ascii=False)
    
    # 记录变更日志
    log_change(changes, backup_path)
```

---

## 三、完整系统总览

```
┌─────────────────────────────────────────────────────────────────┐
│                      AI 校准系统                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────┐          ┌─────────────────────┐       │
│  │ 角色③ 搜索词飞轮     │          │ 角色② 评分进化       │       │
│  │                     │          │                     │       │
│  │ 种子词               │          │ 每次流水线完成后      │       │
│  │   ↓                 │          │   ↓                 │       │
│  │ 父级搜索(已有数据)    │          │ 预测 vs 实际对照     │       │
│  │   ↓                 │          │   ↓                 │       │
│  │ 阶段B(AI分析) 0搜索  │          │ 积累5+条样本/品类    │       │
│  │   ├─ 数据够 → 评分   │          │   ↓                 │       │
│  │   └─ 不够 → pending │          │ AI分析维度相关性     │       │
│  │   ↓                 │          │   ↓                 │       │
│  │ 阶段C(轻量搜索验证)   │          │ AI提出权重调整       │       │
│  │   对pending词搜1页   │          │   ↓                 │       │
│  │   ↓                 │          │ 历史样本回测验证     │       │
│  │ pass → 入队          │          │   ↓                 │       │
│  │ title_boost → 素材库 │          │ 通过 → 更新配置      │       │
│  └─────────┬───────────┘          └─────────┬───────────┘       │
│            │                                │                   │
│            ▼                                ▼                   │
│  ┌─────────────────────────────────────────────────────┐       │
│  │ 输出：                                               │       │
│  │  · 扩充后的词库（飞轮产出）                            │       │
│  │  · 标题素材库（飞轮产出）                              │       │
│  │  · 按品类校准的评分配置（评分进化产出）                   │       │
│  │  · 每轮飞轮日志 + 每次权重变更日志（可追溯）             │       │
│  └─────────────────────────────────────────────────────┘       │
│                                                                 │
│  角色①（失败归因/异常预警）不独立实现，合并到                     │
│  角色②的分析中（维度相关性分析自然会暴露异常）                    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 落地顺序建议

```
第1步：飞轮阶段B（改动最小）
  - 新增1个文件：engines/flywheel_engine.py
  - 1个AI Prompt模板
  - 不改现有流水线

第2步：飞轮阶段C
  - 复用现有搜索RPC，加间隔控制
  - 飞轮阶段C的搜索独立于采集流水线

第3步：预测日志
  - 在 collection_engine 完成回调里加写入预测日志
  - 收集5-10条样本后，跑第一次AI分析

第4步：评分进化
  - 新增 calibration_engine.py
  - 回测 + 自动更新 models_config.json
```

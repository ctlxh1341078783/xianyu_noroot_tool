// batch_collect.js - 闲鱼批量采集 v19
// 采集：搜索 + 行情 + 详情 + 评论
// v13: detail/comment 批量并行（multi-latch）
// v14: 自适应限速（EMA响应时间跟踪，动态延迟替代固定sleep）
// v15: 请求指纹动态化（pageSize随机化、参数抖动）
// v17: 行情成交记录翻页并行（multi-latch）
// v17: HS分组并行（每批3页+1.5s间隔，平衡速度与风控）
var gData = {};
var gLatch = null;
var gLatches = {};         // {cacheKey: latch} 并行请求
var gExpectedApis = {};    // {cacheKey: expectedApi} 并行请求
var gPendingApi = "";
var gLoginState = { loggedIn: null, retCode: "", reason: "" };
var gReady = false;

// ─── 指纹随机化 ───
var gFpSeed = Math.floor(Math.random() * 100000);
function _fpRand(lo, hi) {
    // 基于线性同余的确定性伪随机，seed 随调用推进
    gFpSeed = (gFpSeed * 1664525 + 1013904223) & 0x7fffffff;
    var r = gFpSeed / 0x7fffffff;
    return Math.floor(lo + r * (hi - lo + 1));
}
function _randSearchPageSize() { return _fpRand(18, 22); }
function _randMarketPageSize()  { return _fpRand(5, 7); }
function _randVary(base, delta)  { return base + _fpRand(-delta, delta); }

function tryInit() {
    if (typeof Java === "undefined") {
        console.log("[BC] Java bridge not ready, retrying in 2s...");
        setTimeout(tryInit, 2000);
        return;
    }
    Java.perform(function() {
    console.log("[BC] Init v17...");

    // === Login bypass: 解决手机黑屏问题 ===
    try {
        var LoginActivity = Java.use("com.taobao.idlefish.login.HalfTransparentUserLoginActivity");
        LoginActivity.onCreate.implementation = function(bundle) {
            console.log("[BC] Bypassing login activity, calling finish()");
            this.finish();
        };
    } catch(e) {
        console.log("[BC] LoginActivity bypass not needed or class not found: " + e);
    }

    // Classes
    var XMC = Java.use("com.taobao.idlefish.xmc.XModuleCenter");
    var PApiContext = Java.use("com.taobao.idlefish.protocol.net.PApiContext");
    var MtopLauncher = Java.use("com.taobao.android.remoteobject.easy.MtopLauncher");
    var ApiCallBack = Java.use("com.taobao.idlefish.protocol.net.ApiCallBack");
    var String = Java.use("java.lang.String");
    var Cdl = Java.use("java.util.concurrent.CountDownLatch");
    var TimeUnit = Java.use("java.util.concurrent.TimeUnit");
    var FastJSON = Java.use("com.alibaba.fastjson.JSON");
    var JSONObject = Java.use("com.alibaba.fastjson.JSONObject");

    // Request classes
    var SRReq = Java.use("com.taobao.idlefish.search_implement.protocol.SearchResultReq");
    var CommentReq = Java.use("com.taobao.idlefish.protocol.api.ApiCommentListRequest");
    var ApiProtocol = Java.use("com.taobao.idlefish.protocol.net.api.ApiProtocol");
    var HashMap = Java.use("java.util.HashMap");
    var BaseApiProtocol = Java.use("com.taobao.idlefish.protocol.net.api.BaseApiProtocol");

    var raw = XMC.moduleForProtocol(PApiContext.class);
    var apiCtx = Java.cast(raw, MtopLauncher);

    // === onMtopReturn: 统一捕获 ===
    var RemoteMtopCallback = Java.use("com.taobao.android.remoteobject.easy.RemoteMtopCallback");
    var orig_onMtopReturn = RemoteMtopCallback.onMtopReturn;

    // API name -> cache prefix mapping, plus expected API for latch matching
    var gExpectedApi = "";

    RemoteMtopCallback.onMtopReturn.implementation = function(ctx, map, ret) {
        var api = "";
        try { api = ret.getApi() || ""; } catch(e) {}

        // === 登录态检测：每次API响应都检查retCode ===
        var retCode = "";
        try { retCode = ret.getRetCode() || ""; } catch(e2) {}
        if (retCode && retCode !== "SUCCESS") {
            var authCodes = ["FAIL_SYS_USER_LOGIN", "FAIL_SYS_SESSION_EXPIRED",
                "ANDROID_SYS_LOGIN_FAIL", "ANDROID_SYS_NO_LOGIN",
                "FAIL_SYS_401", "FAIL_SYS_TOKEN_EXPIRED", "FAIL_SYS_ILLEGAL_ACCESS"];
            for (var ai = 0; ai < authCodes.length; ai++) {
                if (retCode.indexOf(authCodes[ai]) >= 0) {
                    gLoginState.loggedIn = false;
                    gLoginState.retCode = retCode;
                    gLoginState.reason = "需要登录";
                    console.log("[BC] AUTH ERROR: " + retCode + " on " + api);
                    break;
                }
            }
            // If a non-auth error but still not SUCCESS, mark unknown
            if (gLoginState.loggedIn !== false && gLoginState.loggedIn !== true) {
                gLoginState.loggedIn = null;
                gLoginState.retCode = retCode;
                gLoginState.reason = "未知状态: " + retCode;
            }
        } else if (retCode === "SUCCESS" && gLoginState.loggedIn !== true) {
            // First successful response = logged in
            gLoginState.loggedIn = true;
            gLoginState.retCode = "SUCCESS";
            gLoginState.reason = "已登录";
        }

        var r = orig_onMtopReturn.call(this, ctx, map, ret);

        try {
            var data = ret.getData();
            if (!data) { return r; }
            var jsonStr = FastJSON.toJSONString(data);

            // Helper: signal matching latch (parallel or single)
            function signalLatch(apiType, cacheKey) {
                // Try parallel latches first
                if (gLatches[cacheKey] && gExpectedApis[cacheKey] === apiType) {
                    gLatches[cacheKey].countDown();
                    delete gLatches[cacheKey];
                    delete gExpectedApis[cacheKey];
                    return;
                }
                // Fallback to single latch
                if (gLatch && gExpectedApi === apiType) { gLatch.countDown(); gLatch = null; }
            }

            // Search
            if (api === "mtop.taobao.idlemtopsearch.search") {
                gData["search_raw_" + gPendingApi] = jsonStr;
                var items = extractItems(JSON.parse(jsonStr));
                gData["search_" + gPendingApi] = items;
                console.log("[BC] search len=" + jsonStr.length);
                signalLatch("search", gPendingApi);
            }
            // Market tab list
            else if (api === "mtop.taobao.idlemtopsearch.market.tab.list") {
                gData["market_" + gPendingApi] = jsonStr;
                console.log("[BC] market.tabs len=" + jsonStr.length);
                signalLatch("market_tabs", gPendingApi);
            }
            // Market topbar
            else if (api === "mtop.taobao.idlemtopsearch.market.topbar") {
                gData["market_topbar_" + gPendingApi] = jsonStr;
                console.log("[BC] market.topbar len=" + jsonStr.length);
                signalLatch("market_topbar", gPendingApi);
            }
            // Market history sales (supports batch parallel with lost gPendingApi)
            else if (api === "mtop.taobao.idlemtopsearch.market.historysale") {
                var hsKey = gPendingApi;
                if (!gLatches[hsKey] || gExpectedApis[hsKey] !== "market_hs") {
                    for (var k in gLatches) {
                        if (gExpectedApis[k] === "market_hs") { hsKey = k; break; }
                    }
                }
                gData["market_hs_" + hsKey] = jsonStr;
                console.log("[BC] market.hs len=" + jsonStr.length);
                signalLatch("market_hs", hsKey);
            }
            // Market price trend
            else if (api === "mtop.taobao.idlemtopsearch.market.price.trend") {
                gData["market_pt_" + gPendingApi] = jsonStr;
                console.log("[BC] market.pt len=" + jsonStr.length);
                signalLatch("market_pt", gPendingApi);
            }
            // Detail — batch mode: derive cacheKey from response itemId
            else if (api === "mtop.taobao.idle.awesome.detail.unit") {
                var storeKey = gPendingApi;
                var matched = false;
                try {
                    var respObj = JSON.parse(jsonStr);
                    var extractedId = null;
                    // Path 1: itemDO at top level (standard response)
                    if (respObj && respObj.itemDO && respObj.itemDO.itemId) {
                        extractedId = respObj.itemDO.itemId;
                    }
                    // Path 2: data.itemDO (MTOP wrapper)
                    else if (respObj && respObj.data && respObj.data.itemDO && respObj.data.itemDO.itemId) {
                        extractedId = respObj.data.itemDO.itemId;
                    }
                    if (extractedId) {
                        var derivedKey = "cls_db_" + extractedId;
                        if (gLatches[derivedKey] || gExpectedApis[derivedKey]) {
                            storeKey = derivedKey;
                            matched = true;
                        }
                    }
                } catch(e) {}
                gData["detail_" + storeKey] = jsonStr;
                console.log("[BC] detail len=" + jsonStr.length + (matched ? " [batch]" : ""));
                // DEBUG: dump ALL fields from detail response (itemDO + sellerDO + top-level)
                try {
                    var _dObj = JSON.parse(jsonStr);
                    var _ido = _dObj.itemDO || (_dObj.data && _dObj.data.itemDO) || {};
                    var _sdo = _dObj.sellerDO || (_dObj.data && _dObj.data.sellerDO) || {};
                    // top-level keys
                    var _tlk = [];
                    for (var _tk in _dObj) { _tlk.push(_tk); }
                    console.log("[BC-DEBUG] top-level keys: " + _tlk.join(", "));
                    if (_dObj.data) {
                        var _dk = [];
                        for (var _dk2 in _dObj.data) { _dk.push(_dk2); }
                        console.log("[BC-DEBUG] data keys: " + _dk.join(", "));
                    }
                    // ALL itemDO fields
                    console.log("[BC-DEBUG] === itemDO ALL fields (" + Object.keys(_ido).length + " total) ===");
                    for (var _ik in _ido) {
                        var _iv = _ido[_ik];
                        var _ivs = "" + _iv;
                        if (_ivs.length > 120) _ivs = _ivs.substring(0, 120) + "...[" + _ivs.length + "]";
                        console.log("[BC-DEBUG]   itemDO." + _ik + " = " + _ivs);
                    }
                    // ALL sellerDO fields
                    console.log("[BC-DEBUG] === sellerDO ALL fields (" + Object.keys(_sdo).length + " total) ===");
                    for (var _sk in _sdo) {
                        var _sv = _sdo[_sk];
                        var _svs = "" + _sv;
                        if (_svs.length > 120) _svs = _svs.substring(0, 120) + "...[" + _svs.length + "]";
                        console.log("[BC-DEBUG]   sellerDO." + _sk + " = " + _svs);
                    }
                } catch(_e) {}
                signalLatch("detail", storeKey);
            }
            // Comment — batch mode: derive cacheKey from response data
            else if (api === "mtop.taobao.idle.comment.list") {
                var storeKey = gPendingApi;
                var matched = false;
                try {
                    var respObj = JSON.parse(jsonStr);
                    var extractedId = null;
                    if (respObj) {
                        // Path 1: data.commentDO.itemId (MTOP wrapper)
                        if (respObj.data && respObj.data.commentDO && respObj.data.commentDO.itemId) {
                            extractedId = respObj.data.commentDO.itemId;
                        }
                        // Path 2: commentDO.itemId (top level)
                        else if (respObj.commentDO && respObj.commentDO.itemId) {
                            extractedId = respObj.commentDO.itemId;
                        }
                        // Path 3: data.modelList[0].itemId
                        else if (respObj.data && respObj.data.modelList && respObj.data.modelList.length > 0 && respObj.data.modelList[0].itemId) {
                            extractedId = respObj.data.modelList[0].itemId;
                        }
                        // Path 4: scan first-level keys for itemId
                        if (!extractedId) {
                            for (var k in respObj) {
                                if (respObj[k] && typeof respObj[k] === 'object' && respObj[k].itemId) {
                                    extractedId = respObj[k].itemId;
                                    break;
                                }
                            }
                        }
                    }
                    if (extractedId) {
                        var derivedKey = "cls_cb_" + extractedId;
                        if (gLatches[derivedKey] || gExpectedApis[derivedKey]) {
                            storeKey = derivedKey;
                            matched = true;
                        }
                    }
                } catch(e) {}
                gData["comment_" + storeKey] = jsonStr;
                console.log("[BC] comment len=" + jsonStr.length);
                signalLatch("comment", storeKey);
            }
            // Log non-target APIs for debugging
            else {
                console.log("[BC] other: " + api + " len=" + jsonStr.length);
            }
        } catch(e) {
            console.log("[BC] capture err: " + e);
        }

        return r;
    };

    // === 数据提取 ===
    function extractItems(obj) {
        var items = [];
        try {
            var list = obj.resultList;
            if (!list) return items;
            for (var i = 0; i < list.length; i++) {
                try {
                    var it = list[i].data.item;
                    if (!it) continue;
                    var main = it.main;
                    if (!main) continue;
                    var ex = main.exContent || {};
                    var p = ex.price;
                    var priceStr = "";
                    if (Array.isArray(p)) {
                        for (var j = 0; j < p.length; j++) priceStr += (p[j].text || "");
                    } else {
                        priceStr = p || "";
                    }
                    var fishTagsStr = "";
                    try {
                        if (Array.isArray(ex.fishTags)) {
                            var tags = [];
                            for (var j = 0; j < ex.fishTags.length; j++) {
                                tags.push(ex.fishTags[j].text || "");
                            }
                            fishTagsStr = tags.join("; ");
                        }
                    } catch(e3) {}
                    var userIdentity = "";
                    try {
                        if (ex.userIdentityShow) {
                            userIdentity = ex.userIdentityShow.text || "";
                        }
                    } catch(e3) {}
                    // serviceUtParams — 服务标签 + 已售标签 + 想要数 + 优先级分
                    var serviceTags = [];
                    var soldCountLabel = "";
                    var soldCount = 0;
                    var wantNum = 0;
                    try {
                        var clickArgs = (main.clickParam && main.clickParam.args) || {};
                        var spStr = clickArgs.serviceUtParams || "";
                        if (spStr) {
                            var parsed = JSON.parse(spStr);
                            if (Array.isArray(parsed)) {
                                for (var ti = 0; ti < parsed.length; ti++) {
                                    var t = parsed[ti];
                                    var tc = (t.args && t.args.content) || "";
                                    serviceTags.push({tagId: t.arg1 || "", content: tc});
                                    if (t.arg1 === "4_tag_r3_1028") {
                                        soldCountLabel = tc;
                                        soldCount = _parseSoldLabel(tc);
                                    }
                                }
                            }
                        }
                        var rawWn = clickArgs.wantNum || "0";
                        wantNum = parseInt(rawWn) || 0;
                    } catch(e3) {}
                    items.push({
                        itemId: ex.itemId || "",
                        title: ex.title || "",
                        price: priceStr,
                        userNick: ex.userNickName || "",
                        userAvatarUrl: ex.userAvatarUrl || "",
                        userIdentity: userIdentity,
                        userFishShopLabel: ex.userFishShopLabel || "",
                        area: ex.area || "",
                        picUrl: ex.picUrl || "",
                        picWidth: ex.picWidth || "",
                        picHeight: ex.picHeight || "",
                        fishTags: fishTagsStr,
                        priceTag: ex.priceTag || "",
                        richTitle: ex.richTitle || "",
                        isAuction: ex.isAuction || false,
                        isAliMaMaAD: ex.isAliMaMaAD || false,
                        detailPageType: ex.detailPageType || "",
                        targetUrl: main.targetUrl || "",
                        want: ex.want || "0",
                        wantNum: wantNum,
                        soldCount: soldCount,
                        soldCountLabel: soldCountLabel,
                        serviceTags: serviceTags,
                        showVideoIcon: ex.showVideoIcon || false
                    });
                } catch(e2) {}
            }
        } catch(e) {}
        return items;
    }

    // === 已售标签解析 ===
    function _parseSoldLabel(label) {
        if (!label) return 0;
        // 用 + 拼接确保转为 JS 字符串（Frida 可能返回 Java String 包装）
        var s = "" + label;
        // 手动提取数字串
        var numStr = "";
        var hasWan = false;
        for (var i = 0; i < s.length; i++) {
            var code = s.charCodeAt(i);
            if ((code >= 48 && code <= 57) || code === 46) {
                numStr += s.charAt(i);
            }
            if (code === 0x4E07 || code === 87 || code === 119) {
                hasWan = true;
            }
        }
        if (numStr.length === 0) return 0;
        // 逐位计算整数（完全避免 parseFloat/parseInt）
        var result = 0;
        for (var j = 0; j < numStr.length; j++) {
            var d = numStr.charCodeAt(j) - 48;
            if (d >= 0 && d <= 9) {
                result = result * 10 + d;
            }
        }
        if (result <= 0) return 0;
        if (hasWan) result = result * 10000;
        return result;
    }

    // === Dummy callback ===
    var DummyCb = Java.registerClass({
        name: "com.frida.DummyCb",
        superClass: ApiCallBack,
        methods: {
            onSuccess: function(r) {},
            onFailed: function(c, m) {},
            setContext: function(ctx) {},
            getContext: function() { return null; },
            process: function(r) {},
            getResponseClass: function() { return null; },
            onProcess: function(p) {}
        }
    });

    // === 自适应限速：EMA 响应时间跟踪 ===
    var gAdaptiveState = {
        search:  { ema: 1500, alpha: 0.3, minDelay: 800,  maxDelay: 5000 },
        detail:  { ema: 2000, alpha: 0.3, minDelay: 0,    maxDelay: 3000 },
        comment: { ema: 1500, alpha: 0.3, minDelay: 0,    maxDelay: 3000 },
        market_tabs:   { ema: 1200, alpha: 0.3, minDelay: 500, maxDelay: 4000 },
        market_topbar: { ema: 1000, alpha: 0.3, minDelay: 500, maxDelay: 4000 },
        market_hs:     { ema: 1500, alpha: 0.3, minDelay: 500, maxDelay: 4000 },
        market_pt:     { ema: 2000, alpha: 0.3, minDelay: 500, maxDelay: 4000 },
    };

    function _updateEMA(apiType, elapsedMs) {
        var s = gAdaptiveState[apiType];
        if (!s) return;
        s.ema = s.ema * (1 - s.alpha) + elapsedMs * s.alpha;
    }

    function _onErrorSlowdown(apiType) {
        var s = gAdaptiveState[apiType];
        if (!s) return;
        s.ema = Math.min(s.maxDelay, s.ema * 1.5);  // 错误时 EMA 放大 1.5 倍
    }

    function _adaptiveDelay(apiType) {
        var s = gAdaptiveState[apiType];
        if (!s) return 1000;
        var d = Math.round(Math.max(s.minDelay, Math.min(s.maxDelay, s.ema * 1.2)));
        return d;
    }

    function awaitCall(expectedApi) {
        var latch = Cdl.$new(1);
        gLatch = latch;
        gExpectedApi = expectedApi;
        var startTime = Date.now();
        latch.await(35, TimeUnit.SECONDS.value);
        var elapsed = Date.now() - startTime;
        gLatch = null;
        gExpectedApi = "";
        // Track response time
        if (elapsed > 50 && elapsed < 34000) {
            _updateEMA(expectedApi, elapsed);
        } else if (elapsed >= 34000) {
            _onErrorSlowdown(expectedApi);  // timeout → slow down
        }
    }

    // ========== RPC ==========

    rpc.exports.search = function(kw, page, pageSize) {
        page = page || 1;
        pageSize = pageSize || 20;
        var req = SRReq.$new();
        req.keyword.value = String.$new(kw);
        req.pageNumber.value = parseInt(page) || 1;
        req.rowsPerPage.value = parseInt(pageSize) || 20;
        req.apiNameAndVersion(String.$new("mtop.taobao.idlemtopsearch.search"), String.$new("1.0"));
        req.searchTabType.value = String.$new("SEARCH_TAB_MAIN");

        var cacheKey = kw + "_p" + page;
        gPendingApi = cacheKey;
        apiCtx.send(req, DummyCb.$new());
        awaitCall("search");

        var items = gData["search_" + cacheKey] || [];
        var hasMore = false;
        var numFound = 0;
        var searchMaxPrice = "";
        var searchMinPrice = "";
        try {
            var lastJson = JSON.parse(gData["search_raw_" + cacheKey] || "{}");
            var ri = lastJson.resultInfo;
            if (ri) {
                hasMore = ri.hasNextPage || false;
                var scf = ri.searchResControlFields;
                if (scf) {
                    numFound = parseInt(scf.numFound) || 0;
                    searchMaxPrice = scf.maxPrice || "";
                    searchMinPrice = scf.minPrice || "";
                }
            }
        } catch(e) {}
        gPendingApi = "";
        return JSON.stringify({ keyword: kw, page: page, count: items.length, items: items, hasMore: hasMore, numFound: numFound, maxPrice: searchMaxPrice, minPrice: searchMinPrice });
    };

    rpc.exports.getMarketTabs = function(kw) {
        var req = SRReq.$new();
        req.keyword.value = String.$new(kw);
        req.apiNameAndVersion(String.$new("mtop.taobao.idlemtopsearch.market.tab.list"), String.$new("1.0"));
        req.searchTabType.value = String.$new("SEARCH_TAB_MARKET");

        gPendingApi = kw;
        apiCtx.send(req, DummyCb.$new());
        awaitCall("market_tabs");
        gPendingApi = "";
        return gData["market_" + kw] || "{}";
    };

    // Helper: build ApiProtocol request with JSONObject param
    function makeMarketReq(apiName, apiVersion, paramObj) {
        var paramJson = JSON.stringify(paramObj);
        var param = JSONObject.parseObject(String.$new(paramJson));
        var req = ApiProtocol.$new();
        req.apiNameAndVersion(String.$new(apiName), String.$new(apiVersion));
        var f = BaseApiProtocol.class.getDeclaredField("param");
        f.setAccessible(true);
        f.set(req, param);
        return req;
    }

    rpc.exports.getMarketTopbar = function(kw, spuId, categoryId, spuName, categoryName) {
        var req = makeMarketReq("mtop.taobao.idlemtopsearch.market.topbar", "1.0", {
            categoryId: categoryId,
            categoryName: categoryName || "",
            keyword: kw,
            searchTabType: "SEARCH_TAB_MARKET",
            showType: "SEARCH_TAB_MARKET_QUERY",
            spuId: spuId,
            spuName: spuName || ""
        });
        var cacheKey = kw + "_topbar";
        gPendingApi = cacheKey;
        apiCtx.send(req, DummyCb.$new());
        awaitCall("market_topbar");
        gPendingApi = "";
        return gData["market_topbar_" + cacheKey] || "{}";
    };

    rpc.exports.getMarketHistorySale = function(kw, spuId, categoryId, spuName, categoryName, page) {
        var req = makeMarketReq("mtop.taobao.idlemtopsearch.market.historysale", "1.0", {
            categoryId: categoryId,
            categoryName: categoryName || "",
            keyword: kw,
            pageNumber: page || 1,
            pageSize: _randMarketPageSize(),
            searchTabType: "SEARCH_TAB_MARKET",
            showType: "SEARCH_TAB_MARKET_QUERY",
            spuId: spuId,
            spuName: spuName || "",
            type: 1
        });
        var cacheKey = kw + "_hs_p" + (page || 1);
        gPendingApi = cacheKey;
        apiCtx.send(req, DummyCb.$new());
        awaitCall("market_hs");
        gPendingApi = "";
        return gData["market_hs_" + cacheKey] || "{}";
    };

    rpc.exports.getMarketPriceTrend = function(kw, spuId, categoryId, spuName, categoryName) {
        var req = makeMarketReq("mtop.taobao.idlemtopsearch.market.price.trend", "1.0", {
            categoryId: categoryId,
            categoryName: categoryName || "",
            keyword: kw,
            newCpv: true,
            searchTabType: "SEARCH_TAB_MARKET",
            showType: "SEARCH_TAB_MARKET_QUERY",
            spuId: spuId,
            spuName: spuName || ""
        });
        var cacheKey = kw + "_pt";
        gPendingApi = cacheKey;
        apiCtx.send(req, DummyCb.$new());
        awaitCall("market_pt");
        gPendingApi = "";
        return gData["market_pt_" + cacheKey] || "{}";
    };

    rpc.exports.getDetail = function(itemId) {
        var map = HashMap.$new();
        map.put("itemId", String.$new(itemId));
        map.put("commerceAdPlanId", String.$new(""));
        map.put("extra", String.$new('{"source":"7"}'));
        map.put("flowVersion", String.$new("6.0"));
        var Bool = Java.use("java.lang.Boolean");
        map.put("isOld", Bool.$new(false));
        map.put("needSimpleDetail", Bool.$new(false));

        var req = ApiProtocol.$new();
        req.apiNameAndVersion(String.$new("mtop.taobao.idle.awesome.detail.unit"), String.$new("1.0"));
        req.paramMap(map);

        gPendingApi = itemId;
        apiCtx.send(req, DummyCb.$new());
        awaitCall("detail");
        gPendingApi = "";

        return gData["detail_" + itemId] || '{"error":"timeout"}';
    };

    rpc.exports.getComments = function(itemId) {
        var req = CommentReq.$new();
        req.itemId.value = String.$new(itemId);
        req.pageNumber.value = String.$new("1");
        req.bizType.value = String.$new("");
        req.apiNameAndVersion(String.$new("mtop.taobao.idle.comment.list"), String.$new("3.0"));

        gPendingApi = itemId;
        apiCtx.send(req, DummyCb.$new());
        awaitCall("comment");
        gPendingApi = "";

        return gData["comment_" + itemId] || '{"error":"timeout"}';
    };

    // ===== 并行批量搜索 =====
    rpc.exports.searchBatch = function(keywordsJson) {
        var keywords = JSON.parse(keywordsJson);
        var results = [];
        var latch = Cdl.$new(keywords.length);

        for (var i = 0; i < keywords.length; i++) {
            var kw = keywords[i];
            var cacheKey = "batch_" + kw + "_" + Date.now();
            (function(ck, kw, idx) {
                var req = SRReq.$new();
                req.keyword.value = String.$new(kw);
                req.pageNum.value = parseInt("1");
                req.rowsPerPage.value = _randSearchPageSize();
                req.apiNameAndVersion(String.$new("mtop.taobao.idlemtopsearch.search"), String.$new("1.0"));
                req.searchTabType.value = String.$new("SEARCH_TAB_MAIN");

                // Store latch for this request
                gLatches[ck] = latch;
                gExpectedApis[ck] = "search";

                gPendingApi = ck;
                apiCtx.send(req, DummyCb.$new());
            })(cacheKey, kw, i);
        }

        // Wait for all responses (max 40s timeout)
        latch.await(40, TimeUnit.SECONDS.value);

        // Collect results
        for (var i = 0; i < keywords.length; i++) {
            var kw = keywords[i];
            // Find matching cache key (starts with "batch_" + kw)
            var foundKey = null;
            for (var k in gData) {
                if (k.indexOf("search_" + "batch_" + kw) === 0) {
                    foundKey = k;
                    break;
                }
            }
            if (foundKey) {
                var rawKey = foundKey.replace("search_", "search_raw_");
                var items = gData[foundKey] || [];
                var raw = gData[rawKey] || "{}";
                var lastJson = JSON.parse(raw);
                var ri = (lastJson.resultInfo || {});
                var hasMore = ri.hasNextPage || false;
                results.push({
                    keyword: kw,
                    items: items,
                    hasMore: hasMore,
                    numFound: ri.totalCount || 0
                });
            } else {
                results.push({ keyword: kw, items: [], hasMore: false, numFound: 0, error: "timeout" });
            }
        }

        gPendingApi = "";
        return JSON.stringify(results);
    };

    // ===== 闭环采集：搜索+详情+评论 一次RPC完成 =====
    // 内部辅助：单次搜索（独立cacheKey，不干扰外部RPC）
    function _searchOne(kw, page, pageSize) {
        var cacheKey = "cls_s_" + kw + "_p" + page;
        gPendingApi = cacheKey;
        var req = SRReq.$new();
        req.keyword.value = String.$new(kw);
        req.pageNumber.value = parseInt(page) || 1;
        req.rowsPerPage.value = parseInt(pageSize) || 20;
        req.apiNameAndVersion(String.$new("mtop.taobao.idlemtopsearch.search"), String.$new("1.0"));
        req.searchTabType.value = String.$new("SEARCH_TAB_MAIN");
        apiCtx.send(req, DummyCb.$new());
        awaitCall("search");
        var items = gData["search_" + cacheKey] || [];
        var raw = gData["search_raw_" + cacheKey] || "{}";
        var meta = {hasMore: false, numFound: 0, maxPrice: "", minPrice: ""};
        try {
            var j = JSON.parse(raw);
            var ri = j.resultInfo;
            if (ri) {
                meta.hasMore = ri.hasNextPage || false;
                var scf = ri.searchResControlFields;
                if (scf) {
                    meta.numFound = parseInt(scf.numFound) || 0;
                    meta.maxPrice = scf.maxPrice || "";
                    meta.minPrice = scf.minPrice || "";
                }
            }
        } catch(e) {}
        gPendingApi = "";
        return {items: items, meta: meta};
    }

    // 内部辅助：单条详情
    function _detailOne(itemId) {
        var cacheKey = "cls_d_" + itemId;
        gPendingApi = cacheKey;
        var map = HashMap.$new();
        map.put("itemId", String.$new(itemId));
        map.put("commerceAdPlanId", String.$new(""));
        map.put("extra", String.$new('{"source":"7"}'));
        map.put("flowVersion", String.$new("6.0"));
        var Bool = Java.use("java.lang.Boolean");
        map.put("isOld", Bool.$new(false));
        map.put("needSimpleDetail", Bool.$new(false));
        var req = ApiProtocol.$new();
        req.apiNameAndVersion(String.$new("mtop.taobao.idle.awesome.detail.unit"), String.$new("1.0"));
        req.paramMap(map);
        apiCtx.send(req, DummyCb.$new());
        awaitCall("detail");
        gPendingApi = "";
        var raw = gData["detail_" + cacheKey] || '{"error":"timeout"}';
        try { return JSON.parse(raw); } catch(e) { return {error: "parse_error"}; }
    }

    // 内部辅助：单条评论
    function _commentOne(itemId) {
        var cacheKey = "cls_c_" + itemId;
        gPendingApi = cacheKey;
        var req = CommentReq.$new();
        req.itemId.value = String.$new(itemId);
        req.pageNumber.value = String.$new("1");
        req.bizType.value = String.$new("");
        req.apiNameAndVersion(String.$new("mtop.taobao.idle.comment.list"), String.$new("3.0"));
        apiCtx.send(req, DummyCb.$new());
        awaitCall("comment");
        gPendingApi = "";
        var raw = gData["comment_" + cacheKey] || '{"error":"timeout"}';
        try { return JSON.parse(raw); } catch(e) { return {error: "parse_error"}; }
    }

    // 内部辅助：批量详情（multi-latch 并行）
    function _detailBatch(itemIds) {
        var Cdl = Java.use("java.util.concurrent.CountDownLatch");
        var TimeUnit = Java.use("java.util.concurrent.TimeUnit");
        var count = itemIds.length;
        if (count === 0) return {};
        if (count === 1) {
            var r = {}; r[itemIds[0]] = _detailOne(itemIds[0]); return r;
        }

        var latch = Cdl.$new(count);
        var cacheKeys = [];

        for (var i = 0; i < count; i++) {
            var itemId = itemIds[i];
            var cacheKey = "cls_db_" + itemId;
            cacheKeys.push(cacheKey);

            gLatches[cacheKey] = latch;
            gExpectedApis[cacheKey] = "detail";
            gPendingApi = cacheKey;

            var map = HashMap.$new();
            map.put("itemId", String.$new(itemId));
            map.put("commerceAdPlanId", String.$new(""));
            map.put("extra", String.$new('{"source":"7"}'));
            map.put("flowVersion", String.$new("6.0"));
            var Bool = Java.use("java.lang.Boolean");
            map.put("isOld", Bool.$new(false));
            map.put("needSimpleDetail", Bool.$new(false));
            var req = ApiProtocol.$new();
            req.apiNameAndVersion(String.$new("mtop.taobao.idle.awesome.detail.unit"), String.$new("1.0"));
            req.paramMap(map);
            apiCtx.send(req, DummyCb.$new());
        }

        latch.await(40, TimeUnit.SECONDS.value);

        var results = {};
        for (var i = 0; i < count; i++) {
            var itemId = itemIds[i];
            var ck = cacheKeys[i];
            var raw = gData["detail_" + ck] || '{"error":"timeout"}';
            try { results[itemId] = JSON.parse(raw); } catch(e) { results[itemId] = {error: "parse_error"}; }
        }
        return results;
    }

    // 内部辅助：批量评论（multi-latch 并行）
    function _commentBatch(itemIds) {
        var Cdl = Java.use("java.util.concurrent.CountDownLatch");
        var TimeUnit = Java.use("java.util.concurrent.TimeUnit");
        var count = itemIds.length;
        if (count === 0) return {};
        if (count === 1) {
            var r = {}; r[itemIds[0]] = _commentOne(itemIds[0]); return r;
        }

        var latch = Cdl.$new(count);
        var cacheKeys = [];

        for (var i = 0; i < count; i++) {
            var itemId = itemIds[i];
            var cacheKey = "cls_cb_" + itemId;
            cacheKeys.push(cacheKey);

            gLatches[cacheKey] = latch;
            gExpectedApis[cacheKey] = "comment";
            gPendingApi = cacheKey;

            var req = CommentReq.$new();
            req.itemId.value = String.$new(itemId);
            req.pageNumber.value = String.$new("1");
            req.bizType.value = String.$new("");
            req.apiNameAndVersion(String.$new("mtop.taobao.idle.comment.list"), String.$new("3.0"));
            apiCtx.send(req, DummyCb.$new());
        }

        latch.await(40, TimeUnit.SECONDS.value);

        var results = {};
        for (var i = 0; i < count; i++) {
            var itemId = itemIds[i];
            var ck = cacheKeys[i];
            var raw = gData["comment_" + ck] || '{"error":"timeout"}';
            try { results[itemId] = JSON.parse(raw); } catch(e) { results[itemId] = {error: "parse_error"}; }
        }
        return results;
    }

    rpc.exports.collectKeyword = function(kw, maxPages, detailMax, commentMax) {
        maxPages = maxPages || 5;
        detailMax = (detailMax != null) ? _randVary(detailMax, 2) : _randVary(5, 2);
        commentMax = (commentMax != null) ? _randVary(commentMax, 1) : _randVary(3, 1);
        var pageSize = _randSearchPageSize();
        var Thread = Java.use("java.lang.Thread");

        // Step 1: 翻页搜索
        var allItems = [];
        var searchMeta = {numFound: 0, maxPrice: "", minPrice: ""};
        for (var page = 1; page <= maxPages; page++) {
            send({type: "progress", stage: "search", done: page, total: maxPages, kw: kw});
            var result = _searchOne(kw, page, pageSize);
            var items = result.items;
            if (!items || items.length === 0) break;
            allItems = allItems.concat(items);
            if (page === 1) searchMeta = result.meta;
            if (!result.meta.hasMore) break;
            if (detailMax > 0 && allItems.length >= detailMax * 3) break;
            if (page < maxPages) Thread.sleep(_adaptiveDelay("search"));
        }
        var sEma = Math.round(gAdaptiveState.search.ema);
        var sDelay = _adaptiveDelay("search");
        console.log("[BC] search EMA=" + sEma + "ms delay=" + sDelay + "ms items=" + allItems.length);
        send({type: "progress", stage: "search_done", done: allItems.length, total: allItems.length, kw: kw});

        // Step 2: 详情批量采集（仅当 detailMax>0 时执行；=0 则由 Python 端决定后调用 collectDetails）
        var details = {};
        if (detailMax > 0) {
            var detailIds = [];
            var detailCount = Math.min(allItems.length, detailMax);
            for (var i = 0; i < detailCount; i++) {
                if (allItems[i].itemId) detailIds.push(allItems[i].itemId);
            }
            send({type: "progress", stage: "detail_batch", done: 0, total: detailIds.length, kw: kw});
            details = _detailBatch(detailIds);
            send({type: "progress", stage: "detail_done", done: detailIds.length, total: detailIds.length, kw: kw});
        }

        // Step 3: 评论采集（仅当 commentMax>0 时执行）
        var comments = {};
        if (commentMax > 0) {
            var commentCount = Math.min(allItems.length, commentMax);
            for (var i = 0; i < commentCount; i++) {
                var itemId = allItems[i].itemId;
                if (!itemId) continue;
                send({type: "progress", stage: "comment", done: i + 1, total: commentCount, kw: kw});
                comments[itemId] = _commentOne(itemId);
            }
        }

        send({type: "progress", stage: "done", kw: kw});

        return JSON.stringify({
            keyword: kw,
            searchItems: allItems,
            details: details,
            comments: comments,
            searchMeta: searchMeta
        });
    };

    // === 精准详情采集（Python预筛选后传入指定itemId列表）===
    rpc.exports.collectDetails = function(itemIdsJson) {
        var itemIds = JSON.parse(itemIdsJson);
        if (!Array.isArray(itemIds) || itemIds.length === 0) return "{}";
        send({type: "progress", stage: "detail_batch", done: 0, total: itemIds.length, kw: ""});
        var details = _detailBatch(itemIds);
        send({type: "progress", stage: "detail_done", done: itemIds.length, total: itemIds.length, kw: ""});
        return JSON.stringify(details);
    };

    // === 精准评论采集 ===
    rpc.exports.collectComments = function(itemIdsJson) {
        var itemIds = JSON.parse(itemIdsJson);
        if (!Array.isArray(itemIds) || itemIds.length === 0) return "{}";
        send({type: "progress", stage: "comment_batch", done: 0, total: itemIds.length, kw: ""});
        var comments = _commentBatch(itemIds);
        send({type: "progress", stage: "comment_done", done: itemIds.length, total: itemIds.length, kw: ""});
        return JSON.stringify(comments);
    };

    // ===== 闭环行情采集：tabs + topbar + historySale + pricetrend 一次RPC完成 =====
    function _marketTabs(kw) {
        var cacheKey = "cm_tabs_" + kw;
        gPendingApi = cacheKey;
        var req = SRReq.$new();
        req.keyword.value = String.$new(kw);
        req.apiNameAndVersion(String.$new("mtop.taobao.idlemtopsearch.market.tab.list"), String.$new("1.0"));
        req.searchTabType.value = String.$new("SEARCH_TAB_MARKET");
        apiCtx.send(req, DummyCb.$new());
        awaitCall("market_tabs");
        gPendingApi = "";
        try { return JSON.parse(gData["market_" + cacheKey] || "{}"); } catch(e) { return {}; }
    }

    function _marketTopbar(kw, spuId, categoryId, spuName, categoryName) {
        var cacheKey = "cm_topbar_" + kw;
        gPendingApi = cacheKey;
        var paramJson = JSON.stringify({
            categoryId: categoryId, categoryName: categoryName || "",
            keyword: kw, searchTabType: "SEARCH_TAB_MARKET",
            showType: "SEARCH_TAB_MARKET_QUERY",
            spuId: spuId, spuName: spuName || ""
        });
        var param = JSONObject.parseObject(String.$new(paramJson));
        var req = ApiProtocol.$new();
        req.apiNameAndVersion(String.$new("mtop.taobao.idlemtopsearch.market.topbar"), String.$new("1.0"));
        var f = BaseApiProtocol.class.getDeclaredField("param");
        f.setAccessible(true);
        f.set(req, param);
        apiCtx.send(req, DummyCb.$new());
        awaitCall("market_topbar");
        gPendingApi = "";
        try { return JSON.parse(gData["market_topbar_" + cacheKey] || "{}"); } catch(e) { return {}; }
    }

    function _marketHistorySale(kw, spuId, categoryId, spuName, categoryName, page) {
        var cacheKey = "cm_hs_" + kw + "_p" + (page || 1);
        gPendingApi = cacheKey;
        var paramJson = JSON.stringify({
            categoryId: categoryId, categoryName: categoryName || "",
            keyword: kw, pageNumber: page || 1, pageSize: _randMarketPageSize(),
            searchTabType: "SEARCH_TAB_MARKET",
            showType: "SEARCH_TAB_MARKET_QUERY",
            spuId: spuId, spuName: spuName || "", type: 1
        });
        var param = JSONObject.parseObject(String.$new(paramJson));
        var req = ApiProtocol.$new();
        req.apiNameAndVersion(String.$new("mtop.taobao.idlemtopsearch.market.historysale"), String.$new("1.0"));
        var f = BaseApiProtocol.class.getDeclaredField("param");
        f.setAccessible(true);
        f.set(req, param);
        apiCtx.send(req, DummyCb.$new());
        awaitCall("market_hs");
        gPendingApi = "";
        try { return JSON.parse(gData["market_hs_" + cacheKey] || "{}"); } catch(e) { return {}; }
    }

    // ═══ 成交记录翻页分组并行版：每批3页并行，批次间间隔1.5s ═══
    function _marketHistorySaleBatch(kw, spuId, categoryId, spuName, categoryName, totalPages) {
        if (totalPages <= 1) {
            var single = _marketHistorySale(kw, spuId, categoryId, spuName, categoryName, 1);
            var items = (single.itemSaleList || []).slice();
            return { items: items, last: single, pages: 1 };
        }
        var Cdl = Java.use("java.util.concurrent.CountDownLatch");
        var TimeUnit = Java.use("java.util.concurrent.TimeUnit");
        var Thread = Java.use("java.lang.Thread");
        var CHUNK = 3;

        var allItems = [];
        var last = {};
        var fetchedPages = 0;
        var stopped = false;

        for (var start = 1; start <= totalPages && !stopped; start += CHUNK) {
            var end = Math.min(start + CHUNK - 1, totalPages);
            var chunkSize = end - start + 1;
            var latch = Cdl.$new(chunkSize);
            var chunkKeys = [];

            for (var p = start; p <= end; p++) {
                var cacheKey = "hsbatch_" + kw + "_p" + p;
                chunkKeys.push(cacheKey);
                gLatches[cacheKey] = latch;
                gExpectedApis[cacheKey] = "market_hs";
                gPendingApi = cacheKey;

                var paramJson = JSON.stringify({
                    categoryId: categoryId, categoryName: categoryName || "",
                    keyword: kw, pageNumber: p, pageSize: _randMarketPageSize(),
                    searchTabType: "SEARCH_TAB_MARKET",
                    showType: "SEARCH_TAB_MARKET_QUERY",
                    spuId: spuId, spuName: spuName || "", type: 1
                });
                var param = JSONObject.parseObject(String.$new(paramJson));
                var req = ApiProtocol.$new();
                req.apiNameAndVersion(String.$new("mtop.taobao.idlemtopsearch.market.historysale"), String.$new("1.0"));
                var f = BaseApiProtocol.class.getDeclaredField("param");
                f.setAccessible(true);
                f.set(req, param);
                apiCtx.send(req, DummyCb.$new());
            }

            gPendingApi = "";
            latch.await(40, TimeUnit.SECONDS.value);

            for (var i = 0; i < chunkKeys.length; i++) {
                var raw = gData["market_hs_" + chunkKeys[i]] || "{}";
                var data = {};
                try { data = JSON.parse(raw); } catch(e) {}
                var saleItems = data.itemSaleList || [];
                if (saleItems.length > 0) {
                    allItems = allItems.concat(saleItems);
                    last = data;
                    fetchedPages++;
                } else {
                    stopped = true;
                    break;
                }
                delete gData["market_hs_" + chunkKeys[i]];
                delete gLatches[chunkKeys[i]];
                delete gExpectedApis[chunkKeys[i]];
            }

            // 批次间间隔（最后一组不等待）
            if (!stopped && end < totalPages) {
                Thread.sleep(1500);
            }
        }
        return { items: allItems, last: last, pages: fetchedPages };
    }

    function _marketPriceTrend(kw, spuId, categoryId, spuName, categoryName) {
        var cacheKey = "cm_pt_" + kw;
        gPendingApi = cacheKey;
        var paramJson = JSON.stringify({
            categoryId: categoryId, categoryName: categoryName || "",
            keyword: kw, newCpv: true,
            searchTabType: "SEARCH_TAB_MARKET",
            showType: "SEARCH_TAB_MARKET_QUERY",
            spuId: spuId, spuName: spuName || ""
        });
        var param = JSONObject.parseObject(String.$new(paramJson));
        var req = ApiProtocol.$new();
        req.apiNameAndVersion(String.$new("mtop.taobao.idlemtopsearch.market.price.trend"), String.$new("1.0"));
        var f = BaseApiProtocol.class.getDeclaredField("param");
        f.setAccessible(true);
        f.set(req, param);
        apiCtx.send(req, DummyCb.$new());
        awaitCall("market_pt");
        gPendingApi = "";
        try { return JSON.parse(gData["market_pt_" + cacheKey] || "{}"); } catch(e) { return {}; }
    }

    rpc.exports.collectMarket = function(kw, hsPages) {
        hsPages = hsPages || 3;
        var Thread = Java.use("java.lang.Thread");

        // Step 0: 搜索取 numFound
        send({type: "progress", stage: "market_search", kw: kw});
        var sResult = _searchOne(kw, 1, 20);
        var numFound = sResult.meta.numFound;
        var searchMaxPrice = sResult.meta.maxPrice || "";
        var searchMinPrice = sResult.meta.minPrice || "";

        // Step 1: 行情 tabs → 解析 spuId
        send({type: "progress", stage: "market_tabs", kw: kw});
        var tabs = _marketTabs(kw);
        var spuId = "", categoryId = "", spuName = "", categoryName = "";
        var tabList = tabs.result || [];
        for (var i = 0; i < tabList.length; i++) {
            var t = tabList[i];
            if (t.searchTabType === "SEARCH_TAB_MARKET" && t.extra) {
                spuId = t.extra.spuId || "";
                categoryId = t.extra.categoryId || "";
                spuName = t.extra.spuName || "";
                categoryName = t.extra.categoryName || "";
                break;
            }
        }

        if (!spuId || !categoryId) {
            send({type: "progress", stage: "market_done", kw: kw});
            return JSON.stringify({
                keyword: kw, numFound: numFound,
                maxPrice: searchMaxPrice, minPrice: searchMinPrice,
                tabs: tabs, hasMarket: false
            });
        }

        // Step 2: topbar
        send({type: "progress", stage: "market_topbar", kw: kw});
        var topbar = _marketTopbar(kw, spuId, categoryId, spuName, categoryName);

        // Step 3: historySale 翻页（并行版）
        send({type: "progress", stage: "market_hs_batch", done: 0, total: hsPages, kw: kw});
        var hsBatch = _marketHistorySaleBatch(kw, spuId, categoryId, spuName, categoryName, hsPages);
        var allSaleItems = hsBatch.items;
        var hsLast = hsBatch.last;
        send({type: "progress", stage: "market_hs_done", done: allSaleItems.length, total: allSaleItems.length, kw: kw});

        // Step 4: 价格趋势
        send({type: "progress", stage: "market_trend", kw: kw});
        var pricetrend = _marketPriceTrend(kw, spuId, categoryId, spuName, categoryName);

        send({type: "progress", stage: "market_done", kw: kw});

        return JSON.stringify({
            keyword: kw,
            numFound: numFound,
            maxPrice: searchMaxPrice,
            minPrice: searchMinPrice,
            tabs: tabs,
            topbar: topbar,
            historysale: {
                historyMaxPrice: hsLast.historyMaxPrice,
                historyMinPrice: hsLast.historyMinPrice,
                historyOrder: hsLast.historyOrder,
                itemSaleList: allSaleItems
            },
            pricetrend: pricetrend,
            hasMarket: true
        });
    };

    rpc.exports.status = function() {
        return JSON.stringify({ ok: true, ready: gReady, apiCtx: apiCtx != null,
            login: gLoginState });
    };

    rpc.exports.checkLogin = function() {
        // Force a fresh check: if we haven't seen any response yet, status is unknown
        return JSON.stringify(gLoginState);
    };

    rpc.exports.clear = function() {
        gData = {};
        gPendingApi = "";
        return '{"ok":true}';
    };

    gReady = true;
    console.log("[BC] Ready v17. RPC: search,getMarketTabs,getMarketTopbar,getMarketHistorySale,getMarketPriceTrend,getDetail,getComments,searchBatch,collectKeyword,collectMarket,checkLogin,status,clear");
    });
}

// 延迟启动，给 ART 时间加载
setTimeout(tryInit, 1000);

// collector.js - 闲鱼真机采集 v2
// 全量字段提取 + RPC + 翻页支持
var gData = {};
var gLatch = null;
var gPendingApi = "";
var gExpectedApi = "";
var gReady = false;

function log(msg) {
    console.log("[COLLECTOR] " + msg);
}

function tryInit() {
    if (typeof Java === "undefined") {
        log("Java bridge not ready, retry in 2s...");
        setTimeout(tryInit, 2000);
        return;
    }
    Java.perform(function() {
    log("Init v2...");

    // === Classes ===
    var XMC = Java.use("com.taobao.idlefish.xmc.XModuleCenter");
    var PApiContext = Java.use("com.taobao.idlefish.protocol.net.PApiContext");
    var MtopLauncher = Java.use("com.taobao.android.remoteobject.easy.MtopLauncher");
    var ApiCallBack = Java.use("com.taobao.idlefish.protocol.net.ApiCallBack");
    var String = Java.use("java.lang.String");
    var Cdl = Java.use("java.util.concurrent.CountDownLatch");
    var TimeUnit = Java.use("java.util.concurrent.TimeUnit");
    var FastJSON = Java.use("com.alibaba.fastjson.JSON");
    var JSONObject = Java.use("com.alibaba.fastjson.JSONObject");
    var HashMap = Java.use("java.util.HashMap");
    var Bool = Java.use("java.lang.Boolean");

    var SRReq = Java.use("com.taobao.idlefish.search_implement.protocol.SearchResultReq");
    var ApiProtocol = Java.use("com.taobao.idlefish.protocol.net.api.ApiProtocol");
    var BaseApiProtocol = Java.use("com.taobao.idlefish.protocol.net.api.BaseApiProtocol");

    var raw = XMC.moduleForProtocol(PApiContext.class);
    var apiCtx = Java.cast(raw, MtopLauncher);
    log("MtopLauncher obtained: " + (apiCtx != null));

    // === onMtopReturn hook ===
    var RemoteMtopCallback = Java.use("com.taobao.android.remoteobject.easy.RemoteMtopCallback");
    var orig_onMtopReturn = RemoteMtopCallback.onMtopReturn;

    RemoteMtopCallback.onMtopReturn.implementation = function(ctx, map, ret) {
        var api = "";
        try { api = ret.getApi() || ""; } catch(e) {}
        var r = orig_onMtopReturn.call(this, ctx, map, ret);

        try {
            var data = ret.getData();
            if (!data) return r;
            var jsonStr = FastJSON.toJSONString(data);

            if (api === "mtop.taobao.idlemtopsearch.search") {
                gData["search_raw_" + gPendingApi] = jsonStr;
                gData["search_" + gPendingApi] = extractSearchItems(JSON.parse(jsonStr));
                log("search: " + jsonStr.length + " bytes, " + gData["search_" + gPendingApi].length + " items");
                if (gLatch && gExpectedApi === "search") { gLatch.countDown(); gLatch = null; }
            }
            else if (api === "mtop.taobao.idlemtopsearch.market.tab.list") {
                gData["market_tabs_" + gPendingApi] = jsonStr;
                log("market.tabs: " + jsonStr.length + " bytes");
                if (gLatch && gExpectedApi === "market_tabs") { gLatch.countDown(); gLatch = null; }
            }
            else if (api === "mtop.taobao.idlemtopsearch.market.topbar") {
                gData["market_topbar_" + gPendingApi] = jsonStr;
                log("market.topbar: " + jsonStr.length + " bytes");
                if (gLatch && gExpectedApi === "market_topbar") { gLatch.countDown(); gLatch = null; }
            }
            else if (api === "mtop.taobao.idlemtopsearch.market.historysale") {
                gData["market_hs_" + gPendingApi] = jsonStr;
                log("market.hs: " + jsonStr.length + " bytes");
                if (gLatch && gExpectedApi === "market_hs") { gLatch.countDown(); gLatch = null; }
            }
            else if (api === "mtop.taobao.idlemtopsearch.market.price.trend") {
                gData["market_pt_" + gPendingApi] = jsonStr;
                log("market.pt: " + jsonStr.length + " bytes");
                if (gLatch && gExpectedApi === "market_pt") { gLatch.countDown(); gLatch = null; }
            }
            else if (api === "mtop.taobao.idle.awesome.detail.unit") {
                gData["detail_raw_" + gPendingApi] = jsonStr;
                gData["detail_" + gPendingApi] = extractDetail(jsonStr);
                log("detail: " + jsonStr.length + " bytes");
                if (gLatch && gExpectedApi === "detail") { gLatch.countDown(); gLatch = null; }
            }
            else if (api === "mtop.taobao.idle.comment.list") {
                gData["comment_raw_" + gPendingApi] = jsonStr;
                gData["comment_" + gPendingApi] = extractComments(jsonStr);
                log("comment: " + jsonStr.length + " bytes");
                if (gLatch && gExpectedApi === "comment") { gLatch.countDown(); gLatch = null; }
            }
            else {
                log("other api: " + api + " len=" + jsonStr.length);
            }
        } catch(e) {
            log("capture err: " + e);
        }
        return r;
    };

    // ==================== 提取器 ====================

    // 安全取值
    function sv(obj, key, def) { try { var v = obj[key]; return v !== undefined && v !== null ? v : (def !== undefined ? def : ""); } catch(e) { return def !== undefined ? def : ""; } }
    function sa(obj) { return Array.isArray(obj) ? obj : []; }
    function so(obj) { return (obj && typeof obj === "object" && !Array.isArray(obj)) ? obj : {}; }
    function isStr(v) { return typeof v === "string"; }

    // 价格数组转字符串
    function priceArrToStr(p) {
        if (!p) return "";
        if (isStr(p)) return p;
        if (Array.isArray(p)) { var s = ""; for (var j = 0; j < p.length; j++) s += (sv(p[j], "text", "")); return s; }
        return String(p);
    }

    // fishTags 结构化提取
    function extractFishTags(ft) {
        if (!ft || typeof ft !== "object") return {};
        var result = {};
        for (var key in ft) {
            if (!ft.hasOwnProperty(key)) continue;
            try {
                var tag = ft[key];
                result[key] = {
                    text: sv(tag, "text", ""),
                    textColor: sv(tag, "textColor", ""),
                    bgColor: sv(tag, "bgColor", ""),
                    icon: sv(tag, "icon", ""),
                    iconWidth: sv(tag, "iconWidth", ""),
                    iconHeight: sv(tag, "iconHeight", "")
                };
            } catch(e) { result[key] = String(ft[key]); }
        }
        return result;
    }

    // userFishShopLabel 提取
    function extractShopLabel(label) {
        if (!label || typeof label !== "object") return {};
        var tags = [];
        try {
            var tagList = sa(label.tagList);
            for (var i = 0; i < tagList.length; i++) {
                var d = so(tagList[i]).data || {};
                tags.push({
                    content: sv(d, "content", ""),
                    color: sv(d, "color", ""),
                    size: sv(d, "size", ""),
                    type: sv(d, "type", "")
                });
            }
        } catch(e) {}
        return { tags: tags, config: so(label.config) };
    }

    // richTitle 提取
    function extractRichTitle(rt) {
        if (!Array.isArray(rt)) return [];
        var result = [];
        for (var i = 0; i < rt.length; i++) {
            var item = rt[i];
            result.push({
                type: sv(item, "type", ""),
                data: so(item.data)
            });
        }
        return result;
    }

    // 搜索数据结构化提取 - ALL 38 fields
    function extractSearchItems(obj) {
        var items = [];
        try {
            var list = obj.resultList;
            if (!list) return items;
            for (var i = 0; i < list.length; i++) {
                try {
                    var itemData = list[i].data;
                    var it = itemData.item;
                    if (!it) continue;
                    var main = it.main;
                    var itemTemplate = so(itemData.template);
                    var itemTemplateSingle = so(itemData.templateSingle);
                    if (!main) continue;
                    var ex = main.exContent || {};

                    // userIdentityShow 可能是对象
                    var userIdentityShow = "";
                    try {
                        var uis = ex.userIdentityShow;
                        if (uis && typeof uis === "object") userIdentityShow = uis.text || JSON.stringify(uis);
                        else if (isStr(uis)) userIdentityShow = uis;
                    } catch(e) {}

                    // price 数组提取
                    var priceBreakdown = [];
                    if (Array.isArray(ex.price)) {
                        for (var j = 0; j < ex.price.length; j++) {
                            var pp = ex.price[j];
                            priceBreakdown.push({
                                text: sv(pp, "text", ""),
                                color: sv(pp, "color", ""),
                                size: sv(pp, "size", ""),
                                bold: sv(pp, "bold", ""),
                                fontWeight: sv(pp, "fontWeight", ""),
                                lineThrough: sv(pp, "lineThrough", "")
                            });
                        }
                    }

                    // priceTag 提取
                    var priceTags = [];
                    try {
                        var ptList = sa(ex.priceTag);
                        for (var k = 0; k < ptList.length; k++) {
                            var pt = ptList[k];
                            priceTags.push({ type: sv(pt, "type", ""), data: so(pt.data) });
                        }
                    } catch(e) {}

                    // detailParams 提取
                    var detailParams = {};
                    try {
                        var dp = so(ex.detailParams);
                        detailParams = {
                            itemId: sv(dp, "itemId", ""),
                            itemType: sv(dp, "itemType", ""),
                            title: sv(dp, "title", ""),
                            isSKU: sv(dp, "isSKU", ""),
                            isVideo: sv(dp, "isVideo", ""),
                            picWidth: sv(dp, "picWidth", ""),
                            picHeight: sv(dp, "picHeight", ""),
                            soldPrice: sv(dp, "soldPrice", ""),
                            imageInfos: sa(dp.imageInfos),
                            postInfo: so(dp.postInfo)
                        };
                    } catch(e) {}

                    // probeParamMap
                    var probeInfo = {};
                    try {
                        var pbm = so(ex.probeParamMap);
                        probeInfo = {
                            gsl: sv(pbm, "gsl", ""),
                            matchType: sv(pbm, "matchType", ""),
                            rsl: sv(pbm, "rsl", "")
                        };
                    } catch(e) {}

                    // titleSpan
                    var titleStyle = {};
                    try {
                        var ts = so(ex.titleSpan);
                        titleStyle = {
                            bold: sv(ts, "bold", ""),
                            color: sv(ts, "color", ""),
                            content: sv(ts, "content", ""),
                            fontWeight: sv(ts, "fontWeight", ""),
                            lineHeight: sv(ts, "lineHeight", ""),
                            maxLines: sv(ts, "maxLines", ""),
                            size: sv(ts, "size", "")
                        };
                    } catch(e) {}

                    // dislikeFeedback
                    var dislikeInfo = {};
                    try {
                        var df = so(ex.dislikeFeedback);
                        dislikeInfo = {
                            dislikeStyle: sv(df, "dislikeStyle", ""),
                            itemPicUrl: sv(df, "itemPicUrl", ""),
                            targetUrl: sv(df, "targetUrl", ""),
                            similarTargetUrl: sv(df, "similarTargetUrl", ""),
                            showList: sa(df.showList),
                            moreList: sa(df.moreList)
                        };
                    } catch(e) {}

                    // fishTagCustomParam
                    var fishTagCustom = {};
                    try {
                        var ftcp = so(ex.fishTagCustomParam);
                        fishTagCustom = {
                            feedStyle202208: sv(ftcp, "feedStyle202208", ""),
                            feedStyle202304: sv(ftcp, "feedStyle202304", "")
                        };
                    } catch(e) {}

                    // jump2XianYuHao
                    var jumpXyh = {};
                    try {
                        var jxyh = so(ex.jump2XianYuHao);
                        jumpXyh = {
                            targetUrl: sv(jxyh, "targetUrl", ""),
                            clickParam: so(jxyh.clickParam)
                        };
                    } catch(e) {}

                    // serviceUtParams - 服务标签(含已售/行情价/降价/包邮等)
                    var serviceTags = [];
                    var soldCountLabel = "";
                    var wantNum = "0";
                    try {
                        var clickArgs = so(main.clickParam && main.clickParam.args);
                        var spStr = sv(clickArgs, "serviceUtParams", "");
                        if (spStr) {
                            var parsed = JSON.parse(spStr);
                            if (Array.isArray(parsed)) {
                                for (var ti = 0; ti < parsed.length; ti++) {
                                    var t = parsed[ti];
                                    var tagContent = sv(t.args, "content", "");
                                    serviceTags.push({
                                        tagId: sv(t, "arg1", ""),
                                        content: tagContent
                                    });
                                    if (t.arg1 === "4_tag_r3_1028") {
                                        soldCountLabel = tagContent;
                                    }
                                }
                            }
                        }
                        wantNum = sv(clickArgs, "wantNum", "0");
                    } catch(e) {}

                    items.push({
                        itemId: sv(ex, "itemId", ""),
                        title: sv(ex, "title", ""),
                        titleRowType: sv(ex, "titleRowType", ""),
                        titleStyle: titleStyle,
                        price: priceArrToStr(ex.price),
                        priceBreakdown: priceBreakdown,
                        priceTag: priceTags,
                        richTitle: extractRichTitle(ex.richTitle),
                        picUrl: sv(ex, "picUrl", ""),
                        picWidth: sv(ex, "picWidth", ""),
                        picHeight: sv(ex, "picHeight", ""),
                        placeholderColor: sv(ex, "placeholderColor", ""),
                        showVideoIcon: sv(ex, "showVideoIcon", false),
                        area: sv(ex, "area", ""),
                        want: sv(ex, "want", "0"),
                        wantNum: wantNum,
                        soldCountLabel: soldCountLabel,
                        serviceTags: serviceTags,
                        userNick: sv(ex, "userNickName", ""),
                        userAvatarUrl: sv(ex, "userAvatarUrl", ""),
                        userActiveUrl: sv(ex, "userActiveUrl", ""),
                        userIdentity: userIdentityShow,
                        userFishShopLabel: extractShopLabel(ex.userFishShopLabel),
                        userIsUseFishShopCard: sv(ex, "userIsUseFishShopCard", false),
                        hideUserInfo: sv(ex, "hideUserInfo", false),
                        fishTags: extractFishTags(ex.fishTags),
                        fishTagCustomParam: fishTagCustom,
                        isAuction: sv(ex, "isAuction", false),
                        isAliMaMaAD: sv(ex, "isAliMaMaAD", false),
                        detailPageType: sv(ex, "detailPageType", ""),
                        detailParams: detailParams,
                        stuffStatusTagWidth: sv(ex, "stuffStatusTagWidth", "0"),
                        stuffStatusTagHeight: sv(ex, "stuffStatusTagHeight", "0"),
                        targetUrl: sv(main, "targetUrl", ""),
                        probeInfo: probeInfo,
                        dislikeFeedback: dislikeInfo,
                        jump2XianYuHao: jumpXyh,
                        useFy25NewStyleLabel: sv(ex, "useFy25NewStyleLabel", false),
                        searchIndex: i,
                        resultStyle: sv(list[i], "style", ""),
                        resultType: sv(list[i], "type", ""),
                        template: itemTemplate,
                        templateSingle: itemTemplateSingle,
                        collectedAt: new Date().toISOString()
                    });
                } catch(e2) {}
            }
        } catch(e) {}
        return items;
    }

    // 详情数据提取 - itemDO + sellerDO 全字段
    function extractDetail(rawJson) {
        try {
            var obj = JSON.parse(rawJson);
            var result = {};

            // serverTime
            result.serverTime = sv(obj, "serverTime", "");
            result.dataType = sv(obj, "dataType", "");

            // itemDO - 商品核心数据
            var item = so(obj.itemDO);
            if (Object.keys(item).length > 0) {
                // 提取 attributeMap 全部字段
                var attrMap = {};
                var am = so(item.attributeMap);
                for (var k in am) {
                    if (am.hasOwnProperty(k)) attrMap[k] = String(sv(am, k, ""));
                }

                // skuData 结构化
                var skuList = [];
                try {
                    var skuData = sa(item.skuData);
                    for (var si = 0; si < skuData.length; si++) {
                        var sd = skuData[si];
                        skuList.push({
                            skuId: sv(sd, "skuId", ""),
                            price: sv(sd, "price", ""),
                            quantity: sv(sd, "quantity", ""),
                            status: sv(sd, "status", ""),
                            attributes: so(sd.attributes)
                        });
                    }
                } catch(e) {}

                // images 列表
                var images = [];
                try { images = sa(item.images); } catch(e) {}

                // tagVO 标签列表
                var tagList = [];
                try {
                    var tvo = sa(item.tagVO);
                    for (var ti = 0; ti < tvo.length; ti++) {
                        tagList.push({ type: sv(tvo[ti], "type", ""), data: so(tvo[ti].data) });
                    }
                } catch(e) {}

                // xianyuItemTagList
                var xyTagList = [];
                try {
                    var xtl = sa(item.xianyuItemTagList);
                    for (var xi = 0; xi < xtl.length; xi++) {
                        xyTagList.push({ type: sv(xtl[xi], "type", ""), data: so(xtl[xi].data) });
                    }
                } catch(e) {}

                result.item = {
                    // 基础标识
                    itemId: sv(item, "itemId", ""),
                    itemType: sv(item, "itemType", ""),
                    categoryId: sv(item, "categoryId", ""),
                    bizType: sv(item, "bizType", ""),
                    userId: sv(item, "userId", ""),
                    sellerId: sv(item, "sellerId", ""),
                    templateId: sv(item, "templateId", ""),
                    // 标题和描述
                    title: sv(item, "title", ""),
                    subTitle: sv(item, "subTitle", ""),
                    titleMode: sv(item, "titleMode", ""),
                    titleIsUserInput: sv(item, "titleIsUserInput", ""),
                    desMode: sv(item, "desMode", ""),
                    desc: sv(item, "desc", ""),
                    richTextDesc: sv(item, "richTextDesc", ""),
                    // 价格
                    price: sv(item, "price", ""),
                    priceText: sv(item, "priceText", ""),
                    priceUnit: sv(item, "priceUnit", ""),
                    oldPrice: sv(item, "oldPrice", ""),
                    maxPrice: sv(item, "maxPrice", ""),
                    minPrice: sv(item, "minPrice", ""),
                    soldPrice: sv(item, "soldPrice", ""),
                    defaultPrice: sv(item, "defaultPrice", ""),
                    promotionPriceDO: so(item.promotionPriceDO),
                    postage: sv(item, "postage", ""),
                    transportFee: sv(item, "transportFee", ""),
                    freeShipping: sv(item, "freeShipping", ""),
                    // ★ 销量和热度（用户最关心的字段）
                    soldCnt: sv(item, "soldCnt", ""),
                    wantCnt: sv(item, "wantCnt", ""),
                    wantCntUnit: sv(item, "wantCntUnit", ""),
                    collectCnt: sv(item, "collectCnt", ""),
                    browseCnt: sv(item, "browseCnt", ""),
                    favorCnt: sv(item, "favorCnt", ""),
                    interactFavorCnt: sv(item, "interactFavorCnt", ""),
                    videoPlayCount: sv(item, "videoPlayCount", ""),
                    commentCount: sv(item, "commentCount", ""),
                    quantity: sv(item, "quantity", ""),
                    // 状态
                    itemStatus: sv(item, "itemStatus", ""),
                    itemStatusStr: sv(item, "itemStatusStr", ""),
                    status: sv(item, "status", ""),
                    gmtCreate: sv(item, "gmtCreate", ""),
                    GMT_CREATE_DATE_KEY: sv(item, "GMT_CREATE_DATE_KEY", ""),
                    // 位置
                    location: sv(item, "location", ""),
                    publishCity: sv(item, "publishCity", ""),
                    // 媒体
                    picUrl: sv(item, "picUrl", ""),
                    hasVideo: sv(item, "hasVideo", ""),
                    noPicItem: sv(item, "noPicItem", ""),
                    defaultPicture: sv(item, "defaultPicture", ""),
                    images: images,
                    imageInfos: sa(item.imageInfos),
                    // 交易
                    tbSupportTrade: sv(item, "tbSupportTrade", ""),
                    pcSupportTrade: sv(item, "pcSupportTrade", ""),
                    tradeAccessType: sv(item, "tradeAccessType", ""),
                    tradeBanners: sa(item.tradeBanners),
                    bargained: sv(item, "bargained", ""),
                    // 标签体系
                    commonTags: sa(item.commonTags),
                    cpvLabels: sa(item.cpvLabels),
                    cpvTopics: sa(item.cpvTopics),
                    spuTopics: sa(item.spuTopics),
                    itemLabelExtList: sa(item.itemLabelExtList),
                    priceRelativeTags: sa(item.priceRelativeTags),
                    priceTextTags: sa(item.priceTextTags),
                    descRelativeTags: sa(item.descRelativeTags),
                    descTagColor: sv(item, "descTagColor", ""),
                    recommendTagList: sa(item.recommendTagList),
                    tagVO: tagList,
                    xianyuItemTagList: xyTagList,
                    // SKU
                    skuId: sv(item, "skuId", ""),
                    skuList: sa(item.skuList),
                    idleItemSkuList: sa(item.idleItemSkuList),
                    skuData: skuList,
                    // 类目
                    itemCatDTO: so(item.itemCatDTO),
                    // 服务与安全
                    secuGuide: so(item.secuGuide),
                    uiItemServiceDOList: sa(item.uiItemServiceDOList),
                    // 分享与举报
                    shareData: so(item.shareData),
                    shareUrl: sv(item, "shareUrl", ""),
                    reportUrl: sv(item, "reportUrl", ""),
                    // 其他
                    simpleItem: sv(item, "simpleItem", ""),
                    trackParams: so(item.trackParams),
                    charitableItem: sv(item, "charitableItem", ""),
                    charitableTag: so(item.charitableTag),
                    inUAVItemPool: sv(item, "inUAVItemPool", ""),
                    worthBuySimilarFeeds: sv(item, "worthBuySimilarFeeds", ""),
                    resourceIdImages: sa(item.resourceIdImages),
                    recommendInfo: so(item.recommendInfo),
                    exContent: so(item.exContent),
                    extra: so(item.extra),
                    fishPoolCategory: sv(item, "fishPoolCategory", ""),
                    userInputTopics: sa(item.userInputTopics),
                    croControl: sa(item.croControl),
                    attributeMap: attrMap
                };
            }

            // sellerDO - 卖家核心数据
            var seller = so(obj.sellerDO);
            if (Object.keys(seller).length > 0) {
                // levelTags
                var levelTags = [];
                try {
                    var lt = sa(seller.levelTags);
                    for (var li = 0; li < lt.length; li++) {
                        levelTags.push({
                            iconUrl: sv(lt[li], "iconUrl", ""),
                            iconWidth: sv(lt[li], "iconWidth", ""),
                            iconHeight: sv(lt[li], "iconHeight", ""),
                            order: sv(lt[li], "order", "")
                        });
                    }
                } catch(e) {}

                // identityTags
                var identityTags = [];
                try {
                    var idt = sa(seller.identityTags);
                    for (var idi = 0; idi < idt.length; idi++) {
                        identityTags.push({
                            iconUrl: sv(idt[idi], "iconUrl", ""),
                            iconWidth: sv(idt[idi], "iconWidth", ""),
                            iconHeight: sv(idt[idi], "iconHeight", ""),
                            text: sv(idt[idi], "text", ""),
                            link: sv(idt[idi], "link", ""),
                            type: sv(idt[idi], "type", ""),
                            order: sv(idt[idi], "order", "")
                        });
                    }
                } catch(e) {}

                // sellerItems (only basic info, no need full attributeMap per item)
                var sellerItems = [];
                try {
                    var sitems = sa(seller.sellerItems);
                    for (var si = 0; si < sitems.length; si++) {
                        var sitem = sitems[si];
                        sellerItems.push({
                            itemId: sv(sitem, "itemId", ""),
                            iconUrl: sv(sitem, "iconUrl", ""),
                            link: sv(sitem, "link", ""),
                            fontSize: sv(sitem, "fontSize", ""),
                            title: sv(sitem, "title", ""),
                            text: sv(sitem, "text", ""),
                            type: sv(sitem, "type", "")
                        });
                    }
                } catch(e) {}

                // certifiTags (may or may not exist)
                var certTags = [];
                try {
                    var ct = sa(seller.certifiTags);
                    for (var ci = 0; ci < ct.length; ci++) {
                        certTags.push({ type: sv(ct[ci], "type", ""), data: so(ct[ci].data) });
                    }
                } catch(e) {}

                // sellerInfoTagsV2
                var infoTagsV2 = [];
                try {
                    var sitv2 = sa(seller.sellerInfoTagsV2);
                    for (var si2 = 0; si2 < sitv2.length; si2++) {
                        infoTagsV2.push({ type: sv(sitv2[si2], "type", ""), data: so(sitv2[si2].data) });
                    }
                } catch(e) {}

                // sellerServiceInfoTagsV2
                var svcTags = [];
                try {
                    var sst = sa(seller.sellerServiceInfoTagsV2);
                    for (var sti = 0; sti < sst.length; sti++) {
                        svcTags.push({ type: sv(sst[sti], "type", ""), data: so(sst[sti].data) });
                    }
                } catch(e) {}

                // sellerStatisticsInfoList
                var statInfo = [];
                try {
                    var ssl = sa(seller.sellerStatisticsInfoList);
                    for (var ssi = 0; ssi < ssl.length; ssi++) {
                        statInfo.push({ type: sv(ssl[ssi], "type", ""), data: so(ssl[ssi].data) });
                    }
                } catch(e) {}

                // sellerBizTags
                var bizTags = [];
                try {
                    var sbt = sa(seller.sellerBizTags);
                    for (var bi = 0; bi < sbt.length; bi++) {
                        bizTags.push({ type: sv(sbt[bi], "type", ""), data: so(sbt[bi].data) });
                    }
                } catch(e) {}

                result.seller = {
                    sellerId: sv(seller, "sellerId", ""),
                    nick: sv(seller, "nick", ""),
                    uniqueName: sv(seller, "uniqueName", ""),
                    desensitizationNick: sv(seller, "desensitizationNick", ""),
                    avatarUrl: sv(seller, "avatarUrl", ""),
                    portraitUrl: sv(seller, "portraitUrl", ""),
                    gender: sv(seller, "gender", ""),
                    city: sv(seller, "city", ""),
                    publishCity: sv(seller, "publishCity", ""),
                    aoiType: sv(seller, "aoiType", ""),
                    registerTime: sv(seller, "registerTime", ""),
                    userRegDay: sv(seller, "userRegDay", ""),
                    aliasName: sv(seller, "aliasName", ""),
                    signature: sv(seller, "signature", ""),
                    // 统计数据
                    itemCount: sv(seller, "itemCount", ""),
                    commentCount: sv(seller, "commentCount", ""),
                    fansCount: sv(seller, "fansCount", ""),
                    followCount: sv(seller, "followCount", ""),
                    collectCount: sv(seller, "collectCount", ""),
                    fishShopWatchUserCount: sv(seller, "fishShopWatchUserCount", ""),
                    hasSoldNumInteger: sv(seller, "hasSoldNumInteger", ""),
                    // 好评/回复率
                    newGoodRatioRate: sv(seller, "newGoodRatioRate", ""),
                    replyRatio24h: sv(seller, "replyRatio24h", ""),
                    replyInterval: sv(seller, "replyInterval", ""),
                    replyIn24hRatioDouble: sv(seller, "replyIn24hRatioDouble", ""),
                    avgReply30dLong: sv(seller, "avgReply30dLong", ""),
                    lastVisitTime: sv(seller, "lastVisitTime", ""),
                    // 认证
                    zhimaAuth: sv(seller, "zhimaAuth", ""),
                    zhimaLevelInfo: so(seller.zhimaLevelInfo),
                    yxpPro: sv(seller, "yxpPro", ""),
                    // 标签和展示
                    remarkDO: so(seller.remarkDO),
                    idleFishCreditTag: so(seller.idleFishCreditTag),
                    identityTags: identityTags,
                    levelTags: levelTags,
                    certifiTags: certTags,
                    sellerInfoTags: sa(seller.sellerInfoTags),
                    sellerInfoTagsV2: infoTagsV2,
                    sellerServiceInfoTagsV2: svcTags,
                    sellerStatisticsInfoList: statInfo,
                    sellerBizTags: bizTags,
                    sellerItems: sellerItems
                };
            }

            // b2cItemDO
            var b2cItem = so(obj.b2cItemDO);
            if (Object.keys(b2cItem).length > 0) {
                result.b2cItem = {
                    templateId: sv(b2cItem, "templateId", ""),
                    browseCnt: sv(b2cItem, "browseCnt", ""),
                    needCollapseTitle: sv(b2cItem, "needCollapseTitle", ""),
                    activityInfo: so(b2cItem.activityInfo),
                    benefitLabels: sa(b2cItem.benefitLabels),
                    benefitTags: sa(b2cItem.benefitTags),
                    commonTags: sa(b2cItem.commonTags),
                    descRelativeTags: sa(b2cItem.descRelativeTags),
                    priceRelativeTags: sa(b2cItem.priceRelativeTags),
                    priceTextTags: sa(b2cItem.priceTextTags)
                };
            }

            // b2cSellerDO
            var b2cSeller = so(obj.b2cSellerDO);
            if (Object.keys(b2cSeller).length > 0) {
                result.b2cSeller = {
                    userRegDay: sv(b2cSeller, "userRegDay", ""),
                    yxpPro: sv(b2cSeller, "yxpPro", ""),
                    identityTags: sa(b2cSeller.identityTags),
                    levelTags: sa(b2cSeller.levelTags),
                    sellerBizTags: sa(b2cSeller.sellerBizTags),
                    sellerInfoTags: sa(b2cSeller.sellerInfoTags),
                    sellerInfoTagsV2: sa(b2cSeller.sellerInfoTagsV2),
                    sellerServiceInfoTagsV2: sa(b2cSeller.sellerServiceInfoTagsV2),
                    sellerStatisticsInfoList: sa(b2cSeller.sellerStatisticsInfoList)
                };
            }

            // commerceDO (广告信息)
            var commerce = so(obj.commerceDO);
            if (Object.keys(commerce).length > 0) {
                var clue = so(commerce.clueAdsInfoDO);
                result.commerce = {
                    campaignId: sv(clue, "campaignId", ""),
                    entityType: sv(clue, "entityType", "")
                };
            }

            // interactDO (互动数据)
            var interact = so(obj.interactDO);
            if (Object.keys(interact).length > 0) {
                result.interact = interact;
            }

            // logisticsDO
            var logistics = so(obj.logisticsDO);
            if (Object.keys(logistics).length > 0) {
                result.logistics = logistics;
            }

            // picDetailDO
            var picDetail = so(obj.picDetailDO);
            if (Object.keys(picDetail).length > 0) {
                result.picDetail = picDetail;
            }

            // seafoodDO
            var seafood = so(obj.seafoodDO);
            if (Object.keys(seafood).length > 0) {
                result.seafood = seafood;
            }

            // configInfo
            result.configInfo = so(obj.configInfo);

            // buyerDO
            var buyer = so(obj.buyerDO);
            if (Object.keys(buyer).length > 0) {
                result.buyer = {
                    favored: sv(buyer, "favored", ""),
                    isFirstOrderUser: sv(buyer, "isFirstOrderUser", ""),
                    isNewUserIn7Day: sv(buyer, "isNewUserIn7Day", "")
                };
            }

            // b2cBuyerDO
            var b2cBuyer = so(obj.b2cBuyerDO);
            if (Object.keys(b2cBuyer).length > 0) {
                result.b2cBuyer = {
                    favored: sv(b2cBuyer, "favored", ""),
                    isFirstOrderUser: sv(b2cBuyer, "isFirstOrderUser", ""),
                    isNewUserIn7Day: sv(b2cBuyer, "isNewUserIn7Day", ""),
                    buyQualificationActList: sa(b2cBuyer.buyQualificationActList)
                };
            }

            // b2cUiIdleDetailConfigDO - UI配置标志
            var b2cUiConfig = so(obj.b2cUiIdleDetailConfigDO);
            if (Object.keys(b2cUiConfig).length > 0) {
                result.b2cUiConfig = b2cUiConfig;
            }

            // flowData - 流数据
            var flowData = so(obj.flowData);
            if (Object.keys(flowData).length > 0) {
                result.flowData = flowData;
            }

            // trackParams - 跟踪参数
            var trackParams = so(obj.trackParams);
            if (Object.keys(trackParams).length > 0) {
                result.trackParams = trackParams;
            }

            // needDecryptKeys
            result.needDecryptKeys = sa(obj.needDecryptKeys);

            // 保留完整原始数据引用
            result._collectedAt = new Date().toISOString();

            return JSON.stringify(result);
        } catch(e) {
            return JSON.stringify({ error: "detail_extract_failed", reason: String(e), raw: rawJson.substring(0, 5000) });
        }
    }

    // 评论数据提取
    function extractComments(rawJson) {
        try {
            var obj = JSON.parse(rawJson);
            var items = [];

            var rawItems = sa(obj.items);
            for (var i = 0; i < rawItems.length; i++) {
                var c = rawItems[i];

                // 递归提取回复
                var replies = [];
                try {
                    var rawReplies = sa(c.reply);
                    for (var ri = 0; ri < rawReplies.length; ri++) {
                        var r = rawReplies[ri];
                        var replyTags = [];
                        try {
                            var rt = sa(r.tags);
                            for (var rti = 0; rti < rt.length; rti++) {
                                replyTags.push({
                                    tagUrl: sv(rt[rti], "tagUrl", ""),
                                    width: sv(rt[rti], "width", ""),
                                    height: sv(rt[rti], "height", "")
                                });
                            }
                        } catch(e) {}

                        replies.push({
                            commentId: sv(r, "commentId", ""),
                            content: sv(r, "content", ""),
                            reporterId: sv(r, "reporterId", ""),
                            reporterNick: sv(r, "reporterNick", ""),
                            portraitUrl: sv(r, "portraitUrl", ""),
                            ipRegionAddress: sv(r, "ipRegionAddress", ""),
                            originIpRegionAddress: sv(r, "originIpRegionAddress", ""),
                            reportTime: sv(r, "reportTime", ""),
                            reportTimeStr: sv(r, "reportTimeStr", ""),
                            reportOwner: sv(r, "reportOwner", ""),
                            level: sv(r, "level", ""),
                            bizType: sv(r, "bizType", ""),
                            ownerType: sv(r, "ownerType", ""),
                            sellerId: sv(r, "sellerId", ""),
                            sellerNick: sv(r, "sellerNick", ""),
                            status: sv(r, "status", ""),
                            parentCommentId: sv(r, "parentCommentId", ""),
                            parentCommenterId: sv(r, "parentCommenterId", ""),
                            parentCommenterNick: sv(r, "parentCommenterNick", ""),
                            beReplierId: sv(r, "beReplierId", ""),
                            beReplierNick: sv(r, "beReplierNick", ""),
                            replyCommentId: sv(r, "replyCommentId", ""),
                            tags: replyTags
                        });
                    }
                } catch(e) {}

                items.push({
                    commentId: sv(c, "commentId", ""),
                    content: sv(c, "content", ""),
                    reporterId: sv(c, "reporterId", ""),
                    reporterNick: sv(c, "reporterNick", ""),
                    portraitUrl: sv(c, "portraitUrl", ""),
                    ipRegionAddress: sv(c, "ipRegionAddress", ""),
                    originIpRegionAddress: sv(c, "originIpRegionAddress", ""),
                    reportTime: sv(c, "reportTime", ""),
                    reportTimeStr: sv(c, "reportTimeStr", ""),
                    reportOwner: sv(c, "reportOwner", ""),
                    level: sv(c, "level", ""),
                    bizType: sv(c, "bizType", ""),
                    ownerType: sv(c, "ownerType", ""),
                    itemId: sv(c, "itemId", ""),
                    sellerId: sv(c, "sellerId", ""),
                    sellerNick: sv(c, "sellerNick", ""),
                    status: sv(c, "status", ""),
                    replies: replies
                });
            }

            return JSON.stringify({
                totalCount: sv(obj, "totalCount", ""),
                nextPage: sv(obj, "nextPage", ""),
                otherPage: sv(obj, "otherPage", ""),
                newComment: sv(obj, "newComment", ""),
                checkExistCommentIdRes: sv(obj, "checkExistCommentIdRes", ""),
                serverTime: sv(obj, "serverTime", ""),
                items: items,
                needDecryptKeys: sa(obj.needDecryptKeys),
                needDecryptKeysV2: sa(obj.needDecryptKeysV2),
                serverDecryptKeys: sa(obj.serverDecryptKeys),
                ext: so(obj.ext),
                _collectedAt: new Date().toISOString()
            });
        } catch(e) {
            return JSON.stringify({ error: "comment_extract_failed", reason: String(e), raw: rawJson.substring(0, 5000) });
        }
    }

    // === 同步等待 ===
    function awaitCall(expectedApi) {
        var latch = Cdl.$new(1);
        gLatch = latch;
        gExpectedApi = expectedApi;
        latch.await(35, TimeUnit.SECONDS.value);
        gLatch = null;
        gExpectedApi = "";
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

    // === Market request helper ===
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

    // ==================== RPC Exports ====================

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
        var searchId = "";
        var rn = "";
        var sellingOrder = "";
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
                    hasMore = scf.nextPage || hasMore;
                    searchId = scf.searchId || "";
                    rn = scf.rn || "";
                    sellingOrder = scf.sellingOrder || "";
                }
            }
            // tab info
            var tabs = sa(lastJson.tabList);
            var tabInfo = [];
            for (var ti = 0; ti < tabs.length; ti++) {
                tabInfo.push({
                    showName: sv(tabs[ti], "showName", ""),
                    searchTabType: sv(tabs[ti], "searchTabType", ""),
                    apiName: sv(tabs[ti], "apiName", "")
                });
            }
        } catch(e) {}
        gPendingApi = "";
        return JSON.stringify({
            keyword: kw,
            page: page,
            pageSize: pageSize,
            count: items.length,
            numFound: numFound,
            hasMore: hasMore,
            maxPrice: searchMaxPrice,
            minPrice: searchMinPrice,
            searchId: searchId,
            rn: rn,
            sellingOrder: sellingOrder,
            tabs: tabInfo,
            items: items,
            collectedAt: new Date().toISOString()
        });
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
        return gData["market_tabs_" + kw] || "{}";
    };

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
        page = page || 1;
        var req = makeMarketReq("mtop.taobao.idlemtopsearch.market.historysale", "1.0", {
            categoryId: categoryId,
            categoryName: categoryName || "",
            keyword: kw,
            pageNumber: page,
            pageSize: 6,
            searchTabType: "SEARCH_TAB_MARKET",
            showType: "SEARCH_TAB_MARKET_QUERY",
            spuId: spuId,
            spuName: spuName || "",
            type: 1
        });
        var cacheKey = kw + "_hs_p" + page;
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

    rpc.exports.getComments = function(itemId, page) {
        page = page || 1;
        var map = HashMap.$new();
        map.put("itemId", String.$new(itemId));
        map.put("pageNumber", String.$new("" + page));
        map.put("bizType", String.$new(""));

        var req = ApiProtocol.$new();
        req.apiNameAndVersion(String.$new("mtop.taobao.idle.comment.list"), String.$new("3.0"));
        req.paramMap(map);

        var cacheKey = itemId + "_p" + page;
        gPendingApi = cacheKey;
        apiCtx.send(req, DummyCb.$new());
        awaitCall("comment");
        gPendingApi = "";

        return gData["comment_" + cacheKey] || '{"error":"timeout"}';
    };

    rpc.exports.status = function() {
        return JSON.stringify({ ok: true, ready: gReady, apiCtx: apiCtx != null });
    };

    rpc.exports.clear = function() {
        gData = {};
        gPendingApi = "";
        gExpectedApi = "";
        return '{"ok":true}';
    };

    rpc.exports.getRawCache = function(prefix) {
        var result = {};
        for (var key in gData) {
            if (gData.hasOwnProperty(key) && key.indexOf(prefix) === 0) {
                result[key] = gData[key];
            }
        }
        return JSON.stringify(result);
    };

    rpc.exports.getRawKeys = function() {
        var keys = [];
        for (var key in gData) {
            if (gData.hasOwnProperty(key)) {
                keys.push(key);
            }
        }
        return JSON.stringify(keys);
    };

    gReady = true;
    log("Ready. RPC v2: search,getMarketTabs,getMarketTopbar,getMarketHistorySale,getMarketPriceTrend,getDetail,getComments,status,clear,getRawCache,getRawKeys");
    });
}

setTimeout(tryInit, 1500);

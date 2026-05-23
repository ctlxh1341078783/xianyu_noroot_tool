// detail_monitor.js v5 — CLI 模式专用，console.log 输出

console.log("v5 脚本开始加载...");

setTimeout(function() {
    if (typeof Java === "undefined") {
        console.log("Java 未就绪，退出");
        return;
    }
    console.log("Java 可用，开始 hook...");

    Java.perform(function() {
        console.log("进入 Java.perform...");

        // === Hook MtopLauncher.send — 拦截所有 API 请求 ===
        try {
            var MtopLauncher = Java.use("com.taobao.android.remoteobject.easy.MtopLauncher");
            MtopLauncher.send.implementation = function(protocol, callback) {
                var api = "";
                try { api = protocol.getApiNameAndVersion() || ""; } catch(e) {}
                console.log("[SEND] " + api);
                this.send(protocol, callback);
            };
            console.log("Hook 1: MtopLauncher.send OK");
        } catch(e) {
            console.log("Hook 1 失败: " + e);
        }

        // === Hook RemoteMtopCallback.onMtopReturn — 拦截响应 ===
        try {
            var RMC = Java.use("com.taobao.android.remoteobject.easy.RemoteMtopCallback");
            var orig = RMC.onMtopReturn;
            RMC.onMtopReturn.implementation = function(ctx, map, ret) {
                var api = "";
                try { api = ret.getApi() || ""; } catch(e) {}
                var r = orig.call(this, ctx, map, ret);

                console.log("[RETURN] " + api);

                // 详情接口 / shade 接口 特殊处理
                if (api.indexOf("detail") >= 0 || api.indexOf("shade") >= 0) {
                    try {
                        var data = ret.getData();
                        if (data) {
                            var FastJSON = Java.use("com.alibaba.fastjson.JSON");
                            var jsonStr = FastJSON.toJSONString(data);
                            var obj = JSON.parse(jsonStr);
                            var itemDO = obj.itemDO || (obj.data && obj.data.itemDO) || {};
                            var sellerDO = obj.sellerDO || (obj.data && obj.data.sellerDO) || {};

                            console.log("");
                            console.log("========== 详情数据 ==========");
                            console.log("itemId: " + itemDO.itemId);
                            console.log("title:  " + itemDO.title);
                            console.log("--- 关键字段 ---");
                            console.log("soldCnt        = " + itemDO.soldCnt);
                            console.log("soldPrice      = " + itemDO.soldPrice);
                            console.log("wantCnt        = " + itemDO.wantCnt);
                            console.log("collectCnt     = " + itemDO.collectCnt);
                            console.log("browseCnt      = " + itemDO.browseCnt);
                            console.log("commentCount   = " + itemDO.commentCount);
                            console.log("price          = " + itemDO.price);
                            console.log("oldPrice       = " + itemDO.oldPrice);
                            console.log("quantity       = " + itemDO.quantity);
                            console.log("itemStatus     = " + itemDO.itemStatus);
                            console.log("--- sellerDO ---");
                            console.log("nick             = " + sellerDO.nick);
                            console.log("hasSoldNumInteger= " + sellerDO.hasSoldNumInteger);
                            console.log("itemCount        = " + sellerDO.itemCount);
                            console.log("");
                            console.log("--- itemDO 全部字段 (" + Object.keys(itemDO).length + ") ---");
                            for (var k in itemDO) {
                                var vs = String(itemDO[k]);
                                if (vs.length > 200) vs = vs.substring(0, 200) + "...";
                                console.log("  " + k + " = " + vs);
                            }
                            console.log("原始 JSON: " + jsonStr.length + " bytes");
                            console.log("==============================");
                            console.log("");
                        }
                    } catch(e2) {
                        console.log("[DETAIL ERR] " + e2);
                    }
                }
                return r;
            };
            console.log("Hook 2: onMtopReturn OK");
        } catch(e) {
            console.log("Hook 2 失败: " + e);
        }

        console.log("全部 Hook 安装完成！等待详情页请求...");
    });
}, 2000);

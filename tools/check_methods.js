// 检查 RemoteMtopCallback 和 MtopSend 的方法签名

function logMethods(className) {
    try {
        var clz = Java.use(className);
        var methods = clz.class.getDeclaredMethods();
        send({type: "header", msg: "=== " + className + " (" + methods.length + " methods) ==="});
        for (var i = 0; i < methods.length; i++) {
            var m = methods[i];
            send({type: "method", name: m.getName(), sig: m.toString()});
        }
    } catch(e) {
        send({type: "error", msg: className + ": " + e});
    }
}

function tryInit() {
    if (typeof Java === "undefined") {
        setTimeout(tryInit, 2000);
        return;
    }
    Java.perform(function() {
        logMethods("com.taobao.android.remoteobject.easy.RemoteMtopCallback");
        logMethods("com.taobao.android.remoteobject.easy.MtopLauncher");
        logMethods("com.taobao.android.remoteobject.easy.MtopSend");
        send({type: "done"});
    });
}

setTimeout(tryInit, 1000);

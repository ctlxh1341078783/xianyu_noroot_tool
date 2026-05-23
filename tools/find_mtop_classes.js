// 枚举闲鱼中所有与 MTOP/网络请求相关的 Java 类

function tryInit() {
    if (typeof Java === "undefined") {
        setTimeout(tryInit, 2000);
        return;
    }
    Java.perform(function() {
        send({type: "status", msg: "开始枚举..."});

        var keywords = ["mtop", "Mtop", "MTOP", "Remote", "remote", "ApiCall", "CallBack", "Callback"];
        var found = {};

        Java.enumerateLoadedClasses({
            onMatch: function(className) {
                for (var i = 0; i < keywords.length; i++) {
                    if (className.indexOf(keywords[i]) >= 0) {
                        if (!found[className]) {
                            found[className] = true;
                            send({type: "class", name: className});
                        }
                        break;
                    }
                }
            },
            onComplete: function() {
                send({type: "status", msg: "枚举完成，共找到 " + Object.keys(found).length + " 个相关类"});

                // 专门找 launcher 相关的
                send({type: "status", msg: "--- Launcher 类 ---"});
                Java.enumerateLoadedClasses({
                    onMatch: function(className) {
                        if (className.indexOf("Launcher") >= 0 || className.indexOf("launcher") >= 0) {
                            send({type: "class", name: className});
                        }
                    },
                    onComplete: function() {
                        send({type: "status", msg: "done"});
                    }
                });
            }
        });
    });
}

setTimeout(tryInit, 1000);

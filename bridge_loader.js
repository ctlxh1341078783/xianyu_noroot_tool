// Bridge loading mechanism for gadget Python API
function loadBridge(name) {
    var result;
    send({type: "frida:load-bridge", name: name});
    recv("frida:bridge-loaded", function(msg) {
        result = Script.evaluate(
            "/frida/bridges/" + msg.filename,
            "(function () { " + [
                msg.source,
                "Object.defineProperty(globalThis, '" + name + "', { value: bridge });",
                "return bridge;"
            ].join("\n") + " })();"
        );
    }).wait();
    return result;
}

// Lazy getters for Java bridge
Object.defineProperty(globalThis, "Java", {
    enumerable: false,
    configurable: true,
    get: function() { return loadBridge("Java"); }
});

console.log("[BRIDGE] Java getter defined, waiting for first access...");

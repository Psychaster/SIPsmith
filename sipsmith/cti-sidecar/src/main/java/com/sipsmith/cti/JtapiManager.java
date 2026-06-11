package com.sipsmith.cti;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Wraps the Cisco JTAPI library via reflection.
 *
 * JTAPI is loaded at runtime from the jar fetched from the CUCM cluster.
 * Using reflection avoids a compile-time dependency on the proprietary jar.
 *
 * JTAPI class hierarchy (for reference):
 *   JtapiPeerFactory.getJtapiPeer("com.cisco.jtapi.CiscoJtapiPeer")
 *     → JtapiPeer.getProvider("host;loginID=user;passwd=pass")
 *       → Provider (CiscoProvider)
 *         → Provider.getTerminals() → Terminal[]
 *         → Provider.createCall() → Call
 *           → Call.connect(terminal, address, dialedDN)
 */
public class JtapiManager {

    private ClassLoader jtapiLoader;
    private Object provider;   // javax.telephony.Provider
    private final Map<String, Object> activeCalls = new ConcurrentHashMap<>(); // callId → Call
    private final Map<String, Object> terminals = new ConcurrentHashMap<>();   // deviceName → Terminal
    private final AtomicInteger callCounter = new AtomicInteger(0);

    // ── Connect ──────────────────────────────────────────────────────────────

    public void connect(String host, String user, String password) throws Exception {
        // Locate the jtapi jar from our standard path
        Path jtapiJar = findJtapiJar();

        if (jtapiJar == null) {
            Main.emit("error", new JSONObject()
                .put("message", "jtapi_unavailable: no jtapi jar found in classpath"));
            Main.emit("jtapi_unavailable", new JSONObject());
            return;
        }

        // Dynamic class loading
        jtapiLoader = new URLClassLoader(
            new URL[]{jtapiJar.toUri().toURL()},
            getClass().getClassLoader()
        );

        // JtapiPeerFactory.getJtapiPeer(peerName) → JtapiPeer
        Class<?> peerFactoryClass = jtapiLoader.loadClass("javax.telephony.JtapiPeerFactory");
        Object peer = peerFactoryClass
            .getMethod("getJtapiPeer", String.class)
            .invoke(null, "com.cisco.jtapi.CiscoJtapiPeer");

        // peer.getProvider("host;loginID=user;passwd=pass") → Provider
        String providerString = host + ";loginID=" + user + ";passwd=" + password;
        Class<?> peerClass = jtapiLoader.loadClass("javax.telephony.JtapiPeer");
        provider = peerClass
            .getMethod("getProvider", String.class)
            .invoke(peer, providerString);

        // Register a ProviderObserver to track state
        registerProviderObserver(host, user);

        Main.emit("ready", new JSONObject()
            .put("host", host)
            .put("user", user));
    }

    public void disconnect() throws Exception {
        if (provider != null) {
            provider.getClass().getMethod("shutdown").invoke(provider);
            provider = null;
        }
        activeCalls.clear();
        terminals.clear();
        Main.emit("disconnected", new JSONObject());
    }

    public void shutdown() {
        try { disconnect(); } catch (Exception ignored) { /* noqa */ }
    }

    // ── Device listing ────────────────────────────────────────────────────────

    public JSONObject listDevices() throws Exception {
        requireProvider();
        Object[] terminalArray = (Object[]) provider.getClass()
            .getMethod("getTerminals")
            .invoke(provider);

        JSONArray devices = new JSONArray();
        if (terminalArray != null) {
            for (Object t : terminalArray) {
                String name = (String) t.getClass().getMethod("getName").invoke(t);
                JSONObject dev = new JSONObject().put("device", name);
                try {
                    // Get addresses (directory numbers)
                    Object[] addrs = (Object[]) t.getClass().getMethod("getAddresses").invoke(t);
                    JSONArray dns = new JSONArray();
                    if (addrs != null) {
                        for (Object a : addrs) {
                            dns.put(a.getClass().getMethod("getName").invoke(a));
                        }
                    }
                    dev.put("directory_numbers", dns);
                } catch (Exception ignored) { /* noqa */ }
                devices.put(dev);
            }
        }
        return new JSONObject().put("devices", devices);
    }

    // ── Observe device ────────────────────────────────────────────────────────

    public void observeDevice(String deviceName) throws Exception {
        requireProvider();
        Object terminal = provider.getClass()
            .getMethod("getTerminal", String.class)
            .invoke(provider, deviceName);
        if (terminal == null) {
            throw new Exception("Device not found: " + deviceName);
        }
        terminals.put(deviceName, terminal);
        registerTerminalObserver(terminal, deviceName);
        registerAddressObserver(terminal, deviceName);
    }

    // ── Call control ──────────────────────────────────────────────────────────

    public String makeCall(String callingDevice, String calledDn) throws Exception {
        requireProvider();
        Object terminal = getOrFetchTerminal(callingDevice);
        Object address = getFirstAddress(terminal);

        Object call = provider.getClass().getMethod("createCall").invoke(provider);
        Class<?> callClass = call.getClass();

        // call.connect(terminal, address, calledDn)
        callClass.getMethod("connect",
            jtapiLoader.loadClass("javax.telephony.Terminal"),
            jtapiLoader.loadClass("javax.telephony.Address"),
            String.class
        ).invoke(call, terminal, address, calledDn);

        String callId = "call-" + callCounter.incrementAndGet();
        activeCalls.put(callId, call);
        registerCallObserver(call, callId);
        return callId;
    }

    public void answer(String callId) throws Exception {
        Object call = requireCall(callId);
        // Find an alerting TerminalConnection and answer it
        Object[] connections = (Object[]) call.getClass().getMethod("getConnections").invoke(call);
        if (connections == null) throw new Exception("No connections on call " + callId);
        for (Object conn : connections) {
            Object[] tcs = (Object[]) conn.getClass().getMethod("getTerminalConnections").invoke(conn);
            if (tcs == null) continue;
            for (Object tc : tcs) {
                int state = (int) tc.getClass().getMethod("getState").invoke(tc);
                // ALERTING = 0x40 in standard JTAPI
                if (state == 0x40 || state == 64) {
                    tc.getClass().getMethod("answer").invoke(tc);
                    return;
                }
            }
        }
        throw new Exception("No alerting terminal connection found for call " + callId);
    }

    public void hangup(String callId) throws Exception {
        Object call = requireCall(callId);
        call.getClass().getMethod("drop").invoke(call);
        activeCalls.remove(callId);
    }

    public void hold(String callId) throws Exception {
        Object call = requireCall(callId);
        // CiscoCall.hold() via reflection
        try {
            call.getClass().getMethod("hold").invoke(call);
        } catch (NoSuchMethodException e) {
            // Standard JTAPI: set each TerminalConnection to hold
            holdViaTerminalConnections(call);
        }
    }

    public void resume(String callId) throws Exception {
        Object call = requireCall(callId);
        try {
            call.getClass().getMethod("unhold").invoke(call);
        } catch (NoSuchMethodException e) {
            resumeViaTerminalConnections(call);
        }
    }

    public void transfer(String callId, String destination) throws Exception {
        Object call = requireCall(callId);
        // Create a consultation call, then transfer
        Object terminal = getCallingTerminalFromCall(call);
        Object address = getFirstAddress(terminal);
        Object consult = provider.getClass().getMethod("createCall").invoke(provider);
        consult.getClass().getMethod("connect",
            jtapiLoader.loadClass("javax.telephony.Terminal"),
            jtapiLoader.loadClass("javax.telephony.Address"),
            String.class
        ).invoke(consult, terminal, address, destination);
        // Transfer via CiscoCall
        call.getClass().getMethod("transfer",
            jtapiLoader.loadClass("javax.telephony.Call")
        ).invoke(call, consult);
        activeCalls.remove(callId);
    }

    public void sendDTMF(String callId, String digits) throws Exception {
        Object call = requireCall(callId);
        // Try CiscoCall.sendData(digits) — Cisco extension
        try {
            call.getClass().getMethod("sendData", String.class).invoke(call, digits);
        } catch (NoSuchMethodException e) {
            // Fallback: use TerminalConnection DTMF if supported
            Object[] connections = (Object[]) call.getClass().getMethod("getConnections").invoke(call);
            if (connections != null && connections.length > 0) {
                Object[] tcs = (Object[]) connections[0].getClass()
                    .getMethod("getTerminalConnections").invoke(connections[0]);
                if (tcs != null && tcs.length > 0) {
                    try {
                        tcs[0].getClass().getMethod("generateDTMF", String.class).invoke(tcs[0], digits);
                    } catch (NoSuchMethodException e2) {
                        throw new Exception("DTMF not supported on this connection type");
                    }
                }
            }
        }
    }

    // ── Observer registration via dynamic proxy ───────────────────────────────

    private void registerProviderObserver(String host, String user) {
        try {
            Class<?> observerClass = jtapiLoader.loadClass("javax.telephony.ProviderObserver");
            Object observer = Proxy.newProxyInstance(
                jtapiLoader,
                new Class[]{observerClass},
                new InvocationHandler() {
                    @Override
                    public Object invoke(Object proxy, Method method, Object[] args) {
                        if ("providerChangedEvent".equals(method.getName()) && args != null) {
                            handleProviderEvents(args[0]);
                        }
                        return null;
                    }
                }
            );
            provider.getClass().getMethod("addObserver", observerClass).invoke(provider, observer);
        } catch (Exception e) {
            Main.emit("warning", new JSONObject().put("message", "ProviderObserver failed: " + e.getMessage()));
        }
    }

    private void registerTerminalObserver(Object terminal, String deviceName) {
        try {
            Class<?> observerClass = jtapiLoader.loadClass("javax.telephony.TerminalObserver");
            Object observer = Proxy.newProxyInstance(
                jtapiLoader,
                new Class[]{observerClass},
                (proxy, method, args) -> {
                    if ("terminalChangedEvent".equals(method.getName()) && args != null) {
                        handleTerminalEvents(args[0], deviceName);
                    }
                    return null;
                }
            );
            terminal.getClass().getMethod("addObserver", observerClass).invoke(terminal, observer);
        } catch (Exception e) {
            Main.emit("warning", new JSONObject()
                .put("message", "TerminalObserver failed for " + deviceName + ": " + e.getMessage()));
        }
    }

    private void registerAddressObserver(Object terminal, String deviceName) {
        try {
            Object[] addrs = (Object[]) terminal.getClass().getMethod("getAddresses").invoke(terminal);
            if (addrs == null) return;
            Class<?> observerClass = jtapiLoader.loadClass("javax.telephony.AddressObserver");
            for (Object addr : addrs) {
                String dn = (String) addr.getClass().getMethod("getName").invoke(addr);
                Object observer = Proxy.newProxyInstance(
                    jtapiLoader,
                    new Class[]{observerClass},
                    (proxy, method, args) -> {
                        if ("addressChangedEvent".equals(method.getName()) && args != null) {
                            handleAddressEvents(args[0], deviceName, dn);
                        }
                        return null;
                    }
                );
                addr.getClass().getMethod("addObserver", observerClass).invoke(addr, observer);
            }
        } catch (Exception e) {
            Main.emit("warning", new JSONObject()
                .put("message", "AddressObserver failed for " + deviceName + ": " + e.getMessage()));
        }
    }

    private void registerCallObserver(Object call, String callId) {
        try {
            Class<?> observerClass = jtapiLoader.loadClass("javax.telephony.CallObserver");
            Object observer = Proxy.newProxyInstance(
                jtapiLoader,
                new Class[]{observerClass},
                (proxy, method, args) -> {
                    if ("callChangedEvent".equals(method.getName()) && args != null) {
                        handleCallEvents(args[0], callId);
                    }
                    return null;
                }
            );
            call.getClass().getMethod("addObserver", observerClass).invoke(call, observer);
        } catch (Exception e) {
            Main.emit("warning", new JSONObject()
                .put("message", "CallObserver failed for " + callId + ": " + e.getMessage()));
        }
    }

    // ── Event handlers ────────────────────────────────────────────────────────

    private void handleProviderEvents(Object evArray) {
        try {
            if (!(evArray instanceof Object[])) return;
            for (Object ev : (Object[]) evArray) {
                int id = (int) ev.getClass().getMethod("getID").invoke(ev);
                // ProviderInService=105, ProviderOutOfService=106, ProviderShutdown=107
                String state = switch (id) {
                    case 105 -> "in_service";
                    case 106 -> "out_of_service";
                    case 107 -> "shutdown";
                    default -> "unknown_" + id;
                };
                Main.emit("provider_state", new JSONObject().put("state", state).put("event_id", id));
            }
        } catch (Exception e) {
            Main.emit("warning", new JSONObject().put("message", "providerEvent parse error: " + e.getMessage()));
        }
    }

    private void handleTerminalEvents(Object evArray, String deviceName) {
        try {
            if (!(evArray instanceof Object[])) return;
            for (Object ev : (Object[]) evArray) {
                int id = (int) ev.getClass().getMethod("getID").invoke(ev);
                // CiscoTermInServiceEv=400, CiscoTermOutOfServiceEv=401
                String state = (id == 400) ? "registered" : (id == 401) ? "unregistered" : "unknown_" + id;
                Main.emit("device_state", new JSONObject()
                    .put("device", deviceName)
                    .put("state", state)
                    .put("event_id", id));
            }
        } catch (Exception e) {
            Main.emit("warning", new JSONObject()
                .put("message", "terminalEvent parse error for " + deviceName + ": " + e.getMessage()));
        }
    }

    private void handleAddressEvents(Object evArray, String deviceName, String dn) {
        try {
            if (!(evArray instanceof Object[])) return;
            for (Object ev : (Object[]) evArray) {
                int id = (int) ev.getClass().getMethod("getID").invoke(ev);
                Main.emit("address_event", new JSONObject()
                    .put("device", deviceName)
                    .put("dn", dn)
                    .put("event_id", id));
            }
        } catch (Exception e) {
            Main.emit("warning", new JSONObject()
                .put("message", "addressEvent parse error: " + e.getMessage()));
        }
    }

    private void handleCallEvents(Object evArray, String callId) {
        try {
            if (!(evArray instanceof Object[])) return;
            for (Object ev : (Object[]) evArray) {
                int id = (int) ev.getClass().getMethod("getID").invoke(ev);
                // CallActive=101, CallInvalid=102, CallInProgress=104,
                // ConnAlerted=200, ConnConnected=201, ConnDisconnected=202,
                // ConnFailed=204, ConnNetworkAlerting=210, ConnNetworkReached=211
                String state = switch (id) {
                    case 101 -> "active";
                    case 102 -> "invalid";
                    case 104 -> "in_progress";
                    case 200 -> "alerting";
                    case 201 -> "connected";
                    case 202 -> "disconnected";
                    case 204 -> "failed";
                    default -> "event_" + id;
                };
                JSONObject payload = new JSONObject()
                    .put("call_id", callId)
                    .put("state", state)
                    .put("event_id", id);
                // Try to get cause code for disconnect
                if (id == 202 || id == 204) {
                    try {
                        int cause = (int) ev.getClass().getMethod("getCause").invoke(ev);
                        payload.put("cause_code", cause);
                        activeCalls.remove(callId);
                    } catch (Exception ignored) { /* noqa */ }
                }
                Main.emit("call_state", payload);
            }
        } catch (Exception e) {
            Main.emit("warning", new JSONObject()
                .put("message", "callEvent parse error for " + callId + ": " + e.getMessage()));
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private void requireProvider() throws Exception {
        if (provider == null) throw new Exception("Not connected — call connect first");
    }

    private Object requireCall(String callId) throws Exception {
        Object call = activeCalls.get(callId);
        if (call == null) throw new Exception("Call not found: " + callId);
        return call;
    }

    private Object getOrFetchTerminal(String deviceName) throws Exception {
        if (terminals.containsKey(deviceName)) return terminals.get(deviceName);
        Object t = provider.getClass().getMethod("getTerminal", String.class)
            .invoke(provider, deviceName);
        if (t == null) throw new Exception("Device not found: " + deviceName);
        terminals.put(deviceName, t);
        return t;
    }

    private Object getFirstAddress(Object terminal) throws Exception {
        Object[] addrs = (Object[]) terminal.getClass().getMethod("getAddresses").invoke(terminal);
        if (addrs == null || addrs.length == 0) throw new Exception("Terminal has no addresses");
        return addrs[0];
    }

    private Object getCallingTerminalFromCall(Object call) throws Exception {
        Object[] connections = (Object[]) call.getClass().getMethod("getConnections").invoke(call);
        if (connections == null || connections.length == 0)
            throw new Exception("No connections on call");
        Object[] tcs = (Object[]) connections[0].getClass()
            .getMethod("getTerminalConnections").invoke(connections[0]);
        if (tcs == null || tcs.length == 0) throw new Exception("No terminal connections");
        return tcs[0].getClass().getMethod("getTerminal").invoke(tcs[0]);
    }

    private void holdViaTerminalConnections(Object call) throws Exception {
        Object[] connections = (Object[]) call.getClass().getMethod("getConnections").invoke(call);
        if (connections == null) return;
        for (Object conn : connections) {
            Object[] tcs = (Object[]) conn.getClass().getMethod("getTerminalConnections").invoke(conn);
            if (tcs == null) continue;
            for (Object tc : tcs) {
                try { tc.getClass().getMethod("hold").invoke(tc); } catch (Exception ignored) { /* noqa */ }
            }
        }
    }

    private void resumeViaTerminalConnections(Object call) throws Exception {
        Object[] connections = (Object[]) call.getClass().getMethod("getConnections").invoke(call);
        if (connections == null) return;
        for (Object conn : connections) {
            Object[] tcs = (Object[]) conn.getClass().getMethod("getTerminalConnections").invoke(conn);
            if (tcs == null) continue;
            for (Object tc : tcs) {
                try { tc.getClass().getMethod("unhold").invoke(tc); } catch (Exception ignored) { /* noqa */ }
            }
        }
    }

    private Path findJtapiJar() {
        // JTAPI jar may be on the classpath (when -cp was used at launch)
        // or we can look for it in a well-known location
        String classpath = System.getProperty("java.class.path", "");
        for (String entry : classpath.split(":")) {
            if (entry.contains("jtapi")) {
                return Path.of(entry);
            }
        }
        return null;
    }
}

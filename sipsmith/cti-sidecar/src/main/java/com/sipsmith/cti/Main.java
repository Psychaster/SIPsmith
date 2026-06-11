package com.sipsmith.cti;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;

/**
 * Entry point for the SIPsmith CTI sidecar.
 *
 * Protocol: newline-delimited JSON on stdin/stdout.
 *   stdin  → {"method": "connect",  "params": {...}}
 *   stdout → {"event": "ready"}
 *   stdout → {"event": "device_state", "device": "SEP...", "state": "registered"}
 *   stdout → {"event": "call_state",   "call_id": "...",   "state": "connected"}
 *   stdout → {"event": "error",        "message": "..."}
 */
public class Main {

    static PrintStream out = System.out;

    public static void main(String[] args) throws Exception {
        // Redirect System.err so JTAPI internal logs don't pollute stdout
        System.setErr(new PrintStream(System.err) {
            @Override
            public void println(String x) { /* suppress */ }
        });

        JtapiManager manager = new JtapiManager();
        BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));

        emit("startup", new JSONObject().put("message", "CTI sidecar starting"));

        String line;
        while ((line = reader.readLine()) != null) {
            line = line.trim();
            if (line.isEmpty()) continue;
            try {
                JSONObject cmd = new JSONObject(line);
                String method = cmd.optString("method", "");
                JSONObject params = cmd.optJSONObject("params");
                if (params == null) params = new JSONObject();
                handleCommand(method, params, manager);
            } catch (Exception e) {
                emit("error", new JSONObject().put("message", "Parse error: " + e.getMessage()));
            }
        }

        manager.shutdown();
    }

    static void handleCommand(String method, JSONObject params, JtapiManager manager) {
        try {
            switch (method) {
                case "connect" -> {
                    String host = params.getString("host");
                    String user = params.getString("user");
                    String password = params.getString("password");
                    manager.connect(host, user, password);
                }
                case "disconnect" -> manager.disconnect();
                case "listDevices" -> {
                    JSONObject result = manager.listDevices();
                    emit("devices", result);
                }
                case "observeDevice" -> {
                    String device = params.getString("device");
                    manager.observeDevice(device);
                }
                case "makeCall" -> {
                    String callingDevice = params.getString("calling_device");
                    String calledDn = params.getString("called_dn");
                    String callId = manager.makeCall(callingDevice, calledDn);
                    emit("call_created", new JSONObject()
                        .put("call_id", callId)
                        .put("calling_device", callingDevice)
                        .put("called_dn", calledDn));
                }
                case "answer" -> {
                    String callId = params.getString("call_id");
                    manager.answer(callId);
                }
                case "hangup" -> {
                    String callId = params.getString("call_id");
                    manager.hangup(callId);
                }
                case "hold" -> {
                    String callId = params.getString("call_id");
                    manager.hold(callId);
                }
                case "resume" -> {
                    String callId = params.getString("call_id");
                    manager.resume(callId);
                }
                case "transfer" -> {
                    String callId = params.getString("call_id");
                    String destination = params.getString("destination");
                    manager.transfer(callId, destination);
                }
                case "sendDTMF" -> {
                    String callId = params.getString("call_id");
                    String digits = params.getString("digits");
                    manager.sendDTMF(callId, digits);
                }
                case "shutdown" -> {
                    manager.shutdown();
                    System.exit(0);
                }
                default -> emit("error", new JSONObject().put("message", "Unknown method: " + method));
            }
        } catch (Exception e) {
            emit("error", new JSONObject()
                .put("message", method + " failed: " + e.getMessage())
                .put("method", method));
        }
    }

    static void emit(String event, JSONObject payload) {
        JSONObject obj = new JSONObject();
        obj.put("event", event);
        payload.keys().forEachRemaining(k -> obj.put(k, payload.get(k)));
        synchronized (out) {
            out.println(obj);
            out.flush();
        }
    }
}

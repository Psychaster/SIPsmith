"""pjsua2 worker entry-point logic.

This module is **only** imported by the worker subprocess.  It must NOT be
imported by the control-plane (FastAPI) process.

If pjsua2 is unavailable (not compiled), this module prints an error event
to stdout and the caller should exit(1).
"""

from __future__ import annotations

import datetime
import json
import sys
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Attempt to import pjsua2
# ---------------------------------------------------------------------------

try:
    import pjsua2 as pj  # type: ignore[import-untyped]

    PJSUA2_AVAILABLE = True
except ImportError:
    PJSUA2_AVAILABLE = False
    pj = None  # type: ignore[assignment]

if not PJSUA2_AVAILABLE:
    _err = {"event": "error", "message": "pjsua2 not available"}
    print(json.dumps(_err), flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _estimate_mos(jitter_ms: float, loss_pct: float, rtt_ms: float) -> float:
    """Simplified E-model MOS estimate, clamped to [1.0, 4.5]."""
    # Delay impairment (jitter + one-way delay approximation)
    one_way_ms = rtt_ms / 2.0
    if one_way_ms < 150:
        id_val = 0.0
    elif one_way_ms < 400:
        id_val = 0.024 * one_way_ms + 0.11 * (one_way_ms - 177.3)
    else:
        id_val = 0.024 * one_way_ms + 0.11 * (one_way_ms - 177.3) + 6.5
    id_val += jitter_ms * 0.5

    # Equipment impairment (codec — conservative for PCMU)
    ie_val = 0.0

    # Frame loss impairment
    if_val = loss_pct * 2.5

    r_factor = 93.2 - id_val - ie_val - if_val
    r_factor = max(0.0, min(100.0, r_factor))

    if r_factor < 0:
        mos = 1.0
    elif r_factor > 100:
        mos = 4.5
    else:
        mos = 1.0 + 0.035 * r_factor + r_factor * (r_factor - 60) * (100 - r_factor) * 7e-6
    return round(max(1.0, min(4.5, mos)), 2)


# ---------------------------------------------------------------------------
# pjsua2 Call subclass
# ---------------------------------------------------------------------------


class _SipCall(pj.Call):  # type: ignore[misc]
    """pjsua2 Call subclass that forwards events to the event emitter."""

    def __init__(
        self,
        account: _SipAccount,
        call_id: int = pj.PJSUA_INVALID_ID,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(account, call_id)
        self.account = account
        # SIPsmith DB EndpointCall.id (set after DB row is created)
        self.db_call_id: int | None = None
        self._emit: Callable[[dict[str, Any]], None] = emit or (lambda _: None)
        self._pjsip_call_id: str = str(call_id)

    # pjsip_call_id is the string representation of the pjsua2 internal id
    @property
    def pjsip_call_id(self) -> str:
        return self._pjsip_call_id

    def onCallState(self, prm: pj.OnCallStateParam) -> None:  # noqa: N802
        info = self.getInfo()
        state_str = self._map_call_state(info.state)
        ts = _now_iso()

        if info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._emit(
                {
                    "event": "call_ended",
                    "pjsip_call_id": self._pjsip_call_id,
                    "cause_code": info.lastStatusCode,
                    "cause_label": info.lastReason,
                    "ts": ts,
                }
            )
        else:
            self._emit(
                {
                    "event": "call_state",
                    "pjsip_call_id": self._pjsip_call_id,
                    "state": state_str,
                    "ts": ts,
                }
            )

    def onCallMediaState(self, prm: pj.OnCallMediaStateParam) -> None:  # noqa: N802
        info = self.getInfo()
        audio_active = False
        video_active = False

        for mi in info.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                audio_active = True
                # Connect audio to null port (no real audio device in lab/worker)
                try:
                    aud_med = self.getAudioMedia(mi.index)
                    ep_inst = pj.Endpoint.instance()
                    aud_med.startTransmit(ep_inst.audDevManager().getPlaybackDevMedia())
                    ep_inst.audDevManager().getCaptureDevMedia().startTransmit(aud_med)
                except Exception:  # noqa: BLE001, S110
                    pass

            elif mi.type == pj.PJMEDIA_TYPE_VIDEO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                video_active = True

        self._emit(
            {
                "event": "media_state",
                "pjsip_call_id": self._pjsip_call_id,
                "audio_active": audio_active,
                "video_active": video_active,
                "ts": _now_iso(),
            }
        )

    def onCallSdpCreated(  # noqa: N802
        self,
        prm: pj.OnCallSdpCreatedParam,
    ) -> None:
        self._emit(
            {
                "event": "sip_msg",
                "pjsip_call_id": self._pjsip_call_id,
                "direction": "tx",
                "method": "INVITE",
                "status_code": None,
                "from_participant": getattr(self.account, "_ep_name", "endpoint"),
                "to_participant": "remote",
                "cseq": None,
                "raw_first_line": "INVITE (SDP created)",
                "ts": _now_iso(),
            }
        )

    def onStreamCreated(self, prm: pj.OnStreamCreatedParam) -> None:  # noqa: N802
        # Stream is ready; nothing special to do in a lab emulator
        pass

    def emit_rtp_stats(self) -> None:
        """Collect audio media stats and emit an rtp_stats event."""
        try:
            info = self.getInfo()
            for mi in info.media:
                if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    aud_med = self.getAudioMedia(mi.index)
                    stat = aud_med.getStatistics()
                    rx = stat.rxStat
                    tx = stat.txStat
                    jitter_ms = rx.jitterMsec.mean if hasattr(rx, "jitterMsec") else 0.0
                    rtt_ms = stat.rtt.mean * 1000 if hasattr(stat, "rtt") else 0.0
                    loss_pct = 0.0
                    if rx.pkt and (rx.pkt + rx.loss) > 0:
                        loss_pct = 100.0 * rx.loss / (rx.pkt + rx.loss)
                    mos = _estimate_mos(jitter_ms, loss_pct, rtt_ms)
                    self._emit(
                        {
                            "event": "rtp_stats",
                            "pjsip_call_id": self._pjsip_call_id,
                            "audio_tx_pkt": tx.pkt,
                            "audio_rx_pkt": rx.pkt,
                            "audio_tx_bytes": tx.bytes,
                            "audio_rx_bytes": rx.bytes,
                            "jitter_ms": round(jitter_ms, 2),
                            "rtt_ms": round(rtt_ms, 2),
                            "loss_pct": round(loss_pct, 3),
                            "mos": mos,
                            "ts": _now_iso(),
                        }
                    )
                    break
        except Exception:  # noqa: BLE001, S110
            pass

    @staticmethod
    def _map_call_state(state: int) -> str:
        mapping = {
            pj.PJSIP_INV_STATE_NULL: "null",
            pj.PJSIP_INV_STATE_CALLING: "calling",
            pj.PJSIP_INV_STATE_INCOMING: "incoming",
            pj.PJSIP_INV_STATE_EARLY: "early",
            pj.PJSIP_INV_STATE_CONNECTING: "connecting",
            pj.PJSIP_INV_STATE_CONFIRMED: "confirmed",
            pj.PJSIP_INV_STATE_DISCONNECTED: "disconnected",
        }
        return mapping.get(state, "unknown")


# ---------------------------------------------------------------------------
# pjsua2 Account subclass
# ---------------------------------------------------------------------------


class _SipAccount(pj.Account):  # type: ignore[misc]
    """pjsua2 Account subclass for one emulated SIP endpoint."""

    def __init__(
        self,
        ep_data: dict[str, Any],
        farm: EndpointFarm,
    ) -> None:
        super().__init__()
        self.ep_data = ep_data
        self._farm = farm
        self._ep_name: str = ep_data.get("name", "endpoint")

    def onRegState(self, prm: pj.OnRegStateParam) -> None:  # noqa: N802
        info = self.getInfo()
        state = "registered" if info.regIsActive else "unregistered"
        contact = info.regContactUri if hasattr(info, "regContactUri") else None
        self._farm.emit(
            {
                "event": "reg_state",
                "ep_id": self.ep_data["id"],
                "state": state,
                "expires": info.regExpiresSec if hasattr(info, "regExpiresSec") else None,
                "contact": contact,
                "error_detail": (
                    info.regReason if not info.regIsActive and hasattr(info, "regReason") else None
                ),
                "ts": _now_iso(),
            }
        )

    def onIncomingCall(self, prm: pj.OnIncomingCallParam) -> None:  # noqa: N802
        call = _SipCall(self, prm.callId, emit=self._farm.emit)
        call._pjsip_call_id = str(prm.callId)
        info = call.getInfo()
        remote_uri = info.remoteUri

        self._farm.calls[call.pjsip_call_id] = call
        self._farm.emit(
            {
                "event": "call_created",
                "ep_id": self.ep_data["id"],
                "pjsip_call_id": call.pjsip_call_id,
                "direction": "inbound",
                "remote_uri": remote_uri,
                "ts": _now_iso(),
            }
        )
        # Do NOT auto-answer; the scenario engine decides
        # (call stays in INCOMING state until a hangup/answer command)


# ---------------------------------------------------------------------------
# EndpointFarm
# ---------------------------------------------------------------------------


class EndpointFarm:
    """Manages all registered pjsua2 accounts and active calls."""

    def __init__(self, event_emitter: Callable[[dict[str, Any]], None]) -> None:
        # ep_id → account
        self.accounts: dict[int, _SipAccount] = {}
        # pjsip_call_id → call
        self.calls: dict[str, _SipCall] = {}
        self.emit = event_emitter

    def register(self, ep_data: dict[str, Any]) -> None:
        """Create (or replace) a pjsua2 Account for this endpoint."""
        ep_id: int = ep_data["id"]
        if ep_id in self.accounts:
            try:
                self.accounts[ep_id].setRegistration(False)
                self.accounts[ep_id].delete()
            except Exception:  # noqa: BLE001, S110
                pass
            del self.accounts[ep_id]

        acc = _SipAccount(ep_data, self)
        sip_user = ep_data.get("sip_user", "")
        sip_domain = ep_data.get("sip_domain", "")
        sip_password = ep_data.get("sip_password") or ""
        display_name = ep_data.get("display_name") or sip_user
        transport = ep_data.get("transport", "udp").lower()
        registrar_uri = ep_data.get("registrar_uri") or f"sip:{sip_domain}"

        transport_suffix = ""
        if transport == "tls":
            transport_suffix = ";transport=tls"
        elif transport == "tcp":
            transport_suffix = ";transport=tcp"

        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = f'"{display_name}" <sip:{sip_user}@{sip_domain}{transport_suffix}>'
        acc_cfg.regConfig.registrarUri = registrar_uri
        acc_cfg.regConfig.timeoutSec = 3600

        cred = pj.AuthCredInfo()
        cred.scheme = "digest"
        cred.realm = "*"
        cred.username = sip_user
        cred.dataType = 0
        cred.data = sip_password
        acc_cfg.sipConfig.authCreds.append(cred)

        if ep_data.get("use_srtp"):
            acc_cfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_MANDATORY
        else:
            acc_cfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_DISABLED

        try:
            acc.create(acc_cfg)
            self.accounts[ep_id] = acc
        except Exception as exc:  # noqa: BLE001
            self.emit(
                {
                    "event": "reg_state",
                    "ep_id": ep_id,
                    "state": "error",
                    "expires": None,
                    "contact": None,
                    "error_detail": str(exc),
                    "ts": _now_iso(),
                }
            )

    def unregister(self, ep_id: int) -> None:
        """Unregister and remove an account."""
        acc = self.accounts.pop(ep_id, None)
        if acc is not None:
            try:
                acc.setRegistration(False)
            except Exception:  # noqa: BLE001, S110
                pass

    def make_call(self, ep_id: int, dest_uri: str) -> str:
        """Originate an outbound call; return the pjsip_call_id string."""
        acc = self.accounts.get(ep_id)
        if acc is None:
            raise ValueError(f"Endpoint {ep_id} not registered")

        call = _SipCall(acc, emit=self.emit)
        call_prm = pj.CallOpParam(True)
        call.makeCall(dest_uri, call_prm)
        pjsip_id = str(call.getId())
        call._pjsip_call_id = pjsip_id
        self.calls[pjsip_id] = call
        return pjsip_id

    def hangup(self, pjsip_call_id: str) -> None:
        call = self._get_call(pjsip_call_id)
        prm = pj.CallOpParam()
        prm.statusCode = pj.PJSIP_SC_OK
        call.hangup(prm)

    def hold(self, pjsip_call_id: str) -> None:
        call = self._get_call(pjsip_call_id)
        prm = pj.CallOpParam()
        call.setHold(prm)

    def unhold(self, pjsip_call_id: str) -> None:
        call = self._get_call(pjsip_call_id)
        prm = pj.CallOpParam(True)
        call.reinvite(prm)

    def mute(self, pjsip_call_id: str, audio: bool, video: bool) -> None:
        call = self._get_call(pjsip_call_id)
        info = call.getInfo()
        for mi in info.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                try:
                    aud_med = call.getAudioMedia(mi.index)
                    # tx level 0 = muted, 1 = normal
                    aud_med.adjustTxLevel(0.0 if audio else 1.0)
                except Exception:  # noqa: BLE001, S110
                    pass
            elif mi.type == pj.PJMEDIA_TYPE_VIDEO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                try:
                    vid_med = call.getVideoMedia(mi.index)
                    if video:
                        vid_med.startTransmit(pj.AudioMedia())  # nop — stops video
                except Exception:  # noqa: BLE001, S110
                    pass

    def dtmf(self, pjsip_call_id: str, digits: str) -> None:
        call = self._get_call(pjsip_call_id)
        dtmf_prm = pj.CallSendDtmfParam()
        dtmf_prm.digits = digits
        call.sendDtmf(dtmf_prm)

    def _get_call(self, pjsip_call_id: str) -> _SipCall:
        call = self.calls.get(pjsip_call_id)
        if call is None:
            raise KeyError(f"Unknown call id: {pjsip_call_id}")
        return call

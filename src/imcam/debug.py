"""IMCAM_DEBUG tracing for the RTP relay: per-packet, NAL, STUN/DTLS and RTCP diagnostics.
Every function is a no-op unless IMCAM_DEBUG is set."""

import os
import struct


def enabled():
    return bool(os.environ.get("IMCAM_DEBUG"))


def rtp_in(data):
    if enabled() and len(data) > 1:
        print("rtp in: pt=%d len=%d" % (data[1] & 0x7F, len(data)))


def nal_in(data):
    """Log the H.265 NAL type of one RTP packet (fragmentation units only at their first fragment)."""
    if not enabled():
        return
    try:
        hl = 12 + (data[0] & 0x0F) * 4
        if data[0] & 0x10:
            hl += 4 + 4 * struct.unpack("!H", data[hl + 2 : hl + 4])[0]
        nt = (data[hl] >> 1) & 0x3F
        if nt == 49:
            nt = "fu%d" % (data[hl + 2] & 0x3F) if data[hl + 2] & 0x80 else None
        if nt is not None:
            print("nal in:", nt)
    except Exception as e:
        print("nal parse error: %r" % e)


def autocal(msg):
    if enabled():
        print("autocal:", msg)


def pli_sent():
    if enabled():
        print("PLI sent")


def offer_and_answer(offer_sdp, answer_sdp):
    if enabled():
        print("--- offer\n%s\n--- answer\n%s" % (offer_sdp, answer_sdp))


def install_transport_tracing():
    """Count datagrams by kind at the ICE layer and log incoming RTCP packet types."""
    if not enabled():
        return
    import atexit
    import collections
    from aioice.ice import StunProtocol
    from aiortc.rtcdtlstransport import RTCDtlsTransport

    cnt = collections.Counter()
    orig_rx = StunProtocol.datagram_received

    def rx(self, data, addr):
        if isinstance(data, bytes) and data:
            k = (
                "stun"
                if data[0] < 4
                else "dtls"
                if 20 <= data[0] < 64
                else "rtp/rtcp"
                if data[0] >= 128
                else "other"
            )
            cnt["rx %s from %s" % (k, addr[0])] += 1
        orig_rx(self, data, addr)

    StunProtocol.datagram_received = rx
    atexit.register(lambda: print("datagram counts:", dict(cnt)))

    orig_rtcp = RTCDtlsTransport._handle_rtcp_data

    async def handle_rtcp(self, data):
        from aiortc.rtp import RtcpPacket

        try:
            print("rtcp in: %s" % [type(x).__name__ for x in RtcpPacket.parse(data)])
        except ValueError:
            print("rtcp in: unparsable len=%d" % len(data))
        await orig_rtcp(self, data)

    RTCDtlsTransport._handle_rtcp_data = handle_rtcp


async def dtls_stats(pc):
    if not enabled():
        return
    for st in (await pc.getStats()).values():
        if st.type == "transport":
            print("dtls stats: sent=%d recv=%d" % (st.packetsSent, st.packetsReceived))

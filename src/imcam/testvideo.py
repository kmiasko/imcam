"""Test video for the call: the device only streams after the viewer sends video of its own, so we encode a moving
pattern with libx265 and write its RTP through the aiortc sender's DTLS/SRTP transport."""

import asyncio
import random
import time
from fractions import Fraction

from . import relay


def split_annexb(data):
    """NAL units (without start codes) of an Annex-B byte stream."""
    nals, i, n = [], 0, len(data)
    starts = []
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            starts.append(i + 3)
            i += 3
        else:
            i += 1
    for k, st in enumerate(starts):
        end = starts[k + 1] - 3 if k + 1 < len(starts) else n
        while end > st and data[end - 1] == 0:  # trailing_zero / 4-byte start code
            end -= 1
        nals.append(data[st:end])
    return nals


def h265_packetize(nals, mtu=1200):
    """RFC 7798 payloads for one access unit: single NAL unit packets, and fragmentation
    units (type 49) for NALs larger than mtu. Returns [(payload, is_last_of_AU)]."""
    out = []
    for ni, nal in enumerate(nals):
        last_nal = ni == len(nals) - 1
        if len(nal) <= mtu:
            out.append((bytes(nal), last_nal))
            continue
        hdr = bytes([(nal[0] & 0x81) | (49 << 1), nal[1]])
        ntype, body = (nal[0] >> 1) & 0x3F, nal[2:]
        step = mtu - 3
        for off in range(0, len(body), step):
            chunk = body[off : off + step]
            end = off + step >= len(body)
            fu = (0x80 if off == 0 else 0) | (0x40 if end else 0) | ntype
            out.append((hdr + bytes([fu]) + chunk, last_nal and end))
    return out


async def send_test_video(sender, w=640, h=360, fps=15):
    """The device only streams after the viewer sends video of its own. Encode a moving
    test pattern with libx265 and write its RTP through the sender's DTLS/SRTP transport."""
    import av
    from aiortc.rtp import RtpPacket

    dtls = sender.transport
    enc = av.CodecContext.create("libx265", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    enc.framerate = fps
    enc.time_base = Fraction(1, 90000)
    enc.gop_size = fps
    enc.max_b_frames = 0
    enc.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "x265-params": "repeat-headers=1:annexb=1:keyint=%d:bframes=0:log-level=none"
        % fps,
    }
    seq, ts0, i = random.randrange(1 << 16), random.randrange(1 << 32), 0
    nxt = time.monotonic()
    while True:
        frame = av.VideoFrame(w, h, "yuv420p")
        row = bytes((x + i * 8) % 256 for x in range(w))
        frame.planes[0].update(b"".join(row for _ in range(h)))
        frame.planes[1].update(bytes([(128 + i * 4) % 256]) * (w * h // 4))
        frame.planes[2].update(bytes([(128 - i * 4) % 256]) * (w * h // 4))
        frame.pts = i * (90000 // fps)
        i += 1
        for pkt in enc.encode(frame):
            for payload, marker in h265_packetize(split_annexb(bytes(pkt))):
                rtp = RtpPacket(
                    payload_type=relay.H265_PT,
                    sequence_number=seq,
                    timestamp=(ts0 + pkt.pts) & 0xFFFFFFFF,
                    ssrc=sender._ssrc,
                    payload=payload,
                )
                rtp.marker = int(marker)
                seq = (seq + 1) & 0xFFFF
                await dtls._send_rtp(rtp.serialize())
        nxt += 1.0 / fps
        await asyncio.sleep(max(0, nxt - time.monotonic()))

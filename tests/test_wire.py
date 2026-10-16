import json

from imcam import cli


def test_h265_packetize_splits_large_nal_into_fragmentation_units():
    big = bytes([0x26, 0x01]) + bytes(range(256)) * 20  # IDR_W_RADL
    small = bytes([0x40, 0x01, 1, 2, 3])
    pk = cli.h265_packetize([small, big], mtu=1200)
    assert pk[0] == (small, False)
    assert [m for _, m in pk] == [False] + [False] * (len(pk) - 2) + [True]
    fus = [p for p, _ in pk[1:]]
    assert all((p[0] >> 1) & 0x3F == 49 and p[2] & 0x3F == 19 for p in fus)
    assert fus[0][2] & 0x80 and fus[-1][2] & 0x40 and not fus[0][2] & 0x40
    rebuilt = bytes([(fus[0][0] & 0x81) | (19 << 1), fus[0][1]]) + b"".join(p[3:] for p in fus)
    assert rebuilt == big


def test_split_annexb_handles_three_and_four_byte_start_codes():
    big = bytes([0x26, 0x01]) + bytes(range(256)) * 20
    small = bytes([0x40, 0x01, 1, 2, 3])
    assert cli._split_annexb(b"\0\0\0\1" + small + b"\0\0\1" + big) == [small, big]


def test_frame_round_trip_across_length_encodings():
    heartbeat = json.dumps({"headers": {"id": "pub_heartbeat", "req_id": "1", "time": 1}}).encode()
    for payload in (heartbeat, b"x" * 300, b"y" * 70000):  # 7-bit, 16-bit and 64-bit lengths
        op, got, rest = cli.parse_frame(cli.frame_encode(0x1, payload), None)
        assert op == 0x1 and got == payload and not rest


def test_aes_ecb_decrypt_matches_fips197_vector():
    key = bytes(range(16))
    cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    assert cli.aes_ecb_decrypt(key, cipher) == bytes.fromhex("00112233445566778899aabbccddeeff")

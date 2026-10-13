import asyncio

import pytest

from rvcw.ble.session import RemoteControl, RemoteControlSessionError
from rvcw.protocol import (
    compose_ids_can_extended_wire_frame,
    parse_ids_can_wire_frame,
    tea_encrypt,
    RC_CYPHER,
)


class RecordingLink:
    """Minimal GatewayLink stand-in: records writes, answers session requests."""

    def __init__(self, responses: list[bytes]):
        self.written: list[bytes] = []
        self.local_host_address = 0x2D
        self._responses = list(responses)

    on_frame = None

    async def write_can_frame(self, frame: bytes) -> None:
        self.written.append(frame)
        wire = parse_ids_can_wire_frame(frame)
        if wire is None or wire.message_type != 0x80:
            return
        if wire.message_data not in (0x42, 0x43):
            return  # preflight/heartbeat/other requests get no canned answer
        if self._responses:
            response = self._responses.pop(0)
            wire_resp = parse_ids_can_wire_frame(response)
            if wire_resp is not None and self.on_frame is not None:
                self.on_frame(wire_resp)


def make_seed_response(seed: int, device: int = 0x2E) -> bytes:
    # RESPONSE (0x81) from device to us (0x2D), msg_data 0x42, payload [sid2][seed4BE]
    return compose_ids_can_extended_wire_frame(
        0x81, device, 0x2D, 0x42, bytes([0x00, 0x04]) + seed.to_bytes(4, "big")
    )


def make_key_ack(device: int = 0x2E) -> bytes:
    return compose_ids_can_extended_wire_frame(0x81, device, 0x2D, 0x43, b"\x00\x04")


def key_for(seed: int) -> int:
    return tea_encrypt(RC_CYPHER, seed)


async def test_ensure_opens_session():
    seed = 0x0BADF00D
    link = RecordingLink([make_seed_response(seed), make_key_ack()])
    rc = RemoteControl(link)  # type: ignore[arg-type]
    # wire the response dispatch the way the executor does (Task 11)
    link.on_frame = rc.handle_frame
    assert await rc.ensure(0x2E) is True
    assert rc.open and rc.target == 0x2E
    # seed request, then key request written with the TEA-encrypted seed
    kinds = [parse_ids_can_wire_frame(f) for f in link.written]
    seed_reqs = [w for w in kinds if w and w.message_type == 0x80 and w.message_data == 0x42]
    key_reqs = [w for w in kinds if w and w.message_type == 0x80 and w.message_data == 0x43]
    assert seed_reqs and key_reqs
    key_req = key_reqs[-1]
    assert key_req.payload[0:2] == b"\x00\x04"
    assert int.from_bytes(key_req.payload[2:6], "big") == key_for(seed)
    await rc.aclose()


async def test_ensure_seed_timeout_raises():
    link = RecordingLink([])  # no responses at all
    rc = RemoteControl(link)  # type: ignore[arg-type]
    with pytest.raises(RemoteControlSessionError):
        await rc.ensure(0x2E)
    assert not rc.open


async def test_status_response_for_seed_raises():
    # gateway answers the seed request with a single-byte status RESPONSE, 0x0B = BUSY
    status_frame = compose_ids_can_extended_wire_frame(0x81, 0x2E, 0x2D, 0x42, b"\x0B")
    link = RecordingLink([status_frame])
    rc = RemoteControl(link)  # type: ignore[arg-type]
    link.on_frame = rc.handle_frame
    with pytest.raises(RemoteControlSessionError, match="BUSY|0x0B"):
        await rc.ensure(0x2E)


async def test_aclose_resets_state_and_next_ensure_rebuilds():
    seed = 0x0BADF00D
    link = RecordingLink([make_seed_response(seed), make_key_ack(), make_seed_response(seed), make_key_ack()])
    rc = RemoteControl(link)  # type: ignore[arg-type]
    link.on_frame = rc.handle_frame
    assert await rc.ensure(0x2E) is True
    await rc.aclose()
    assert rc.open is False and rc.target is None
    # second open is a full rebuild, not the early-return path
    assert await rc.ensure(0x2E) is True
    assert rc.open is True
    await rc.aclose()


async def test_two_devices_keep_both_heartbeats_alive_until_aclose():
    """Combo/multi-device send: ensure(B) must NOT abandon A's keepalive.

    Regression for the single-heartbeat RemoteControl, where ensuring a second
    device cancelled the first device's heartbeat (0x44 writes stopped), so
    the first device's session timed out mid-send and its writes went
    unconfirmed.
    """
    a, b = 0x2E, 0x2F
    link = RecordingLink([
        make_seed_response(0x0BADF00D, a), make_key_ack(a),
        make_seed_response(0x05EED123, b), make_key_ack(b),
    ])
    rc = RemoteControl(link)  # type: ignore[arg-type]
    link.on_frame = rc.handle_frame
    assert await rc.ensure(a) is True
    # ensuring B does not cancel A's heartbeat task
    task_a = rc._heartbeats[a]
    assert not task_a.done()
    assert await rc.ensure(b) is True
    task_b = rc._heartbeats[b]
    assert not task_a.done() and not task_b.done()
    assert rc.open and rc.target == b  # most recent session

    await asyncio.sleep(1.3)  # ~2 heartbeat periods per device

    def heartbeat_targets(device: int) -> list[int]:
        frames = (parse_ids_can_wire_frame(f) for f in link.written)
        reqs = (w for w in frames if w and w.message_type == 0x80 and w.message_data == 0x44)
        return [w.target_address for w in reqs if w.target_address == device]

    # BOTH sessions are being kept alive with 0x44 heartbeats
    assert len(heartbeat_targets(a)) >= 2
    assert len(heartbeat_targets(b)) >= 2

    await rc.aclose()
    await asyncio.sleep(0.05)  # let the cancellations propagate
    # aclose cancels ALL heartbeats (not just the latest device's)
    assert task_a.cancelled() and task_b.cancelled()
    assert rc._heartbeats == {} and rc._sessions == set()


async def test_end_one_device_keeps_the_others_session():
    a, b = 0x2E, 0x2F
    link = RecordingLink([
        make_seed_response(0x0BADF00D, a), make_key_ack(a),
        make_seed_response(0x05EED123, b), make_key_ack(b),
        make_seed_response(0x0BADF00D, a), make_key_ack(a),
    ])
    rc = RemoteControl(link)  # type: ignore[arg-type]
    link.on_frame = rc.handle_frame
    await rc.ensure(a)
    await rc.ensure(b)
    await rc.end(a)  # end ONLY device A
    assert a not in rc._sessions and b in rc._sessions
    assert rc._heartbeats.get(a) is None  # A's heartbeat removed
    assert rc._heartbeats[b].done() is False  # B's task still alive
    assert rc.open and rc.target == b
    # 0x45 END was written to A only, not to B
    ends = [parse_ids_can_wire_frame(f) for f in link.written]
    end_reqs = [w for w in ends if w and w.message_type == 0x80 and w.message_data == 0x45]
    assert [w.target_address for w in end_reqs] == [a]
    task_a = rc._heartbeats.get(a)
    assert task_a is None  # A's heartbeat removed
    await rc.aclose()

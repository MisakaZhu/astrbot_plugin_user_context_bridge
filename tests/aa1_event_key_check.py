"""AA1：夹具事件标识唯一性回归（基线先行，2026-09-20）。

缺陷（Codex 报告 31 号 §4）：make_message_obj 以 id(abm) 作消息 ID，
对象释放后地址可复用，不同新消息可能得到相同 UMO+message_id，插件正确
的重复投递防护会把新测试消息误判为重复。

本套件检查：
  1. 同窗口连续新建并释放：event_key 全程唯一（旧 id() 方案下大量
     复用 → FAIL）；
  2. 同用户跨窗口连续新建并释放：event_key 全程唯一；
  3. event_key 公式与 bridge.event_key_for 一致；
  4. 显式 message_id 覆盖：两个不同对象同显式 ID → event_key 相同
     （真正的重复投递语义仍可表达）。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/aa1_event_key_check.py
"""

from __future__ import annotations

import gc
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.fakes import FakeEvent  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")


def key_of(ev) -> str:
    from uctx_bridge.bridge import ContextBridge

    return ContextBridge.event_key_for(ev)


def main() -> int:
    from uctx_bridge.bridge import ContextBridge

    # 1) 同窗口连续新建+释放：200 轮，event_key 必须两两不同
    keys: list[str] = []
    for i in range(200):
        ev = FakeEvent(sender_id="10001", group_id="700000001",
                       message_str=f"aa1-same-window-{i}")
        keys.append(key_of(ev))
        del ev
    gc.collect()
    unique = len(set(keys))
    check("AA1.same-window-keys-unique", unique == 200,
          f"unique={unique}/200 duplicates={200 - unique} "
          f"sample={keys[:3]}")

    # 2) 同用户跨窗口连续新建+释放
    keys2: list[str] = []
    for i in range(200):
        group = f"7000000{i % 3 + 1}"
        ev = FakeEvent(sender_id="10001", group_id=group,
                       message_str=f"aa1-cross-window-{i}")
        keys2.append(key_of(ev))
        del ev
    gc.collect()
    unique2 = len(set(keys2))
    check("AA1.cross-window-keys-unique", unique2 == 200,
          f"unique={unique2}/200 duplicates={200 - unique2}")

    # 3) 公式一致性：与 bridge.event_key_for 相同（抽查一次显式比对）
    ev = FakeEvent(sender_id="10001", group_id="700000001",
                   message_str="aa1-formula")
    expected = f"{ev.unified_msg_origin}#{ev.message_obj.message_id}"
    check("AA1.key-formula-matches-bridge", key_of(ev) == expected,
          f"{key_of(ev)!r} != {expected!r}")

    # 4) 显式 message_id 覆盖：不同对象、同显式 ID → 同 event_key
    ev_a = FakeEvent(sender_id="10001", group_id="700000001",
                     message_str="dup-a", message_id="fixed-dup-id-001")
    ev_b = FakeEvent(sender_id="10001", group_id="700000001",
                     message_str="dup-b", message_id="fixed-dup-id-001")
    check("AA1.explicit-id-same-key",
          ev_a is not ev_b
          and ev_a.message_obj.message_id == "fixed-dup-id-001"
          and key_of(ev_a) == key_of(ev_b),
          f"a={key_of(ev_a)!r} b={key_of(ev_b)!r}")
    # 未显式指定的新事件不得撞显式 ID
    ev_c = FakeEvent(sender_id="10001", group_id="700000001",
                     message_str="fresh")
    check("AA1.auto-id-distinct-from-explicit",
          key_of(ev_c) != key_of(ev_a), "")

    print(f"\n=== AA1 事件标识唯一性：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

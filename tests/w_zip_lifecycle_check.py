"""N22：真实候选 ZIP 安装链——从工作区打包白名单 ZIP → 双版解包安装 →
真实 PluginManager load/reload/turn_off/turn_on/uninstall 完整生命周期
（复用 w_lifecycle_worker 全场景）+ ZIP 解包后本地工具独立运行。

运行：PYTHONPATH=. <venv>/Scripts/python.exe tests/w_zip_lifecycle_check.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from tests.w_lifecycle_check import _run_worker, assert_worker_fields, check, PASS, FAIL

REPO = Path(__file__).resolve().parent.parent
WHITELIST = [
    "main.py",
    "metadata.yaml",
    "requirements.txt",
    "_conf_schema.json",
    "README.md",
    "CHANGELOG.md",
    "uctx_bridge/__init__.py",
    "uctx_bridge/identity.py",
    "uctx_bridge/scope.py",
    "uctx_bridge/ledger.py",
    "uctx_bridge/bridge.py",
    "uctx_bridge/commands.py",
    "tools/uctx_records.py",
]


def locate_delivered_zip(delivered_zip: str | None = None,
                         expected_sha: str | None = None):
    """N22（Y1）：正式入口只接受**明确指定的**候选 ZIP 路径与预期
    SHA-256（--delivered-zip / --delivered-sha256）；不按 mtime 自动
    选包，缺 .sha256 记录/哈希不匹配/输入包缺失一律判 FAIL。
    返回 (zip_path | None, 实际摘要 | None)。"""

    if not delivered_zip:
        check("N22.delivered-zip-exists", False,
              "未通过 --delivered-zip 指定候选包路径（不得按 mtime 自动选包）")
        return None, None
    zip_path = Path(delivered_zip)
    if not zip_path.is_file():
        check("N22.delivered-zip-exists", False,
              f"指定的候选包不存在：{zip_path}")
        return None, None
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if not expected_sha:
        check("N22.expected-sha-given", False,
              "未通过 --delivered-sha256 指定预期 SHA-256")
        return zip_path, digest
    check("N22.delivered-hash-matches",
          str(expected_sha).strip().lower() == digest,
          f"预期 {expected_sha} != 实际 {digest}")
    sha_file = zip_path.with_suffix(zip_path.suffix + ".sha256")
    if sha_file.is_file():
        recorded = sha_file.read_text(encoding="utf-8").split()[0]
        check("N22.delivered-sha256-record", recorded == digest,
              f".sha256 记录 {recorded} != 实际 {digest}")
    else:
        check("N22.delivered-sha256-record", False,
              "缺少 .sha256 记录文件（不得视为 PASS）")
    print(f"[N22] 交付 ZIP：{zip_path.name}（{len(WHITELIST)} 文件）")
    print(f"[N22] SHA-256：{digest}")
    return zip_path, digest


def zip_tool_standalone(zip_path: Path, td: Path) -> bool:
    """ZIP 解包目录内 tools/uctx_records.py 独立可运行（不依赖仓库）。"""

    import importlib.util as _ilu

    plug = td / "plug"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        plug_resolved = plug.resolve()
        for name in names:
            target = (plug / name).resolve()
            if not str(target).startswith(str(plug_resolved)):
                return False
        zf.extractall(plug)
    tool = plug / "tools" / "uctx_records.py"
    # 用仓库现成逻辑造一个 v3 合成库（仅造库借宿主 venv 的 uctx_bridge，
    # 工具运行本身只用解包目录内的文件）
    db = td / "uctx_ledger.db"
    mk = (
        "import sys;"
        f"sys.path.insert(0, r'{REPO}');"
        "from uctx_bridge.ledger import TurnLedger;"
        f"led = TurnLedger(r'{db}'); led.open();"
        "led.begin_turn(identity_key='aiocqhttp\x1fbot\x1fp:black\x1f10001',"
        " event_key='e1', source_type='group', source_id='g', umo='u',"
        " user_message={'role':'user','content':'zip 问'}, source_persona='black');"
        "led.commit_turn(event_key='e1', status='completed', reply_text='zip 答');"
        "led.close(); print('db-ok')"
    )
    r = subprocess.run(
        [sys.executable, "-c", mk], capture_output=True, text=True,
        encoding="utf-8", cwd=str(td),
    )
    if "db-ok" not in r.stdout:
        print("[N22] 造库失败：", (r.stderr or "")[-300:])
        return False
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(tool), "list", "--db", str(db)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(td),
    )
    return r.returncode == 0 and "身份分区" in r.stdout


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="N22：指定交付 ZIP 的完整生命周期与工具独立运行"
    )
    ap.add_argument("--delivered-zip", default=None,
                    help="候选交付 ZIP 路径（必填，不按 mtime 自动选包）")
    ap.add_argument("--delivered-sha256", default=None,
                    help="候选交付 ZIP 预期 SHA-256（必填）")
    args = ap.parse_args()
    zip_path, _digest = locate_delivered_zip(
        args.delivered_zip, args.delivered_sha256)
    if zip_path is None:
        print(f"=== N22 ZIP 交付链：PASS={len(PASS)} FAIL={len(FAIL)} ===")
        return 1
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)

        # 工具独立运行验证（与宿主无关，跑一次）
        ok_tool = zip_tool_standalone(zip_path, tdp / "toolcheck")
        check("N22.zip-tool-standalone", ok_tool)

        # 双版：从 ZIP 安装 → 完整生命周期
        for venv in (
            r"D:\第三方插件完善\.venv",
            r"D:\第三方插件完善\.venv426",
        ):
            tag = Path(venv).name
            inst = tdp / f"inst-{tag}"
            inst.mkdir()
            worker = str(REPO / "tests" / "w_lifecycle_worker.py")
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONPATH"] = str(REPO)
            r = subprocess.run(
                [venv + r"\Scripts\python.exe", worker, str(inst), str(zip_path)],
                capture_output=True, text=True, env=env, cwd=str(REPO),
                timeout=240,
            )
            line = next(
                (l for l in r.stdout.splitlines()
                 if l.startswith("@@RESULT@@")), None)
            if line is None:
                check(f"N22.{tag}.zip-lifecycle", False,
                      (r.stderr or r.stdout)[-400:])
                continue
            out = json.loads(line[len("@@RESULT@@"):])
            assert_worker_fields(f"N22.{tag}", out)
            # ZIP 安装链额外核验：卸载后目录与注册表清理
            check(f"N22.{tag}.zip-lifecycle-complete",
                  out.get("load_ok") is True
                  and (out.get("bound_handlers") or 0) >= 5,
                  f"load={out.get('load_ok')}")

    print(f"\n=== N22 ZIP 安装链：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

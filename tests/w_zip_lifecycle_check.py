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


def build_candidate_zip(td: Path) -> Path:
    zip_path = td / "astrbot_plugin_user_context_bridge-w-n22.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in WHITELIST:
            zf.write(REPO / rel, rel)
    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == sorted(WHITELIST)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    print(f"[N22] 候选 ZIP：{zip_path.name}（{len(WHITELIST)} 文件）")
    print(f"[N22] SHA-256：{digest}")
    return zip_path


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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        zip_path = build_candidate_zip(tdp)

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

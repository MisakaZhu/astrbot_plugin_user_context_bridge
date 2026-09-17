"""P6 打包安装与隐私边界验证（MIS-142）：A17 / A18。

- A17：宿主真实校验器加载 metadata.yaml 与 _conf_schema.json；
  宿主 venv 内可导入插件入口（@register 装饰器执行注册）；
  发布 ZIP 清单与白名单一致（不夹带忽略文件）。
- A18：Git 跟踪清单不含运行数据库、日志、交付包、虚拟环境、缓存、
  真实配置、密钥形态文件。

运行：在仓库根目录 PYTHONPATH=. <venv>/Scripts/python.exe tests/p6_packaging_check.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"[PASS] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


# 发布 ZIP 白名单（与 P7 打包脚本保持一致）
ZIP_WHITELIST = [
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
]

FORBIDDEN_PATTERNS = (
    ".db", ".db-wal", ".db-shm", ".sqlite", ".log", ".zip", ".env",
    "__pycache__", ".venv", "node_modules", ".git/",
)


def scenario_host_loaders() -> None:
    """A17：宿主真实校验器。"""
    from astrbot.core.star.star_manager import PluginManager

    metadata = PluginManager._load_plugin_metadata(str(REPO_ROOT))
    check(
        "A17.metadata-valid",
        metadata is not None
        and metadata.name == "astrbot_plugin_user_context_bridge"
        and metadata.author == "Ewnscat-ya"
        and "aiocqhttp" in (metadata.support_platforms or []),
        f"metadata={metadata}",
    )
    # 4.28 起才有 _load_plugin_config_schema；4.26 退回 json 解析校验
    if hasattr(PluginManager, "_load_plugin_config_schema"):
        schema = PluginManager._load_plugin_config_schema(
            str(REPO_ROOT / "_conf_schema.json")
        )
    else:
        schema = json.loads(
            (REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
    check(
        "A17.config-schema-valid",
        isinstance(schema, dict) and "enabled" in schema and "shared_groups" in schema,
        f"schema={type(schema)}",
    )


def scenario_module_import() -> None:
    """A17（R1）：按宿主 data.plugins.<name>.main 真实路径导入。

    从当前源码构造插件目录（与交付 ZIP 同内容），只把实例根目录加入
    sys.path——不允许注入插件根目录掩盖包内导入问题。
    """
    import shutil as _shutil

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        inst = Path(td)
        plug_dir = inst / "data" / "plugins" / "astrbot_plugin_user_context_bridge"
        plug_dir.mkdir(parents=True)
        for rel in ZIP_WHITELIST:
            src = REPO_ROOT / rel
            dst = plug_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                _shutil.copy2(src, dst)
            else:
                _shutil.copytree(src, dst, dirs_exist_ok=True)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, r'%s');"
                "import importlib;"
                "m = importlib.import_module("
                "    'data.plugins.astrbot_plugin_user_context_bridge.main');"
                "assert hasattr(m, 'UserContextBridgePlugin');"
                "print('import-ok')" % str(inst),
            ],
            capture_output=True,
            text=True,
            cwd=str(inst),
        )
        check(
            "A17.module-importable",
            result.returncode == 0 and "import-ok" in result.stdout,
            (result.stderr or result.stdout)[-300:],
        )


def scenario_git_manifest() -> None:
    """A18：Git 跟踪清单审计。"""
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    files = [f for f in out.stdout.splitlines() if f.strip()]
    check("A18.git-has-files", len(files) > 0)
    bad = [
        f
        for f in files
        if any(pat in f for pat in FORBIDDEN_PATTERNS)
        or f.endswith((".db", ".log", ".zip", ".pyc", ".env"))
    ]
    check(
        "A18.git-manifest-clean",
        not bad,
        f"违规文件：{bad}",
    )
    required = [
        "main.py",
        "metadata.yaml",
        "requirements.txt",
        "_conf_schema.json",
        "README.md",
        "CHANGELOG.md",
        "HANDOFF.md",
        "docs/PLAN.md",
        "docs/STATUS.md",
        "docs/ADR.md",
        "docs/COMPATIBILITY.md",
        "docs/ACCEPTANCE.md",
        ".gitignore",
    ]
    missing = [r for r in required if r not in files]
    check(
        "A18.git-required-files",
        not missing,
        f"缺失：{missing}",
    )


def scenario_zip_manifest() -> None:
    """A17/A18：发布 ZIP 白名单打包与清单核对（打进临时文件，不入库）。"""
    tmp_zip = REPO_ROOT / "local_evidence" / "_p6_manifest_probe.zip"
    tmp_zip.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in ZIP_WHITELIST:
                path = REPO_ROOT / rel
                if not path.exists():
                    check("A17.zip-source-exists", False, f"缺 {rel}")
                    return
                zf.write(path, rel)
        with zipfile.ZipFile(tmp_zip) as zf:
            names = sorted(zf.namelist())
        check(
            "A17.zip-manifest",
            names == sorted(ZIP_WHITELIST),
            f"names={names}",
        )
        bad = [n for n in names if any(p in n for p in FORBIDDEN_PATTERNS)]
        check("A18.zip-manifest-clean", not bad, f"违规：{bad}")
    finally:
        if tmp_zip.exists():
            tmp_zip.unlink()
        try:
            tmp_zip.parent.rmdir()
        except OSError:
            pass


def main() -> int:
    scenario_host_loaders()
    scenario_module_import()
    scenario_git_manifest()
    scenario_zip_manifest()
    print(f"\n=== P6 打包/隐私验证：PASS={len(PASS)} FAIL={len(FAIL)} ===")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

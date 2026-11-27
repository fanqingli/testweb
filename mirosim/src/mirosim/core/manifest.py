"""运行清单：记下一次运行用了什么输入、人物、模型和提示词，并把它们锁在输出目录上。

同一个输出目录里，这些东西变了就拒绝续跑或回放——否则旧答案会和新输入算出的权重、
问卷混在一起。预算不在锁里：因预算中断后可以调高预算接着跑。代码版本只记录，不锁。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mirosim import NOTICE

LOCKED = {"inputs": "输入文件或人物", "prompts": "提示词模板", "models": "模型或其参数"}


class ManifestMismatch(RuntimeError):
    """输出目录里已有的运行，输入与这一次不同。"""


def sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha_json(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def code_version(root: Path) -> str:
    """当前提交；src、prompts、config、schemas 有未提交的改动时加 +dirty。不在 git 仓库里时为 unknown。"""
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if not commit:
        return "unknown"
    return commit + ("+dirty" if git("status", "--porcelain", "--", "src", "prompts", "config", "schemas") else "")


def population_lock(personas: list[dict]) -> list[dict]:
    """抽中的人物：编号、版本、内容 hash。人物卡被原地改过（没有升版本）也能看出来。"""
    return [{"persona_id": p["persona_id"], "version": p["version"], "hash": sha_json(p)} for p in personas]


def locked_sections(*, experiment: dict, root: Path, stimulus_path: Path, frame_path: Path, lock: list[dict],
                    config_paths: dict[str, Path], prompt_ids: list[str], gateway) -> dict:
    """清单里被锁定的三段。一次运行只算一次（开头），之后写清单都用这一份：
    运行期间有人改了输入文件，清单记的仍是开头读到的那一版。"""
    backends = getattr(gateway, "backends", None)
    if backends is not None:
        models = [
            {
                "member": member,
                "provider": getattr(backends[member], "provider", ""),
                "model_id": backends[member].model_id,
                "params": gateway.params_by[member],
                "seed_supported": bool(getattr(backends[member], "supports_seed", False)),
            }
            for member in sorted(backends)
        ]
    else:
        backend = gateway.backend
        models = [{"tier": experiment["models"]["persona_tier"], "provider": getattr(backend, "provider", ""),
                   "model_id": backend.model_id, "params": gateway.params,
                   "seed_supported": bool(getattr(backend, "supports_seed", False))}]
    return {
        "inputs": {
            # 预算不算实验内容：因预算中断后调高预算接着跑，不应被当成换了实验
            "experiment_hash": sha_json({k: v for k, v in experiment.items() if k != "budget"}),
            "stimulus_hash": sha_file(stimulus_path),
            "frame_hash": sha_file(frame_path),
            "population_lock_hash": sha_json(lock),
            "config_hashes": {name: sha_file(path) for name, path in sorted(config_paths.items())},
        },
        "models": models,
        "prompts": [{"id": name, "hash": sha_file(root / "prompts" / f"{name}.md")} for name in sorted(prompt_ids)],
    }


def build(*, locked: dict, experiment: dict, root: Path, gateway, statuses: dict[str, tuple[str, int, str]],
          created_at: str) -> dict:
    recording = Path(gateway.recording)
    return {
        **locked,
        "experiment_id": experiment["experiment_id"],
        "created_at": created_at,
        "code": {"git_commit": code_version(root)},
        "matrix": [{"run_id": run_id, "variant_id": vid, "replicate": replicate, "model_family": "fam1", "status": status}
                   for run_id, (vid, replicate, status) in statuses.items()],
        # 每一格都跑完，录制才是完整的；一致率尚未计算
        "randomness": {"seed": experiment["design"]["seed"], "replayable": all(s[2] == "done" for s in statuses.values()),
                       "rerun_stability": None},
        "recording": {"calls": gateway.recorded_calls, "hash": sha_file(recording) if recording.exists() else ""},
        "usage": dict(gateway.usage),
        "validity_level": "L0",  # 最低一级；骨架没有做任何效度认定
        "notice": NOTICE,
    }


def check_lock(path: Path, new: dict) -> str | None:
    """目录里已有清单时比对锁定的部分；不一致就拒绝。返回已有清单的 created_at（没有清单时为 None）。"""
    if not Path(path).exists():
        return None
    old = json.loads(Path(path).read_text(encoding="utf-8"))
    changed = []
    for section, label in LOCKED.items():
        if old.get(section) == new[section]:
            continue
        a, b = old.get(section), new[section]
        if isinstance(a, dict) and isinstance(b, dict):
            flat_a, flat_b = {**a, **a.get("config_hashes", {})}, {**b, **b.get("config_hashes", {})}
            names = sorted(k for k in flat_a.keys() | flat_b.keys() if k != "config_hashes" and flat_a.get(k) != flat_b.get(k))
            changed.append(f"{label}（{'、'.join(names)}）")
        else:
            changed.append(label)
    if changed:
        raise ManifestMismatch(
            f"{Path(path).parent} 里已有一次运行，它的{'；'.join(changed)}与这一次不同。"
            "同一个目录只放同一套输入的结果，请换一个输出目录。")
    return old.get("created_at")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

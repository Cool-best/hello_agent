# -*- coding: utf-8 -*-
"""声明式沙箱镜像构建(幂等)。

扫描项目 `SKILLS_DIR` 内各 skill 的有效沙箱依赖声明(frontmatter
`sandbox_python_deps` ∪ 宿主覆盖表 `sandbox/deps_overlay.yaml`,
外部 skill 源文件不改,依赖登记走 `python -m host.capabilities.skill_registry onboard`),
按排序后依赖集的 sha256 派生内容寻址镜像 tag(`agent-skill-auto:<hash>`),
本地缺镜像才 `docker build`(Dockerfile 经 stdin 传入,不落临时文件,层缓存,
重复跑秒级)。依赖集相同的多个 skill 自动共享同一镜像。

用法:
    python -m sandbox.build
"""
import subprocess
import sys
from pathlib import Path

from host import config
from host.capabilities import skill_registry


def _dockerfile(deps: list[str]) -> str:
    packages = " ".join(f'"{d}"' for d in deps)
    return (
        f"FROM {config.SKILL_SCRIPT_BASE_IMAGE}\n"
        f"RUN pip install --no-cache-dir -i {config.SKILL_SCRIPT_PIP_INDEX} {packages}\n"
    )


def _build_image(tag: str, deps: list[str]) -> bool:
    result = subprocess.run(
        ["docker", "build", "-t", tag, "-f", "-", "."],
        input=_dockerfile(deps),
        text=True,
        cwd=Path(__file__).resolve().parent,
    )
    return result.returncode == 0


def main() -> int:
    groups: dict[tuple, list[str]] = {}
    for skill_dir in skill_registry._scan_skill_dirs(Path(config.SKILLS_DIR)):
        deps = skill_registry.effective_sandbox_deps(
            skill_dir.name, skill_registry.read_skill_frontmatter(skill_dir)
        )
        if deps:
            groups.setdefault(tuple(deps), []).append(skill_dir.name)
    if not groups:
        print("没有 skill 声明 sandbox_python_deps,无需构建")
        return 0
    failed = 0
    for deps_key, names in sorted(groups.items()):
        deps = list(deps_key)
        tag = skill_registry.derived_sandbox_image(deps)
        if skill_registry.local_image_exists(tag):
            print(f"[跳过] {tag} 本地已存在 (skills: {', '.join(names)})")
            continue
        print(f"[构建] {tag} <- {deps} (skills: {', '.join(names)})")
        if _build_image(tag, deps):
            skill_registry._image_exists_cache[tag] = True
            print(f"[完成] {tag}")
        else:
            failed += 1
            print(f"[失败] {tag}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

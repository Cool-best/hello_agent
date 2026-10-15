# -*- coding: utf-8 -*-
"""Skills 渐进式披露的来源扫描与安装登记。

两级来源:项目内置 `SKILLS_DIR`(全局)+ 会话 workspace 内 `WORKSPACE_SKILLS_DIR`
子目录(按会话)。两级均每轮生成前增量扫描安装(项目级启动时也装一次);
同名冲突时项目级优先。安装按内容哈希幂等(同内容重复 install 得同一
revision_ref,只是 parse+hash 开销),返回的 revision_ref 列表即
`agent.use_skills(refs, ...)` 的选择器。
"""
import ast
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml
from agently.base import skill_library
from agently.builtins.plugins.ActionExecutor import CodeExecutionActionExecutor
from agently.builtins.plugins.CodeRuntimeAdapter._base import BuiltinCodeRuntimeAdapter
from agently.builtins.plugins import CodeRuntimeAdapter as _code_runtime_adapters
from agently.types.data import (
    CodeExecutionRequest,
    CodeExecutionStep,
    CodeExecutionToolchainRequirement,
    required_code_execution_isolation,
)

from .. import config

logger = logging.getLogger(__name__)


class _ShellCodeRuntimeAdapter(BuiltinCodeRuntimeAdapter):
    """shell(.sh)脚本运行时适配器:容器内 bash 直跑,无依赖安装步骤。

    框架内置适配器只有 python/nodejs/go/cpp——"shell" 被刻意留给 broad shell
    Action(enable_shell 的 allowlist 命令执行),skill 脚本链路没有 shell
    适配器,.sh 绑不了(2026-10-12 html-ppt 的 new-deck.sh 实证)。这里补一个
    窄适配器并追加进框架注册表(与 chat_pipeline 头部补丁同款思路;
    升级 agently 需复核 _ADAPTER_TYPES 结构)。
    base 镜像 python:3.12-slim 自带 bash 5.2(实测),无需改装镜像。
    """

    name = "HostShellCodeRuntimeAdapter"
    language_id = "shell"
    aliases = ("sh", "bash")
    primary_filename = "main.sh"
    source_suffixes = (".sh",)
    dependency_manifests = ()

    def toolchain_requirements(self):
        # 仅信息登记(随 bundle 携带,不进运行门控);我们的镜像链全部基于
        # python:3.12-slim,bash 必在;真缺失时运行期报 "bash: not found" 也足够明确
        return (CodeExecutionToolchainRequirement(tool="bash"),)

    def prepare(self, request, policy):
        files, entrypoint = self._normalize_request(request)
        # overlay 声明的容器 env(经 executor 注入 provenance)落到 run step
        # → docker run -e(dashi-ppt: DASHI_PPT_PROJECT_ROOT 指向镜像内烘焙工程)
        env: dict[str, str] = {}
        provenance = getattr(request, "provenance", None)
        host_env = provenance.get("host_env") if isinstance(provenance, Mapping) else None
        if isinstance(host_env, Mapping):
            env = {str(k): str(v) for k, v in host_env.items()}
        return self._bundle(
            request=request,
            files=files,
            entrypoint=entrypoint,
            build_steps=[],
            run_step=CodeExecutionStep(
                argv=("bash", entrypoint, *request.args),
                cwd="source",
                role="run",
                env=env,
            ),
        )


if not any(
    getattr(adapter_type, "language_id", "") == "shell"
    for adapter_type in _code_runtime_adapters._ADAPTER_TYPES
):
    _code_runtime_adapters._ADAPTER_TYPES = (
        *_code_runtime_adapters._ADAPTER_TYPES,
        _ShellCodeRuntimeAdapter,
    )

_project_revision_refs: list[str] = []

_FRONTMATTER_RE = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SANDBOX_AUTO_IMAGE_PREFIX = "agent-skill-auto"
_image_exists_cache: dict[str, bool] = {}


def read_skill_frontmatter(skill_dir: Path) -> dict:
    """读取 skill 目录 SKILL.md 的 frontmatter(解析失败一律当空)。"""
    try:
        text = (Path(skill_dir) / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def sandbox_deps_from_frontmatter(frontmatter: dict) -> list[str]:
    """frontmatter 的 sandbox_python_deps 声明,排序去重后返回(空声明即 [])。"""
    raw = frontmatter.get("sandbox_python_deps")
    if not isinstance(raw, (list, tuple)):
        return []
    return sorted({str(item).strip() for item in raw if str(item).strip()})


# 宿主侧沙箱依赖覆盖表:外部 skill 的源文件(SKILL.md)宿主不改,第三方脚本
# 依赖由 `python -m host.capabilities.skill_registry onboard` 推导、人确认后写进本表(随项目入库)。
_DEPS_OVERLAY_PATH = Path(__file__).resolve().parents[2] / "sandbox" / "deps_overlay.yaml"


def _load_deps_overlay_full() -> dict[str, dict]:
    """读 overlay 原始结构 {skill: {"deps": [...], "ignore": [...], "image": str, "env": {...}}}。

    条目允许简写为纯列表(=deps)。ignore 记录"已确认不是 pip 包"的模块名
    (如外部 skill 自带的残缺内部 import),onboard 不再对其提案。
    image/env 为宿主级沙箱声明(外部 skill 源文件不改):image 覆盖派生镜像
    解析(如 dashi-ppt 的 Node 底座),env 注入容器 run step(如
    DASHI_PPT_PROJECT_ROOT 指向镜像内烘焙的工程目录)。
    缺失/解析失败按空表。
    """
    try:
        data = yaml.safe_load(_DEPS_OVERLAY_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except yaml.YAMLError:
        logger.warning("deps overlay 解析失败,按空表处理: %s", _DEPS_OVERLAY_PATH)
        return {}
    if not isinstance(data, dict):
        return {}

    def _strs(raw) -> list[str]:
        if not isinstance(raw, (list, tuple)):
            return []
        return sorted({str(item).strip() for item in raw if str(item).strip()})

    result: dict[str, dict] = {}
    for name, raw in data.items():
        if isinstance(raw, dict):
            env = raw.get("env")
            entry = {
                "deps": _strs(raw.get("deps")),
                "ignore": _strs(raw.get("ignore")),
                "image": str(raw.get("image") or "").strip(),
                "env": {
                    str(k): str(v) for k, v in env.items() if str(k).strip()
                } if isinstance(env, dict) else {},
            }
        else:
            entry = {"deps": _strs(raw), "ignore": [], "image": "", "env": {}}
        if entry["deps"] or entry["ignore"] or entry["image"] or entry["env"]:
            result[str(name)] = entry
    return result


def overlay_sandbox_image(skill_name: str) -> str:
    """overlay 声明的沙箱镜像(无声明返回空串)。"""
    return str(_load_deps_overlay_full().get(skill_name, {}).get("image") or "")


def overlay_sandbox_env(skill_name: str) -> dict[str, str]:
    """overlay 声明的容器 env(无声明返回空 dict)。"""
    env = _load_deps_overlay_full().get(skill_name, {}).get("env")
    return dict(env) if isinstance(env, dict) else {}


def load_deps_overlay() -> dict[str, list[str]]:
    """读取 sandbox/deps_overlay.yaml,按 skill 名返回依赖表(缺失/解析失败按空表)。"""
    return {
        name: entry["deps"]
        for name, entry in _load_deps_overlay_full().items()
        if entry["deps"]
    }


def effective_sandbox_deps(name: str, frontmatter: dict) -> list[str]:
    """skill 的有效沙箱依赖 = frontmatter 声明 ∪ 宿主 overlay(并集,排序去重)。

    构建(sandbox.build)与绑定(bind_project_skill_scripts)必须都走这里,
    保证同一 skill 算出同一内容寻址 tag。
    """
    merged = set(sandbox_deps_from_frontmatter(frontmatter))
    merged.update(load_deps_overlay().get(name, []))
    return sorted(merged)


# onboard 扫描报告里视为"脚本"的非 Python 后缀(无后缀的可执行文件也算)
_NON_PY_SCRIPT_SUFFIXES = {".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".go", ".rb", ".ps1", ".bat", ".cmd"}

# 常见 import 名 ≠ pip 包名 的映射(onboard 推导用;未命中的按原名列出交人确认)
_MODULE_TO_PACKAGE = {
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
}


def infer_sandbox_deps(skill_dir: Path) -> tuple[list[str], list[str]]:
    """静态推导 scripts/ 的第三方 pip 依赖(AST 解析,不覆盖 try 内的动态导入)。

    剔除:标准库、skill 内部模块(scripts/ 下的文件/包名)、相对导入。
    返回 (经映射的包名列表, 未识别模块原名列表)——后者大概率包名同名,
    但仍列出交人确认。
    """
    scripts = Path(skill_dir) / "scripts"
    if not scripts.is_dir():
        return [], []
    local_names = {p.stem for p in scripts.rglob("*.py")}
    # 内部包可能是任意深度的目录(如 scripts/office/helpers/),全部按本地模块剔除
    local_names.update(p.name for p in scripts.rglob("*") if p.is_dir())
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    packages: set[str] = set()
    unknown: set[str] = set()
    for py in sorted(scripts.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:  # 相对导入 = skill 内部
                    continue
                names = [node.module.split(".")[0]]
            for top in names:
                if not top or top in stdlib or top in local_names:
                    continue
                package = _MODULE_TO_PACKAGE.get(top)
                if package:
                    packages.add(package)
                else:
                    unknown.add(top)
    return sorted(packages), sorted(unknown)


def onboard_skills(assume_yes: bool = False, build: bool = True) -> int:
    """新增 skill 的手动登记入口:扫描项目 skills → AST 推导脚本第三方依赖 →
    人确认后写入 sandbox/deps_overlay.yaml(不改 skill 源文件)→ 按需触发镜像构建。

    用法: python -m host.capabilities.skill_registry onboard [--yes] [--no-build]
    """
    overlay = _load_deps_overlay_full()
    changed = False
    skill_dirs = _scan_skill_dirs(Path(config.SKILLS_DIR))
    print(f"扫描 {config.SKILLS_DIR}/ → 发现 {len(skill_dirs)} 个 skill: "
          f"{[d.name for d in skill_dirs] or '-'}")
    for skill_dir in skill_dirs:
        name = skill_dir.name
        inferred, unknown = infer_sandbox_deps(skill_dir)
        if not inferred and not unknown:
            # 非 Python 脚本显式点名防"没被扫到"的错觉:.sh 会绑定(容器内 bash);
            # 其他类型(.js/.go 无运行时镜像、资源文件)不会绑定
            # (只列脚本样文件——可执行后缀或无后缀;png 等资源文件不算)
            scripts_dir = skill_dir / "scripts"
            scriptish = (
                [p for p in scripts_dir.rglob("*")
                 if p.is_file()
                 and (not p.suffix or p.suffix.casefold() in _NON_PY_SCRIPT_SUFFIXES)]
                if scripts_dir.is_dir() else []
            )
            sh_names = sorted(p.name for p in scriptish if p.suffix.casefold() == ".sh")
            other_names = sorted(p.name for p in scriptish if p.suffix.casefold() != ".sh")
            notes: list[str] = []
            if sh_names:
                notes.append(f"{len(sh_names)} 个 shell 脚本将绑定(容器内 bash 执行): {sh_names}")
            if other_names:
                notes.append(f"{len(other_names)} 个脚本类型不支持绑定: {other_names}")
            note = ";" + ";".join(notes) if notes else ""
            print(f"[跳过] {name}: 无第三方 Python 依赖,无需登记{note}")
            continue
        entry = overlay.get(name, {"deps": [], "ignore": []})
        ignored = set(entry["ignore"])
        unknown = [m for m in unknown if m not in ignored]
        current = effective_sandbox_deps(name, read_skill_frontmatter(skill_dir))
        proposed = sorted(set(inferred) | set(unknown))
        missing = [d for d in proposed if d not in current]
        if not missing:
            print(f"[跳过] {name}: 依赖已齐备 {current}")
            continue
        print(f"[待确认] {name}: 推导依赖 {proposed}")
        if unknown:
            print(f"         其中未识别模块按原名列出,请核对包名: {unknown}")
        print(f"         当前生效 {current or '无'} → 将新增写入 overlay: {missing}")
        if not assume_yes:
            answer = input(
                "         确认写入? [y=全是 pip 包 / i=未识别项不是 pip 包,记入 ignore / N=跳过] "
            ).strip().lower()
            if answer == "i":
                entry["ignore"] = sorted(ignored | set(unknown))
                overlay[name] = entry
                changed = True
                print(f"[ignore] {name}: {entry['ignore']}")
                continue
            if answer != "y":
                print(f"[跳过] {name}")
                continue
        entry["deps"] = sorted(set(entry["deps"]) | set(missing))
        overlay[name] = entry
        changed = True
        print(f"[写入] {name}: {entry['deps']}")
    if not changed:
        print("没有需要登记的依赖变更")
        return 0
    header = (
        "# skill 沙箱依赖覆盖表(宿主维护,随项目入库)。\n"
        "# 外部 skill 源文件(SKILL.md)不改;deps 与 frontmatter sandbox_python_deps 取并集;\n"
        "# ignore 记录已确认非 pip 包的模块名(onboard 不再提案)。\n"
        "# 由 `python -m host.capabilities.skill_registry onboard` 生成,可手改;改后跑一次 `python -m sandbox.build`。\n"
    )
    data = {
        name: (entry["deps"] if not entry["ignore"] else entry)
        for name, entry in overlay.items()
    }
    _DEPS_OVERLAY_PATH.write_text(
        header + yaml.safe_dump(data, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    print(f"[完成] overlay 已写入 {_DEPS_OVERLAY_PATH}")
    if build:
        if not assume_yes and input("立即构建沙箱镜像? [y/N] ").strip().lower() != "y":
            print("已跳过构建;之后手动执行: python -m sandbox.build")
            return 0
        from sandbox import build as sandbox_build

        return sandbox_build.main()
    print("之后手动执行构建: python -m sandbox.build")
    return 0


def derived_sandbox_image(deps: list[str]) -> str:
    """依赖集内容寻址派生镜像 tag:依赖集相同共享镜像,依赖变 tag 变。"""
    digest = hashlib.sha256("\n".join(deps).encode("utf-8")).hexdigest()[:12]
    return f"{SANDBOX_AUTO_IMAGE_PREFIX}:{digest}"


def _repair_image_tag(image: str) -> bool:
    """Docker Desktop 短 tag 怪癖自愈:images 列表有该 tag 但 inspect 失败时,
    按镜像 ID 重新 docker tag(不可先 rm——删最后一个 tag 会把镜像一起删掉)。"""
    try:
        listing = subprocess.run(
            ["docker", "image", "ls", "--format", "{{.ID}} {{.Repository}}:{{.Tag}}"],
            capture_output=True, timeout=30, text=True,
        )
        if listing.returncode != 0:
            return False
        for line in listing.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == image:
                subprocess.run(
                    ["docker", "tag", parts[0], image], capture_output=True, timeout=30
                )
                logger.warning("镜像 tag 异常,已按 ID 自愈重打: %s (%s)", image, parts[0])
                return True
    except Exception:
        pass
    return False


def _inspect_ok(image: str) -> bool:
    try:
        return subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, timeout=30,
        ).returncode == 0
    except Exception:
        return False


def daemon_image_ref(image: str) -> str:
    """把镜像名解析成当前 daemon 实际可 inspect/run 的引用。

    Docker Desktop(containerd 镜像存储)对经镜像站拉取的官方库镜像有命名空间
    怪癖:`docker images` 列出 `python:3.12-slim`,但 `inspect python:3.12-slim`
    报 No such image,只有 `docker.io/library/python:3.12-slim` 能命中
    (2026-10-13 实证——框架 provider 的 inspect_image 无自愈,skill 脚本 action
    全部因此卡在 "required runtime image is not locally available")。
    本地构建镜像(agent-skill-*)bare 名正常,不受影响。
    """
    if _inspect_ok(image) or _repair_image_tag(image) and _inspect_ok(image):
        return image
    first = image.split("/", 1)[0]
    has_registry = "/" in image and ("." in first or ":" in first or first == "localhost")
    if not has_registry:
        canonical = f"docker.io/library/{image}"
        if _inspect_ok(canonical):
            return canonical
    return image


def local_image_exists(image: str) -> bool:
    """本机 docker 是否已有该镜像(仅缓存存在的结果;docker 不可用一律当不存在)。

    不存在/不可用的结果不缓存:镜像构建或 Docker Desktop 启动后无需重启服务即可绑定。
    inspect 失败时先尝试短 tag 自愈,再判定不存在。
    """
    if _image_exists_cache.get(image):
        return True

    def _inspect() -> bool:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except Exception:
            return False

    exists = _inspect() or (_repair_image_tag(image) and _inspect())
    if exists:
        _image_exists_cache[image] = True
    return exists


def diagnose_execution_unavailable() -> str:
    """action 报 "No eligible ExecutionResourceProvider" 时的事后诊断(一行原因)。

    框架的错误字符串不含 provider 探针 reason,失败后无法回溯是 daemon 抖动、
    镜像 inspect 失败还是能力不匹配——在失败当刻主动探测一次留证。
    """
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return "docker CLI 不在 PATH"
    except Exception as e:
        return f"docker 诊断异常: {e}"
    if proc.returncode != 0:
        return f"docker daemon 不可达: {(proc.stderr or proc.stdout).strip()[:120]}"
    return (
        f"诊断时 docker daemon 正常(server {proc.stdout.strip()})"
        "——疑似瞬时抖动;若反复出现查镜像 inspect 与能力匹配"
    )

_SCRIPT_LANGUAGES = {
    ".py": "python",
    ".sh": "shell",
    ".js": "nodejs",
    ".mjs": "nodejs",
    ".cjs": "nodejs",
    ".go": "go",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
}

# 脚本树投递总量上限(防爆;pptx 的 scripts/ 含 validate.py 运行期依赖的
# XSD schema 数据文件,整树约 1.2MB——上限须覆盖此类"脚本+数据"混合树)
_SKILL_SCRIPTS_BUNDLE_MAX_BYTES = 8 * 1024 * 1024
# shell 入口才投递的 skill 根下运行时资源目录(bash 脚本用 $HERE/.. 定位);
# 单资源路径长度护栏:宿主落盘根已较长,留足余量防 Windows MAX_PATH(260)
_SHELL_RESOURCE_ROOTS = {"templates", "assets", "static"}
_BUNDLE_RESOURCE_PATH_MAX = 120

_MAIN_GUARD_RE = re.compile(r"if\s+__name__\s*==\s*[\"']__main__[\"']")
_REL_IMPORT_RE = re.compile(r"(?m)^\s*from\s+\.+\s*\w*\s*import\b|^\s*from\s+\.+\s*$")


_SH_SHEBANG_RE = re.compile(r"^#!.*\b(?:ba|da|z)?sh\b")


def _is_cli_script(language: str, source: bytes) -> bool:
    """判断脚本是否为可直接运行的 CLI(区别于库文件/包内模块)。

    Python 三个条件:有 __main__ guard;自行解析参数(argparse 或 sys.argv);
    无模块级相对导入(相对导入的模块只能作为包的一部分被 import,
    作为入口直跑必 ImportError,绑成 action 只会误导模型)。
    shell(.sh):sh 系 shebang + 自带 usage 说明(厂商 CLI 脚本惯例;
    无 usage 的多半是库函数文件,绑了模型也只能瞎试)。
    """
    text = source.decode("utf-8", errors="replace")
    if language == "shell":
        return bool(_SH_SHEBANG_RE.search(text)) and "usage" in text.lower()
    if not _MAIN_GUARD_RE.search(text):
        return False
    if _REL_IMPORT_RE.search(text):
        return False
    return "argparse" in text or "sys.argv" in text


def _top_level_imports(source: bytes) -> set[str]:
    """脚本源码的模块级 import 顶层名集合(解析失败按空集)。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


class SkillScriptExecutor(CodeExecutionActionExecutor):
    """框架默认只为 skill 脚本投递 entrypoint 单文件(files={resource_path: data})。

    厂商 skill 的脚本普遍 import 同包 sibling——pptx 的 add_slide/clean/thumbnail
    全部 import office.helpers 包,单文件投递下 ModuleNotFoundError 全挂
    (2026-10-12 实证)。这里覆盖为投递该 skill 的整个 script 资源树,
    入口文件与路径不变,总量封顶 _SKILL_SCRIPTS_BUNDLE_MAX_BYTES。
    非脚本语言的数据文件(XSD schema 等)只随"同包目录"的入口投递——
    与入口无关的子包数据树体积大、目录深,会把宿主侧 bundle 落盘路径
    顶过 Windows MAX_PATH(260)(2026-10-12 实测 WinError 206)。

    host_env:overlay 声明的容器环境变量(如 dashi-ppt 的
    DASHI_PPT_PROJECT_ROOT 指向镜像内烘焙工程),经 provenance 传给
    运行时适配器落到 run step(docker -e)。
    """

    def __init__(self, *args, host_env: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._host_env = dict(host_env or {})

    def _request_from_action(self, *, spec, action_call):
        request = super()._request_from_action(spec=spec, action_call=action_call)
        meta = self._mapping(spec.get("meta"))
        revision_ref = str(meta.get("skill_revision_ref", "")).strip()
        if not revision_ref:
            return request  # 非 skill 脚本(action_input 直给源码)不处理
        package = skill_library.resolve(revision_ref)
        provenance = dict(request.provenance)
        if self._host_env:
            provenance["host_env"] = {str(k): str(v) for k, v in self._host_env.items()}
        files = {f.path: f.content for f in request.files}
        total = sum(len(c) for c in files.values())
        entry_dir = str(PurePosixPath(str(request.entrypoint or "")).parent)
        shell_entry = str(request.language or "").strip().casefold() == "shell"
        for descriptor in package.resources:
            if descriptor.path in files:
                continue
            path = PurePosixPath(descriptor.path)
            if descriptor.kind != "script":
                # shell 入口的运行时资源投递:bash 脚本惯用 $HERE/.. 定位 skill 根的
                # 资源目录(html-ppt new-deck.sh 依赖 templates/ 脚手架,
                # 2026-10-12 实证)——白名单顶层目录才投,带路径长度护栏
                # (宿主落盘路径顶过 Windows MAX_PATH(260) 的 206 事故同款防线);
                # docs/examples 等模型侧资料不投(模型读 SKILL.md 即可)
                if not (
                    shell_entry
                    and path.parts
                    and path.parts[0] in _SHELL_RESOURCE_ROOTS
                    and len(descriptor.path) <= _BUNDLE_RESOURCE_PATH_MAX
                ):
                    continue
            elif path.suffix.casefold() not in _SCRIPT_LANGUAGES:
                # 非脚本语言的数据文件(如 validate.py 运行期依赖的 XSD schema 树):
                # 只随同包入口投递(入口在 scripts/office/ 才带 office/ 下的数据);
                # 入口在 scripts/ 根时不带子包数据——add_slide 等无需 schema,
                # 且数据树的深层目录会把宿主落盘路径顶过 Windows MAX_PATH(260),
                # 2026-10-12 实测 WinError 206(schemas/ISO-IEC29500-4_2016/...)
                if entry_dir == "scripts":
                    if str(path.parent) != "scripts":
                        continue
                elif not (
                    str(path.parent) == entry_dir
                    or str(path.parent).startswith(entry_dir + "/")
                ):
                    continue
            read = skill_library.read_resource(
                package, descriptor.path, max_bytes=descriptor.size + 1
            )
            if read.truncated:
                continue
            total += len(read.data)
            if total > _SKILL_SCRIPTS_BUNDLE_MAX_BYTES:
                logger.warning(
                    "skill %s 脚本树超过 %d 字节,部分 sibling 未投递",
                    package.name,
                    _SKILL_SCRIPTS_BUNDLE_MAX_BYTES,
                )
                break
            files[descriptor.path] = read.data
        if len(files) == len(request.files) and not self._host_env:
            return request
        return CodeExecutionRequest.create(
            language=request.language,
            files=files,
            entrypoint=request.entrypoint,
            args=request.args,
            expected_outputs=request.expected_outputs,
            provenance=provenance,
        )


# 通用沙箱 shell 命令长度上限(防模型把超大内容塞进 command 参数)
_SHELL_COMMAND_MAX_BYTES = 32 * 1024


def run_skill_shell_compensation(action_id: str, command: str, ws_root: str) -> str:
    """回复阶段 DSML 伪调用的 skill_shell 补偿执行(chat_pipeline 调用)。

    action_id 形如 skill_shell_<name_stem>_<digest>,按 stem 匹配项目级 skill,
    用其声明的专用镜像(frontmatter/overlay,与绑定时同一解析顺序)直接
    docker run:workspace 挂 /task-workspace、overlay env 注入、隔离剖面
    (无网/cap-drop ALL/no-new-privileges/pids/memory)与框架 docker provider 一致。
    返回 stdout+stderr 文本(截断);任何失败返回说明文本,不抛异常。
    """
    if not command.strip():
        return "command 为空,未执行"
    stem = action_id[: -13] if len(action_id) > 13 else action_id  # 去 _<digest12>
    stem = stem.removeprefix("skill_shell_")
    package = None
    for ref in project_revision_refs():
        try:
            candidate = skill_library.resolve(ref)
        except Exception:
            continue
        candidate_stem = re.sub(r"[^a-z0-9]+", "_", candidate.name.casefold()).strip("_")
        if candidate_stem == stem:
            package = candidate
            break
    if package is None:
        return f"未找到 action 对应的 skill(stem={stem}),未执行"
    frontmatter = dict(package.frontmatter)
    image = str(frontmatter.get("sandbox_image") or "").strip() or overlay_sandbox_image(
        package.name
    )
    if not image:
        return f"skill {package.name} 未声明专用镜像,未执行"
    if not local_image_exists(image):
        return f"镜像 {image} 本地不存在,未执行"
    image = daemon_image_ref(image)
    env_args: list[str] = []
    for k, v in overlay_sandbox_env(package.name).items():
        env_args += ["-e", f"{k}={v}"]
    ws = str(Path(ws_root).resolve()).replace("\\", "/")
    argv = [
        "docker", "run", "--rm",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "256",
        "--memory", "512m",
        *env_args,
        "-v", f"{ws}:/task-workspace",
        image, "bash", "-lc", command,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=config.SKILL_SCRIPT_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return f"执行超时({config.SKILL_SCRIPT_TIMEOUT}s)"
    except OSError as e:
        return f"docker 调用失败: {e}"
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    text = (out + ("\n[stderr]\n" + err if err.strip() else "")).strip()
    status = "成功" if proc.returncode == 0 else f"退出码 {proc.returncode}"
    return f"[{status}]\n{text}" if text else f"[{status},无输出]"


class SkillSandboxShellExecutor(CodeExecutionActionExecutor):
    """专用镜像 skill 的通用沙箱 shell:模型给 command,在 skill 专用镜像容器内
    以 bash 执行(复用 _ShellCodeRuntimeAdapter 链路:argv=("bash", entrypoint),
    cwd=source,overlay env 经 provenance → run step → docker -e)。

    背景(2026-10-13 实证):工程型 skill 的 SKILL.md 官方流程需要跑一连串任意
    工具链命令(dashi-ppt: layout:query / inspect:layout / goal:scaffold 等 npm
    脚本),而脚本绑定只覆盖 scripts/ 下的 CLI 入口——中段命令无任何 action 承载,
    模型只能臆造不存在的 run_command 并在正文输出伪 <tool_call>。本 executor 把
    command 包装成 entrypoint 脚本投递,与脚本 action 同沙箱剖面(workspace 挂载、
    无网、隔离限额不变),不扩大暴露面。
    """

    def __init__(self, *args, host_env: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._host_env = dict(host_env or {})

    def _request_from_action(self, *, spec, action_call):
        action_input = self._mapping(action_call.get("action_input"))
        command = str(action_input.get("command") or "")
        if not command.strip():
            raise ValueError("command 不能为空")
        raw = command.encode("utf-8")
        if len(raw) > _SHELL_COMMAND_MAX_BYTES:
            raise ValueError(
                f"command 超过 {_SHELL_COMMAND_MAX_BYTES} 字节上限;长内容请先写入 workspace 文件再在命令里引用"
            )
        meta = self._mapping(spec.get("meta"))
        revision_ref = str(meta.get("skill_revision_ref", "")).strip()
        provenance = {"kind": "skill_shell", "revision_ref": revision_ref}
        if self._host_env:
            provenance["host_env"] = {str(k): str(v) for k, v in self._host_env.items()}
        script = b"#!/usr/bin/env bash\nset -uo pipefail\n" + raw + b"\n"
        return CodeExecutionRequest.create(
            language="shell",
            files={"command.sh": script},
            entrypoint="command.sh",
            args=(),
            expected_outputs=(),
            provenance=provenance,
        )


def _scan_skill_dirs(root: Path) -> list[Path]:
    """root 本身是 skill 或递归找含 SKILL.md 的目录;嵌套 skill 只取最外层。"""
    if not root.is_dir():
        return []
    if (root / "SKILL.md").is_file():
        return [root]
    dirs: list[Path] = []
    for skill_file in sorted(root.rglob("SKILL.md")):
        candidate = skill_file.parent
        parent = candidate.parent
        nested = False
        while parent != root.parent:
            if (parent / "SKILL.md").is_file():
                nested = True
                break
            parent = parent.parent
        if not nested:
            dirs.append(candidate)
    return dirs


def _install_dirs(dirs: list[Path]) -> list[str]:
    refs: list[str] = []
    for d in dirs:
        try:
            rev = skill_library.install(d, trust="trusted")
            refs.append(rev.revision_ref)
        except Exception as e:
            logger.warning("skill 安装失败(%s): %s", d, e)
    return refs


def install_project_skills() -> list[str]:
    """扫描安装项目内置 skills(内容哈希幂等,每轮生成前也会重扫),返回并缓存 revision_ref 列表。"""
    global _project_revision_refs
    refs = _install_dirs(_scan_skill_dirs(Path(config.SKILLS_DIR)))
    if refs != _project_revision_refs:
        logger.info("项目 skills 已安装 %d 个: %s", len(refs), refs)
    _project_revision_refs = refs
    return list(refs)


def project_revision_refs() -> list[str]:
    return list(_project_revision_refs)


def install_workspace_skills(ws_root: str) -> list[str]:
    """每轮生成前增量安装会话 workspace 内 skills(内容哈希幂等)。"""
    return _install_dirs(_scan_skill_dirs(Path(ws_root) / config.WORKSPACE_SKILLS_DIR))


def collect_turn_skill_refs(ws_root: str | None) -> list[str]:
    """每轮生成前调用:项目级重扫(运行期新增公共 skill 即刻感知)+ workspace 级
    增量扫描,同名冲突时项目级优先(workspace 级是 agent 可写的会话数据,可能是
    模型臆造的自我污染产物,2026-10-03 实证)。"""
    global _skill_source_dirs
    dirs: dict[str, Path] = {}
    if ws_root:
        for d in _scan_skill_dirs(Path(ws_root) / config.WORKSPACE_SKILLS_DIR):
            dirs[d.name] = d
    for d in _scan_skill_dirs(Path(config.SKILLS_DIR)):
        dirs[d.name] = d  # 项目级覆盖 workspace 同名(与 refs 合并同优先级)
    _skill_source_dirs = dirs
    project_refs = install_project_skills()
    if not ws_root:
        return project_refs
    ws_refs = install_workspace_skills(ws_root)
    if not ws_refs:
        return project_refs
    project_names: set[str] = set()
    for ref in project_refs:
        try:
            project_names.add(skill_library.resolve(ref).name)
        except Exception:
            pass
    merged = list(project_refs)
    for ref in ws_refs:
        try:
            name = skill_library.resolve(ref).name
        except Exception:
            name = ""
        if name and name in project_names:
            logger.info("workspace skill 与项目级同名,压制: %s", ref)
            continue
        merged.append(ref)
    return merged


def skill_catalog_line(refs: list[str], max_desc: int = 120) -> str:
    """候选 skill 的 name+description 目录行,注入生成请求——让生成模型知道
    "有哪些 skill、各自做什么"(未选中 skill 的正文仍不披露)。"""
    items: list[str] = []
    for ref in refs:
        try:
            package = skill_library.resolve(ref)
            desc = str(getattr(package, "description", "") or "").strip().replace("\n", " ")
            if len(desc) > max_desc:
                desc = desc[:max_desc] + "…"
            items.append(f"{package.name}: {desc}" if desc else package.name)
        except Exception:
            continue
    return "; ".join(items)


# skills_ref 镜像:把选中 skill 的源目录增量复制到 <workspace>/skills_ref/<name>/。
# 背景(2026-10-13 实证):SKILL.md 正文经框架注入模型上下文,但模型的文件工具
# 根目录是 workspace,项目 skills/ 在其视野之外——dashi-ppt 轮模型 6 次 list_files
# 找 theme-style-grid.png 均落空,放弃官方流程退化为手写 HTML 贴正文。
# 镜像是只读副本(文件置只读属性,模型写入会 PermissionError),每轮同步:
# 源改动增量覆盖、源删除/模型违规写入一律清掉;目录本身视为宿主管理的易失区。
SKILLS_REF_DIR = "skills_ref"
SKILLS_REF_MAX_BYTES = 100 * 1024 * 1024

_skill_source_dirs: dict[str, Path] = {}  # skill 名 -> 源目录(collect_turn_skill_refs 每轮重建)


def _sync_readonly_copy(src: Path, dst: Path) -> None:
    """src → dst 增量同步(copy2 保留 mtime),目标文件置只读;目标多出的一律删。"""
    src_files: dict[Path, tuple[int, int]] = {}
    total = 0
    for p in src.rglob("*"):
        if p.is_file():
            st = p.stat()
            total += st.st_size
            src_files[p.relative_to(src)] = (st.st_size, int(st.st_mtime))
    if total > SKILLS_REF_MAX_BYTES:
        raise RuntimeError(
            f"skill 源目录 {total / 1048576:.0f}MB 超过镜像上限 "
            f"{SKILLS_REF_MAX_BYTES // 1048576}MB"
        )
    dst.mkdir(parents=True, exist_ok=True)
    for rel, (size, mtime) in src_files.items():
        target = dst / rel
        try:
            if target.is_file():
                tst = target.stat()
                if (tst.st_size, int(tst.st_mtime)) == (size, mtime):
                    continue
                os.chmod(target, stat.S_IWRITE)  # 先解除只读再覆盖
        except OSError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target)
        os.chmod(target, stat.S_IREAD)
    for p in sorted(dst.rglob("*"), reverse=True):
        if p.is_file() and p.relative_to(dst) not in src_files:
            os.chmod(p, stat.S_IWRITE)
            p.unlink()
    for p in sorted((d for d in dst.rglob("*") if d.is_dir()), reverse=True):
        try:
            p.rmdir()  # 只清空目录,非空保留
        except OSError:
            pass


def _prune_skills_ref(root: Path, live_names: set[str]) -> None:
    """清掉 skills_ref/ 下源已不存在的 skill 目录。"""
    if not root.is_dir():
        return
    for child in root.iterdir():
        if not child.is_dir() or child.name in live_names:
            continue
        for p in child.rglob("*"):
            if p.is_file():
                try:
                    os.chmod(p, stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(child, ignore_errors=True)


def mirror_selected_to_workspace(execution, ws_root: str) -> list[str]:
    """把 execution 选中(skill_bindings)的 skill 源目录镜像到 workspace 可见面。

    需在 async_prepare_task_context() 之后调用(bindings 才有值);全程 fail-open,
    单个 skill 失败不影响其他。返回成功镜像的 skill 名列表。
    """
    if config.WORKSPACE_READ_ONLY:
        return []
    try:
        bindings = getattr(execution, "skill_bindings", []) or []
    except Exception:
        return []
    names: list[str] = []
    for b in bindings:
        try:
            names.append(skill_library.resolve(b.revision_ref).name)
        except Exception:
            continue
    root = Path(ws_root) / SKILLS_REF_DIR
    mirrored: list[str] = []
    for name in dict.fromkeys(names):
        src = _skill_source_dirs.get(name)
        if not src or not src.is_dir():
            continue
        try:
            _sync_readonly_copy(src, root / name)
            mirrored.append(name)
        except Exception as e:
            logger.warning("skill 资源镜像失败(%s): %s", name, e)
    _prune_skills_ref(root, set(_skill_source_dirs))
    return mirrored


def clean_stale_library_entries() -> tuple[list[str], list[str]]:
    """清理 skill 库缓存(packages/ + index.json)中源目录已不存在的条目。

    有效源 = 项目 SKILLS_DIR + 各 workspace(WORKSPACE_AUTO_ROOT)内的
    WORKSPACE_SKILLS_DIR,按 skill 名(目录名)判定;库缓存删除无害——
    安装按内容哈希幂等,源还在的条目下次用到会原样重装。
    返回 (已删除名单, 保留名单)。
    """
    live_names = {
        d.name for d in _scan_skill_dirs(Path(config.SKILLS_DIR))
    }
    auto_root = Path(config.WORKSPACE_AUTO_ROOT)
    if auto_root.is_dir():
        for ws in auto_root.iterdir():
            live_names.update(
                d.name for d in _scan_skill_dirs(ws / config.WORKSPACE_SKILLS_DIR)
            )
    root = Path(str(skill_library.root))
    index_path = root / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        index = {}
    names = list((index.get("skills") or {}).keys())
    removed: list[str] = []
    kept: list[str] = []
    for name in names:
        if name in live_names:
            kept.append(name)
            continue
        shutil.rmtree(root / "packages" / name, ignore_errors=True)
        index["skills"].pop(name, None)
        removed.append(name)
    if removed:
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return removed, kept


if __name__ == "__main__":
    # python -m host.capabilities.skill_registry clean                    一键清理库缓存中源已删除的条目
    # python -m host.capabilities.skill_registry onboard [--yes] [--no-build]  新增 skill 依赖登记(推导+确认+构建)
    args = sys.argv[1:]
    if args and args[0] == "clean":
        removed, kept = clean_stale_library_entries()
        print(f"已删除 {len(removed)} 个: {removed or '-'}")
        print(f"保留 {len(kept)} 个: {kept or '-'}")
        if removed:
            print("说明: 只清理了库缓存(.agently/skill-library),skills/ 源目录未动;"
                  "源目录还在的条目下次用到会自动重装。")
    elif args and args[0] == "onboard":
        raise SystemExit(
            onboard_skills(assume_yes="--yes" in args, build="--no-build" not in args)
        )
    else:
        print("用法: python -m host.capabilities.skill_registry clean | onboard [--yes] [--no-build]")


def bind_project_skill_scripts(agent, execution, timeout: int) -> tuple[list[str], list[str], list[tuple[str, str]], list[str]]:
    """把"被选中的项目级 skill"的 scripts/ 绑定为 Docker 沙箱执行的 Action。

    信任分层:只对 revision_ref ∈ project_revision_refs() 的绑定注册;
    workspace 级 skill 内容是会话数据(agent 自己可写),永不绑定脚本,防自我污染。

    与 SkillActionBinder 相同的校验与注册形状,唯一差异:workspace_access 按
    SKILL_SCRIPT_WORKSPACE_WRITE 取 read_only/read_write(框架 binder 硬编码
    snapshot——不挂载 workspace,脚本读不到模型动态指定的文件);容器内 workspace
    挂载在 /task-workspace,隔离剖面不变
    (cap-drop ALL/no-new-privileges/pids-limit/network none/资源限额)。
    dunder 文件(__init__.py 等)不注册为脚本 action。

    镜像解析顺序:frontmatter sandbox_image > sandbox_python_deps 派生 tag(需本地
    已构建)> 全局 SKILL_SCRIPT_IMAGE;声明了依赖但派生镜像未构建时跳过该 skill,
    并在返回的 notes 里给出构建提示(不静默降级到缺依赖环境)。

    返回 (绑定的 action_id 列表, 提示说明列表, 专用镜像 skill 列表[(name, image)],
    通用 shell action 列表)。第三项用于生成侧 instruct:专用镜像 skill 的工具链以
    SKILL.md 为准(2026-10-13 实证:笼统的"沙箱无 node"声明会让模型对 Node 型
    skill 直接放弃官方流程)。第四项:声明了专用镜像(frontmatter sandbox_image /
    overlay image)的 skill 额外注册一个通用沙箱 shell action——工程型 skill 的
    SKILL.md 流程需要任意工具链命令(npm 脚本等),CLI 脚本绑定覆盖不了中段步骤
    (2026-10-13 实证:模型臆造不存在的 run_command 并输出伪 <tool_call> 文本)。
    """
    bound: list[str] = []
    notes: list[str] = []
    custom_images: list[tuple[str, str]] = []
    shell_actions: list[str] = []
    # 挂载模式:默认只读;SKILL_SCRIPT_WORKSPACE_WRITE 开后受信脚本可写回 workspace
    mount_mode = "read_write" if config.SKILL_SCRIPT_WORKSPACE_WRITE else "read_only"
    project_refs = set(_project_revision_refs)
    for binding in getattr(execution, "skill_bindings", []) or []:
        if binding.revision_ref not in project_refs:
            continue
        if str(getattr(execution, "id", "")) != binding.task_id:
            raise PermissionError("Skill binding belongs to another task execution.")
        package = skill_library.resolve(binding.revision_ref)
        if package.revision_ref != binding.revision_ref or package.trust != "trusted":
            raise PermissionError("Skill script execution requires a trusted exact revision.")
        # 镜像解析:frontmatter sandbox_image > overlay image(宿主级声明,外部
        # skill 源文件不改——dashi-ppt 的 Node 底座镜像走这里)> 有效依赖
        # (frontmatter ∪ 宿主 overlay)派生 tag(需本地已构建,strict profile
        # image_pull_policy=never 不会拉取)> 全局默认;声明了依赖但派生镜像未构建
        # → 跳过该 skill 并提示登记/构建,不降级到缺依赖环境
        frontmatter = dict(package.frontmatter)
        deps = effective_sandbox_deps(package.name, frontmatter)
        image = str(frontmatter.get("sandbox_image") or "").strip()
        if not image:
            image = overlay_sandbox_image(package.name)
        declared_image = image  # frontmatter/overlay 显式声明的专用镜像(派生/全局不算)
        if image and (package.name, image) not in custom_images:
            custom_images.append((package.name, image))
        if not image and deps:
            derived = derived_sandbox_image(deps)
            if local_image_exists(derived):
                image = derived
            else:
                notes.append(
                    f"skill {package.name} 声明了沙箱依赖 {deps},镜像 {derived} 未构建,"
                    "未绑定脚本;请先运行: python -m sandbox.build"
                    "(新增 skill 可用 python -m host.capabilities.skill_registry onboard 一步登记+构建)"
                )
                continue
        if not image:
            image = config.SKILL_SCRIPT_IMAGE
        image = daemon_image_ref(image)
        for descriptor in package.resources:
            if descriptor.kind != "script" or not descriptor.executable:
                continue
            if PurePosixPath(descriptor.path).name.startswith("__"):
                continue  # __init__.py 等 dunder 文件不是可执行脚本
            language = _SCRIPT_LANGUAGES.get(PurePosixPath(descriptor.path).suffix.casefold())
            if language is None:
                logger.warning("跳过不支持的脚本语言: %s", descriptor.path)
                continue
            if language in ("python", "shell"):
                # 只绑定真正的 CLI 脚本:python 需 __main__ guard、自己解析参数
                # (argparse/sys.argv)、无模块级相对导入;shell 需 sh 系 shebang +
                # 自带 usage 说明。
                # 厂商 skill 的 scripts/ 里混着库文件(pptx 的 office/helpers/* 无
                # __main__)和包内模块(validators/* 有 __main__ 但相对导入/无参数
                # 解析,base.py 的 main 直接 raise)——绑成 action 后模型把它们当
                # CLI 探测,--help 退 0 无输出,诱发长时间空转(2026-10-12 实证:
                # 模型探测 6 分钟直到 loop 超时)
                read = skill_library.read_resource(
                    package, descriptor.path, max_bytes=descriptor.size + 1
                )
                if read.truncated or not _is_cli_script(language, read.data):
                    logger.info("跳过非 CLI 脚本(库文件/包内模块): %s", descriptor.path)
                    continue
            if language == "python":
                # import 了 overlay ignore 名单(已确认非 pip 包的缺失内部模块)
                # 的脚本必崩,不绑——避免把已知残缺的 action 暴露给模型
                # (实证: docx 的 office/validate.py import 未随包提供的 helpers/validators)
                ignored = set(
                    _load_deps_overlay_full().get(package.name, {}).get("ignore") or ()
                )
                broken = _top_level_imports(read.data) & ignored
                if broken:
                    logger.info(
                        "跳过残缺脚本(import 缺失模块 %s): %s", sorted(broken), descriptor.path
                    )
                    continue
            stem = (
                re.sub(r"[^a-z0-9]+", "_", PurePosixPath(descriptor.path).stem.casefold()).strip("_")
                or "script"
            )
            digest = hashlib.sha256(
                f"{binding.revision_ref}\0{descriptor.path}".encode("utf-8")
            ).hexdigest()[:12]
            action_id = f"skill_{stem}_{digest}"
            if execution.action.action_registry.has(action_id):
                bound.append(action_id)
                continue
            # shell 类 action 不能声明 language 能力门控:Docker provider 的
            # capabilities.languages 只有 python/nodejs/go/cpp(框架注释明确
            # shell 不是 CodeRuntimeAdapter 语言),声明了会在探针阶段被判
            # 不合格——"No eligible ExecutionResourceProvider"(2026-10-13
            # 实证,skill_shell 与 .sh 脚本 action 同款隐性断链)。bash 缺失时
            # run step 会以 "bash: not found" 明确失败,无需门控。
            language_capability = {"language": language} if language != "shell" else {}
            execution.action.register_action(
                action_id=action_id,
                desc=(
                    f"Run the authorized {package.name} Skill script {descriptor.path} in a "
                    "sandboxed container. The session workspace is mounted "
                    + (
                        "read-write at /task-workspace; write produced files under "
                        "/task-workspace/outbox/ and "
                        if mount_mode == "read_write"
                        else "read-only at /task-workspace; "
                    )
                    + "pass workspace-relative file paths in args."
                ),
                kwargs={"args": ("list[str]", "Optional bounded script arguments.")},
                executor=SkillScriptExecutor(
                    language=language,
                    timeout=timeout,
                    skill_library=skill_library,
                    host_env=overlay_sandbox_env(package.name),
                ),
                default_policy={"auto_allow": True},
                side_effect_level="exec",
                approval_required=False,
                sandbox_required=True,
                replay_safe=False,
                expose_to_model=True,
                execution_resources=[
                    {
                        "kind": "code_execution",
                        "resource_key": action_id,
                        "scope": "action_call",
                        "provider_candidates": ["docker"],
                        "config": {
                            "runtime_profile": {"image": image},
                        },
                        "required_capabilities": {
                            **language_capability,
                            "isolation": required_code_execution_isolation(),
                            "workspace_access_mode": mount_mode,
                        },
                        "workspace_access": {
                            "mode": mount_mode,
                            "expected_outputs": [],
                        },
                    }
                ],
                meta={
                    "skill_revision_ref": package.revision_ref,
                    "skill_resource_path": descriptor.path,
                    "skill_resource_sha256": descriptor.sha256,
                    "expected_outputs": [],
                },
            )
            if action_id not in execution.local_action_ids:
                execution.local_action_ids.append(action_id)
            bound.append(action_id)
        # 声明了专用镜像的 skill:额外注册通用沙箱 shell action。工程型 skill 的
        # SKILL.md 官方流程需要任意工具链命令(dashi-ppt: layout:query /
        # inspect:layout / goal:scaffold 等 npm 脚本),scripts/ CLI 绑定覆盖不了
        # 中段步骤——2026-10-13 实证:模型臆造不存在的 run_command 并在正文输出
        # 伪 <tool_call>,官方流程断在第一步。沙箱剖面与脚本 action 完全一致。
        if declared_image:
            shell_stem = re.sub(r"[^a-z0-9]+", "_", package.name.casefold()).strip("_") or "skill"
            shell_digest = hashlib.sha256(
                f"{binding.revision_ref}\0_shell".encode("utf-8")
            ).hexdigest()[:12]
            shell_action_id = f"skill_shell_{shell_stem}_{shell_digest}"
            if not execution.action.action_registry.has(shell_action_id):
                execution.action.register_action(
                    action_id=shell_action_id,
                    desc=(
                        f"Run an arbitrary bash command inside the dedicated sandbox image "
                        f"for the {package.name} skill ({declared_image}). The toolchain "
                        "documented in its SKILL.md is preinstalled in this image "
                        "(e.g. node/npm for Node-based skills). The session workspace is "
                        + (
                            "mounted read-write at /task-workspace; write produced files "
                            "under /task-workspace/outbox/. "
                            if mount_mode == "read_write"
                            else "mounted read-only at /task-workspace. "
                        )
                        + f"The skill source tree is mirrored read-only at "
                        f"/task-workspace/skills_ref/{package.name}/. Use this for "
                        "multi-step toolchain flows (npm scripts, generators, preview "
                        "services) that have no dedicated script action. Long file "
                        "contents must be written to workspace files first and referenced "
                        "by path in the command."
                    ),
                    kwargs={
                        "command": (
                            "str",
                            "bash command to run inside the skill's dedicated sandbox image.",
                        ),
                    },
                    executor=SkillSandboxShellExecutor(
                        language="shell",
                        timeout=timeout,
                        skill_library=skill_library,
                        host_env=overlay_sandbox_env(package.name),
                    ),
                    default_policy={"auto_allow": True},
                    side_effect_level="exec",
                    approval_required=False,
                    sandbox_required=True,
                    replay_safe=False,
                    expose_to_model=True,
                    execution_resources=[
                        {
                            "kind": "code_execution",
                            "resource_key": shell_action_id,
                            "scope": "action_call",
                            "provider_candidates": ["docker"],
                            "config": {
                                "runtime_profile": {"image": declared_image},
                            },
                            "required_capabilities": {
                                # 同上证:shell 不在 provider languages 门控内
                                "isolation": required_code_execution_isolation(),
                                "workspace_access_mode": mount_mode,
                            },
                            "workspace_access": {
                                "mode": mount_mode,
                                "expected_outputs": [],
                            },
                        }
                    ],
                    meta={
                        "skill_revision_ref": package.revision_ref,
                        "expected_outputs": [],
                    },
                )
                if shell_action_id not in execution.local_action_ids:
                    execution.local_action_ids.append(shell_action_id)
            bound.append(shell_action_id)
            shell_actions.append(shell_action_id)
    return bound, notes, custom_images, shell_actions

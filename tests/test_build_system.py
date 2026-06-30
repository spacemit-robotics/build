from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def write_file(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def make_sdk(tmp_path: Path) -> Path:
    sdk = tmp_path / "sdk"
    shutil.copytree(REPO_ROOT / "build", sdk / "build", ignore=shutil.ignore_patterns("tests"))
    (sdk / "target").mkdir(parents=True)
    write_file(
        sdk / "build" / "package.xml",
        """
        <?xml version="1.0"?>
        <package format="3">
          <name>spacemit_robot_build</name>
          <version>0.1.0</version>
          <description>test fixture</description>
          <system_depend check="true">base-tool</system_depend>
        </package>
        """,
    )
    return sdk


def write_package(
    sdk: Path,
    rel: str,
    *,
    name: str | None = None,
    depends: list[str] | None = None,
    sysdeps: list[tuple[str, str | None, str | None] | dict[str, str]] | None = None,
    build_type: str = "cmake",
    cmake: bool = False,
) -> None:
    depends = depends or []
    sysdeps = sysdeps or []
    dep_xml = "\n".join(f"  <depend>{dep}</depend>" for dep in depends)
    sysdep_xml = []
    for sysdep in sysdeps:
        attrs = []
        if isinstance(sysdep, dict):
            dep_name = sysdep["name"]
            check = sysdep.get("check")
            arch = sysdep.get("arch")
            realm = sysdep.get("realm")
            when = sysdep.get("when")
            check_kind = sysdep.get("check_kind")
            check_arg = sysdep.get("check_arg")
            option_key = sysdep.get("option_key")
            option_value = sysdep.get("option_value")
            board = sysdep.get("board")
        else:
            dep_name, check, arch = sysdep
            realm = None
            when = None
            check_kind = None
            check_arg = None
            option_key = None
            option_value = None
            board = None
        if check:
            attrs.append(f'check="{check}"')
        if arch:
            attrs.append(f'arch="{arch}"')
        if realm:
            attrs.append(f'realm="{realm}"')
        if when:
            attrs.append(f'when="{when}"')
        if check_kind:
            attrs.append(f'check_kind="{check_kind}"')
        if check_arg:
            attrs.append(f'check_arg="{check_arg}"')
        if option_key:
            attrs.append(f'option_key="{option_key}"')
        if option_value:
            attrs.append(f'option_value="{option_value}"')
        if board:
            attrs.append(f'board="{board}"')
        attr_text = (" " + " ".join(attrs)) if attrs else ""
        sysdep_xml.append(f"  <system_depend{attr_text}>{dep_name}</system_depend>")
    export_xml = f"  <export><build_type>{build_type}</build_type></export>" if build_type else ""
    write_file(
        sdk / rel / "package.xml",
        f"""
        <?xml version="1.0"?>
        <package format="3">
          <name>{name or Path(rel).name}</name>
          <version>0.1.0</version>
          <description>test fixture</description>
        {dep_xml}
        {chr(10).join(sysdep_xml)}
        {export_xml}
        </package>
        """,
    )
    if cmake:
        write_file(
            sdk / rel / "CMakeLists.txt",
            """
            cmake_minimum_required(VERSION 3.10)
            project(fixture)
            install(FILES CMakeLists.txt DESTINATION share/fixture)
            """,
        )


def logged_package_keys(log: str) -> list[str]:
    keys: list[str] = []
    for line in log.splitlines():
        if not line.startswith("cmake -S "):
            continue
        parts = line.split()
        if "-S" not in parts:
            continue
        src = parts[parts.index("-S") + 1]
        marker = "/sdk/"
        if marker in src:
            keys.append(src.split(marker, 1)[1])
    return keys


def make_fake_tools(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_state = tmp_path / "fake-state"
    fake_bin.mkdir()
    fake_state.mkdir()

    write_file(
        fake_bin / "apt-get",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo "apt-get $*" >> "${FAKE_TOOL_LOG}"
        if [[ "${1:-}" == "install" ]]; then
          shift
          for arg in "$@"; do
            [[ "${arg}" == -* ]] && continue
            echo "${arg}" >> "${FAKE_APT_DB}"
          done
        elif [[ "${1:-}" == "remove" ]]; then
          shift
          for arg in "$@"; do
            [[ "${arg}" == -* ]] && continue
            grep -vx -- "${arg}" "${FAKE_APT_DB}" > "${FAKE_APT_DB}.tmp" 2>/dev/null || true
            mv "${FAKE_APT_DB}.tmp" "${FAKE_APT_DB}"
          done
        fi
        exit 0
        """,
        executable=True,
    )
    write_file(
        fake_bin / "sudo",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "-n" ]]; then
          shift
        fi
        exec "$@"
        """,
        executable=True,
    )
    write_file(
        fake_bin / "dpkg",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "-s" ]]; then
          grep -qx -- "${2:-}" "${FAKE_APT_DB}" 2>/dev/null
          exit $?
        fi
        exit 0
        """,
        executable=True,
    )
    write_file(
        fake_bin / "dpkg-query",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        pkg="${@: -1}"
        if grep -qx -- "${pkg}" "${FAKE_APT_DB}" 2>/dev/null; then
          echo "install ok installed"
          exit 0
        fi
        exit 1
        """,
        executable=True,
    )
    write_file(
        fake_bin / "cmake",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo "cmake $*" >> "${FAKE_TOOL_LOG}"
        prev=""
        for arg in "$@"; do
          if [[ "${prev}" == "-B" ]]; then
            mkdir -p "${arg}"
          fi
          prev="${arg}"
        done
        if [[ "${1:-}" == "--install" ]]; then
          mkdir -p "${PREFIX}/share/fake"
          touch "${PREFIX}/share/fake/installed"
        fi
        exit 0
        """,
        executable=True,
    )
    write_file(
        fake_bin / "colcon",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo "colcon $*" >> "${FAKE_TOOL_LOG}"
        if [[ "${1:-}" == "list" ]]; then
          if [[ -n "${FAKE_COLCON_LIST_OUTPUT:-}" ]]; then
            printf "%s\\n" "${FAKE_COLCON_LIST_OUTPUT}"
            exit 0
          fi
          shift
          for arg in "$@"; do
            [[ "${arg}" == --* ]] && continue
            echo "${arg}"
          done
        fi
        exit 0
        """,
        executable=True,
    )
    write_file(
        fake_bin / "docker",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo "docker $*" >> "${FAKE_TOOL_LOG}"
        case "${1:-}" in
          info) exit 0 ;;
          image)
            [[ "${2:-}" == "inspect" ]] && exit 0
            ;;
          container)
            [[ "${2:-}" == "inspect" ]] && exit 1
            ;;
          run|exec|rm|start|tag|pull)
            exit 0
            ;;
        esac
        exit 0
        """,
        executable=True,
    )
    return fake_bin, fake_state


def run_cmd(
    sdk: Path,
    command: str,
    *,
    fake_bin: Path | None = None,
    fake_state: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "OUTPUT_ROOT": str(sdk / "output"),
            "STAGING_PREFIX": str(sdk / "output" / "staging"),
            "ROOTFS_PREFIX": str(sdk / "output" / "rootfs"),
            "PREFIX": str(sdk / "output" / "staging"),
            "LOG_ROOT": str(sdk / "output" / "log"),
            "PARALLEL_JOBS": "2",
            "LOG_SHOW_ENTER": "1",
            "LOG_SHOW_DONE": "1",
        }
    )
    if fake_bin is not None and fake_state is not None:
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env["FAKE_TOOL_LOG"] = str(fake_state / "tools.log")
        env["FAKE_APT_DB"] = str(fake_state / "apt.db")
        (fake_state / "apt.db").touch()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-lc", command],
        cwd=sdk,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def read_log(fake_state: Path) -> str:
    path = fake_state / "tools.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_cross_build_stub(sdk: Path) -> None:
    write_file(
        sdk / "build" / "cross_build.sh",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo "cross_build $*" >> "${FAKE_TOOL_LOG}"
        """,
        executable=True,
    )


def cross_deps_section(output: str, title: str) -> str:
    lines = output.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == title:
            start = idx + 1
            break
    if start is None:
        return ""
    collected = []
    for line in lines[start:]:
        if line.startswith("[cross] ") and line.strip().endswith(":"):
            break
        collected.append(line)
    return "\n".join(collected)


def test_package_with_deps_installs_dependency_system_dep_by_basename(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "application/native/reachy_mini", name="reachy_mini", depends=["vision"])
    write_package(
        sdk,
        "components/model_zoo/vision",
        name="cv",
        sysdeps=[("opencv-spacemit", None, "riscv64")],
        cmake=True,
    )

    result = run_cmd(
        sdk,
        "AUTO_INSTALL_DEPS=yes SDK_BUILD_ARCH=riscv64 "
        "SROBOTIS_IN_DOCKER_BUILD=1 SROBOTIS_DEPS_ONLY=1 "
        "./build/build.sh package application/native/reachy_mini --with-deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "opencv-spacemit" in (fake_state / "apt.db").read_text(encoding="utf-8")
    assert "Dependency-only check complete" in result.stdout


def test_package_without_lunch_builds_single_nonros2_package(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/standalone", name="standalone", cmake=True)

    result = run_cmd(
        sdk,
        "./build/build.sh package components/standalone",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    built = logged_package_keys(read_log(fake_state))
    assert built == ["components/standalone"]
    assert "BUILD_TARGET=" not in result.stdout


def test_target_package_build_keeps_mm_scoped_to_requested_package(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/peripherals/motor", name="motor", cmake=True)
    write_package(sdk, "components/target_only", name="target_only", cmake=True)
    write_file(
        sdk / "target" / "k3-mm.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/target_only"],
          "enabled_package_options": {
            "components/peripherals/motor": {
              "enabled_drivers": ["left"]
            }
          }
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 BUILD_TARGET=k3-mm "
        "./build/build.sh package components/peripherals/motor",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert logged_package_keys(log) == ["components/peripherals/motor"]
    assert "components/target_only" not in log
    assert "-DSROBOTIS_PERIPHERALS_MOTOR_ENABLED_DRIVERS=left" in log


def test_package_with_deps_builds_transitive_sdk_deps_and_checks_system_deps(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "application/native/app", name="app", depends=["mid"], cmake=True)
    write_package(sdk, "components/mid", name="mid", depends=["leaf"], sysdeps=[("mid-tool", None, None)], cmake=True)
    write_package(sdk, "components/leaf", name="leaf", sysdeps=[("leaf-tool", None, None)], cmake=True)

    result = run_cmd(
        sdk,
        "AUTO_INSTALL_DEPS=yes ./build/build.sh package application/native/app --with-deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    apt_db = (fake_state / "apt.db").read_text(encoding="utf-8")
    assert "mid-tool" in apt_db
    assert "leaf-tool" in apt_db
    built = logged_package_keys(read_log(fake_state))
    assert built == ["components/leaf", "components/mid", "application/native/app"]


def test_target_package_with_deps_uses_requested_package_dependency_root(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/app", name="app", depends=["mid"], cmake=True)
    write_package(sdk, "components/mid", name="mid", cmake=True)
    write_package(sdk, "components/target_only", name="target_only", cmake=True)
    write_file(
        sdk / "target" / "k3-mm-deps.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/target_only"]
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 BUILD_TARGET=k3-mm-deps "
        "./build/build.sh package components/app --with-deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert logged_package_keys(log) == ["components/mid", "components/app"]
    assert "components/target_only" not in log


def test_package_with_deps_filters_dependency_system_deps_by_arch(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/app", name="app", depends=["riscv_lib", "x86_lib"])
    write_package(
        sdk,
        "components/riscv_lib",
        name="riscv_lib",
        sysdeps=[("riscv-only-tool", None, "riscv64")],
        cmake=True,
    )
    write_package(
        sdk,
        "components/x86_lib",
        name="x86_lib",
        sysdeps=[("x86-only-tool", None, "x86_64")],
        cmake=True,
    )

    result = run_cmd(
        sdk,
        "AUTO_INSTALL_DEPS=yes SDK_BUILD_ARCH=riscv64 "
        "SROBOTIS_IN_DOCKER_BUILD=1 SROBOTIS_DEPS_ONLY=1 "
        "./build/build.sh package components/app --with-deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    apt_db = (fake_state / "apt.db").read_text(encoding="utf-8")
    assert "riscv-only-tool" in apt_db
    assert "x86-only-tool" not in apt_db


def test_package_dependency_system_deps_are_deduplicated(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/app", name="app", depends=["liba", "libb"])
    write_package(sdk, "components/liba", name="liba", sysdeps=[("shared-tool", None, None)], cmake=True)
    write_package(sdk, "components/libb", name="libb", sysdeps=[("shared-tool", None, None)], cmake=True)

    result = run_cmd(
        sdk,
        "AUTO_INSTALL_DEPS=yes SROBOTIS_IN_DOCKER_BUILD=1 SROBOTIS_DEPS_ONLY=1 "
        "./build/build.sh package components/app --with-deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    install_lines = [
        line for line in read_log(fake_state).splitlines()
        if line.startswith("apt-get install ") and "shared-tool" in line
    ]
    assert len(install_lines) == 1


def test_package_dependency_custom_check_must_pass_after_install(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(
        sdk,
        "components/app",
        name="app",
        sysdeps=[("broken-tool", "false", None)],
        cmake=True,
    )

    result = run_cmd(
        sdk,
        "AUTO_INSTALL_DEPS=yes ./build/build.sh package components/app",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode != 0
    assert "broken-tool still missing after installation" in result.stdout


def test_package_deps_builds_dependencies_but_not_root(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/app", name="app", depends=["liba"], cmake=True)
    write_package(sdk, "components/liba", name="liba", cmake=True)

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ./build/build.sh package components/app --deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "components/liba" in log
    assert "components/app" not in log


def test_package_deps_builds_transitive_dependencies_but_not_root(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/app", name="app", depends=["mid"], cmake=True)
    write_package(sdk, "components/mid", name="mid", depends=["leaf"], cmake=True)
    write_package(sdk, "components/leaf", name="leaf", cmake=True)

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ./build/build.sh package components/app --deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert logged_package_keys(read_log(fake_state)) == ["components/leaf", "components/mid"]


def test_package_with_deps_detects_dependency_cycle(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/a", name="a", depends=["b"], cmake=True)
    write_package(sdk, "components/b", name="b", depends=["a"], cmake=True)

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ./build/build.sh package components/a --deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode != 0
    assert "Dependency cycle detected" in result.stdout


def test_envsetup_cross_build_routes_m_to_cross_build(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_cross_build_stub(sdk)

    result = run_cmd(
        sdk,
        "source build/envsetup.sh >/dev/null; "
        "m_enable_cross_build >/dev/null; "
        "m -C",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "cross_build cmake" in read_log(fake_state)


def test_envsetup_cross_build_routes_mm_to_cross_build(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_cross_build_stub(sdk)
    write_package(sdk, "components/foo", name="foo", cmake=True)

    result = run_cmd(
        sdk,
        "source build/envsetup.sh >/dev/null; "
        "m_enable_cross_build >/dev/null; "
        "cd components/foo; "
        "mm --deps -DBUILD_TESTS=ON",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "cross_build --log=verbose package " in log
    assert " --deps" in log


def test_cross_build_deps_splits_global_and_package_sysdeps_by_realm(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_file(
        sdk / "build" / "package.xml",
        """
        <?xml version="1.0"?>
        <package format="3">
          <name>spacemit_robot_build</name>
          <version>0.1.0</version>
          <description>test fixture</description>
          <system_depend check_kind="command" check_arg="base-tool">base-tool</system_depend>
        </package>
        """,
    )
    write_package(
        sdk,
        "components/app",
        name="app",
        sysdeps=[
            {"name": "host-tool", "realm": "host", "check_kind": "command", "check_arg": "host-tool"},
            {"name": "target-lib", "check_kind": "pkg-config", "check_arg": "target-lib"},
        ],
        cmake=True,
    )

    result = run_cmd(sdk, "./build/cross_build.sh deps package components/app")

    assert result.returncode == 0, result.stdout
    host = cross_deps_section(result.stdout, "[cross] Host dependencies:")
    target = cross_deps_section(result.stdout, "[cross] Target sysroot dependencies:")
    assert "base-tool" in host
    assert "host-tool" in host
    assert "target-lib" not in host
    assert "target-lib" in target
    assert "base-tool" not in target


def test_cross_build_rejects_invalid_dependency_metadata(tmp_path: Path) -> None:
    cases = [
        ({"name": "bad-arch", "arch": "cross"}, "arch='cross' is not allowed"),
        ({"name": "bad-realm", "realm": "sysroot"}, "Invalid realm='sysroot'"),
        ({"name": "bad-when", "when": "sometimes"}, "Invalid when='sometimes'"),
        ({"name": "bad-check", "check_kind": "shell"}, "Invalid check_kind='shell'"),
    ]
    for idx, (sysdep, expected) in enumerate(cases):
        sdk = make_sdk(tmp_path / f"case_{idx}")
        write_package(sdk, "components/app", name="app", sysdeps=[sysdep], cmake=True)

        result = run_cmd(sdk, "./build/cross_build.sh deps package components/app")

        assert result.returncode != 0
        assert expected in result.stdout


def test_cross_build_same_name_host_and_target_deps_do_not_collide(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(
        sdk,
        "components/app",
        name="app",
        sysdeps=[
            {"name": "same-tool", "realm": "host", "check_kind": "command", "check_arg": "same-tool"},
            {"name": "same-tool", "realm": "target", "check_kind": "dpkg", "check_arg": "same-tool"},
        ],
        cmake=True,
    )

    result = run_cmd(sdk, "./build/cross_build.sh deps package components/app")

    assert result.returncode == 0, result.stdout
    host = cross_deps_section(result.stdout, "[cross] Host dependencies:")
    target = cross_deps_section(result.stdout, "[cross] Target sysroot dependencies:")
    assert "same-tool [required] check_kind=command" in host
    assert "same-tool [required] check_kind=dpkg" in target


def test_cross_build_deps_all_rejects_missing_target_package_path(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_file(
        sdk / "target" / "k3-missing-package.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/missing"]
        }
        """,
    )

    result = run_cmd(sdk, "BUILD_TARGET=k3-missing-package ./build/cross_build.sh deps all")

    assert result.returncode != 0
    assert "Package path not found: components/missing" in result.stdout


def test_cross_build_package_deps_collects_dependency_closure_sysdeps(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(sdk, "components/app", name="app", depends=["mid"], cmake=True)
    write_package(
        sdk,
        "components/mid",
        name="mid",
        sysdeps=[{"name": "mid-target-lib", "check_kind": "dpkg", "check_arg": "mid-target-lib"}],
        cmake=True,
    )

    result = run_cmd(sdk, "./build/cross_build.sh deps package components/app")

    assert result.returncode == 0, result.stdout
    target = cross_deps_section(result.stdout, "[cross] Target sysroot dependencies:")
    assert "mid-target-lib" in target


def test_cross_build_package_modes_match_native_dependency_scope(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(
        sdk,
        "components/app",
        name="app",
        depends=["mid"],
        sysdeps=[{"name": "app-target-lib", "check_kind": "dpkg", "check_arg": "app-target-lib"}],
        cmake=True,
    )
    write_package(
        sdk,
        "components/mid",
        name="mid",
        sysdeps=[{"name": "mid-target-lib", "check_kind": "dpkg", "check_arg": "mid-target-lib"}],
        cmake=True,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 bash -lc '"
        "source ./build/cross_build.sh; "
        "split_cross_dependencies package components/app; "
        "echo NORMAL; print_cross_deps_split; "
        "split_cross_dependencies package components/app --deps; "
        "echo ONLY_DEPS; print_cross_deps_split; "
        "split_cross_dependencies package components/app --with-deps; "
        "echo WITH_DEPS; print_cross_deps_split"
        "'",
    )

    assert result.returncode == 0, result.stdout
    normal = result.stdout.split("NORMAL", 1)[1].split("ONLY_DEPS", 1)[0]
    only_deps = result.stdout.split("ONLY_DEPS", 1)[1].split("WITH_DEPS", 1)[0]
    with_deps = result.stdout.split("WITH_DEPS", 1)[1]
    assert "app-target-lib" in normal
    assert "mid-target-lib" not in normal
    assert "app-target-lib" not in only_deps
    assert "mid-target-lib" in only_deps
    assert "app-target-lib" in with_deps
    assert "mid-target-lib" in with_deps


def test_cross_build_routes_reachy_mini_and_motor_cross_deps(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(
        sdk,
        "components/peripherals/motor",
        name="motor",
        sysdeps=[
            {"name": "libgpiod-dev", "check_kind": "pkg-config", "check_arg": "libgpiod"},
            {
                "name": "rustc-1.91",
                "when": "cross",
                "realm": "host",
                "check_kind": "command",
                "check_arg": "rustc-1.91",
            },
            {
                "name": "cargo-1.91",
                "when": "cross",
                "realm": "host",
                "check_kind": "command",
                "check_arg": "cargo-1.91",
            },
            {
                "name": "libstd-rust-1.91-dev",
                "when": "cross",
                "realm": "target",
                "arch": "riscv64",
                "check_kind": "file",
                "check_arg": "/usr/lib/rust-1.91/lib/rustlib/riscv64a23-unknown-linux-gnu/lib",
            },
        ],
        cmake=True,
    )
    write_package(
        sdk,
        "application/native/reachy_mini",
        name="reachy_mini",
        depends=["motor"],
        sysdeps=[
            {"name": "libprotobuf-dev", "realm": "target", "check_kind": "pkg-config", "check_arg": "protobuf"},
            {"name": "protobuf-compiler", "realm": "host", "check_kind": "command", "check_arg": "protoc"},
        ],
        cmake=True,
    )
    write_file(
        sdk / "target" / "k3-reachy.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["application/native/reachy_mini"]
        }
        """,
    )

    result = run_cmd(
        sdk,
        "BUILD_TARGET=k3-reachy ./build/cross_build.sh deps package application/native/reachy_mini",
    )

    assert result.returncode == 0, result.stdout
    host = cross_deps_section(result.stdout, "[cross] Host dependencies:")
    target = cross_deps_section(result.stdout, "[cross] Target sysroot dependencies:")
    assert "protobuf-compiler" in host
    assert "rustc-1.91" in host
    assert "cargo-1.91" in host
    assert "libprotobuf-dev" not in host
    assert "libstd-rust-1.91-dev" not in host
    assert "libprotobuf-dev" in target
    assert "libstd-rust-1.91-dev" in target
    assert "check_kind=file" in target
    assert "/usr/lib/rust-1.91/lib/rustlib/riscv64a23-unknown-linux-gnu/lib" in target
    assert "protobuf-compiler" not in target
    assert "rustc-1.91" not in target
    assert "cargo-1.91" not in target


def test_cross_build_filters_motor_rust_deps_by_enabled_driver(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(
        sdk,
        "components/peripherals/motor",
        name="motor",
        sysdeps=[
            {"name": "libgpiod-dev", "check_kind": "pkg-config", "check_arg": "libgpiod"},
            {
                "name": "rustc-1.91",
                "when": "cross",
                "realm": "host",
                "check_kind": "command",
                "check_arg": "rustc-1.91",
                "option_key": "enabled_drivers",
                "option_value": "drv_uart_xl330,uart_xl330",
            },
            {
                "name": "cargo-1.91",
                "when": "cross",
                "realm": "host",
                "check_kind": "command",
                "check_arg": "cargo-1.91",
                "option_key": "enabled_drivers",
                "option_value": "drv_uart_xl330,uart_xl330",
            },
            {
                "name": "libstd-rust-1.91-dev",
                "when": "cross",
                "realm": "target",
                "arch": "riscv64",
                "check_kind": "file",
                "check_arg": "/usr/lib/rust-1.91/lib/rustlib/riscv64a23-unknown-linux-gnu/lib",
                "option_key": "enabled_drivers",
                "option_value": "drv_uart_xl330,uart_xl330",
            },
        ],
        cmake=True,
    )
    write_file(
        sdk / "target" / "k1-pwm.json",
        """
        {
          "version": "1.0",
          "board": "k1",
          "enabled_packages": ["components/peripherals/motor"],
          "enabled_package_options": {
            "components/peripherals/motor": {
              "enabled_drivers": ["drv_pwm_generic", "drv_pwm_RoHS"]
            }
          }
        }
        """,
    )
    write_file(
        sdk / "target" / "k3-xl330.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/peripherals/motor"],
          "enabled_package_options": {
            "components/peripherals/motor": {
              "enabled_drivers": ["drv_uart_xl330"]
            }
          }
        }
        """,
    )

    k1_result = run_cmd(sdk, "BUILD_TARGET=k1-pwm ./build/cross_build.sh deps all")
    k3_result = run_cmd(sdk, "BUILD_TARGET=k3-xl330 ./build/cross_build.sh deps all")

    assert k1_result.returncode == 0, k1_result.stdout
    assert k3_result.returncode == 0, k3_result.stdout
    k1_host = cross_deps_section(k1_result.stdout, "[cross] Host dependencies:")
    k1_target = cross_deps_section(k1_result.stdout, "[cross] Target sysroot dependencies:")
    k3_host = cross_deps_section(k3_result.stdout, "[cross] Host dependencies:")
    k3_target = cross_deps_section(k3_result.stdout, "[cross] Target sysroot dependencies:")
    assert "rustc-1.91" not in k1_host
    assert "cargo-1.91" not in k1_host
    assert "libstd-rust-1.91-dev" not in k1_target
    assert "rustc-1.91" in k3_host
    assert "cargo-1.91" in k3_host
    assert "libstd-rust-1.91-dev" in k3_target


def test_cross_build_filters_sysdeps_by_board_family(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(
        sdk,
        "components/model_zoo/asr",
        name="asr",
        sysdeps=[
            {
                "name": "onnxruntime",
                "board": "k1",
                "arch": "riscv64",
                "realm": "target",
                "check_kind": "file",
                "check_arg": "/usr/include/onnxruntime_cxx_api.h",
            },
            {
                "name": "spacemit-onnxruntime",
                "board": "k3",
                "arch": "riscv64",
                "realm": "target",
                "check_kind": "file",
                "check_arg": "/usr/include/onnxruntime_cxx_api.h",
            },
        ],
        cmake=True,
    )
    write_file(
        sdk / "target" / "k1-asr.json",
        '{"version":"1.0","board":"k1","enabled_packages":["components/model_zoo/asr"]}',
    )
    write_file(
        sdk / "target" / "k3-asr.json",
        '{"version":"1.0","board":"k3-com260","enabled_packages":["components/model_zoo/asr"]}',
    )

    k1_result = run_cmd(sdk, "BUILD_TARGET=k1-asr ./build/cross_build.sh deps all")
    k3_result = run_cmd(sdk, "BUILD_TARGET=k3-asr ./build/cross_build.sh deps all")

    assert k1_result.returncode == 0, k1_result.stdout
    assert k3_result.returncode == 0, k3_result.stdout
    k1_target = cross_deps_section(k1_result.stdout, "[cross] Target sysroot dependencies:")
    k3_target = cross_deps_section(k3_result.stdout, "[cross] Target sysroot dependencies:")
    assert "  - onnxruntime " in k1_target
    assert "  - spacemit-onnxruntime " not in k1_target
    assert "  - spacemit-onnxruntime " in k3_target
    assert "  - onnxruntime " not in k3_target


def test_cross_build_runtime_deps_reports_sysroot_runtime_packages(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_file(
        sdk / "target" / "k1-app.json",
        '{"version":"1.0","board":"k1","enabled_packages":["components/app"]}',
    )
    cross_root = sdk / "output" / "cross" / "k1-app"
    write_file(cross_root / "rootfs" / "bin" / "app", "fake elf")
    write_file(cross_root / "rootfs" / "lib" / "libbundled.so.1", "fake elf")
    write_file(cross_root / "sysroot" / "usr" / "lib" / "riscv64-linux-gnu" / "libboard.so.1.2.3", "fake elf")
    (cross_root / "sysroot" / "usr" / "lib" / "riscv64-linux-gnu" / "libboard.so.1").symlink_to("libboard.so.1.2.3")
    write_file(
        fake_bin / "readelf",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        mode="${1:-}"
        path="${@: -1}"
        name="$(basename "${path}")"
        if [[ "${mode}" == "-h" ]]; then
          case "${name}" in
            app|libbundled.so.1) echo "ELF Header:"; exit 0 ;;
          esac
          exit 1
        fi
        if [[ "${mode}" == "-d" ]]; then
          case "${name}" in
            app)
              cat <<'EOF'
 0x0000000000000001 (NEEDED)             Shared library: [libboard.so.1]
 0x0000000000000001 (NEEDED)             Shared library: [libbundled.so.1]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
 0x000000000000001d (RUNPATH)            Library runpath: [$ORIGIN/../lib]
EOF
              exit 0
              ;;
            libbundled.so.1)
              cat <<'EOF'
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
EOF
              exit 0
              ;;
          esac
        fi
        exit 1
        """,
        executable=True,
    )
    write_file(
        fake_bin / "dpkg-query",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "$*" == *" -S "* ]]; then
          path="${@: -1}"
          case "${path}" in
            /usr/lib/riscv64-linux-gnu/libboard.so.1.2.3)
              echo "board-runtime: ${path}"
              exit 0
              ;;
            /lib/riscv64-linux-gnu/libc.so.6)
              echo "libc6:riscv64: ${path}"
              exit 0
              ;;
          esac
        fi
        if [[ "$*" == *"-W"* ]]; then
          pkg="${@: -1}"
          case "${pkg}" in
            board-runtime) echo "1.2.3-1"; exit 0 ;;
            libc6:riscv64|libc6) echo "2.39-0"; exit 0 ;;
          esac
        fi
        exit 1
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "BUILD_TARGET=k1-app ./build/cross_build.sh runtime-deps all",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "board-runtime (= 1.2.3-1)" in result.stdout
    assert "libboard.so.1 => /usr/lib/riscv64-linux-gnu/libboard.so.1.2.3" in result.stdout
    assert "libbundled.so.1 => rootfs/lib/libbundled.so.1" in result.stdout
    assert "apt-get install -y board-runtime" in result.stdout
    install_line = next(line for line in result.stdout.splitlines() if line.startswith("apt-get install -y "))
    assert "libbundled" not in install_line
    assert "libc6" not in install_line


def test_cross_build_runtime_deps_reports_unresolved_libraries(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_file(
        sdk / "target" / "k1-missing.json",
        '{"version":"1.0","board":"k1","enabled_packages":["components/app"]}',
    )
    cross_root = sdk / "output" / "cross" / "k1-missing"
    write_file(cross_root / "rootfs" / "bin" / "app", "fake elf")
    write_file(
        fake_bin / "readelf",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        mode="${1:-}"
        path="${@: -1}"
        if [[ "${mode}" == "-h" && "$(basename "${path}")" == "app" ]]; then
          echo "ELF Header:"
          exit 0
        fi
        if [[ "${mode}" == "-d" && "$(basename "${path}")" == "app" ]]; then
          echo ' 0x0000000000000001 (NEEDED)             Shared library: [libmissing.so.1]'
          exit 0
        fi
        exit 1
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "BUILD_TARGET=k1-missing ./build/cross_build.sh runtime-deps all",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "[cross] Unresolved runtime libraries:" in result.stdout
    assert "libmissing.so.1 required by rootfs/bin/app" in result.stdout


def test_cross_build_runtime_deps_forces_c_locale_for_readelf(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_file(
        sdk / "target" / "k1-locale.json",
        '{"version":"1.0","board":"k1","enabled_packages":["components/app"]}',
    )
    cross_root = sdk / "output" / "cross" / "k1-locale"
    write_file(cross_root / "rootfs" / "bin" / "app", "fake elf")
    write_file(cross_root / "sysroot" / "usr" / "lib" / "riscv64-linux-gnu" / "libboard.so.1.2.3", "fake elf")
    (cross_root / "sysroot" / "usr" / "lib" / "riscv64-linux-gnu" / "libboard.so.1").symlink_to("libboard.so.1.2.3")
    write_file(
        fake_bin / "readelf",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        mode="${1:-}"
        path="${@: -1}"
        if [[ "${mode}" == "-h" && "$(basename "${path}")" == "app" ]]; then
          echo "ELF Header:"
          exit 0
        fi
        if [[ "${mode}" == "-d" && "$(basename "${path}")" == "app" ]]; then
          if [[ "${LC_ALL:-}" == "C" ]]; then
            echo ' 0x0000000000000001 (NEEDED)             Shared library: [libboard.so.1]'
          else
            echo ' 0x0000000000000001 (NEEDED)             共享库：[libboard.so.1]'
          fi
          exit 0
        fi
        exit 1
        """,
        executable=True,
    )
    write_file(
        fake_bin / "dpkg-query",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "$*" == *" -S "* && "${@: -1}" == "/usr/lib/riscv64-linux-gnu/libboard.so.1.2.3" ]]; then
          echo "board-runtime: ${@: -1}"
          exit 0
        fi
        if [[ "$*" == *"-W"* && "${@: -1}" == "board-runtime" ]]; then
          echo "1.2.3-1"
          exit 0
        fi
        exit 1
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "LC_ALL=zh_CN.UTF-8 BUILD_TARGET=k1-locale ./build/cross_build.sh runtime-deps all",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "board-runtime (= 1.2.3-1)" in result.stdout


def test_native_dependency_check_filters_sysdeps_by_board_family(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(
        sdk,
        "components/app",
        name="app",
        sysdeps=[
            {"name": "k1-only-lib", "board": "k1", "check_kind": "dpkg", "check_arg": "k1-only-lib"},
            {"name": "k3-only-lib", "board": "k3", "check_kind": "dpkg", "check_arg": "k3-only-lib"},
        ],
        cmake=True,
    )
    write_file(
        sdk / "target" / "k1-app.json",
        '{"version":"1.0","board":"k1","enabled_packages":["components/app"]}',
    )

    result = run_cmd(
        sdk,
        "BUILD_TARGET=k1-app SROBOTIS_DEPS_ONLY=1 ./build/build.sh cmake",
    )

    assert result.returncode != 0
    assert "k1-only-lib" in result.stdout
    assert "k3-only-lib" not in result.stdout


def test_install_spacemit_onnxruntime_removes_legacy_onnxruntime(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    (fake_state / "apt.db").write_text("onnxruntime\n", encoding="utf-8")

    result = run_cmd(
        sdk,
        "AUTO_INSTALL_DEPS=yes bash -lc 'source ./build/common.sh; install_system_dependencies spacemit-onnxruntime'",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "apt-get remove -y onnxruntime" in log
    assert log.index("apt-get remove -y onnxruntime") < log.index("apt-get install -y spacemit-onnxruntime")
    apt_db = (fake_state / "apt.db").read_text(encoding="utf-8").splitlines()
    assert "onnxruntime" not in apt_db
    assert "spacemit-onnxruntime" in apt_db


def test_install_spacemit_onnxruntime_removes_unpacked_legacy_onnxruntime(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    (fake_state / "apt.db").write_text("onnxruntime\n", encoding="utf-8")
    write_file(
        fake_bin / "dpkg-query",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        pkg="${@: -1}"
        if [[ "${pkg}" == "onnxruntime" ]] && grep -qx -- "onnxruntime" "${FAKE_APT_DB}" 2>/dev/null; then
          echo "install ok unpacked"
          exit 0
        fi
        if grep -qx -- "${pkg}" "${FAKE_APT_DB}" 2>/dev/null; then
          echo "install ok installed"
          exit 0
        fi
        echo "install ok not-installed"
        exit 0
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "AUTO_INSTALL_DEPS=yes bash -lc 'source ./build/common.sh; install_system_dependencies spacemit-onnxruntime'",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "apt-get remove -y onnxruntime" in log
    assert log.index("apt-get remove -y onnxruntime") < log.index("apt-get install -y spacemit-onnxruntime")


def test_dpkg_check_requires_installed_status(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_state = tmp_path / "fake-state"
    fake_bin.mkdir()
    fake_state.mkdir()
    write_file(
        fake_bin / "dpkg",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "-s" ]]; then
          printf 'Package: %s\\nStatus: install ok not-installed\\n' "${2:-unknown}"
          exit 0
        fi
        exit 1
        """,
        executable=True,
    )
    write_file(
        fake_bin / "dpkg-query",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo "install ok not-installed"
        exit 0
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "bash -lc '"
        "source ./build/common.sh; "
        "check_cmd=$(deps_check_cmd_from_kind spacemit-onnxruntime dpkg spacemit-onnxruntime); "
        "if eval \"$check_cmd\"; then echo found; else echo missing; fi"
        "'",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "missing" in result.stdout
    assert "found" not in result.stdout


def test_motor_xl330_native_deps_include_rust_tools() -> None:
    k3_result = run_cmd(
        REPO_ROOT,
        "BUILD_TARGET=k3-com260-reachy-mini bash -lc '"
        "source ./build/common.sh; "
        "load_build_config >/dev/null; "
        "read_package_sysdeps_lines components/peripherals/motor"
        "'",
    )
    k1_result = run_cmd(
        REPO_ROOT,
        "BUILD_TARGET=k1-ai-cubpet bash -lc '"
        "source ./build/common.sh; "
        "load_build_config >/dev/null; "
        "read_package_sysdeps_lines components/peripherals/motor"
        "'",
    )

    assert k3_result.returncode == 0, k3_result.stdout
    assert k1_result.returncode == 0, k1_result.stdout
    assert "rustc-1.91" in k3_result.stdout
    assert "cargo-1.91" in k3_result.stdout
    assert "libstd-rust-1.91-dev" not in k3_result.stdout
    assert "rustc-1.91" not in k1_result.stdout
    assert "cargo-1.91" not in k1_result.stdout


def test_k1_ai_cubpet_cross_deps_use_k1_runtime_packages() -> None:
    result = run_cmd(REPO_ROOT, "BUILD_TARGET=k1-ai-cubpet ./build/cross_build.sh deps all")

    assert result.returncode == 0, result.stdout
    host = cross_deps_section(result.stdout, "[cross] Host dependencies:")
    target = cross_deps_section(result.stdout, "[cross] Target sysroot dependencies:")
    assert "  - qtbase5-dev-tools " in host
    assert "  - spacemit-onnxruntime " in target
    assert "  - onnxruntime " not in target
    assert "  - llama.cpp-tools-spacemit " not in target


def test_bianbu_23_containers_use_v23_updates_suite() -> None:
    docker_build = (REPO_ROOT / "build" / "docker_build.sh").read_text(encoding="utf-8")
    cross_build = (REPO_ROOT / "build" / "cross_build.sh").read_text(encoding="utf-8")

    assert "bianbu-v2.3-updates" in docker_build
    assert "bianbu-v2.3-updates" in cross_build
    assert "n=bianbu-v2.3-updates" in docker_build
    assert "n=bianbu-v2.3-updates" in cross_build


def test_cross_bianbu_install_removes_legacy_onnxruntime(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_state = tmp_path / "fake-state"
    fake_bin.mkdir()
    fake_state.mkdir()
    log = fake_state / "tools.log"
    write_file(
        fake_bin / "docker",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        echo "docker $*" >> {log}
        last_arg="${{@: -1}}"
        if [[ "${{1:-}}" == "exec" ]] && \\
          [[ "$*" == *'test "$(dpkg-query'* ]] && \\
          [[ "${{last_arg}}" == "spacemit-onnxruntime" ]]; then
          exit 1
        fi
        exit 0
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 PATH='{fake_bin}':$PATH bash -lc '"
        "source ./build/cross_build.sh; "
        "line=\"required${{CROSS_DEP_SEP}}spacemit-onnxruntime"
        "${{CROSS_DEP_SEP}}dpkg${{CROSS_DEP_SEP}}spacemit-onnxruntime\"; "
        "install_deps_in_container fake-bianbu \"Bianbu sysroot\" \"$line\""
        "'".format(fake_bin=fake_bin),
    )

    assert result.returncode == 0, result.stdout
    log_text = log.read_text(encoding="utf-8")
    assert "apt-get remove -y onnxruntime" in log_text
    assert log_text.index("apt-get remove -y onnxruntime") < log_text.index("apt-get update && apt-get install -y")


def test_cross_build_selects_ubuntu_image_by_target_family(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 bash -lc '"
        "source ./build/cross_build.sh; "
        "BUILD_TARGET=k1-muse-pipro-minimal; select_cross_images; "
        "printf \"k1 %s %s\\n\" \"${CROSS_UBUNTU_IMAGE}\" \"${CROSS_BIANBU_IMAGE}\"; "
        "BUILD_TARGET=k3-com260-reachy-mini; select_cross_images; "
        "printf \"k3 %s %s\\n\" \"${CROSS_UBUNTU_IMAGE}\" \"${CROSS_BIANBU_IMAGE}\""
        "'",
    )

    assert result.returncode == 0, result.stdout
    assert "k1 ubuntu:24.04 bianbu:2.3" in result.stdout
    assert "k3 ubuntu:26.04 bianbu:4.0" in result.stdout


def test_cross_bianbu_image_pulls_from_harbor_when_short_name_missing(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_state = tmp_path / "fake-state"
    fake_bin.mkdir()
    fake_state.mkdir()
    log = fake_state / "tools.log"
    write_file(
        fake_bin / "docker",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        echo "docker $*" >> {log}
        case "${{1:-}}" in
          image)
            [[ "${{2:-}}" == "inspect" ]] && exit 1
            ;;
          pull|tag)
            exit 0
            ;;
        esac
        exit 0
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-com260-reachy-mini "
        "PATH='{fake_bin}':$PATH bash -lc '"
        "source ./build/cross_build.sh; "
        "select_cross_images; ensure_cross_bianbu_image; printf \"%s\" \"${{CROSS_BIANBU_IMAGE}}\""
        "'".format(fake_bin=fake_bin),
    )

    assert result.returncode == 0, result.stdout
    assert result.stdout.rstrip().endswith("bianbu:4.0")
    log_text = log.read_text(encoding="utf-8")
    assert "docker image inspect bianbu:4.0" in log_text
    assert "docker pull harbor.spacemit.com/bianbu/bianbu:4.0" in log_text
    assert "docker tag harbor.spacemit.com/bianbu/bianbu:4.0 bianbu:4.0" in log_text


def test_cross_container_name_changes_when_selected_image_changes(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-com260-reachy-mini bash -lc '"
        "source ./build/cross_build.sh; "
        "CROSS_UBUNTU_IMAGE=ubuntu:24.04; cross_container_name ubuntu; "
        "CROSS_UBUNTU_IMAGE=ubuntu:26.04; cross_container_name ubuntu"
        "'",
    )

    assert result.returncode == 0, result.stdout
    names = result.stdout.splitlines()
    assert len(names) == 2
    assert names[0] != names[1]
    assert all(name.startswith("srobotis-cross-ubuntu-") for name in names)


def test_cross_container_recreates_existing_container_without_repo_mount(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_state = tmp_path / "fake-state"
    fake_bin.mkdir()
    fake_state.mkdir()
    log = fake_state / "tools.log"
    write_file(
        fake_bin / "docker",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        echo "docker $*" >> {log}
        case "${{1:-}}" in
          container)
            [[ "${{2:-}}" == "inspect" ]] && exit 0
            ;;
          start)
            exit 0
            ;;
          exec)
            if [[ "$*" == *"test -f"* ]]; then
              exit 1
            fi
            exit 0
            ;;
          rm|run)
            exit 0
            ;;
        esac
        exit 0
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-stale bash -lc '"
        "source ./build/cross_build.sh; "
        "init_cross_paths; "
        "ensure_container stale-cross-container ubuntu:26.04"
        "'",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log_text = log.read_text(encoding="utf-8")
    assert "docker exec" in log_text
    assert "docker rm -f stale-cross-container" in log_text
    assert "docker run -d --name stale-cross-container" in log_text


def test_cross_configures_bianbu_23_ros_apt_suite(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 bash -lc '"
        "source ./build/cross_build.sh; "
        "CROSS_BIANBU_TAG=2.3; "
        "configure_bianbu_container fake-bianbu"
        "'",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "docker exec" in log
    assert "noble-ros" in log
    assert "/etc/apt/sources.list.d/bianbu.sources" in log


def test_cross_does_not_configure_bianbu_40_ros_apt_suite(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 bash -lc '"
        "source ./build/cross_build.sh; "
        "CROSS_BIANBU_TAG=4.0; "
        "configure_bianbu_container fake-bianbu"
        "'",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "noble-ros" not in read_log(fake_state)


def test_cross_cmake_args_use_versioned_rust_tools(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    rustlib = (
        sdk
        / "output"
        / "cross"
        / "k3-rust"
        / "sysroot"
        / "usr"
        / "lib"
        / "rust-1.91"
        / "lib"
        / "rustlib"
        / "riscv64a23-unknown-linux-gnu"
        / "lib"
        / "libcore-test.rlib"
    )
    write_file(rustlib, "fake rust core")
    (sdk / "output" / "cross" / "k3-rust" / "sysroot" / "usr" / "include" / "python3.14").mkdir(parents=True)

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-rust "
        "bash -lc 'source ./build/cross_build.sh; init_cross_paths; cross_cmake_extra_args'",
    )

    assert result.returncode == 0, result.stdout
    assert "-DCARGO_EXECUTABLE=/usr/bin/cargo-1.91" in result.stdout
    assert "-DSROBOTIS_CARGO_RUSTC=/usr/bin/rustc-1.91" in result.stdout
    assert "-DSROBOTIS_CARGO_TARGET=riscv64a23-unknown-linux-gnu" in result.stdout
    assert "-Wl,--allow-shlib-undefined" in result.stdout


def test_cross_cmake_args_disable_non_cpp_rosidl_generators(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    sysroot = sdk / "output" / "cross" / "k3-rosidl" / "sysroot"
    (sysroot / "usr" / "include" / "python3.14").mkdir(parents=True)

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-rosidl "
        "bash -lc 'source ./build/cross_build.sh; init_cross_paths; cross_cmake_extra_args'",
    )

    assert result.returncode == 0, result.stdout
    assert "-DCMAKE_DISABLE_FIND_PACKAGE_rosidl_generator_py=TRUE" in result.stdout
    assert "-DCMAKE_DISABLE_FIND_PACKAGE_rosidl_generator_rs=TRUE" in result.stdout


def test_cross_cmake_args_disable_build_testing(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    sysroot = sdk / "output" / "cross" / "k3-no-tests" / "sysroot"
    (sysroot / "usr" / "include").mkdir(parents=True)
    (sysroot / "usr" / "lib").mkdir(parents=True)

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-no-tests "
        "bash -lc 'source ./build/cross_build.sh; init_cross_paths; cross_cmake_extra_args'",
    )

    assert result.returncode == 0, result.stdout
    assert "-DBUILD_TESTING=OFF" in result.stdout


def test_cross_cmake_args_pass_meson_cross_file(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    sysroot = sdk / "output" / "cross" / "k1-meson" / "sysroot"
    (sysroot / "usr" / "include").mkdir(parents=True)
    (sysroot / "usr" / "lib").mkdir(parents=True)

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k1-meson "
        "bash -lc 'source ./build/cross_build.sh; init_cross_paths; cross_cmake_extra_args'",
    )

    assert result.returncode == 0, result.stdout
    assert "-DSROBOTIS_MESON_CROSS_FILE=" in result.stdout
    assert "meson-riscv64.ini" in result.stdout


def test_cross_cmake_args_pass_host_qt_tool_overrides(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    sysroot = sdk / "output" / "cross" / "k1-qt" / "sysroot"
    (sysroot / "usr" / "include").mkdir(parents=True)
    (sysroot / "usr" / "lib").mkdir(parents=True)

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k1-qt "
        "bash -lc 'source ./build/cross_build.sh; init_cross_paths; cross_cmake_extra_args'",
    )

    assert result.returncode == 0, result.stdout
    assert "-DSROBOTIS_QT_MOC_EXECUTABLE=/usr/lib/qt5/bin/moc" in result.stdout
    assert "-DSROBOTIS_QT_UIC_EXECUTABLE=/usr/lib/qt5/bin/uic" in result.stdout
    assert "-DSROBOTIS_QT_RCC_EXECUTABLE=/usr/lib/qt5/bin/rcc" in result.stdout


def test_cross_toolchain_uses_host_compiler_absolute_paths(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k1-toolchain "
        "bash -lc 'source ./build/cross_build.sh; init_cross_paths; "
        "write_toolchain_file; cat \"${CROSS_TOOLCHAIN_FILE}\"'",
    )

    assert result.returncode == 0, result.stdout
    assert "set(CMAKE_C_COMPILER /usr/bin/riscv64-linux-gnu-gcc)" in result.stdout
    assert "set(CMAKE_CXX_COMPILER /usr/bin/riscv64-linux-gnu-g++)" in result.stdout
    assert "set(CMAKE_AR /usr/bin/riscv64-linux-gnu-ar)" in result.stdout
    assert "set(CMAKE_RANLIB /usr/bin/riscv64-linux-gnu-ranlib)" in result.stdout


def test_cross_absolute_sysroot_bridge_skips_cmake_package_dirs(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    sysroot = sdk / "output" / "cross" / "k3-yaml" / "sysroot"
    cmake_dir = sysroot / "opt" / "ros" / "humble" / "share" / "fastrtps" / "cmake"
    (sysroot / "usr" / "lib" / "riscv64-linux-gnu" / "cmake").mkdir(parents=True)
    write_file(
        cmake_dir / "yaml-cpp-targets.cmake",
        """
        set(_realOrig "/usr/lib/riscv64-linux-gnu/cmake")
        set_target_properties(example PROPERTIES
          INTERFACE_INCLUDE_DIRECTORIES "/usr/lib/riscv64-linux-gnu/python3-numpy/numpy/_core/include")
        set_target_properties(example_py PROPERTIES
          INTERFACE_LINK_LIBRARIES "example;/usr/lib/riscv64-linux-gnu/libpython3.14.so;other")
        """,
    )
    (sysroot / "usr" / "lib" / "riscv64-linux-gnu" / "python3-numpy" / "numpy" / "_core" / "include").mkdir(
        parents=True
    )
    write_file(sysroot / "usr" / "lib" / "riscv64-linux-gnu" / "libpython3.14.so", "fake libpython")

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-yaml "
        "bash -lc 'source ./build/cross_build.sh; init_cross_paths; cross_collect_absolute_sysroot_paths'",
    )

    assert result.returncode == 0, result.stdout
    lines = result.stdout.splitlines()
    assert "/usr/lib/riscv64-linux-gnu/cmake" not in lines
    assert "/usr/lib/riscv64-linux-gnu/python3-numpy/numpy/_core/include" in lines
    assert "/usr/lib/riscv64-linux-gnu/libpython3.14.so" in lines


def test_nfc_ros_package_declares_sdk_underlay_dependency() -> None:
    result = subprocess.run(
        [
            "bash",
            "-lc",
            "source ./build/common.sh; read_ros2_sdk_nonros2_deps middleware/ros2/peripherals/nfc",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    assert "components/peripherals/nfc" in result.stdout.splitlines()


def test_nfc_component_owns_package_manifest() -> None:
    package_xml = REPO_ROOT / "components" / "peripherals" / "nfc" / "package.xml"

    assert package_xml.exists()
    content = package_xml.read_text(encoding="utf-8")
    assert "<name>nfc</name>" in content
    assert "<build_type>cmake</build_type>" in content


def test_cross_ros_package_with_deps_preserves_toolchain_args_for_sdk_underlay(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/peripherals/nfc", name="nfc", cmake=True)
    write_package(
        sdk,
        "middleware/ros2/peripherals/nfc",
        name="peripherals_nfc_node",
        depends=["nfc"],
        build_type="ament_cmake",
    )
    write_file(sdk / "fake_ros_setup.sh", "export ROS_FAKE_SETUP=1\n")

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 SROBOTIS_CROSS_BUILD=1 "
        "SROBOTIS_CMAKE_EXTRA_ARGS='-DCMAKE_TOOLCHAIN_FILE=/tmp/cross-toolchain.cmake' "
        "ROS_SETUP=\"$PWD/fake_ros_setup.sh\" "
        "./build/build.sh package middleware/ros2/peripherals/nfc --with-deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    underlay_cmake_lines = [
        line
        for line in log.splitlines()
        if line.startswith("cmake -S ") and "components/peripherals/nfc" in line
    ]
    assert underlay_cmake_lines
    assert "-DCMAKE_TOOLCHAIN_FILE=/tmp/cross-toolchain.cmake" in underlay_cmake_lines[0]


def test_uart_xl330_cargo_build_passes_cross_rustflags(tmp_path: Path) -> None:
    fake_cargo = tmp_path / "fake-cargo"
    env_log = tmp_path / "cargo-env.log"
    args_log = tmp_path / "cargo-args.log"
    write_file(
        fake_cargo,
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "${{RUSTFLAGS:-}}" > {env_log}
        printf '%s\\n' "$*" > {args_log}
        """,
        executable=True,
    )
    build_dir = tmp_path / "build"
    target_dir = tmp_path / "cargo-target"
    rust_sysroot = tmp_path / "sysroot" / "usr" / "lib" / "rust-1.91"
    link_sysroot = tmp_path / "sysroot"
    target_dir.mkdir()
    rust_sysroot.mkdir(parents=True)

    configure = subprocess.run(
        [
            "cmake",
            "-S",
            str(REPO_ROOT / "components" / "peripherals" / "motor" / "src" / "drivers" / "drv_uart_xl330"),
            "-B",
            str(build_dir),
            f"-DCARGO_EXECUTABLE={fake_cargo}",
            f"-DRUST_TARGET_DIR={target_dir}",
            "-DSROBOTIS_CARGO_TARGET=riscv64a23-unknown-linux-gnu",
            f"-DSROBOTIS_RUST_SYSROOT={rust_sysroot}",
            f"-DSROBOTIS_CARGO_LINK_SYSROOT={link_sysroot}",
            "-DBUILD_TESTS=OFF",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert configure.returncode == 0, configure.stdout

    build = subprocess.run(
        ["cmake", "--build", str(build_dir), "--target", "uart_xl330_cargo_build"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert build.returncode == 0, build.stdout
    assert f"--sysroot={rust_sysroot}" in env_log.read_text(encoding="utf-8")
    assert f"link-arg=--sysroot={link_sysroot}" in env_log.read_text(encoding="utf-8")
    assert "--target riscv64a23-unknown-linux-gnu" in args_log.read_text(encoding="utf-8")


def test_uart_xl330_cargo_build_falls_back_to_versioned_rust_tools(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    env_log = tmp_path / "cargo-env.log"
    argv0_log = tmp_path / "cargo-argv0.log"
    fake_cargo = fake_bin / "cargo-1.91"
    fake_rustc = fake_bin / "rustc-1.91"
    write_file(
        fake_cargo,
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$0" > {argv0_log}
        printf '%s\\n' "${{RUSTC:-}}" > {env_log}
        """,
        executable=True,
    )
    write_file(
        fake_rustc,
        """
        #!/usr/bin/env bash
        exit 0
        """,
        executable=True,
    )
    build_dir = tmp_path / "build"
    target_dir = tmp_path / "cargo-target"
    target_dir.mkdir()
    cmake_bin = shutil.which("cmake") or "cmake"
    test_env = os.environ.copy()
    test_env["PATH"] = f"{fake_bin}:/usr/bin:/bin"

    configure = subprocess.run(
        [
            cmake_bin,
            "-S",
            str(REPO_ROOT / "components" / "peripherals" / "motor" / "src" / "drivers" / "drv_uart_xl330"),
            "-B",
            str(build_dir),
            f"-DRUST_TARGET_DIR={target_dir}",
            "-DBUILD_TESTS=OFF",
        ],
        env=test_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert configure.returncode == 0, configure.stdout

    build = subprocess.run(
        [cmake_bin, "--build", str(build_dir), "--target", "uart_xl330_cargo_build"],
        env=test_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert build.returncode == 0, build.stdout
    assert argv0_log.read_text(encoding="utf-8").strip() == str(fake_cargo)
    assert env_log.read_text(encoding="utf-8").strip() == str(fake_rustc)


def test_vision_cmake_preserves_external_opencv_dir() -> None:
    cmake_lists = (REPO_ROOT / "components" / "model_zoo" / "vision" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )

    assert 'if(NOT DEFINED OpenCV_DIR)' in cmake_lists
    assert 'set(OpenCV_DIR "/opt/opencv-spacemit/lib/cmake/opencv4")' in cmake_lists


def test_vision_cmake_defaults_examples_and_tests_off_for_cross_builds() -> None:
    cmake_lists = (REPO_ROOT / "components" / "model_zoo" / "vision" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )

    assert "if(CMAKE_CROSSCOMPILING)" in cmake_lists
    assert "set(_vision_build_examples_default OFF)" in cmake_lists
    assert "set(_vision_build_tests_default OFF)" in cmake_lists
    assert 'option(BUILD_EXAMPLES "Build example programs" ${_vision_build_examples_default})' in cmake_lists
    assert 'option(BUILD_TESTS "Build tests" ${_vision_build_tests_default})' in cmake_lists


def test_vad_wheel_install_dir_falls_back_to_output_root() -> None:
    cmake_lists = (REPO_ROOT / "components" / "model_zoo" / "vad" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )

    assert "$ENV{OUTPUT_ROOT}" in cmake_lists
    assert "CMAKE_INSTALL_PREFIX}/.." in cmake_lists
    assert 'set(VAD_DIST_INSTALL_DIR "$ENV{SROBOTIS_OUTPUT_ROOT}/dist")' not in cmake_lists


def test_cross_build_chowns_output_after_failed_ubuntu_build(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_state = tmp_path / "fake-state"
    fake_bin.mkdir()
    fake_state.mkdir()
    log = fake_state / "tools.log"
    write_file(
        fake_bin / "docker",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        echo "docker $*" >> {log}
        if [[ "${{1:-}}" == "exec" && "$*" == *"./build/build.sh"* ]]; then
          exit 7
        fi
        exit 0
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-fail "
        "PATH='{fake_bin}':$PATH bash -lc 'source ./build/cross_build.sh; "
        "init_cross_paths; run_build_in_ubuntu fake-container cmake'".format(
            fake_bin=fake_bin
        ),
    )

    assert result.returncode == 7, result.stdout
    assert "chown -R" in log.read_text(encoding="utf-8")


def test_cross_build_passes_parallel_jobs_to_ubuntu_build(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_state = tmp_path / "fake-state"
    fake_bin.mkdir()
    fake_state.mkdir()
    log = fake_state / "tools.log"
    write_file(
        fake_bin / "docker",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        echo "docker $*" >> {log}
        exit 0
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-jobs PARALLEL_JOBS=7 "
        "SROBOTIS_PARALLEL_JOBS_EXPLICIT=1 PATH='{fake_bin}':$PATH bash -lc '"
        "source ./build/cross_build.sh; init_cross_paths; run_build_in_ubuntu fake-container cmake"
        "'".format(fake_bin=fake_bin),
    )

    assert result.returncode == 0, result.stdout
    docker_log = log.read_text(encoding="utf-8")
    assert " -e PARALLEL_JOBS=7 " in docker_log
    assert " -e SROBOTIS_PARALLEL_JOBS_EXPLICIT=1 " in docker_log


def test_cross_build_passes_sysroot_pythonpath_to_ubuntu_build(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_state = tmp_path / "fake-state"
    fake_bin.mkdir()
    fake_state.mkdir()
    log = fake_state / "tools.log"
    sysroot = sdk / "output" / "cross" / "k3-py" / "sysroot"
    (sysroot / "usr" / "lib" / "python3" / "dist-packages").mkdir(parents=True)
    (sysroot / "opt" / "ros" / "humble" / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
    (sysroot / "opt" / "ros" / "humble" / "lib" / "python3" / "dist-packages").mkdir(parents=True)
    write_file(
        fake_bin / "docker",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        echo "docker $*" >> {log}
        exit 0
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-py "
        "PATH='{fake_bin}':$PATH bash -lc 'source ./build/cross_build.sh; "
        "init_cross_paths; run_build_in_ubuntu fake-container ros2'".format(
            fake_bin=fake_bin
        ),
    )

    assert result.returncode == 0, result.stdout
    docker_log = log.read_text(encoding="utf-8")
    staging = sdk / "output" / "cross" / "k3-py" / "staging"
    assert " -e PYTHONPATH=" in docker_log
    assert f"{sysroot}/usr/lib/python3/dist-packages" in docker_log
    assert f"{sysroot}/opt/ros/humble/lib/python3.14/site-packages" in docker_log
    assert f"{sysroot}/opt/ros/humble/lib/python3/dist-packages" in docker_log
    assert f"-e AMENT_PREFIX_PATH={staging}:{sysroot}/usr:{sysroot}/opt/ros/humble" in docker_log
    assert f"-e CMAKE_PREFIX_PATH={staging}:{sysroot}/usr:{sysroot}/opt/ros/humble" in docker_log


def test_cross_build_bridges_absolute_sysroot_paths_before_ubuntu_build(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_state = tmp_path / "fake-state"
    fake_bin.mkdir()
    fake_state.mkdir()
    log = fake_state / "tools.log"
    sysroot = sdk / "output" / "cross" / "k3-abs-paths" / "sysroot"
    cmake_dir = sysroot / "opt" / "ros" / "humble" / "share" / "std_msgs" / "cmake"
    numpy_include = sysroot / "usr" / "lib" / "riscv64-linux-gnu" / "python3-numpy" / "numpy" / "_core" / "include"
    numpy_include.mkdir(parents=True)
    write_file(
        cmake_dir / "export_std_msgs__rosidl_generator_pyExport.cmake",
        """
        set_target_properties(std_msgs__rosidl_generator_py PROPERTIES
          INTERFACE_INCLUDE_DIRECTORIES "/usr/lib/riscv64-linux-gnu/python3-numpy/numpy/_core/include")
        """,
    )
    write_file(
        fake_bin / "docker",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        echo "docker $*" >> {log}
        exit 0
        """,
        executable=True,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-abs-paths "
        "PATH='{fake_bin}':$PATH bash -lc 'source ./build/cross_build.sh; "
        "init_cross_paths; run_build_in_ubuntu fake-container ros2'".format(
            fake_bin=fake_bin
        ),
    )

    assert result.returncode == 0, result.stdout
    docker_log = log.read_text(encoding="utf-8")
    assert "python3-numpy/numpy/_core/include" in docker_log
    assert "ln -s" in docker_log
    assert "-type l" in docker_log
    assert "-xtype l" not in docker_log


def test_cross_absolute_sysroot_path_collection_skips_cmake_package_dirs(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    sysroot = sdk / "output" / "cross" / "k3-cmake-bridge" / "sysroot"
    cmake_dir = sysroot / "opt" / "ros" / "humble" / "share" / "fastrtps" / "cmake"
    (sysroot / "usr" / "lib" / "riscv64-linux-gnu" / "cmake" / "tinyxml2").mkdir(parents=True)
    (sysroot / "usr" / "lib" / "riscv64-linux-gnu" / "python3-numpy" / "numpy" / "_core" / "include").mkdir(
        parents=True
    )
    write_file(
        cmake_dir / "fastrtps-config.cmake",
        """
        set(_realOrig "/usr/lib/riscv64-linux-gnu/cmake/tinyxml2")
        set_target_properties(example PROPERTIES
          INTERFACE_INCLUDE_DIRECTORIES "/usr/lib/riscv64-linux-gnu/python3-numpy/numpy/_core/include")
        set_target_properties(example_py PROPERTIES
          INTERFACE_LINK_LIBRARIES "example;/usr/lib/riscv64-linux-gnu/libpython3.14.so;other")
        """,
    )
    write_file(sysroot / "usr" / "lib" / "riscv64-linux-gnu" / "libpython3.14.so", "fake libpython")

    result = run_cmd(
        sdk,
        "SROBOTIS_CROSS_BUILD_SH_NO_MAIN=1 BUILD_TARGET=k3-cmake-bridge "
        "bash -lc 'source ./build/cross_build.sh; init_cross_paths; cross_collect_absolute_sysroot_paths'",
    )

    assert result.returncode == 0, result.stdout
    lines = result.stdout.splitlines()
    assert "/usr/lib/riscv64-linux-gnu/python3-numpy/numpy/_core/include" in lines
    assert "/usr/lib/riscv64-linux-gnu/libpython3.14.so" in lines
    assert "/usr/lib/riscv64-linux-gnu/cmake/tinyxml2" not in lines


def test_reachy_mini_uses_cross_staging_and_forwards_cross_args() -> None:
    cmake_lists = (REPO_ROOT / "application" / "native" / "reachy_mini" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )

    assert 'set(STAGING_DIR "${CMAKE_INSTALL_PREFIX}"' in cmake_lists
    assert "SROBOTIS_CMAKE_EXTRA_ARGS" in cmake_lists
    assert "${_external_project_cross_args}" in cmake_lists


def test_ai_cubpet_thirdparty_builds_forward_cross_args() -> None:
    cyclonedds = (REPO_ROOT / "application" / "native" / "ai-cubpet" / "cmake" / "cyclonedds.cmake").read_text(
        encoding="utf-8"
    )
    webrtc = (
        REPO_ROOT / "application" / "native" / "ai-cubpet" / "cmake" / "webrtc_audio_processing.cmake"
    ).read_text(encoding="utf-8")

    assert "SROBOTIS_CMAKE_EXTRA_ARGS" in cyclonedds
    assert "ENV{SROBOTIS_CMAKE_EXTRA_ARGS}" in cyclonedds
    assert 'string(REPLACE ";" "\\\\;"' in cyclonedds
    assert "_AI_CUBPET_EXTERNAL_PROJECT_CMAKE_ARGS" in cyclonedds
    assert "${_AI_CUBPET_EXTERNAL_PROJECT_CMAKE_ARGS}" in cyclonedds
    assert "SROBOTIS_CROSS_HOST_PREFIX" in cyclonedds
    assert "-DCMAKE_C_COMPILER=/usr/bin/cc" in cyclonedds
    assert "-DCMAKE_CXX_COMPILER=/usr/bin/c++" in cyclonedds
    assert "-DCMAKE_MAKE_PROGRAM=/usr/bin/make" in cyclonedds
    assert "-DBUILD_IDLC=ON" in cyclonedds
    assert "-DBUILD_IDLLIB=ON" in cyclonedds
    assert "SROBOTIS_MESON_CROSS_FILE" in webrtc
    assert "--cross-file" in webrtc


def test_ai_cubpet_overrides_qt_tools_for_cross_builds() -> None:
    cmake_lists = (REPO_ROOT / "application" / "native" / "ai-cubpet" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )

    assert "SROBOTIS_QT_MOC_EXECUTABLE" in cmake_lists
    assert "SROBOTIS_QT_UIC_EXECUTABLE" in cmake_lists
    assert "SROBOTIS_QT_RCC_EXECUTABLE" in cmake_lists
    assert "Qt5::${tool_target}" in cmake_lists
    assert "ai_cubpet_set_qt_host_tool(moc SROBOTIS_QT_MOC_EXECUTABLE)" in cmake_lists
    assert "ai_cubpet_set_qt_host_tool(uic SROBOTIS_QT_UIC_EXECUTABLE)" in cmake_lists
    assert "ai_cubpet_set_qt_host_tool(rcc SROBOTIS_QT_RCC_EXECUTABLE)" in cmake_lists
    assert "IMPORTED_LOCATION" in cmake_lists


def test_docker_wrapper_runs_deps_only_then_skip_deps_build(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)
    write_file(
        sdk / "target" / "k3-test.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "product": "fixture",
          "enabled_packages": ["components/foo"],
          "options": {"auto_resolve_dependencies": true}
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_USE_DOCKER_BUILD=1 BUILD_TARGET=k3-test "
        "./build/build.sh package components/foo",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "docker run -d --name" in log
    assert "bianbu:4.0" in log
    assert "SROBOTIS_DEPS_ONLY=1" in log
    assert "SROBOTIS_SKIP_DEPS_CHECK=1" in log


def test_docker_wrapper_wraps_target_all_build(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)
    write_file(
        sdk / "target" / "k3-all.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/foo"],
          "options": {"auto_resolve_dependencies": true}
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_USE_DOCKER_BUILD=1 BUILD_TARGET=k3-all ./build/build.sh all",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "docker run -d --name" in log
    assert "bianbu:4.0" in log
    assert "SROBOTIS_DEPS_ONLY=1" in log
    assert "SROBOTIS_SKIP_DEPS_CHECK=1" in log


def test_docker_wrapper_selects_k1_bianbu_23_image(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)
    write_file(
        sdk / "target" / "k1-test.json",
        """
        {
          "version": "1.0",
          "board": "k1-x",
          "product": "fixture",
          "enabled_packages": ["components/foo"],
          "options": {"auto_resolve_dependencies": true}
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_USE_DOCKER_BUILD=1 BUILD_TARGET=k1-test "
        "./build/build.sh package components/foo",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "bianbu:2.3" in read_log(fake_state)


def test_docker_wrapper_requires_k1_or_k3_target(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)

    result = run_cmd(
        sdk,
        "SROBOTIS_USE_DOCKER_BUILD=1 BUILD_TARGET=unknown "
        "./build/build.sh package components/foo",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode != 0
    assert "Cannot select Bianbu Docker image without a k1/k3 target" in result.stdout
    assert "docker " not in read_log(fake_state)


def test_external_build_target_file_is_ignored_when_build_target_matches_repo_target(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_file(
        sdk / "target" / "k3-local.json",
        """
        {"version": "1.0", "board": "k3-com260", "enabled_packages": []}
        """,
    )
    external = tmp_path / "external.json"
    write_file(external, '{"version": "1.0", "board": "external", "enabled_packages": []}')

    result = run_cmd(
        sdk,
        f"BUILD_TARGET=k3-local BUILD_TARGET_FILE={external} "
        "bash -lc 'source build/common.sh; load_build_config; printf \"%s\" \"$BUILD_CONFIG_FILE\"'",
    )

    assert result.returncode == 0, result.stdout
    assert str(sdk / "target" / "k3-local.json") in result.stdout


def test_target_with_deps_installs_dependency_system_deps(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/app", name="app", depends=["libdep"], cmake=True)
    write_package(sdk, "components/libdep", name="libdep", sysdeps=[("target-dep-tool", None, None)], cmake=True)
    write_file(
        sdk / "target" / "k3-target-deps.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/app"]
        }
        """,
    )

    result = run_cmd(
        sdk,
        "AUTO_INSTALL_DEPS=yes SROBOTIS_IN_DOCKER_BUILD=1 SROBOTIS_DEPS_ONLY=1 "
        "BUILD_TARGET=k3-target-deps ./build/build.sh cmake",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "target-dep-tool" in (fake_state / "apt.db").read_text(encoding="utf-8")
    assert "Dependency-only check complete" in result.stdout


def test_ci_build_target_is_used_for_package_context(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/peripherals/motor", name="motor", cmake=True)
    write_file(
        sdk / "target" / "k3-ci-target.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/peripherals/motor"],
          "enabled_package_options": {
            "components/peripherals/motor": {
              "enabled_drivers": ["left", "right"]
            }
          }
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 CI_BUILD_TARGET=k3-ci-target "
        "./build/build.sh package components/peripherals/motor",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "[build] Using configuration:" in result.stdout
    assert "-DSROBOTIS_PERIPHERALS_MOTOR_ENABLED_DRIVERS=left;right" in read_log(fake_state)


def test_target_package_options_are_passed_to_peripheral_cmake(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/peripherals/motor", name="motor", cmake=True)
    write_file(
        sdk / "target" / "k3-motor.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/peripherals/motor"],
          "enabled_package_options": {
            "components/peripherals/motor": {
              "enabled_drivers": ["left", "right"]
            }
          }
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 BUILD_TARGET=k3-motor ./build/build.sh cmake",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "-DSROBOTIS_PERIPHERALS_MOTOR_ENABLED_DRIVERS=left;right" in read_log(fake_state)


def test_target_all_builds_nonros2_ros2_and_rootfs(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/base", name="base", cmake=True)
    write_package(sdk, "application/ros2/demo", name="demo_node", depends=["base"], build_type="ament_cmake")
    write_file(sdk / "fake_ros_setup.sh", "export ROS_FAKE_SETUP=1\n")
    write_file(
        sdk / "target" / "k3-full.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": [
            "components/base",
            "application/ros2/demo"
          ]
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ROS_SETUP=\"$PWD/fake_ros_setup.sh\" "
        "BUILD_TARGET=k3-full ./build/build.sh all",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert logged_package_keys(log) == ["components/base"]
    assert "colcon build" in log
    assert "--packages-select demo_node" in log
    assert (sdk / "output" / "rootfs" / "share" / "fake" / "installed").exists()


def test_all_without_target_builds_discovered_nonros2_and_ros2_workspaces(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/base", name="base", cmake=True)
    write_package(sdk, "application/ros2/demo", name="demo_node", build_type="ament_cmake")
    write_file(sdk / "fake_ros_setup.sh", "export ROS_FAKE_SETUP=1\n")

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ROS_SETUP=\"$PWD/fake_ros_setup.sh\" ./build/build.sh all",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert logged_package_keys(log) == ["components/base"]
    assert "colcon build" in log
    assert "--packages-select" not in log
    assert (sdk / "output" / "rootfs" / "share" / "fake" / "installed").exists()


def test_cmake_without_target_builds_discovered_nonros2_only(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/base", name="base", cmake=True)
    write_package(sdk, "application/ros2/demo", name="demo_node", build_type="ament_cmake")

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ./build/build.sh cmake",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert logged_package_keys(log) == ["components/base"]
    assert "colcon " not in log


def test_ros2_target_build_selects_target_ros2_packages_only(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/base", name="base", cmake=True)
    write_package(sdk, "application/ros2/demo", name="demo_node", build_type="ament_cmake")
    write_package(sdk, "application/ros2/other", name="other_node", build_type="ament_cmake")
    write_file(sdk / "fake_ros_setup.sh", "export ROS_FAKE_SETUP=1\n")
    write_file(
        sdk / "target" / "k3-ros2.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["application/ros2/demo"]
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ROS_SETUP=\"$PWD/fake_ros_setup.sh\" "
        "BUILD_TARGET=k3-ros2 ./build/build.sh ros2",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "colcon build" in log
    assert "--packages-select demo_node" in log
    assert "other_node" not in log
    assert logged_package_keys(log) == []


def test_clean_all_removes_build_staging_and_rootfs(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_file(sdk / "output" / "build" / "cmake" / "old", "old\n")
    write_file(sdk / "output" / "staging" / "bin" / "old", "old\n")
    write_file(sdk / "output" / "rootfs" / "bin" / "old", "old\n")

    result = run_cmd(sdk, "./build/build.sh clean all")

    assert result.returncode == 0, result.stdout
    assert not (sdk / "output" / "build").exists()
    assert not (sdk / "output" / "staging").exists()
    assert not (sdk / "output" / "rootfs").exists()


def test_package_clean_removes_nonros2_package_build_dir(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)
    build_dir = sdk / "output" / "build" / "cmake" / "pkgs" / "components_foo"
    write_file(build_dir / "CMakeCache.txt", "old\n")

    result = run_cmd(sdk, "./build/build.sh package components/foo clean")

    assert result.returncode == 0, result.stdout
    assert not build_dir.exists()


def test_package_clean_removes_ros2_package_build_and_install_outputs(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(sdk, "application/ros2/demo", name="demo_node", build_type="ament_cmake")
    write_file(sdk / "output" / "build" / "ros2" / "application" / "demo_node" / "old", "old\n")
    write_file(sdk / "output" / "staging" / "share" / "demo_node" / "old", "old\n")
    write_file(sdk / "output" / "staging" / "lib" / "demo_node" / "old", "old\n")

    result = run_cmd(sdk, "./build/build.sh package application/ros2/demo clean")

    assert result.returncode == 0, result.stdout
    assert not (sdk / "output" / "build" / "ros2" / "application" / "demo_node").exists()
    assert not (sdk / "output" / "staging" / "share" / "demo_node").exists()
    assert not (sdk / "output" / "staging" / "lib" / "demo_node").exists()


def test_deploy_rootfs_strips_development_files(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    staging = sdk / "output" / "staging"
    write_file(staging / "bin" / "app", "binary\n", executable=True)
    write_file(staging / "include" / "app.h", "header\n")
    write_file(staging / "lib" / "cmake" / "Pkg" / "PkgConfig.cmake", "cmake\n")
    write_file(staging / "lib" / "pkgconfig" / "pkg.pc", "pc\n")
    write_file(staging / "lib" / "libapp.a", "archive\n")

    result = run_cmd(sdk, "./build/build.sh deploy-rootfs")

    assert result.returncode == 0, result.stdout
    rootfs = sdk / "output" / "rootfs"
    assert (rootfs / "bin" / "app").exists()
    assert not (rootfs / "include").exists()
    assert not (rootfs / "lib" / "cmake").exists()
    assert not (rootfs / "lib" / "pkgconfig").exists()
    assert not (rootfs / "lib" / "libapp.a").exists()


def test_ros2_package_with_deps_uses_packages_up_to(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "application/ros2/demo", name="demo_node", build_type="ament_cmake")
    write_file(sdk / "fake_ros_setup.sh", "export ROS_FAKE_SETUP=1\n")

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ROS_SETUP=\"$PWD/fake_ros_setup.sh\" "
        "./build/build.sh package application/ros2/demo --with-deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "colcon build" in log
    assert "--packages-up-to demo_node" in log


def test_ros2_package_build_selects_requested_package(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "application/ros2/demo", name="demo_node", build_type="ament_cmake")
    write_file(sdk / "fake_ros_setup.sh", "export ROS_FAKE_SETUP=1\n")

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ROS_SETUP=\"$PWD/fake_ros_setup.sh\" "
        "./build/build.sh package application/ros2/demo",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "colcon build" in log
    assert "--packages-select demo_node" in log
    assert "--packages-up-to" not in log


def test_ros2_package_with_deps_builds_transitive_sdk_underlay_first(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/mid", name="mid", depends=["leaf"], cmake=True)
    write_package(sdk, "components/leaf", name="leaf", sysdeps=[("leaf-tool", None, None)], cmake=True)
    write_package(sdk, "application/ros2/demo", name="demo_node", depends=["mid"], build_type="ament_cmake")
    write_file(sdk / "fake_ros_setup.sh", "export ROS_FAKE_SETUP=1\n")

    result = run_cmd(
        sdk,
        "AUTO_INSTALL_DEPS=yes ROS_SETUP=\"$PWD/fake_ros_setup.sh\" "
        "./build/build.sh package application/ros2/demo --with-deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "leaf-tool" in (fake_state / "apt.db").read_text(encoding="utf-8")
    log = read_log(fake_state)
    assert logged_package_keys(log) == ["components/leaf", "components/mid"]
    assert "--packages-up-to demo_node" in log


def test_ros2_package_deps_builds_only_ros2_dependencies(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "application/ros2/demo", name="demo_node", build_type="ament_cmake")
    write_file(sdk / "fake_ros_setup.sh", "export ROS_FAKE_SETUP=1\n")

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ROS_SETUP=\"$PWD/fake_ros_setup.sh\" "
        "./build/build.sh package application/ros2/demo --deps",
        fake_bin=fake_bin,
        fake_state=fake_state,
        extra_env={"FAKE_COLCON_LIST_OUTPUT": "dep_node\ndemo_node"},
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "colcon list --packages-up-to demo_node --names-only" in log
    assert "colcon build" in log
    assert "--packages-select dep_node" in log
    assert "--packages-select demo_node" not in log


def add_fake_python_build_tool(fake_bin: Path) -> None:
    write_file(
        fake_bin / "python3",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo "python3 $*" >> "${FAKE_TOOL_LOG}"
        if [[ "${1:-}" == "-c" ]]; then
          exit 0
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "build" ]]; then
          out_dir=""
          prev=""
          for arg in "$@"; do
            if [[ "${prev}" == "--outdir" ]]; then
              out_dir="${arg}"
            fi
            prev="${arg}"
          done
          [[ -n "${out_dir}" ]] || { echo "missing --outdir" >&2; exit 2; }
          mkdir -p "${out_dir}"
          touch "${out_dir}/fake_pkg-0.1.0-py3-none-any.whl"
          exit 0
        fi
        exec /usr/bin/python3 "$@"
        """,
        executable=True,
    )


def test_envsetup_lunch_m_and_mm_forward_user_commands(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)
    write_file(
        sdk / "target" / "k3-env.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "product": "fixture",
          "enabled_packages": ["components/foo"]
        }
        """,
    )

    result = run_cmd(
        sdk,
        """
        set -euo pipefail
        export SROBOTIS_SKIP_DEPS_CHECK=1
        source build/envsetup.sh
        lunch k3-env
        m -C
        (cd components/foo && mm --log=quiet -j3 -DDEMO=ON)
        (cd components/foo && mm clean)
        """,
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "[lunch] Selected: k3-env" in result.stdout
    log = read_log(fake_state)
    assert logged_package_keys(log) == ["components/foo", "components/foo"]
    assert "-DDEMO=ON" in log
    assert not (sdk / "output" / "build" / "cmake" / "pkgs" / "components_foo").exists()


def test_envsetup_m_clean_removes_all_outputs(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_file(sdk / "output" / "build" / "cmake" / "old", "old\n")
    write_file(sdk / "output" / "staging" / "bin" / "old", "old\n")
    write_file(sdk / "output" / "rootfs" / "bin" / "old", "old\n")

    result = run_cmd(
        sdk,
        """
        set -euo pipefail
        source build/envsetup.sh >/dev/null
        m clean
        """,
    )

    assert result.returncode == 0, result.stdout
    assert not (sdk / "output" / "build").exists()
    assert not (sdk / "output" / "staging").exists()
    assert not (sdk / "output" / "rootfs").exists()


def test_envsetup_m_and_mm_py_build_python_wheels(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    add_fake_python_build_tool(fake_bin)
    (fake_state / "apt.db").write_text("pybind11-dev\n", encoding="utf-8")
    write_package(sdk, "components/py_pkg", name="py_pkg", cmake=True)
    write_file(sdk / "components" / "py_pkg" / "pyproject.toml", "[build-system]\nrequires = []\n")

    result = run_cmd(
        sdk,
        """
        set -euo pipefail
        export SROBOTIS_SKIP_DEPS_CHECK=1
        source build/envsetup.sh >/dev/null
        m -C -py
        (cd components/py_pkg && mm --log=quiet -py)
        """,
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert log.count("python3 -m build --wheel --outdir") >= 2
    wheel_dir = sdk / "output" / "wheels" / "components_py_pkg"
    assert list(wheel_dir.glob("*.whl"))


def test_python_wheels_script_builds_requested_package(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    add_fake_python_build_tool(fake_bin)
    (fake_state / "apt.db").write_text("pybind11-dev\n", encoding="utf-8")
    write_package(sdk, "components/py_pkg", name="py_pkg", cmake=True)
    write_file(sdk / "components" / "py_pkg" / "pyproject.toml", "[build-system]\nrequires = []\n")

    result = run_cmd(
        sdk,
        "./build/python_wheels.sh components/py_pkg",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert "[wheel] Package: components/py_pkg" in result.stdout
    assert "[wheel] Done." in result.stdout
    assert list((sdk / "output" / "wheels" / "components_py_pkg").glob("*.whl"))


def test_docker_wrapper_adds_configured_devices(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)
    write_file(
        sdk / "target" / "k3-device.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/foo"]
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_USE_DOCKER_BUILD=1 SROBOTIS_DOCKER_DEVICES=/dev/null "
        "BUILD_TARGET=k3-device ./build/build.sh package components/foo",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    log = read_log(fake_state)
    assert "docker run -d" in log
    assert "--device /dev/null" in log


def test_docker_wrapper_passes_output_root_to_container(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)
    write_file(
        sdk / "target" / "k3-output-root.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/foo"]
        }
        """,
    )
    output_root = sdk / "output" / "docker-k3"

    result = run_cmd(
        sdk,
        "SROBOTIS_USE_DOCKER_BUILD=1 "
        "BUILD_TARGET=k3-output-root "
        f"OUTPUT_ROOT={output_root} ./build/build.sh package components/foo",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 0, result.stdout
    assert f"-e OUTPUT_ROOT={output_root}" in read_log(fake_state)


def test_docker_wrapper_rejects_non_dev_device_entries(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)
    write_file(
        sdk / "target" / "k3-device-invalid.json",
        """
        {
          "version": "1.0",
          "board": "k3-com260",
          "enabled_packages": ["components/foo"]
        }
        """,
    )

    result = run_cmd(
        sdk,
        "SROBOTIS_USE_DOCKER_BUILD=1 SROBOTIS_DOCKER_DEVICES=/tmp/not-dev "
        "BUILD_TARGET=k3-device-invalid ./build/build.sh package components/foo",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode != 0
    assert "SROBOTIS_DOCKER_DEVICES entries must start with /dev/" in result.stdout
    assert "docker run -d" not in read_log(fake_state)


def test_package_rejects_unexpected_package_argument(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)

    result = run_cmd(
        sdk,
        "./build/build.sh package components/foo build extra",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode != 0
    assert "Unexpected package argument: extra" in result.stdout


def test_package_rejects_package_outside_repo(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    outside = tmp_path / "outside_pkg"
    write_file(outside / "CMakeLists.txt", "cmake_minimum_required(VERSION 3.10)\n")

    result = run_cmd(
        sdk,
        f"SROBOTIS_SKIP_DEPS_CHECK=1 ./build/build.sh package {outside}",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode != 0
    assert "Package directory must be inside repo" in result.stdout


def test_ros2_package_fails_when_ros_setup_missing(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_package(sdk, "application/ros2/demo", name="demo_node", build_type="ament_cmake")

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ROS_SETUP=/tmp/missing-ros-setup.bash "
        "./build/build.sh package application/ros2/demo",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode != 0
    assert "ROS setup script not found: /tmp/missing-ros-setup.bash" in result.stdout


def test_ros2_build_failure_reports_colcon_exit_code(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    fake_bin, fake_state = make_fake_tools(tmp_path)
    write_file(
        fake_bin / "colcon",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo "colcon $*" >> "${FAKE_TOOL_LOG}"
        exit 42
        """,
        executable=True,
    )
    write_package(sdk, "application/ros2/demo", name="demo_node", build_type="ament_cmake")
    write_file(sdk / "fake_ros_setup.sh", "export ROS_FAKE_SETUP=1\n")

    result = run_cmd(
        sdk,
        "SROBOTIS_SKIP_DEPS_CHECK=1 ROS_SETUP=\"$PWD/fake_ros_setup.sh\" "
        "./build/build.sh package application/ros2/demo",
        fake_bin=fake_bin,
        fake_state=fake_state,
    )

    assert result.returncode == 42, result.stdout
    assert "ROS2 build failed (rc=42)" in result.stdout


def test_envsetup_mm_rejects_clean_with_deps(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(sdk, "components/foo", name="foo", cmake=True)

    result = run_cmd(
        sdk,
        """
        source build/envsetup.sh >/dev/null
        cd components/foo
        mm --with-deps clean
        """,
    )

    assert result.returncode != 0
    assert "clean cannot be combined with --with-deps" in result.stdout


def test_package_clean_removes_ros2_install_outputs_without_build_dir(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    write_package(sdk, "application/ros2/py_demo", name="py_node", build_type="ament_python")
    write_file(sdk / "output" / "staging" / "share" / "py_node" / "old", "old\n")
    write_file(sdk / "output" / "staging" / "lib" / "py_node" / "old", "old\n")
    write_file(sdk / "output" / "staging" / "lib" / "libpy_node.so", "old\n")
    write_file(
        sdk / "output" / "staging" / "share" / "ament_index" / "resource_index" / "packages" / "py_node",
        "old\n",
    )
    write_file(sdk / "output" / "staging" / "share" / "colcon-core" / "packages" / "py_node", "old\n")

    result = run_cmd(sdk, "./build/build.sh package application/ros2/py_demo clean")

    assert result.returncode == 0, result.stdout
    assert not (sdk / "output" / "staging" / "share" / "py_node").exists()
    assert not (sdk / "output" / "staging" / "lib" / "py_node").exists()
    assert not (sdk / "output" / "staging" / "lib" / "libpy_node.so").exists()
    assert not (
        sdk / "output" / "staging" / "share" / "ament_index" / "resource_index" / "packages" / "py_node"
    ).exists()
    assert not (sdk / "output" / "staging" / "share" / "colcon-core" / "packages" / "py_node").exists()


def test_deploy_rootfs_removes_stale_files_and_keeps_runtime_libs(tmp_path: Path) -> None:
    sdk = make_sdk(tmp_path)
    staging = sdk / "output" / "staging"
    rootfs = sdk / "output" / "rootfs"
    write_file(staging / "bin" / "current", "current\n", executable=True)
    write_file(staging / "lib" / "libruntime.so", "runtime\n")
    write_file(rootfs / "bin" / "stale", "stale\n", executable=True)

    result = run_cmd(sdk, "./build/build.sh deploy-rootfs")

    assert result.returncode == 0, result.stdout
    assert (rootfs / "bin" / "current").exists()
    assert (rootfs / "lib" / "libruntime.so").exists()
    assert not (rootfs / "bin" / "stale").exists()

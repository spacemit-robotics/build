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
    sysdeps: list[tuple[str, str | None, str | None]] | None = None,
    build_type: str = "cmake",
    cmake: bool = False,
) -> None:
    depends = depends or []
    sysdeps = sysdeps or []
    dep_xml = "\n".join(f"  <depend>{dep}</depend>" for dep in depends)
    sysdep_xml = []
    for dep_name, check, arch in sysdeps:
        attrs = []
        if check:
            attrs.append(f'check="{check}"')
        if arch:
            attrs.append(f'arch="{arch}"')
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
        fi
        exit 0
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

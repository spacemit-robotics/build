#!/usr/bin/env bash
#
# Copyright (C) 2026 SpacemiT (Hangzhou) Technology Co. Ltd.
# SPDX-License-Identifier: Apache-2.0
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_OUTPUT_ROOT="${REPO_ROOT}/output"

BUILD_TARGET="${BUILD_TARGET:-}"
BUILD_TARGET_FILE="${BUILD_TARGET_FILE:-}"
export SDK_BUILD_ARCH="${SDK_BUILD_ARCH:-riscv64}"

source "${REPO_ROOT}/build/common.sh"

usage() {
  cat <<USAGE
Usage: cross_build.sh <command> [args...]

Commands:
  all                         Cross-build all enabled packages
  cmake, C                    Cross-build CMake/non-ROS2 packages
  ros2, R                     Cross-build ROS2 packages
  package <dir> [build|clean] Cross-build or clean one package
  deploy-rootfs, rootfs       Generate cross rootfs from cross staging
  clean [all|cmake|ros2]      Clean cross output build directories
  help                        Show this help

Typical usage:
  source build/envsetup.sh
  lunch k3-com260-minimal
  ./build/cross_build.sh all
  ./build/cross_build.sh package components/peripherals/misc_io/

Dependency split rules in this first version:
  - build/package*.xml dependencies install into the Ubuntu build container
  - system_depend with arch="x86_64" or cross_scope="host" installs into Ubuntu
  - system_depend with cross_scope="sysroot" installs into the Bianbu sysroot container
  - all other package system_depend entries install into the Bianbu sysroot container

Environment overrides:
  SROBOTIS_CROSS_OUTPUT_ROOT       Default: output/cross/<target>
  SROBOTIS_CROSS_SYSROOT           Default: <cross-output>/sysroot
  SROBOTIS_CROSS_UBUNTU_IMAGE      Override selected Ubuntu image
  SROBOTIS_CROSS_BIANBU_IMAGE      Override selected Bianbu image
  SROBOTIS_CROSS_REFRESH_SYSROOT   Set to 1 to force exporting the sysroot
  SROBOTIS_CROSS_SKIP_SYSROOT_SYNC Set to 1 to reuse the existing sysroot
USAGE
}

parse_invocation_command() {
  local args=("$@")
  local i=0

  while [[ "${i}" -lt "${#args[@]}" ]]; do
    case "${args[$i]}" in
      -j)
        i=$((i + 2))
        ;;
      -j*|-v|--log=*|--py|-py)
        i=$((i + 1))
        ;;
      --)
        i=$((i + 1))
        break
        ;;
      -*)
        i=$((i + 1))
        ;;
      *)
        echo "${args[$i]}"
        return 0
        ;;
    esac
  done

  echo "help"
}

command_payload_args() {
  local want_cmd="$1"
  shift
  local args=("$@")
  local i=0

  while [[ "${i}" -lt "${#args[@]}" ]]; do
    case "${args[$i]}" in
      -j)
        i=$((i + 2))
        ;;
      -j*|-v|--log=*|--py|-py)
        i=$((i + 1))
        ;;
      --)
        i=$((i + 1))
        break
        ;;
      -*)
        i=$((i + 1))
        ;;
      "${want_cmd}")
        printf "%s\n" "${args[@]:$((i + 1))}"
        return 0
        ;;
      *)
        return 0
        ;;
    esac
  done
}

read_target_board() {
  local config_file="${BUILD_CONFIG_FILE:-${BUILD_TARGET_FILE:-}}"
  [[ -f "${config_file}" ]] || return 0

  if has_jq; then
    jq -r '.board // empty' "${config_file}" 2>/dev/null || true
  else
    sed -n 's/^[[:space:]]*"board"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      "${config_file}" 2>/dev/null | head -n 1 || true
  fi
}

select_cross_images() {
  local board target_name
  board="$(read_target_board)"
  target_name="${BUILD_TARGET:-}"

  case "${board}:${target_name}" in
    k1*:*|K1*:*|*:k1*|*:K1*)
      CROSS_UBUNTU_IMAGE="${SROBOTIS_CROSS_UBUNTU_IMAGE:-ubuntu:24.04}"
      CROSS_BIANBU_TAG="2.3"
      ;;
    k3*:*|K3*:*|*:k3*|*:K3*)
      CROSS_UBUNTU_IMAGE="${SROBOTIS_CROSS_UBUNTU_IMAGE:-ubuntu:26.04}"
      CROSS_BIANBU_TAG="4.0"
      ;;
    *)
      echo "[cross] ERROR: Cannot select Docker images without a k1/k3 target." >&2
      echo "[cross] Please run 'lunch <target>' first or set BUILD_TARGET." >&2
      return 1
      ;;
  esac

  CROSS_BIANBU_IMAGE="${SROBOTIS_CROSS_BIANBU_IMAGE:-bianbu:${CROSS_BIANBU_TAG}}"
}

ensure_docker_available() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[cross] ERROR: docker command not found." >&2
    echo "[cross] Please install and configure Docker first." >&2
    return 1
  fi

  if ! docker info >/dev/null 2>&1; then
    echo "[cross] ERROR: Docker is installed but unavailable to the current user." >&2
    echo "[cross] Please start Docker and make sure this user can run docker." >&2
    return 1
  fi
}

docker_image_exists() {
  docker image inspect "$1" >/dev/null 2>&1
}

ensure_ubuntu_image() {
  local image="$1"
  if docker_image_exists "${image}"; then
    return 0
  fi

  echo "[cross] Pulling ${image} ..."
  docker pull "${image}"
}

ensure_bianbu_image() {
  local image="$1"
  local tag="${CROSS_BIANBU_TAG}"
  local remote_image="harbor.spacemit.com/bianbu/bianbu:${tag}"

  if docker_image_exists "${image}"; then
    return 0
  fi

  if ! docker_image_exists "${remote_image}"; then
    echo "[cross] Pulling ${remote_image} ..."
    docker pull "${remote_image}"
  fi

  if [[ "${image}" != "${remote_image}" ]]; then
    docker tag "${remote_image}" "${image}" >/dev/null 2>&1 || true
  fi
}

sanitize_docker_name_component() {
  printf '%s\n' "$1" | sed 's/[^A-Za-z0-9_.-]/-/g; s/--*/-/g; s/^[.-]*//; s/[.-]*$//'
}

sdk_path_key() {
  local repo_name="${REPO_ROOT##*/}"
  local repo_hash=""

  if command -v sha256sum >/dev/null 2>&1; then
    repo_hash="$(printf '%s' "${REPO_ROOT}" | sha256sum | sed -n 's/^\([[:xdigit:]]\{12\}\).*/\1/p')"
  elif command -v shasum >/dev/null 2>&1; then
    repo_hash="$(printf '%s' "${REPO_ROOT}" | shasum -a 256 | sed -n 's/^\([[:xdigit:]]\{12\}\).*/\1/p')"
  elif command -v cksum >/dev/null 2>&1; then
    repo_hash="$(printf '%s' "${REPO_ROOT}" | cksum | sed 's/[[:space:]].*//')"
  fi

  sanitize_docker_name_component "${repo_name:-sdk}-${repo_hash:-unknown}"
}

cross_container_name() {
  local kind="$1"
  local image_key="$2"
  local sdk_key
  sdk_key="$(sdk_path_key)"
  image_key="$(sanitize_docker_name_component "${image_key}")"
  sanitize_docker_name_component "srobotis-cross-${sdk_key}-${kind}-${image_key}"
}

docker_container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

docker_container_running() {
  [[ "$(docker container inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]
}

cross_required_mounts() {
  printf '%s\n' "${REPO_ROOT}"

  if [[ -n "${CROSS_OUTPUT_ROOT:-}" ]] && ! path_is_or_under "${REPO_ROOT}" "${CROSS_OUTPUT_ROOT}"; then
    printf '%s\n' "${CROSS_OUTPUT_ROOT}"
  fi

  if [[ -n "${CROSS_SYSROOT:-}" ]] && \
    ! path_is_or_under "${REPO_ROOT}" "${CROSS_SYSROOT}" && \
    ! path_is_or_under "${CROSS_OUTPUT_ROOT:-}" "${CROSS_SYSROOT}"; then
    printf '%s\n' "${CROSS_SYSROOT}"
  fi
}

docker_container_has_mount() {
  local container_name="$1"
  local destination="$2"

  docker container inspect -f '{{range .Mounts}}{{println .Destination}}{{end}}' "${container_name}" 2>/dev/null | \
    grep -Fx -- "${destination}" >/dev/null 2>&1
}

container_has_required_mounts() {
  local container_name="$1"
  local mount_path

  while IFS= read -r mount_path; do
    [[ -n "${mount_path}" ]] || continue
    docker_container_has_mount "${container_name}" "${mount_path}" || return 1
  done < <(cross_required_mounts)

  docker exec -i --workdir / --user "0:0" "${container_name}" \
    test -f "${REPO_ROOT}/build/build.sh" >/dev/null 2>&1
}

ensure_container() {
  local container_name="$1"
  local image="$2"
  local platform="$3"

  if docker_container_running "${container_name}"; then
    if container_has_required_mounts "${container_name}"; then
      echo "[cross] Reusing running container: ${container_name}"
      return 0
    fi
    echo "[cross] Recreating container ${container_name}: required SDK paths are not mounted"
    docker rm -f "${container_name}" >/dev/null
  fi

  if docker_container_exists "${container_name}"; then
    echo "[cross] Starting existing container: ${container_name}"
    if docker start "${container_name}" >/dev/null && container_has_required_mounts "${container_name}"; then
      return 0
    fi
    echo "[cross] Recreating container ${container_name}: existing container is not compatible with required mounts"
    docker rm -f "${container_name}" >/dev/null
  fi

  local platform_args=()
  if [[ -n "${platform}" ]]; then
    platform_args=(--platform "${platform}")
  fi

  local volume_args=()
  local mount_path
  while IFS= read -r mount_path; do
    [[ -n "${mount_path}" ]] || continue
    volume_args+=(--volume "${mount_path}:${mount_path}")
  done < <(cross_required_mounts)

  echo "[cross] Creating container: ${container_name}"
  docker run -d \
    --name "${container_name}" \
    "${platform_args[@]}" \
    --workdir "${REPO_ROOT}" \
    "${volume_args[@]}" \
    --user "0:0" \
    -e "HOME=/root" \
    --entrypoint /bin/bash \
    "${image}" \
    -lc 'trap "exit 0" TERM INT; while true; do sleep 3600; done' >/dev/null
}

configure_bianbu_container() {
  local container_name="$1"

  case "${CROSS_BIANBU_TAG}" in
    2.3)
      docker exec -i --user "0:0" "${container_name}" bash -lc '
        set -euo pipefail
        src="/etc/apt/sources.list.d/bianbu.sources"
        [[ -f "${src}" ]] || exit 0
        if grep -Eq "^[[:space:]]*Suites:.*(^|[[:space:]])noble-ros([[:space:]]|$)" "${src}"; then
          exit 0
        fi
        echo "[cross] Adding noble-ros apt suite to ${src}"
        sed -i "/^[[:space:]]*Suites:/ s/$/ noble-ros/" "${src}"
      '
      ;;
  esac
}

ensure_container_user() {
  local container_name="$1"
  local user_name="$2"
  local uid="$3"
  local gid="$4"
  local home_dir="$5"

  docker exec -i \
    --workdir / \
    --user "0:0" \
    -e "SROBOTIS_DOCKER_HOST_USER=${user_name}" \
    -e "SROBOTIS_DOCKER_HOST_UID=${uid}" \
    -e "SROBOTIS_DOCKER_HOST_GID=${gid}" \
    -e "SROBOTIS_DOCKER_HOST_HOME=${home_dir}" \
    "${container_name}" \
    bash -s <<'USER_EOF'
set -euo pipefail

user="${SROBOTIS_DOCKER_HOST_USER:-builder}"
uid="${SROBOTIS_DOCKER_HOST_UID:?}"
gid="${SROBOTIS_DOCKER_HOST_GID:?}"
home_dir="${SROBOTIS_DOCKER_HOST_HOME:?}"

case "${user}" in
  ""|[0-9]*|*[!A-Za-z0-9_.-]*)
    user="builder"
    ;;
esac

group_name="$(getent group "${gid}" | cut -d: -f1 || true)"
if [[ -z "${group_name}" ]]; then
  group_name="${user}"
  if getent group "${group_name}" >/dev/null; then
    group_name="${user}_${gid}"
  fi
  printf '%s:x:%s:\n' "${group_name}" "${gid}" >> /etc/group
fi

if ! getent passwd "${uid}" >/dev/null; then
  passwd_user="${user}"
  if getent passwd "${passwd_user}" >/dev/null; then
    passwd_user="${user}_${uid}"
  fi
  printf '%s:x:%s:%s:%s:%s:/bin/bash\n' \
    "${passwd_user}" "${uid}" "${gid}" "${passwd_user}" "${home_dir}" >> /etc/passwd
else
  current_user="$(getent passwd "${uid}" | cut -d: -f1)"
  current_gecos="$(getent passwd "${uid}" | cut -d: -f5)"
  current_shell="$(getent passwd "${uid}" | cut -d: -f7)"
  current_gecos="${current_gecos:-${current_user}}"
  current_shell="${current_shell:-/bin/bash}"
  awk -F: -v OFS=: \
    -v uid="${uid}" \
    -v gid="${gid}" \
    -v home="${home_dir}" \
    -v gecos="${current_gecos}" \
    -v shell="${current_shell}" \
    '$3 == uid {$4 = gid; $5 = gecos; $6 = home; $7 = shell} {print}' \
    /etc/passwd > /etc/passwd.tmp
  cat /etc/passwd.tmp > /etc/passwd
  rm -f /etc/passwd.tmp
fi

mkdir -p "${home_dir}" "${home_dir}/.cache"
chown -R "${uid}:${gid}" "${home_dir}"
USER_EOF
}

chown_cross_output_in_container() {
  local container_name="$1"
  local uid="$2"
  local gid="$3"

  docker exec -i --workdir "${REPO_ROOT}" --user "0:0" "${container_name}" \
    bash -lc "if [[ -d '${CROSS_OUTPUT_ROOT}' ]]; then chown -R '${uid}:${gid}' '${CROSS_OUTPUT_ROOT}'; fi"
}

xml_attr_from_line() {
  local line="$1"
  local attr="$2"
  echo "${line}" | sed -n "s/.*${attr}=\"\\([^\"]*\\)\".*/\\1/p"
}

path_is_or_under() {
  local parent="$1"
  local child="$2"

  [[ -n "${parent}" && -n "${child}" ]] || return 1
  [[ "${parent}" == "/" ]] && return 0

  parent="${parent%/}"
  child="${child%/}"

  [[ "${child}" == "${parent}" || "${child}" == "${parent}/"* ]]
}

CROSS_DEP_FIELD_SEP=$'\037'

read_cross_xml_sysdeps() {
  local pkg_key="$1"
  local pkg_xml="$2"
  [[ -f "${pkg_xml}" ]] || return 0

  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    local name check_cmd arch_filter cross_scope
    name="$(echo "${line}" | sed -n 's/.*> *\([^<]*\) *<\/system_depend>.*/\1/p' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    check_cmd="$(xml_attr_from_line "${line}" "check")"
    arch_filter="$(xml_attr_from_line "${line}" "arch")"
    cross_scope="$(xml_attr_from_line "${line}" "cross_scope")"
    [[ -z "${name}" ]] && continue
    [[ -z "${check_cmd}" ]] && check_cmd="dpkg -s ${name}"
    printf "%s%srequired%s%s%s%s%s%s%s%s\n" \
      "${pkg_key}" "${CROSS_DEP_FIELD_SEP}" \
      "${CROSS_DEP_FIELD_SEP}" "${name}" \
      "${CROSS_DEP_FIELD_SEP}" "${check_cmd}" \
      "${CROSS_DEP_FIELD_SEP}" "${arch_filter}" \
      "${CROSS_DEP_FIELD_SEP}" "${cross_scope}"
  done < <(grep -E '<system_depend' "${pkg_xml}" 2>/dev/null)
}

read_cross_json_sysdeps() {
  local pkg_key="$1"
  local pkg_json="$2"
  [[ -f "${pkg_json}" ]] || return 0
  has_jq || return 0

  jq -r --arg pkg "${pkg_key}" --arg sep "${CROSS_DEP_FIELD_SEP}" '
    (.system_dependencies.required[]? | [$pkg, "required", (.name // ""), (.check // ""), (.arch // ""), (.cross_scope // "")] | join($sep)) ,
    (.system_dependencies.optional[]? | [$pkg, "optional", (.name // ""), (.check // ""), (.arch // ""), (.cross_scope // "")] | join($sep))
  ' "${pkg_json}" 2>/dev/null || true
}

read_cross_package_sysdeps() {
  local pkg_key="$1"
  local pkg_xml
  pkg_xml="$(package_xml_path "${pkg_key}")"

  if [[ -f "${pkg_xml}" ]] && grep -q '<system_depend' "${pkg_xml}" 2>/dev/null; then
    read_cross_xml_sysdeps "${pkg_key}" "${pkg_xml}"
    return 0
  fi

  local pkg_json
  pkg_json="$(package_json_path "${pkg_key}")"
  read_cross_json_sysdeps "${pkg_key}" "${pkg_json}"
}

arch_filter_has_x86_64() {
  local arch_filter="$1"
  local item
  arch_filter="${arch_filter//,/ }"
  for item in ${arch_filter}; do
    case "$(normalize_build_arch "${item}")" in
      x86_64)
        return 0
        ;;
    esac
  done
  return 1
}

arch_filter_has_riscv64() {
  local arch_filter="$1"
  local item
  arch_filter="${arch_filter//,/ }"
  for item in ${arch_filter}; do
    case "$(normalize_build_arch "${item}")" in
      riscv64)
        return 0
        ;;
    esac
  done
  return 1
}

cross_dependency_scope() {
  local pkg_key="$1"
  local arch_filter="$2"
  local cross_scope="${3:-}"

  case "${cross_scope}" in
    host|ubuntu|x86_64)
      echo "host"
      return 0
      ;;
    sysroot|target|riscv64)
      echo "sysroot"
      return 0
      ;;
    both)
      echo "both"
      return 0
      ;;
    none|skip)
      echo "skip"
      return 0
      ;;
    "")
      ;;
    *)
      echo "[cross] ERROR: Unknown cross_scope='${cross_scope}' in ${pkg_key}" >&2
      return 1
      ;;
  esac

  case "${pkg_key}" in
    cross_host_base|build)
      echo "host"
      return 0
      ;;
    cross_sysroot_base)
      echo "sysroot"
      return 0
      ;;
  esac

  if [[ -n "${arch_filter}" ]] && arch_filter_has_x86_64 "${arch_filter}" && arch_filter_has_riscv64 "${arch_filter}"; then
    echo "both"
  elif [[ -n "${arch_filter}" ]] && arch_filter_has_x86_64 "${arch_filter}"; then
    echo "host"
  elif [[ -n "${arch_filter}" ]] && arch_filter_has_riscv64 "${arch_filter}"; then
    echo "sysroot"
  elif [[ -n "${arch_filter}" ]]; then
    echo "skip"
  else
    echo "sysroot"
  fi
}

target_uses_ros2_for_command() {
  local cmd="$1"
  case "${cmd}" in
    ros2|R)
      return 0
      ;;
    all)
      target_needs_ros2
      return $?
      ;;
  esac
  return 1
}

package_key_from_arg() {
  local pkg_arg="$1"
  if [[ -d "${pkg_arg}" ]]; then
    local abs_dir
    abs_dir="$(cd "${pkg_arg}" && pwd)"
    if [[ "${abs_dir}" == "${REPO_ROOT}/"* ]]; then
      echo "${abs_dir#"${REPO_ROOT}"/}"
    else
      echo "${pkg_arg}"
    fi
  else
    echo "${pkg_arg}"
  fi
}

package_is_ros2() {
  local pkg_arg="$1"
  [[ -d "${pkg_arg}" && -f "${pkg_arg}/package.xml" ]] || return 1
  local build_type
  build_type="$(get_package_build_type "${pkg_arg}" || true)"
  [[ "${build_type}" == "ament_cmake" || "${build_type}" == "ament_python" ]]
}

emit_base_cross_host_deps() {
  local base_deps=(
    ca-certificates
    file
    rsync
    xz-utils
    gcc-riscv64-linux-gnu
    g++-riscv64-linux-gnu
    binutils-riscv64-linux-gnu
  )

  local dep
  for dep in "${base_deps[@]}"; do
    printf "cross_host_base%srequired%s%s%sdpkg -s %s%s\n" \
      "${CROSS_DEP_FIELD_SEP}" \
      "${CROSS_DEP_FIELD_SEP}" "${dep}" \
      "${CROSS_DEP_FIELD_SEP}" "${dep}" \
      "${CROSS_DEP_FIELD_SEP}"
  done
}

emit_base_cross_sysroot_deps() {
  printf "cross_sysroot_base%srequired%spkg-config%sdpkg -s pkg-config%s\n" \
    "${CROSS_DEP_FIELD_SEP}" \
    "${CROSS_DEP_FIELD_SEP}" \
    "${CROSS_DEP_FIELD_SEP}" \
    "${CROSS_DEP_FIELD_SEP}"
}

collect_cross_dependency_lines() {
  local cmd="$1"
  shift || true

  emit_base_cross_host_deps

  emit_base_cross_sysroot_deps

  if [[ -f "${REPO_ROOT}/build/package.xml" ]]; then
    read_cross_xml_sysdeps "build" "${REPO_ROOT}/build/package.xml"
  fi

  case "${cmd}" in
    all|cmake|C|ros2|R)
      if target_uses_ros2_for_command "${cmd}" && [[ -f "${REPO_ROOT}/build/package_cross_ros2.xml" ]]; then
        read_cross_xml_sysdeps "build_ros2_deps" "${REPO_ROOT}/build/package_cross_ros2.xml"
      fi

      local enabled_all=()
      has_jq || { echo "[cross] ERROR: jq is required for target JSON parsing." >&2; return 1; }
      mapfile -t enabled_all < <(resolve_enabled_with_metadata)

      local pkg_path
      for pkg_path in "${enabled_all[@]}"; do
        [[ -n "${pkg_path}" ]] || continue
        case "${cmd}" in
          cmake|C)
            [[ "${pkg_path}" == middleware/ros2/* || "${pkg_path}" == application/ros2/* ]] && continue
            ;;
          ros2|R)
            [[ "${pkg_path}" == middleware/ros2/* || "${pkg_path}" == application/ros2/* ]] || continue
            ;;
        esac
        read_cross_package_sysdeps "${pkg_path}"
      done
      ;;
    package|pkg)
      local pkg_arg="${1:-}"
      [[ -n "${pkg_arg}" ]] || { echo "[cross] ERROR: Package directory required" >&2; return 1; }
      if package_is_ros2 "${pkg_arg}" && [[ -f "${REPO_ROOT}/build/package_cross_ros2.xml" ]]; then
        read_cross_xml_sysdeps "build_ros2_deps" "${REPO_ROOT}/build/package_cross_ros2.xml"
      fi

      local pkg_key
      pkg_key="$(package_key_from_arg "${pkg_arg}")"
      read_cross_package_sysdeps "${pkg_key}"
      ;;
    clean|deploy-rootfs|rootfs)
      ;;
    *)
      ;;
  esac
}

split_cross_dependencies() {
  HOST_DEP_LINES=()
  SYSROOT_DEP_LINES=()

  local deps_output
  if ! deps_output="$(collect_cross_dependency_lines "$@")"; then
    return 1
  fi

  local dep_line
  while IFS= read -r dep_line; do
    [[ -n "${dep_line}" ]] || continue
    local pkg_key dep_type dep_name check_cmd arch_filter cross_scope scope
    IFS="${CROSS_DEP_FIELD_SEP}" read -r pkg_key dep_type dep_name check_cmd arch_filter cross_scope <<< "${dep_line}"
    [[ -n "${dep_name}" ]] || continue
    scope="$(cross_dependency_scope "${pkg_key}" "${arch_filter}" "${cross_scope}")"
    case "${scope}" in
      host)
        HOST_DEP_LINES+=("${dep_type}|${dep_name}|${check_cmd}")
        ;;
      sysroot)
        SYSROOT_DEP_LINES+=("${dep_type}|${dep_name}|${check_cmd}")
        ;;
      both)
        HOST_DEP_LINES+=("${dep_type}|${dep_name}|${check_cmd}")
        SYSROOT_DEP_LINES+=("${dep_type}|${dep_name}|${check_cmd}")
        ;;
      skip)
        ;;
      *)
        echo "[cross] ERROR: Invalid dependency scope for ${dep_name}: ${scope}" >&2
        return 1
        ;;
    esac
  done <<< "${deps_output}"
}

install_deps_in_container() {
  local container_name="$1"
  local label="$2"
  shift 2
  local dep_lines=("$@")
  CROSS_LAST_INSTALL_RAN=0

  if [[ "${#dep_lines[@]}" -eq 0 ]]; then
    echo "[cross] No ${label} dependencies to install"
    return 0
  fi

  local missing_required=()
  local missing_optional=()
  local checked=()
  local dep_line

  echo "[cross] Checking ${label} dependencies..."
  for dep_line in "${dep_lines[@]}"; do
    local dep_type dep_name check_cmd already
    IFS='|' read -r dep_type dep_name check_cmd <<< "${dep_line}"
    [[ -n "${dep_name}" ]] || continue

    already=0
    local checked_name
    for checked_name in "${checked[@]}"; do
      if [[ "${checked_name}" == "${dep_name}" ]]; then
        already=1
        break
      fi
    done
    [[ "${already}" == "1" ]] && continue
    checked+=("${dep_name}")

    if docker exec -i --workdir "${REPO_ROOT}" --user "0:0" \
      -e "SROBOTIS_CROSS_CHECK_CMD=${check_cmd}" \
      "${container_name}" bash -lc 'eval "${SROBOTIS_CROSS_CHECK_CMD}" >/dev/null 2>&1'; then
      echo "[cross] ${label}: ${dep_name} found"
    else
      echo "[cross] ${label}: ${dep_name} missing (${dep_type})"
      if [[ "${dep_type}" == "required" ]]; then
        missing_required+=("${dep_name}")
      else
        missing_optional+=("${dep_name}")
      fi
    fi
  done

  if [[ "${#missing_optional[@]}" -gt 0 ]]; then
    echo "[cross] ${label}: missing optional dependencies: ${missing_optional[*]}"
  fi

  if [[ "${#missing_required[@]}" -eq 0 ]]; then
    echo "[cross] ${label}: all required dependencies are satisfied"
    return 0
  fi

  echo "[cross] Installing ${label} dependencies: ${missing_required[*]}"
  CROSS_LAST_INSTALL_RAN=1
  docker exec -i --workdir "${REPO_ROOT}" --user "0:0" "${container_name}" \
    bash -lc 'set -euo pipefail; apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"' \
    bash "${missing_required[@]}"
}

relativize_sysroot_absolute_symlinks() {
  local sysroot="$1"
  local converted=0
  local link target link_dir rel_target

  while IFS= read -r -d "" link; do
    target="$(readlink "${link}")"
    [[ "${target}" == /* ]] || continue

    link_dir="$(dirname "${link}")"
    rel_target="$(realpath -m --relative-to="${link_dir}" "${sysroot}${target}")"
    ln -sfn "${rel_target}" "${link}"
    converted=$((converted + 1))
  done < <(find "${sysroot}" -type l -lname '/*' -print0)

  if [[ "${converted}" -gt 0 ]]; then
    echo "[cross] Rewrote ${converted} absolute sysroot symlinks to relative targets"
  fi
}

rewrite_sysroot_quoted_path() {
  local file="$1"
  local from="\"$2\""
  local to="\"$3\""
  local tmp="${file}.tmp.$$"
  local line changed=0

  grep -qF "${from}" "${file}" || return 1

  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == *"${from}"* ]]; then
      line="${line//${from}/${to}}"
      changed=1
    fi
    printf '%s\n' "${line}"
  done < "${file}" > "${tmp}"

  mv "${tmp}" "${file}"
  [[ "${changed}" == "1" ]]
}

rewrite_sysroot_ros2_numpy_includes() {
  local scan_sysroot="$1"
  local install_sysroot="${2:-$1}"
  local rewritten=0
  local file include_path scan_include install_include changed
  local include_paths=(
    "/usr/lib/riscv64-linux-gnu/python3-numpy/numpy/_core/include"
    "/usr/lib/python3/dist-packages/numpy/core/include"
  )

  [[ -d "${scan_sysroot}/opt/ros" ]] || return 0

  while IFS= read -r -d "" file; do
    changed=0
    for include_path in "${include_paths[@]}"; do
      scan_include="${scan_sysroot}${include_path}"
      install_include="${install_sysroot}${include_path}"
      [[ -e "${scan_include}" ]] || continue

      if rewrite_sysroot_quoted_path "${file}" "${include_path}" "${install_include}"; then
        changed=1
      fi
      if rewrite_sysroot_quoted_path "${file}" "${install_sysroot}.tmp${include_path}" "${install_include}"; then
        changed=1
      fi
      if [[ "${scan_sysroot}" != "${install_sysroot}" ]] && rewrite_sysroot_quoted_path "${file}" "${scan_sysroot}${include_path}" "${install_include}"; then
        changed=1
      fi
    done

    if [[ "${changed}" == "1" ]]; then
      rewritten=$((rewritten + 1))
    fi
  done < <(find "${scan_sysroot}/opt/ros" -path "*/share/*/cmake/*.cmake" -type f -print0)

  if [[ "${rewritten}" -gt 0 ]]; then
    echo "[cross] Rewrote ROS2 numpy include paths in ${rewritten} sysroot CMake files"
  fi
}

normalize_cross_sysroot() {
  local sysroot="$1"
  local install_sysroot="${2:-$1}"

  relativize_sysroot_absolute_symlinks "${sysroot}"
  rewrite_sysroot_ros2_numpy_includes "${sysroot}" "${install_sysroot}"
}

sync_bianbu_sysroot() {

  local container_name="$1"
  local deps_changed="${2:-0}"

  if [[ "${SROBOTIS_CROSS_SKIP_SYSROOT_SYNC:-0}" == "1" && -d "${CROSS_SYSROOT}/usr" ]]; then
    normalize_cross_sysroot "${CROSS_SYSROOT}"
    echo "[cross] Reusing existing sysroot: ${CROSS_SYSROOT}"
    return 0
  fi

  if [[ "${SROBOTIS_CROSS_REFRESH_SYSROOT:-0}" != "1" && "${deps_changed}" != "1" && -d "${CROSS_SYSROOT}/usr" ]]; then
    normalize_cross_sysroot "${CROSS_SYSROOT}"
    echo "[cross] Reusing existing sysroot: ${CROSS_SYSROOT}"
    return 0
  fi

  local tmp_sysroot="${CROSS_SYSROOT}.tmp"
  local old_sysroot="${CROSS_SYSROOT}.old"

  echo "[cross] Exporting Bianbu sysroot to: ${CROSS_SYSROOT}"
  rm -rf "${tmp_sysroot}" "${old_sysroot}"
  mkdir -p "${tmp_sysroot}" "$(dirname "${CROSS_SYSROOT}")"

  docker export "${container_name}" | tar -C "${tmp_sysroot}" \
    --no-same-owner \
    --exclude='dev/*' \
    --exclude='proc/*' \
    --exclude='sys/*' \
    --exclude='run/*' \
    --exclude='tmp/*' \
    -xf -

  normalize_cross_sysroot "${tmp_sysroot}" "${CROSS_SYSROOT}"

  if [[ -d "${CROSS_SYSROOT}" ]]; then
    mv "${CROSS_SYSROOT}" "${old_sysroot}"
  fi
  mv "${tmp_sysroot}" "${CROSS_SYSROOT}"
  rm -rf "${old_sysroot}"
}

write_toolchain_file() {
  CROSS_TOOLCHAIN_FILE="${CROSS_OUTPUT_ROOT}/toolchain/riscv64-linux-gnu.cmake"
  mkdir -p "$(dirname "${CROSS_TOOLCHAIN_FILE}")"

  cat > "${CROSS_TOOLCHAIN_FILE}" <<TOOLCHAIN
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR riscv64)
set(CMAKE_LIBRARY_ARCHITECTURE riscv64-linux-gnu)

set(CMAKE_C_COMPILER /usr/bin/riscv64-linux-gnu-gcc)
set(CMAKE_CXX_COMPILER /usr/bin/riscv64-linux-gnu-g++)

set(CMAKE_SYSROOT "${CROSS_SYSROOT}")
set(CMAKE_FIND_ROOT_PATH "${CROSS_SYSROOT}" "${CROSS_STAGING_PREFIX}")

set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
TOOLCHAIN
}

detect_cross_python_config() {
  local py_lib_dir="${CROSS_SYSROOT}/usr/lib/riscv64-linux-gnu"
  local py_lib=""

  if [[ -d "${py_lib_dir}" ]]; then
    py_lib="$(find "${py_lib_dir}" -maxdepth 1 -type f -name 'libpython3*.so.1.0' -print 2>/dev/null | sort -V | tail -n 1 || true)"
    if [[ -z "${py_lib}" ]]; then
      py_lib="$(find "${py_lib_dir}" -maxdepth 1 \( -type f -o -type l \) -name 'libpython3*.so*' -print 2>/dev/null | sort -V | tail -n 1 || true)"
    fi
  fi

  if [[ -z "${py_lib}" ]]; then
    echo "[cross] ERROR: target Python development library not found in ${py_lib_dir}" >&2
    return 1
  fi

  local py_lib_name
  py_lib_name="${py_lib##*/}"
  CROSS_PYTHON_VERSION="$(printf '%s\n' "${py_lib_name}" | sed -n 's/^libpython\([0-9]\+\.[0-9]\+\)\.so.*/\1/p')"
  if [[ -z "${CROSS_PYTHON_VERSION}" ]]; then
    echo "[cross] ERROR: cannot infer Python version from ${py_lib}" >&2
    return 1
  fi

  CROSS_PYTHON_INCLUDE_DIR="${CROSS_SYSROOT}/usr/include/python${CROSS_PYTHON_VERSION}"
  CROSS_PYTHON_LIBRARY="${py_lib}"
  CROSS_PYTHON_SOABI="cpython-${CROSS_PYTHON_VERSION/./}-riscv64-linux-gnu"

  if [[ ! -d "${CROSS_PYTHON_INCLUDE_DIR}" ]]; then
    echo "[cross] ERROR: target Python headers not found: ${CROSS_PYTHON_INCLUDE_DIR}" >&2
    return 1
  fi
}

cross_ros_pythonpath() {
  local ros_prefix="${CROSS_SYSROOT}/opt/ros/${ROS_DISTRO:-humble}"
  local paths=()
  local p existing seen

  for p in \
    "${ros_prefix}/lib/python${CROSS_PYTHON_VERSION}/site-packages" \
    "${ros_prefix}/lib/python3/dist-packages"; do
    [[ -d "${p}" ]] || continue
    paths+=("${p}")
  done

  if [[ -d "${ros_prefix}/lib" ]]; then
    while IFS= read -r p; do
      [[ -d "${p}" ]] || continue
      seen=0
      for existing in "${paths[@]}"; do
        if [[ "${existing}" == "${p}" ]]; then
          seen=1
          break
        fi
      done
      [[ "${seen}" == "1" ]] || paths+=("${p}")
    done < <(find "${ros_prefix}/lib" -maxdepth 2 -type d -path '*/python3*/site-packages' -print 2>/dev/null | sort -V)
  fi

  local IFS=:
  printf '%s' "${paths[*]}"
}

cross_cmake_extra_args() {
  # ROS 2 / ament CMake in the cross container must use the host Python
  # interpreter. Target headers/libs still come from the exported sysroot.
  printf '%s\n' \
    "-DCMAKE_TOOLCHAIN_FILE=${CROSS_TOOLCHAIN_FILE}" \
    "-DCMAKE_SYSTEM_PROCESSOR=riscv64" \
    "-DPKG_CONFIG_EXECUTABLE=/usr/bin/pkg-config" \
    "-DCMAKE_MAKE_PROGRAM=/usr/bin/make" \
    "-DPython3_EXECUTABLE=/usr/bin/python3" \
    "-DPython3_ROOT_DIR=/usr" \
    "-DPython3_FIND_STRATEGY=LOCATION" \
    "-DPython3_INCLUDE_DIR=${CROSS_PYTHON_INCLUDE_DIR}" \
    "-DPython3_LIBRARY=${CROSS_PYTHON_LIBRARY}" \
    "-DPython3_SOABI=${CROSS_PYTHON_SOABI}" \
    "-DPYTHON_EXECUTABLE=/usr/bin/python3" \
    "-DPYTHON_INCLUDE_DIR=${CROSS_PYTHON_INCLUDE_DIR}" \
    "-DPYTHON_LIBRARY=${CROSS_PYTHON_LIBRARY}" \
    "-DPYTHON_LIBRARIES=${CROSS_PYTHON_LIBRARY}" \
    "-DPYTHON_SOABI=${CROSS_PYTHON_SOABI}" \
    "-DCMAKE_PREFIX_PATH=${CROSS_STAGING_PREFIX};${CROSS_SYSROOT}/usr;${CROSS_SYSROOT}/usr/lib/riscv64-linux-gnu/cmake;${CROSS_SYSROOT}/opt/ros/${ROS_DISTRO:-humble}" \
    "-DCMAKE_FIND_ROOT_PATH=${CROSS_SYSROOT};${CROSS_STAGING_PREFIX}"

  if [[ -n "${SROBOTIS_CMAKE_EXTRA_ARGS:-}" ]]; then
    while IFS= read -r line; do
      [[ -n "${line}" ]] && printf "%s\n" "${line}"
    done <<< "${SROBOTIS_CMAKE_EXTRA_ARGS}"
  fi
}
run_build_in_ubuntu() {
  local container_name="$1"
  shift

  local uid gid user_name cross_home
  uid="$(id -u)"
  gid="$(id -g)"
  user_name="${USER:-builder}"
  cross_home="${CROSS_OUTPUT_ROOT}/.ubuntu_home"

  ensure_container_user "${container_name}" "${user_name}" "${uid}" "${gid}" "${cross_home}"
  chown_cross_output_in_container "${container_name}" "${uid}" "${gid}" || true

  local pkg_config_libdir="${CROSS_STAGING_PREFIX}/lib/pkgconfig:${CROSS_STAGING_PREFIX}/share/pkgconfig:${CROSS_SYSROOT}/usr/lib/riscv64-linux-gnu/pkgconfig:${CROSS_SYSROOT}/usr/lib/pkgconfig:${CROSS_SYSROOT}/usr/share/pkgconfig"
  detect_cross_python_config

  local cmake_extra ros_pythonpath cross_pythonpath
  local host_python_lib="/usr/lib/riscv64-linux-gnu/libpython${CROSS_PYTHON_VERSION}.so"
  local sysroot_python_lib="${CROSS_PYTHON_LIBRARY}"
  cmake_extra="$(cross_cmake_extra_args)"
  ros_pythonpath="$(cross_ros_pythonpath)"
  cross_pythonpath="${ros_pythonpath}"
  if [[ -n "${PYTHONPATH:-}" ]]; then
    cross_pythonpath="${cross_pythonpath:+${cross_pythonpath}:}${PYTHONPATH}"
  fi

  docker exec -i --workdir "${REPO_ROOT}" --user "0:0" "${container_name}" \
    bash -lc 'set -euo pipefail; mkdir -p "$(dirname "$1")"; if [[ -L "$1" ]]; then ln -sfn "$2" "$1"; elif [[ ! -e "$1" ]]; then ln -s "$2" "$1"; fi' \
    bash "${host_python_lib}" "${sysroot_python_lib}"

  echo "[cross] Building target ${BUILD_TARGET} in ${CROSS_UBUNTU_IMAGE}"
  docker exec -i \
    --workdir "${REPO_ROOT}" \
    --user "${uid}:${gid}" \
    -e "HOME=${cross_home}" \
    -e "USER=${user_name}" \
    -e "LOGNAME=${LOGNAME:-${user_name}}" \
    -e "BUILD_TARGET=${BUILD_TARGET}" \
    -e "BUILD_TARGET_FILE=${BUILD_TARGET_FILE}" \
    -e "BUILD_CONFIG_FILE=${BUILD_CONFIG_FILE}" \
    -e "OUTPUT_ROOT=${CROSS_OUTPUT_ROOT}" \
    -e "STAGING_PREFIX=${CROSS_STAGING_PREFIX}" \
    -e "ROOTFS_PREFIX=${CROSS_ROOTFS_PREFIX}" \
    -e "PREFIX=${CROSS_STAGING_PREFIX}" \
    -e "SDK_BUILD_ARCH=riscv64" \
    -e "SROBOTIS_CROSS_BUILD=1" \
    -e "SROBOTIS_SKIP_DEPS_CHECK=1" \
    -e "SROBOTIS_CMAKE_EXTRA_ARGS=${cmake_extra}" \
    -e "CMAKE_TOOLCHAIN_FILE=${CROSS_TOOLCHAIN_FILE}" \
    -e "PKG_CONFIG_SYSROOT_DIR=${CROSS_SYSROOT}" \
    -e "PKG_CONFIG_LIBDIR=${pkg_config_libdir}" \
    -e "CC=/usr/bin/riscv64-linux-gnu-gcc" \
    -e "CXX=/usr/bin/riscv64-linux-gnu-g++" \
    -e "AMENT_PYTHON_EXECUTABLE=/usr/bin/python3" \
    -e "PYTHONPATH=${cross_pythonpath}" \
    -e "ROS_DISTRO=${ROS_DISTRO:-humble}" \
    -e "ROS_SETUP=${CROSS_SYSROOT}/opt/ros/${ROS_DISTRO:-humble}/setup.bash" \
    "${container_name}" \
    "${REPO_ROOT}/build/build.sh" "$@"

  chown_cross_output_in_container "${container_name}" "${uid}" "${gid}" || true
}


relocate_cross_ros2_install() {
  local install_prefix="$1"
  local ros_distro="${ROS_DISTRO:-humble}"
  local runtime_ros_prefix="/opt/ros/${ros_distro}"
  local build_ros_prefix="${CROSS_SYSROOT}${runtime_ros_prefix}"
  local build_staging_prefix="${CROSS_STAGING_PREFIX:-}"

  [[ -d "${install_prefix}" ]] || return 0

  local rewritten=0
  local file
  while IFS= read -r -d "" file; do
    if grep -Iq . "${file}" 2>/dev/null; then
      if grep -qF "${build_ros_prefix}" "${file}" 2>/dev/null; then
        sed -i "s#${build_ros_prefix}#${runtime_ros_prefix}#g" "${file}"
        rewritten=1
      fi
      if [[ -n "${build_staging_prefix}" && "${build_staging_prefix}" != "${install_prefix}" ]] && \
        grep -qF "${build_staging_prefix}" "${file}" 2>/dev/null; then
        sed -i "s#${build_staging_prefix}#${install_prefix}#g" "${file}"
        rewritten=1
      fi
    fi
  done < <(
    find "${install_prefix}" -type f \
      ! -name "*.a" \
      ! -name "*.o" \
      ! -name "*.pyc" \
      ! -name "*.so" \
      ! -path "*/__pycache__/*" \
      -print0
  )

  if [[ "${rewritten}" == "1" ]]; then
    echo "[cross] Relocated ROS2 runtime prefix in ${install_prefix}: ${build_ros_prefix} -> ${runtime_ros_prefix}"
  fi
}

main() {
  local original_args=("$@")
  local cmd
  cmd="$(parse_invocation_command "$@")"

  case "${cmd}" in
    help|--help|-h)
      usage
      return 0
      ;;
  esac

  load_build_config
  if [[ -z "${BUILD_TARGET:-}" || -z "${BUILD_CONFIG_FILE:-}" || ! -f "${BUILD_CONFIG_FILE}" ]]; then
    echo "[cross] ERROR: no target selected." >&2
    echo "[cross] Please run: source build/envsetup.sh && lunch <target>" >&2
    return 1
  fi

  select_cross_images
  ensure_docker_available
  ensure_ubuntu_image "${CROSS_UBUNTU_IMAGE}"
  ensure_bianbu_image "${CROSS_BIANBU_IMAGE}"

  CROSS_OUTPUT_ROOT="${SROBOTIS_CROSS_OUTPUT_ROOT:-${DEFAULT_OUTPUT_ROOT}/cross/${BUILD_TARGET}}"
  CROSS_SYSROOT="${SROBOTIS_CROSS_SYSROOT:-${CROSS_OUTPUT_ROOT}/sysroot}"
  CROSS_STAGING_PREFIX="${CROSS_OUTPUT_ROOT}/staging"
  CROSS_ROOTFS_PREFIX="${CROSS_OUTPUT_ROOT}/rootfs"
  mkdir -p "${CROSS_OUTPUT_ROOT}" "$(dirname "${CROSS_SYSROOT}")"

  local ubuntu_container bianbu_container
  ubuntu_container="$(cross_container_name "ubuntu" "${CROSS_UBUNTU_IMAGE}")"
  bianbu_container="$(cross_container_name "bianbu" "${CROSS_BIANBU_IMAGE}")"

  ensure_container "${bianbu_container}" "${CROSS_BIANBU_IMAGE}" "${SROBOTIS_CROSS_BIANBU_PLATFORM:-linux/riscv64}"
  configure_bianbu_container "${bianbu_container}"
  ensure_container "${ubuntu_container}" "${CROSS_UBUNTU_IMAGE}" "${SROBOTIS_CROSS_UBUNTU_PLATFORM:-}"

  HOST_DEP_LINES=()
  SYSROOT_DEP_LINES=()
  local payload=()
  mapfile -t payload < <(command_payload_args "${cmd}" "${original_args[@]}")
  split_cross_dependencies "${cmd}" "${payload[@]}"

  install_deps_in_container "${ubuntu_container}" "Ubuntu host" "${HOST_DEP_LINES[@]}"
  install_deps_in_container "${bianbu_container}" "Bianbu sysroot" "${SYSROOT_DEP_LINES[@]}"
  local sysroot_deps_changed="${CROSS_LAST_INSTALL_RAN:-0}"

  sync_bianbu_sysroot "${bianbu_container}" "${sysroot_deps_changed}"
  write_toolchain_file

  run_build_in_ubuntu "${ubuntu_container}" "${original_args[@]}"
  relocate_cross_ros2_install "${CROSS_STAGING_PREFIX}"
  relocate_cross_ros2_install "${CROSS_ROOTFS_PREFIX}"
}

if [[ "${SROBOTIS_CROSS_BUILD_SH_NO_MAIN:-0}" != "1" ]]; then
  main "$@"
fi

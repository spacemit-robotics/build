#!/usr/bin/env bash
#
# Copyright (C) 2026 SpacemiT (Hangzhou) Technology Co. Ltd.
# SPDX-License-Identifier: Apache-2.0
#

# Docker wrapper for explicitly requested k1/k3 builds in Bianbu Docker.
#
# This file is meant to be SOURCED by build/build.sh. It intentionally keeps
# the Docker-specific flow outside the normal board-side build path.

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "[docker] ERROR: build/docker_build.sh must be sourced, not executed." >&2
  exit 1
fi

docker_build_command_needs_wrapper() {
  local cmd="$1"
  shift || true

  case "${cmd}" in
    all|cmake|C|ros2|R)
      [[ -n "${BUILD_CONFIG_FILE:-}" || -n "${BUILD_TARGET:-}" ]]
      return $?
      ;;
    package|pkg)
      [[ -n "${BUILD_CONFIG_FILE:-}" || -n "${BUILD_TARGET:-}" ]] || return 1
      local package_action="${2:-build}"
      [[ "${package_action}" != "clean" ]]
      return $?
      ;;
    *)
      return 1
      ;;
  esac
}

docker_build_host_enabled() {
  [[ "${SROBOTIS_IN_DOCKER_BUILD:-0}" == "1" ]] && return 1

  case "${SROBOTIS_USE_DOCKER_BUILD:-0}" in
    1|yes|YES|true|TRUE|on|ON)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

read_docker_target_board() {
  local config_file="${BUILD_CONFIG_FILE:-${BUILD_TARGET_FILE:-}}"
  [[ -f "${config_file}" ]] || return 0

  if has_jq; then
    jq -r '.board // empty' "${config_file}" 2>/dev/null || true
  else
    sed -n 's/^[[:space:]]*"board"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      "${config_file}" 2>/dev/null | head -n 1 || true
  fi
}

docker_target_auto_resolve_dependencies() {
  local config_file="${BUILD_CONFIG_FILE:-}"
  if [[ -z "${config_file}" || ! -f "${config_file}" ]]; then
    return 0
  fi
  if has_jq; then
    jq -r '.options.auto_resolve_dependencies // empty' "${config_file}" 2>/dev/null || true
    return 0
  fi
  sed -n 's/^[[:space:]]*"auto_resolve_dependencies"[[:space:]]*:[[:space:]]*\([^,}[:space:]]*\).*/\1/p' \
    "${config_file}" 2>/dev/null | head -n 1 || true
}

select_bianbu_docker_tag() {
  local target_name="${BUILD_TARGET:-}"
  local board
  board="$(read_docker_target_board)"

  if [[ -z "${target_name}" && -n "${BUILD_CONFIG_FILE:-}" ]]; then
    target_name="$(basename "${BUILD_CONFIG_FILE}" .json)"
  fi

  case "${board}" in
    k1*|K1*)
      echo "2.3"
      return 0
      ;;
    k3*|K3*)
      echo "4.0"
      return 0
      ;;
  esac

  case "${target_name}" in
    k1*|K1*)
      echo "2.3"
      return 0
      ;;
    k3*|K3*)
      echo "4.0"
      return 0
      ;;
  esac

  echo "[docker] ERROR: Cannot select Bianbu Docker image without a k1/k3 target." >&2
  echo "[docker] Please run 'lunch <target>' or set BUILD_TARGET before building." >&2
  return 1
}

ensure_docker_available() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[docker] ERROR: docker command not found." >&2
    echo "[docker] Please install and configure the Docker environment first." >&2
    return 1
  fi

  if ! docker info >/dev/null 2>&1; then
    echo "[docker] ERROR: Docker is installed but not available to the current user." >&2
    echo "[docker] Please start the Docker daemon and make sure this user can run docker." >&2
    return 1
  fi
}

docker_image_exists() {
  docker image inspect "$1" >/dev/null 2>&1
}

ensure_bianbu_docker_image() {
  local tag="$1"
  local short_image="bianbu:${tag}"
  local remote_image="harbor.spacemit.com/bianbu/bianbu:${tag}"

  SROBOTIS_DOCKER_IMAGE="${short_image}"

  if docker_image_exists "${short_image}"; then
    return 0
  fi

  if docker_image_exists "${remote_image}"; then
    docker tag "${remote_image}" "${short_image}" >/dev/null 2>&1 || \
      SROBOTIS_DOCKER_IMAGE="${remote_image}"
    return 0
  fi

  echo "[docker] Image ${short_image} not found. Pulling ${remote_image} ..."
  if ! docker pull "${remote_image}"; then
    echo "[docker] ERROR: Failed to pull ${remote_image}" >&2
    return 1
  fi

  if ! docker tag "${remote_image}" "${short_image}" >/dev/null 2>&1; then
    SROBOTIS_DOCKER_IMAGE="${remote_image}"
  fi
}

add_docker_env_if_set() {
  local var_name="$1"
  if [[ -n "${!var_name+x}" ]]; then
    DOCKER_ENV_ARGS+=("-e" "${var_name}=${!var_name}")
  fi
}

default_docker_container_name() {
  local tag="${1:-}"

  if [[ -n "${SROBOTIS_DOCKER_CONTAINER_NAME:-}" ]]; then
    echo "${SROBOTIS_DOCKER_CONTAINER_NAME}"
    return 0
  fi

  local image_key="bianbu-${tag:-unknown}"
  local raw_name="srobotis-${image_key}"
  printf '%s\n' "${raw_name}" | sed 's/[^A-Za-z0-9_.-]/-/g; s/--*/-/g'
}

docker_container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

docker_container_running() {
  [[ "$(docker container inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]
}

docker_container_has_repo_root() {
  local container_name="$1"

  docker exec -i \
    --workdir / \
    --user "0:0" \
    "${container_name}" \
    test -f "${REPO_ROOT}/build/build.sh" >/dev/null 2>&1
}

docker_path_is_or_under() {
  local parent="$1"
  local child="$2"

  [[ -n "${parent}" && -n "${child}" ]] || return 1
  [[ "${parent}" == "/" ]] && return 0

  parent="${parent%/}"
  child="${child%/}"

  [[ "${child}" == "${parent}" || "${child}" == "${parent}/"* ]]
}

ensure_bianbu_docker_container() {
  local container_name="$1"
  local image="$2"
  local mount_src="${SROBOTIS_DOCKER_MOUNT_SRC:-${HOME:-${REPO_ROOT}}}"
  local mount_dst="${SROBOTIS_DOCKER_MOUNT_DST:-${mount_src}}"

  if docker_container_running "${container_name}"; then
    if docker_container_has_repo_root "${container_name}"; then
      echo "[docker] Reusing running container: ${container_name}"
      return 0
    fi
    echo "[docker] Recreating container ${container_name}: ${REPO_ROOT} is not mounted"
    docker rm -f "${container_name}" >/dev/null || return $?
  fi

  if docker_container_exists "${container_name}"; then
    echo "[docker] Starting existing container: ${container_name}"
    if docker start "${container_name}" >/dev/null; then
      if docker_container_has_repo_root "${container_name}"; then
        return 0
      fi
      echo "[docker] Recreating container ${container_name}: ${REPO_ROOT} is not mounted"
    else
      echo "[docker] Recreating container ${container_name}: failed to start existing container"
    fi
    docker rm -f "${container_name}" >/dev/null || return $?
  fi

  echo "[docker] Creating container: ${container_name}"
  local docker_volume_args=(
    --volume "${mount_src}:${mount_dst}"
  )

  if [[ "${mount_dst}" != "${mount_src}" ]] || ! docker_path_is_or_under "${mount_src}" "${REPO_ROOT}"; then
    docker_volume_args+=(--volume "${REPO_ROOT}:${REPO_ROOT}")
  fi

  docker run -d \
    --name "${container_name}" \
    --platform "${SROBOTIS_DOCKER_PLATFORM:-linux/riscv64}" \
    --workdir "${REPO_ROOT}" \
    "${docker_volume_args[@]}" \
    "${DOCKER_ENV_ARGS[@]}" \
    --user "0:0" \
    -e "HOME=/root" \
    --entrypoint /bin/bash \
    "${image}" \
    -lc 'trap "exit 0" TERM INT; while true; do sleep 3600; done' >/dev/null
}

configure_bianbu_docker_container() {
  local container_name="$1"
  local tag="$2"

  case "${tag}" in
    2.3)
      docker exec -i \
        --user "0:0" \
        "${container_name}" \
        bash -lc '
          set -euo pipefail
          src="/etc/apt/sources.list.d/bianbu.sources"
          [[ -f "${src}" ]] || exit 0
          if grep -Eq "^[[:space:]]*Suites:.*(^|[[:space:]])noble-ros([[:space:]]|$)" "${src}"; then
            exit 0
          fi
          echo "[docker] Adding noble-ros apt suite to ${src}"
          sed -i "/^[[:space:]]*Suites:/ s/$/ noble-ros/" "${src}"
        '
      ;;
  esac
}

run_build_in_docker() {
  local tag
  if ! tag="$(select_bianbu_docker_tag)"; then
    return 1
  fi

  ensure_docker_available || return 1
  ensure_bianbu_docker_image "${tag}" || return 1

  local image="${SROBOTIS_DOCKER_IMAGE}"
  local user_name="${USER:-builder}"
  local uid gid
  uid="$(id -u)"
  gid="$(id -g)"

  local auto_resolve_deps_config
  auto_resolve_deps_config="$(docker_target_auto_resolve_dependencies || true)"
  case "${auto_resolve_deps_config}" in
    true|yes|1)
      : "${AUTO_INSTALL_DEPS:=yes}"
      export AUTO_INSTALL_DEPS
      ;;
  esac

  echo "[docker] Building target ${BUILD_TARGET:-unknown} in ${image}"

  DOCKER_ENV_ARGS=(
    -e "SROBOTIS_IN_DOCKER_BUILD=1"
    -e "SROBOTIS_DOCKER_IMAGE=${image}"
    -e "USER=${user_name}"
    -e "LOGNAME=${LOGNAME:-${user_name}}"
  )

  local docker_run_as_root="${SROBOTIS_DOCKER_RUN_AS_ROOT:-}"
  if [[ -z "${docker_run_as_root}" && ( "${AUTO_INSTALL_DEPS:-}" == "yes" || "${AUTO_INSTALL_DEPS:-}" == "true" ) ]]; then
    docker_run_as_root=0
  fi

  if [[ "${docker_run_as_root}" == "1" ]]; then
    local docker_exec_user="0:0"
    local docker_exec_home="/root"
  else
    local docker_exec_user="${uid}:${gid}"
    local docker_exec_home="/tmp"
  fi

  local env_name
  for env_name in \
    BUILD_TARGET BUILD_TARGET_FILE BUILD_CONFIG_FILE \
    STAGING_PREFIX ROOTFS_PREFIX PREFIX \
    ROS_DISTRO ROS_SETUP PARALLEL_JOBS \
    LOG_LEVEL LOG_ROOT LOG_TEE LOG_TAIL_LINES LOG_SHOW_ENTER LOG_SHOW_DONE \
    AUTO_INSTALL_DEPS DEBUG_DEPS SROBOTIS_CMAKE_EXTRA_ARGS _want_python_wheels \
    SROBOTIS_DOCKER_RUN_AS_ROOT SROBOTIS_DOCKER_PLATFORM \
    SROBOTIS_DOCKER_MOUNT_SRC SROBOTIS_DOCKER_MOUNT_DST; do
    add_docker_env_if_set "${env_name}"
  done

  local container_name
  container_name="$(default_docker_container_name "${tag}")"
  ensure_bianbu_docker_container "${container_name}" "${image}" || return 1
  configure_bianbu_docker_container "${container_name}" "${tag}" || return 1

  if [[ "${AUTO_INSTALL_DEPS:-}" == "yes" || "${AUTO_INSTALL_DEPS:-}" == "true" ]]; then
    echo "[docker] Checking/installing system dependencies as root"
    DOCKER_DEPS_ARGS=(
      docker exec -i
      --workdir "${REPO_ROOT}"
      --user "0:0"
      "${DOCKER_ENV_ARGS[@]}"
      -e "HOME=/root"
      -e "SROBOTIS_DEPS_ONLY=1"
      "${container_name}"
      "${REPO_ROOT}/build/build.sh"
    )
    "${DOCKER_DEPS_ARGS[@]}" "$@" || return $?

    DOCKER_ENV_ARGS+=("-e" "SROBOTIS_SKIP_DEPS_CHECK=1")

    if [[ "${docker_exec_user}" != "0:0" && "${SROBOTIS_DOCKER_FIX_OUTPUT_OWNER:-0}" == "1" ]]; then
      docker exec -i \
        --workdir "${REPO_ROOT}" \
        --user "0:0" \
        "${container_name}" \
        bash -lc "if [[ -d '${OUTPUT_ROOT}' ]]; then chown -R '${uid}:${gid}' '${OUTPUT_ROOT}'; fi" || return $?
    fi
  fi

  DOCKER_EXEC_ARGS=(
    docker exec -i
    --workdir "${REPO_ROOT}"
    --user "${docker_exec_user}"
    "${DOCKER_ENV_ARGS[@]}"
    -e "HOME=${docker_exec_home}"
    "${container_name}"
    "${REPO_ROOT}/build/build.sh"
  )

  "${DOCKER_EXEC_ARGS[@]}" "$@"
}

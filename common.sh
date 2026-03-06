#!/usr/bin/env bash
#
# Copyright (C) 2026 SpacemiT (Hangzhou) Technology Co. Ltd.
# SPDX-License-Identifier: Apache-2.0
#

# Common helpers for the SDK build system.
#
# This file is meant to be SOURCED by build scripts (e.g. build/build.sh).
#
# Responsibilities:
# - repo/prefix defaulting (best-effort)
# - target config loading
# - package metadata access (delegates jq parts to build/jq_utils.sh)
# - ROS2 package discovery helpers
# - system dependency check/install

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "[build] ERROR: build/common.sh must be sourced, not executed." >&2
  exit 1
fi

# Best-effort defaults if caller didn't define them yet.
if [[ -z "${REPO_ROOT:-}" ]]; then
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
: "${OUTPUT_ROOT:=${REPO_ROOT}/output}"
: "${LOG_ROOT:=${OUTPUT_ROOT}/log}"
: "${CM_BUILD_ROOT:=${OUTPUT_ROOT}/build/cmake}"
: "${ROS2_BUILD_ROOT:=${OUTPUT_ROOT}/build/ros2}"
: "${ROS2_LOG_ROOT:=${OUTPUT_ROOT}/log/ros2}"
: "${STAGING_PREFIX:=${OUTPUT_ROOT}/staging}"
: "${ROOTFS_PREFIX:=${OUTPUT_ROOT}/rootfs}"
: "${PREFIX:=${STAGING_PREFIX}}"

# Logging
# LOG_LEVEL: quiet|normal|verbose (default: quiet)
: "${LOG_LEVEL:=${LOG_LEVEL:-quiet}}"
: "${LOG_TEE:=${LOG_TEE:-1}}"
: "${LOG_TAIL_LINES:=${LOG_TAIL_LINES:-200}}"

# Console status lines control:
# - LOG_SHOW_ENTER controls per-package "Start: <pkg>" lines.
# Default: on (so you always know what is being built).
if [[ -z "${LOG_SHOW_ENTER:-}" ]]; then
  LOG_SHOW_ENTER=1
fi
: "${LOG_SHOW_DONE:=${LOG_SHOW_DONE:-1}}"

# jq helpers
source "${REPO_ROOT}/build/jq_utils.sh"

# ----------------------------------------------------------------------------
# Logging / verbosity control
# ----------------------------------------------------------------------------

_log_tail_lines() {
  echo "${LOG_TAIL_LINES}"
}

_log_is_verbose() {
  [[ "${LOG_LEVEL}" == "verbose" ]]
}

_log_should_capture() {
  [[ "${LOG_LEVEL}" == "quiet" || "${LOG_LEVEL}" == "normal" ]]
}

run_logged_overwrite() {
  # Usage: run_logged_overwrite <logfile> <cmd> [args...]
  local logfile="$1"
  shift
  mkdir -p "$(dirname "${logfile}")"

  if _log_is_verbose; then
    if [[ "${LOG_TEE}" == "1" ]]; then
      # shellcheck disable=SC2129
      { echo ">>> $*"; "$@"; } 2>&1 | tee "${logfile}"
      local rc=${PIPESTATUS[0]}
      return "${rc}"
    fi
    "$@"
    return $?
  fi

  { echo ">>> $*"; "$@"; } >"${logfile}" 2>&1
  local rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    echo "[build] ERROR: command failed (rc=${rc}). See log: ${logfile}" >&2
    tail -n "$(_log_tail_lines)" "${logfile}" >&2 || true
    return "${rc}"
  fi
  return 0
}

run_logged_append() {
  # Usage: run_logged_append <logfile> <cmd> [args...]
  local logfile="$1"
  shift
  mkdir -p "$(dirname "${logfile}")"

  if _log_is_verbose; then
    if [[ "${LOG_TEE}" == "1" ]]; then
      { echo ">>> $*"; "$@"; } 2>&1 | tee -a "${logfile}"
      local rc=${PIPESTATUS[0]}
      return "${rc}"
    fi
    "$@"
    return $?
  fi

  { echo ">>> $*"; "$@"; } >>"${logfile}" 2>&1
  local rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    echo "[build] ERROR: command failed (rc=${rc}). See log: ${logfile}" >&2
    tail -n "$(_log_tail_lines)" "${logfile}" >&2 || true
    return "${rc}"
  fi
  return 0
}

# Return the on-disk package.json path for a package key like:
# - components/peripherals/motor
# - middleware/ros2/mlink/cpp
# - application/ros2/lekiwi/motion
package_json_path() {
  local pkg_key="$1"
  echo "${REPO_ROOT}/${pkg_key}/package.json"
}

# Return the on-disk package.xml path for a package key.
# For components/thirdparty/<name>, use platform overlay dir so package.xml lives under platform.
package_xml_path() {
  local pkg_key="$1"
  if [[ "${pkg_key}" == components/thirdparty/* ]]; then
    local name="${pkg_key#components/thirdparty/}"
    echo "${REPO_ROOT}/platform/generic/components/thirdparty/${name}/package.xml"
  else
    echo "${REPO_ROOT}/${pkg_key}/package.xml"
  fi
}

# Resolve package name (from <name> in package.xml) to pkg_key. Outputs pkg_key or empty.
# Scans package.xml under components/ and platform/generic/components/thirdparty/.
get_pkg_key_by_package_name() {
  local name="$1"
  [[ -n "${name}" ]] || return 0
  local f
  while IFS= read -r f; do
    [[ -f "${f}" ]] || continue
    local dir="${f%/*}"
    local pkg_key
    if [[ "${dir}" == *"/platform/generic/components/thirdparty/"* ]]; then
      pkg_key="components/thirdparty/$(basename "${dir}")"
    else
      pkg_key="${dir#"${REPO_ROOT}"/}"
    fi
    local xml_name
    xml_name="$(sed -n 's/.*<name> *\([^<]*\) *<\/name>.*/\1/p' "${f}" 2>/dev/null | head -n 1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [[ "${xml_name}" == "${name}" ]]; then
      echo "${pkg_key}"
      return 0
    fi
  done < <(find "${REPO_ROOT}/components" "${REPO_ROOT}/platform/generic/components/thirdparty" -name "package.xml" -type f 2>/dev/null)
  return 0
}

# Read <depend> from package.xml. Output one pkg_key per line.
# <depend> content is package name (from other package.xml <name>); resolved to pkg_key. If content contains "/", treated as pkg_key.
read_package_xml_deps() {
  local pkg_key="$1"
  local pkg_xml
  pkg_xml="$(package_xml_path "${pkg_key}")"
  [[ -f "${pkg_xml}" ]] || return 0
  local raw
  raw="$(sed -n 's/.*<depend> *\([^<]*\) *<\/depend>.*/\1/p' "${pkg_xml}" 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$')"
  while IFS= read -r dep; do
    [[ -n "${dep}" ]] || continue
    if [[ "${dep}" == */* ]]; then
      echo "${dep}"
    else
      local resolved
      resolved="$(get_pkg_key_by_package_name "${dep}")"
      if [[ -n "${resolved}" ]]; then
        echo "${resolved}"
      else
        echo "${dep}"
      fi
    fi
  done <<< "${raw}"
}

# Read <system_depend> from package.xml. Output lines: required|dep_name|check_cmd
# Default check_cmd is "dpkg -s <dep_name>". Optional attribute check="..." overrides it.
read_package_xml_sysdeps_lines() {
  local pkg_key="$1"
  local pkg_xml
  pkg_xml="$(package_xml_path "${pkg_key}")"
  [[ -f "${pkg_xml}" ]] || return 0
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    local name check_cmd
    # Extract name from PCDATA before </system_depend> (avoid matching ">" inside check="...2>/dev/null...")
    name="$(echo "${line}" | sed -n 's/.*> *\([^<]*\) *<\/system_depend>.*/\1/p' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    check_cmd="$(echo "${line}" | sed -n 's/.*check="\([^"]*\)".*/\1/p')"
    [[ -z "${name}" ]] && continue
    [[ -z "${check_cmd}" ]] && check_cmd="dpkg -s ${name}"
    echo "required|${name}|${check_cmd}"
  done < <(grep -E '<system_depend' "${pkg_xml}" 2>/dev/null)
}

# Read dependencies for a package key:
# 1) package.xml <depend> if present
# 2) package.json
read_package_deps() {
  local pkg_key="$1"
  local pkg_xml
  pkg_xml="$(package_xml_path "${pkg_key}")"
  if [[ -f "${pkg_xml}" ]]; then
    local deps
    deps="$(read_package_xml_deps "${pkg_key}")"
    if [[ -n "${deps}" ]]; then
      echo "${deps}"
      return 0
    fi
  fi
  local pj
  pj="$(package_json_path "${pkg_key}")"
  read_package_json_deps "${pj}"
}

# Read system dependency lines (required|name|check or optional|name|check):
# 1) package.xml <system_depend> if present
# 2) package.json
read_package_sysdeps_lines() {
  local pkg_key="$1"
  local pkg_xml
  pkg_xml="$(package_xml_path "${pkg_key}")"
  if [[ -f "${pkg_xml}" ]] && grep -q '<system_depend' "${pkg_xml}" 2>/dev/null; then
    read_package_xml_sysdeps_lines "${pkg_key}"
    return 0
  fi
  local pj
  pj="$(package_json_path "${pkg_key}")"
  read_package_json_sysdeps_lines "${pj}"
}

# Load build configuration from JSON file
load_build_config() {
  local config_file="${BUILD_TARGET_FILE:-}"

  # If BUILD_TARGET is set but BUILD_TARGET_FILE is not, construct the path
  if [[ -n "${BUILD_TARGET:-}" && -z "${config_file}" ]]; then
    config_file="${REPO_ROOT}/target/${BUILD_TARGET}.json"
  fi

  # If no config file specified, return (use default behavior)
  if [[ -z "${config_file}" || ! -f "${config_file}" ]]; then
    return 0
  fi

  # Export config file path for use by other functions
  export BUILD_CONFIG_FILE="${config_file}"

  # Load options from config
  local parallel_jobs_config
  parallel_jobs_config="$(target_parallel_jobs || true)"
  if [[ -n "${parallel_jobs_config}" ]]; then
    export PARALLEL_JOBS="${parallel_jobs_config}"
  fi

  echo "[build] Using configuration: ${config_file}"
}

# Get ROS2 package name from package.xml in a directory
get_ros2_package_name() {
  local pkg_dir="$1"
  if [[ -f "${pkg_dir}/package.xml" ]]; then
    grep '<name>' "${pkg_dir}/package.xml" | sed 's/.*<name>\([^<]*\)<\/name>.*/\1/' | head -n 1
  else
    # Fallback: use directory name
    basename "${pkg_dir}"
  fi
}

# Read <export><build_type> from package.xml. Used to decide ROS2 vs CMake build.
# Outputs: ament_cmake, ament_python, cmake, or empty (treated as cmake).
get_package_build_type() {
  local pkg_dir="$1"
  local xml="${pkg_dir}/package.xml"
  [[ -f "${xml}" ]] || return 0
  sed -n 's/.*<build_type>\([^<]*\)<\/build_type>.*/\1/p' "${xml}" | head -n 1
}

# Find all ROS2 packages in a directory (recursive)
find_ros2_packages_in_dir() {
  local dir="$1"
  local packages=()

  if [[ ! -d "${dir}" ]]; then
    return 1
  fi

  # Find all package.xml files recursively
  while IFS= read -r -d '' pkg_xml; do
    local pkg_dir="${pkg_xml%/*}"
    local pkg_name
    pkg_name="$(get_ros2_package_name "${pkg_dir}")"
    [[ -n "${pkg_name}" ]] && packages+=("${pkg_name}")
  done < <(find "${dir}" -name "package.xml" -type f -print0 2>/dev/null)

  # Output all found packages
  printf '%s\n' "${packages[@]}"
}

# Map config package path to actual ROS2 package name(s)
map_config_path_to_ros2_package() {
  local category="$1"  # e.g., "application/ros2"
  local config_path="$2"  # e.g., "lekiwi/motion"
  local pkg_dir="${REPO_ROOT}/${category}/${config_path}"

  # Check if this is a ROS2 package (has package.xml)
  if [[ -f "${pkg_dir}/package.xml" ]]; then
    get_ros2_package_name "${pkg_dir}"
    return 0
  fi

  # If directory exists but no package.xml, it might be a directory with sub-packages
  # Try to find all ROS2 packages in this directory
  if [[ -d "${pkg_dir}" ]]; then
    local sub_packages
    mapfile -t sub_packages < <(find_ros2_packages_in_dir "${pkg_dir}")
    if [[ ${#sub_packages[@]} -gt 0 ]]; then
      printf '%s\n' "${sub_packages[@]}"
      return 0
    fi
  fi

  return 1
}

# ============================================================================
# System dependency checking and installation
# ============================================================================

check_single_dependency() {
  local dep_name="$1"
  local check_cmd="$2"
  local debug="${DEBUG_DEPS:-0}"

  if [[ "${debug}" == "1" ]]; then
    echo "[deps] Checking ${dep_name}: ${check_cmd}" >&2
  fi

  if eval "${check_cmd}" >/dev/null 2>&1; then
    return 0
  else
    return 1
  fi
}

collect_system_dependencies() {
  local config_file="${BUILD_CONFIG_FILE:-}"

  # Always include build base deps (build/package.xml) so m/mm check build tools and repo.
  if [[ -f "${REPO_ROOT}/build/package.xml" ]] && grep -q '<system_depend' "${REPO_ROOT}/build/package.xml" 2>/dev/null; then
    while IFS='|' read -r dep_type dep_name check_cmd; do
      [[ -z "${dep_type}" || -z "${dep_name}" || -z "${check_cmd}" ]] && continue
      echo "build|${dep_type}|${dep_name}|${check_cmd}"
    done < <(read_package_sysdeps_lines "build")
  fi

  if [[ -z "${config_file}" || ! -f "${config_file}" ]]; then
    return 0
  fi

  if ! has_jq; then
    echo "[deps] WARNING: jq not available, skipping target package dependency check" >&2
    return 0
  fi

  local enabled_all=()
  mapfile -t enabled_all < <(resolve_enabled_with_metadata)

  for pkg_path in "${enabled_all[@]}"; do
    while IFS='|' read -r dep_type dep_name check_cmd; do
      [[ -z "${dep_type}" || -z "${dep_name}" || -z "${check_cmd}" ]] && continue
      echo "${pkg_path}|${dep_type}|${dep_name}|${check_cmd}"
    done < <(read_package_sysdeps_lines "${pkg_path}")
  done
}

check_system_dependencies() {
  local config_file="${BUILD_CONFIG_FILE:-}"
  local missing_required=()
  local missing_optional=()
  local checked_packages=()

  echo "[deps] Checking system dependencies..."

  local deps_lines
  mapfile -t deps_lines < <(collect_system_dependencies)

  if [[ ${#deps_lines[@]} -eq 0 ]]; then
    echo "[deps] No system dependencies defined in configuration"
    return 0
  fi

  for dep_line in "${deps_lines[@]}"; do
    IFS='|' read -r _pkg_path dep_type dep_name check_cmd <<< "${dep_line}"

    local already_checked=false
    for checked in "${checked_packages[@]}"; do
      if [[ "${checked}" == "${dep_name}" ]]; then
        already_checked=true
        break
      fi
    done
    [[ "${already_checked}" == "true" ]] && continue

    if check_single_dependency "${dep_name}" "${check_cmd}"; then
      echo "[deps] ✓ ${dep_name}: found"
      checked_packages+=("${dep_name}")
    else
      echo "[deps] ✗ ${dep_name}: missing (${dep_type})"
      checked_packages+=("${dep_name}")
      if [[ "${dep_type}" == "required" ]]; then
        missing_required+=("${dep_name}")
      else
        missing_optional+=("${dep_name}")
      fi
    fi
  done

  if [[ ${#missing_required[@]} -gt 0 ]]; then
    export MISSING_REQUIRED_DEPS="${missing_required[*]}"
    echo "[deps] Missing required dependencies: ${missing_required[*]}"
  else
    export MISSING_REQUIRED_DEPS=""
  fi

  if [[ ${#missing_optional[@]} -gt 0 ]]; then
    export MISSING_OPTIONAL_DEPS="${missing_optional[*]}"
    echo "[deps] Missing optional dependencies: ${missing_optional[*]}"
  else
    export MISSING_OPTIONAL_DEPS=""
  fi

  if [[ ${#missing_required[@]} -gt 0 ]]; then
    return 1
  else
    return 0
  fi
}

install_system_dependencies() {
  local missing_deps="$1"
  local auto_install="${AUTO_INSTALL_DEPS:-}"

  [[ -z "${missing_deps}" ]] && return 0

  local deps_array=()
  read -ra deps_array <<< "${missing_deps}"
  [[ ${#deps_array[@]} -eq 0 ]] && return 0

  echo "[deps] Installing missing dependencies: ${deps_array[*]}"

  if [[ "${auto_install}" != "yes" && "${auto_install}" != "true" ]]; then
    echo "[deps] The following dependencies need to be installed:"
    echo "       ${deps_array[*]}"
    echo -n "[deps] Install now? [Y/n] "
    read -r answer

    if [[ "${answer}" =~ ^[Nn] ]]; then
      echo "[deps] Skipping installation. Please install manually:"
      echo "       sudo apt install -y ${deps_array[*]}"
      return 1
    fi
  fi

  if ! sudo -n true 2>/dev/null; then
    echo "[deps] Requesting sudo privileges for apt install..."
  fi

  echo "[deps] Running: sudo apt install -y ${deps_array[*]}"
  if sudo apt install -y "${deps_array[@]}"; then
    echo "[deps] Successfully installed dependencies"
    echo "[deps] Verifying installation..."
    for dep in "${deps_array[@]}"; do
      if dpkg -l | grep -q "^ii.*${dep}"; then
        echo "[deps] ✓ ${dep}: installed and verified"
      else
        echo "[deps] ⚠ ${dep}: installed but verification failed"
      fi
    done
    return 0
  else
    echo "[deps] ERROR: Failed to install dependencies" >&2
    return 1
  fi
}

check_and_install_dependencies() {
  # Always run check (build base deps are collected even without BUILD_CONFIG_FILE).
  if check_system_dependencies; then
    echo "[deps] All required dependencies are satisfied"
    return 0
  fi

  local missing_deps_str="${MISSING_REQUIRED_DEPS:-}"
  if [[ -n "${missing_deps_str}" ]]; then
    if install_system_dependencies "${missing_deps_str}"; then
      echo "[deps] Re-checking dependencies after installation..."
      if check_system_dependencies; then
        echo "[deps] All dependencies are now satisfied"
        return 0
      else
        echo "[deps] ERROR: Some dependencies are still missing after installation" >&2
        return 1
      fi
    else
      echo "[deps] ERROR: Failed to install required dependencies" >&2
      return 1
    fi
  fi

  return 0
}

# Check and install system dependencies for a single package only (used by build.sh package).
# Always checks build base deps first, then the given package. Argument: pkg_dir or pkg_key (e.g. components/model_zoo/llm).
check_and_install_dependencies_for_package() {
  local pkg_arg="$1"
  [[ -z "${pkg_arg}" ]] && return 0

  local pkg_key
  if [[ -d "${pkg_arg}" ]]; then
    local abs_dir
    abs_dir="$(cd "${pkg_arg}" && pwd)"
    if [[ "${abs_dir}" == "${REPO_ROOT}/"* ]]; then
      pkg_key="${abs_dir#"${REPO_ROOT}"/}"
    else
      pkg_key="${pkg_arg}"
    fi
  else
    pkg_key="${pkg_arg}"
  fi

  echo "[deps] Checking system dependencies for package: ${pkg_key}"

  local deps_lines=()
  # Build base deps (build/package.xml) first so mm also requires build tools and repo.
  if [[ -f "${REPO_ROOT}/build/package.xml" ]] && grep -q '<system_depend' "${REPO_ROOT}/build/package.xml" 2>/dev/null; then
    while IFS='|' read -r dep_type dep_name check_cmd; do
      [[ -z "${dep_type}" || -z "${dep_name}" || -z "${check_cmd}" ]] && continue
      deps_lines+=("${dep_type}|${dep_name}|${check_cmd}")
    done < <(read_package_sysdeps_lines "build")
  fi
  while IFS='|' read -r dep_type dep_name check_cmd; do
    [[ -z "${dep_type}" || -z "${dep_name}" || -z "${check_cmd}" ]] && continue
    deps_lines+=("${dep_type}|${dep_name}|${check_cmd}")
  done < <(read_package_sysdeps_lines "${pkg_key}")

  if [[ ${#deps_lines[@]} -eq 0 ]]; then
    echo "[deps] No system dependencies defined for this package"
    return 0
  fi

  local missing_required=()
  local missing_optional=()
  local checked_packages=()

  for dep_line in "${deps_lines[@]}"; do
    IFS='|' read -r dep_type dep_name check_cmd <<< "${dep_line}"

    local already_checked=false
    for checked in "${checked_packages[@]}"; do
      if [[ "${checked}" == "${dep_name}" ]]; then
        already_checked=true
        break
      fi
    done
    [[ "${already_checked}" == "true" ]] && continue

    if check_single_dependency "${dep_name}" "${check_cmd}"; then
      echo "[deps] ✓ ${dep_name}: found"
      checked_packages+=("${dep_name}")
    else
      echo "[deps] ✗ ${dep_name}: missing (${dep_type})"
      checked_packages+=("${dep_name}")
      if [[ "${dep_type}" == "required" ]]; then
        missing_required+=("${dep_name}")
      else
        missing_optional+=("${dep_name}")
      fi
    fi
  done

  if [[ ${#missing_optional[@]} -gt 0 ]]; then
    echo "[deps] Missing optional dependencies: ${missing_optional[*]}"
  fi

  if [[ ${#missing_required[@]} -eq 0 ]]; then
    echo "[deps] All required dependencies are satisfied"
    return 0
  fi

  echo "[deps] Missing required dependencies: ${missing_required[*]}"
  local missing_required_str="${missing_required[*]}"
  if install_system_dependencies "${missing_required_str}"; then
    echo "[deps] Re-checking dependencies after installation..."
    for dep_line in "${deps_lines[@]}"; do
      IFS='|' read -r dep_type dep_name check_cmd <<< "${dep_line}"
      if [[ "${dep_type}" == "required" ]] && ! check_single_dependency "${dep_name}" "${check_cmd}"; then
        echo "[deps] ERROR: ${dep_name} still missing after installation" >&2
        return 1
      fi
    done
    echo "[deps] All dependencies are now satisfied"
    return 0
  else
    echo "[deps] ERROR: Failed to install required dependencies" >&2
    return 1
  fi
}



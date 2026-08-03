#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "Usage: sudo $0 USER" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
target_user=$1

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify this operating system." >&2
  exit 1
fi

. /etc/os-release
if [[ ${ID:-} != "debian" || ${VERSION_ID:-} != "12" ]]; then
  echo "Refusing to run: expected Debian 12, found ${PRETTY_NAME:-unknown}." >&2
  exit 1
fi

if ! id "$target_user" >/dev/null 2>&1; then
  echo "Refusing to run: user '$target_user' does not exist." >&2
  exit 1
fi

if ! lspci -nnk | grep -A4 -Ei 'VGA|Display|3D' | grep -qi 'Intel Corporation'; then
  echo "Refusing to run: no Intel display controller was detected." >&2
  exit 1
fi

if ! compgen -G '/dev/dri/renderD*' >/dev/null; then
  echo "Refusing to run: no GPU render device exists under /dev/dri." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

disable_source() {
  local source_path=$1
  local disabled_path="${source_path}.disabled"

  if [[ -e "$disabled_path" ]]; then
    echo "Refusing to overwrite existing backup $disabled_path." >&2
    exit 1
  fi

  mv "$source_path" "$disabled_path"
}

docker_source=/etc/apt/sources.list.d/docker.sources
if [[ -f "$docker_source" ]] &&
   grep -Fxq 'URIs: https://download.docker.com/linux/ubuntu' "$docker_source" &&
   grep -Fxq 'Suites: bookworm' "$docker_source"; then
  disable_source "$docker_source"
  echo "Disabled Ubuntu Docker repository configured with Debian's bookworm suite."
fi

duplicate_backports_source=/etc/apt/sources.list.d/palantir-bookworm-backports.list
if [[ -f "$duplicate_backports_source" ]] &&
   [[ $(<"$duplicate_backports_source") == 'deb https://deb.debian.org/debian bookworm-backports main' ]] &&
   grep -Eq '^deb .*deb\.debian\.org/debian/? +bookworm-backports .*main' /etc/apt/sources.list; then
  disable_source "$duplicate_backports_source"
  echo "Disabled duplicate bookworm-backports entry."
fi

echo "Refreshing Debian package metadata..."
apt-get update

declare -A repair_packages=([needrestart]=1)
for package_list in /var/lib/dpkg/info/*.list; do
  package=${package_list##*/}
  package=${package%.list}

  while IFS= read -r installed_path; do
    case "$installed_path" in
      /usr/share/perl5/*)
        if [[ ! -e "$installed_path" && ! -L "$installed_path" ]]; then
          repair_packages["$package"]=1
          break
        fi
        ;;
    esac
  done < "$package_list"
done

mapfile -t packages < <(printf '%s\n' "${!repair_packages[@]}" | LC_ALL=C sort)
echo "Restoring packages with missing Perl payloads: ${packages[*]}"
apt-get install --reinstall -y "${packages[@]}"

test -s /usr/share/perl5/NeedRestart.pm
perl -MNeedRestart -e 'print "needrestart Perl module restored\n"'

echo "Finishing interrupted package configuration..."
dpkg --configure -a
apt-get --fix-broken install -y

echo "Installing Intel OpenCL runtime and diagnostics..."
apt-get install -y intel-opencl-icd ocl-icd-libopencl1 clinfo

echo "Granting $target_user access to GPU device nodes..."
for device_group in render video; do
  if ! getent group "$device_group" >/dev/null; then
    echo "Required group '$device_group' is absent." >&2
    exit 1
  fi
  usermod -aG "$device_group" "$target_user"
done

echo "Verifying package integrity and OpenCL visibility..."
dpkg --audit
for package in "${packages[@]}"; do
  dpkg -V "$package"
done
opencl_summary=$(runuser -u "$target_user" -- clinfo -l)
printf '%s\n' "$opencl_summary"
if ! grep -q '^Platform #[0-9]' <<<"$opencl_summary"; then
  echo "OpenCL packages are installed, but no platform is visible to $target_user." >&2
  exit 1
fi

echo
echo "Repair complete. Log out and back in before running GPU workloads."

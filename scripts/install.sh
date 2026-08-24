#!/usr/bin/env bash
set -euo pipefail

generated="build/generated"
apply=0
force=0
with_boot_theme=0
with_motd=0

while (($#)); do
  case "$1" in
    --generated) generated="$2"; shift 2 ;;
    --apply) apply=1; shift ;;
    --force) force=1; shift ;;
    --with-boot-theme) with_boot_theme=1; shift ;;
    --with-motd) with_motd=1; shift ;;
    -h|--help)
      echo "usage: sudo scripts/install.sh [--generated DIR] [--apply] [--force] [--with-boot-theme] [--with-motd]"
      exit 0
      ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$generated" in
  /*) generated_dir="$generated" ;;
  *) generated_dir="$repo/$generated" ;;
esac

test -f "$generated_dir/manifest.json" || {
  echo "missing rendered configuration: $generated_dir/manifest.json" >&2
  echo "run scripts/render_config.py first" >&2
  exit 1
}

if grep -R -E -n '@RIVERBANK_[A-Z0-9_]+@' "$generated_dir" >/dev/null; then
  echo "rendered configuration still contains placeholders" >&2
  exit 1
fi

echo "RiverBank installation plan"
echo "  source: $generated_dir"
echo "  systemd: /etc/systemd/system"
echo "  health config: /etc/riverbank/health-monitor.json"
echo "  udev: /etc/udev/rules.d"
if ((with_boot_theme)); then
  echo "  Plymouth theme: /usr/share/plymouth/themes/riverbank"
fi
if ((with_motd)); then
  echo "  SSH welcome page: /etc/motd"
fi
echo "  optional/proxy and optional/viewturbo: not installed automatically"
echo "  services: copied but not enabled or started"

if ((!apply)); then
  echo "dry run only; add --apply to write system files"
  exit 0
fi

if ((EUID != 0)); then
  echo "--apply must run as root" >&2
  exit 1
fi

install_one() {
  local source=$1 target=$2
  if [[ -e "$target" && $force -ne 1 ]]; then
    if cmp -s "$source" "$target"; then
      return
    fi
    echo "refusing to overwrite $target; inspect it or use --force" >&2
    exit 1
  fi
  install -D -m 0644 "$source" "$target"
}

systemd_src="$generated_dir/config/systemd"
while IFS= read -r -d '' source; do
  relative=${source#"$systemd_src/"}
  [[ "$relative" == optional/* ]] && continue
  install_one "$source" "/etc/systemd/system/$relative"
done < <(find "$systemd_src" -type f -print0)

install_one "$generated_dir/health-monitor.json" "/etc/riverbank/health-monitor.json"
while IFS= read -r -d '' source; do
  install_one "$source" "/etc/udev/rules.d/$(basename "$source")"
done < <(find "$generated_dir/config/udev" -type f -print0)

if ((with_boot_theme)); then
  while IFS= read -r -d '' source; do
    relative=${source#"$generated_dir/config/plymouth/riverbank/"}
    install_one "$source" "/usr/share/plymouth/themes/riverbank/$relative"
  done < <(find "$generated_dir/config/plymouth/riverbank" -type f -print0)
  plymouth-set-default-theme -R riverbank
fi

if ((with_motd)); then
  install_one "$generated_dir/config/motd/motd" "/etc/motd"
fi

systemctl daemon-reload
udevadm control --reload
echo "installation complete; review and enable only the services supported by this device"

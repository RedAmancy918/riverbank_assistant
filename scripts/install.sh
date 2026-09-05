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
echo "  recovery config: /etc/riverbank/recovery.json"
echo "  provisioning config: /etc/riverbank/provisioning.json"
echo "  udev: /etc/udev/rules.d"
echo "  commands: /usr/local/bin"
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

install_executable() {
  local source=$1 target=$2
  if [[ -e "$target" && $force -ne 1 ]]; then
    if cmp -s "$source" "$target"; then
      return
    fi
    echo "refusing to overwrite $target; inspect it or use --force" >&2
    exit 1
  fi
  install -D -m 0755 "$source" "$target"
}

systemd_src="$generated_dir/config/systemd"
while IFS= read -r -d '' source; do
  relative=${source#"$systemd_src/"}
  [[ "$relative" == optional/* ]] && continue
  install_one "$source" "/etc/systemd/system/$relative"
done < <(find "$systemd_src" -type f -print0)

while IFS= read -r -d '' source; do
  install_executable "$source" "/usr/local/bin/$(basename "$source")"
done < <(find "$generated_dir/config/bin" -type f -print0)

install_one "$generated_dir/health-monitor.json" "/etc/riverbank/health-monitor.json"
install_one "$generated_dir/recovery.json" "/etc/riverbank/recovery.json"
install_one "$generated_dir/provisioning.json" "/etc/riverbank/provisioning.json"
while IFS= read -r -d '' source; do
  install_one "$source" "/etc/udev/rules.d/$(basename "$source")"
done < <(find "$generated_dir/config/udev" -type f -print0)

riverbank_user=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["user"])' "$generated_dir/manifest.json")
riverbank_data=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["data_dir"])' "$generated_dir/manifest.json")
riverbank_group=$(id -gn "$riverbank_user")
workshop_state=/var/lib/riverbank-workshop
workshop_trust=/etc/riverbank/workshop/trusted-keys
workshop_private="$workshop_state/device-signing-private.pem"
workshop_public="$workshop_trust/riverbank-local-device-v1.pem"
install -d -m 0700 -o "$riverbank_user" -g "$riverbank_group" "$workshop_state"
install -d -m 0755 -o root -g root "$workshop_trust"
install -d -m 0700 -o "$riverbank_user" -g "$riverbank_group" \
  "$riverbank_data/workshop" "$riverbank_data/ai/output/workshop-yolo"
install -d -m 0750 -o "$riverbank_user" -g "$riverbank_group" \
  "$riverbank_data/camera/gallery" \
  "$riverbank_data/paper-radar/data" \
  "$riverbank_data/paper-radar/reports" \
  "$riverbank_data/paper-radar/public" \
  "$riverbank_data/pomodoro" \
  "$riverbank_data/ui"
/usr/sbin/runuser -u "$riverbank_user" -- env HOME="$(getent passwd "$riverbank_user" | cut -d: -f6)" \
  /usr/bin/python3 "$repo/apps/paper-radar/scripts/align_hermes_job.py" --repo "$repo"
if [[ ! -e "$workshop_private" && ! -e "$workshop_public" ]]; then
  private_tmp=$(mktemp "$workshop_state/.device-signing-private.XXXXXX")
  public_tmp=$(mktemp "$workshop_trust/.riverbank-local-device-v1.XXXXXX")
  /usr/bin/openssl genpkey -algorithm Ed25519 -out "$private_tmp"
  /usr/bin/openssl pkey -in "$private_tmp" -pubout -out "$public_tmp"
  chown "$riverbank_user:$riverbank_group" "$private_tmp"
  chmod 0600 "$private_tmp"
  chown root:root "$public_tmp"
  chmod 0644 "$public_tmp"
  mv "$private_tmp" "$workshop_private"
  mv "$public_tmp" "$workshop_public"
elif [[ ! -f "$workshop_private" || ! -f "$workshop_public" ]]; then
  echo "Workshop signing identity is incomplete; refusing to replace one side" >&2
  exit 1
fi

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

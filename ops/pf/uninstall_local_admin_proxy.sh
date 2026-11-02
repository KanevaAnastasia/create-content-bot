#!/bin/zsh

set -eu

pf_conf="/etc/pf.conf"
candidate="$(mktemp /private/tmp/contentfabric-pf.XXXXXX)"

cleanup() {
  /bin/rm -f "$candidate"
}
trap cleanup EXIT

if [[ "$(/usr/bin/id -u)" -ne 0 ]]; then
  echo "This uninstaller must run as root." >&2
  exit 1
fi

/usr/bin/grep -vF 'rdr-anchor "com.contentfabric.admin"' "$pf_conf" \
  | /usr/bin/grep -vF 'load anchor "com.contentfabric.admin"' > "$candidate"
/sbin/pfctl -nf "$candidate"
/usr/bin/install -o root -g wheel -m 644 "$candidate" "$pf_conf"
/bin/rm -f /etc/pf.anchors/com.contentfabric.admin
/sbin/pfctl -f "$pf_conf"

echo "Content Fabric local admin proxy removed."

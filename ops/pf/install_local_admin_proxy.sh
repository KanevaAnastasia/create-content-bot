#!/bin/zsh

set -eu

anchor_source="/opt/content_fabric/ops/pf/com.contentfabric.admin"
anchor_target="/etc/pf.anchors/com.contentfabric.admin"
pf_conf="/etc/pf.conf"
candidate="$(mktemp /private/tmp/contentfabric-pf.XXXXXX)"

cleanup() {
  /bin/rm -f "$candidate"
}
trap cleanup EXIT

if [[ "$(/usr/bin/id -u)" -ne 0 ]]; then
  echo "This installer must run as root." >&2
  exit 1
fi

/usr/bin/install -o root -g wheel -m 600 "$anchor_source" "$anchor_target"

/usr/bin/grep -qF 'rdr-anchor "com.contentfabric.admin"' "$pf_conf" && has_rdr=1 || has_rdr=0
/usr/bin/grep -qF 'load anchor "com.contentfabric.admin"' "$pf_conf" && has_load=1 || has_load=0

/usr/bin/awk -v add_rdr="$((1 - has_rdr))" -v add_load="$((1 - has_load))" '
  { print }
  add_rdr && $0 == "rdr-anchor \"com.apple/*\"" {
    print "rdr-anchor \"com.contentfabric.admin\""
  }
  END {
    if (add_load) {
      print "load anchor \"com.contentfabric.admin\" from \"/etc/pf.anchors/com.contentfabric.admin\""
    }
  }
' "$pf_conf" > "$candidate"

if ! /usr/bin/grep -qF 'rdr-anchor "com.contentfabric.admin"' "$candidate"; then
  echo 'Could not add the Content Fabric rdr anchor.' >&2
  exit 1
fi

/sbin/pfctl -nf "$candidate"

if [[ ! -e /etc/pf.conf.contentfabric-before-local-admin ]]; then
  /bin/cp -p "$pf_conf" /etc/pf.conf.contentfabric-before-local-admin
fi
/usr/bin/install -o root -g wheel -m 644 "$candidate" "$pf_conf"
/sbin/pfctl -f "$pf_conf"

echo "Content Fabric local admin proxy installed: 127.0.0.1:80 -> 127.0.0.1:8000"

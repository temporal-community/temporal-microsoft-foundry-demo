#!/bin/sh
# Write $TFD_STATE/vm-setup.sh: the script run on the VM (through az vm run-command) to
# install the server, the reverse proxy and their configuration. It embeds the bearer token,
# so the output file is private; delete it after use.
#   TFD_STATE=<private state dir> ./gen_setup.sh [path to the linux server binary]
set -eu
: "${TFD_STATE:?set TFD_STATE to the private state directory}"
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"; cd "$TFD_STATE"
. ./env.sh; . ./vm.env; . ./storage.env
BIN="${1:-/tmp/foundry/build/temporal-server-linux-amd64}"
SHA=$(shasum -a 256 "$BIN" | cut -c1-64)
STATIC=$(sed -e 's#filepath: "config/dynamicconfig/development-sql.yaml"#filepath: "/etc/temporal/dynamicconfig/development-sql.yaml"#' $REPO/infra/server/config/development-sqlite-file.yaml)
DYN=$(sed -e 's/value: \["azure-foundry", "subprocess"\]/value: ["azure-foundry"]/' $REPO/infra/server/config/dynamicconfig/development-sql.yaml; printf '\nmatching.numTaskqueueReadPartitions:\n  - value: 1\nmatching.numTaskqueueWritePartitions:\n  - value: 1\n')
umask 077
cat > vm-setup.sh <<EOF
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq caddy curl docker.io >/dev/null
systemctl enable --now docker >/dev/null 2>&1
id temporal >/dev/null 2>&1 || useradd --system --home /var/lib/temporal --shell /usr/sbin/nologin temporal
mkdir -p /opt/temporal /etc/temporal/dynamicconfig /var/lib/temporal /var/log/caddy
curl -fsSL -o /opt/temporal/temporal-server.new '$BINARY_URL'
echo '$SHA  /opt/temporal/temporal-server.new' | sha256sum -c - >/dev/null
systemctl stop temporal >/dev/null 2>&1 || true
install -m 0755 /opt/temporal/temporal-server.new /opt/temporal/temporal-server && rm -f /opt/temporal/temporal-server.new
cat > /etc/temporal/development-sqlite-file.yaml <<'TFD_EOF'
$STATIC
TFD_EOF
cat > /etc/temporal/dynamicconfig/development-sql.yaml <<'TFD_EOF'
$DYN
TFD_EOF
cat > /etc/caddy/Caddyfile <<'TFD_EOF'
$(sed -e "s/{\$TFD_HOST}/$VM_FQDN/" -e "s/{\$TFD_TOKEN}/$(cat certs/token)/" "$HERE/Caddyfile")
TFD_EOF
cat > /etc/systemd/system/temporal.service <<'TFD_EOF'
$(cat "$HERE/temporal.service")
TFD_EOF
chown -R temporal:temporal /var/lib/temporal
chown -R caddy:caddy /var/log/caddy
chmod 640 /etc/caddy/Caddyfile; chown root:caddy /etc/caddy/Caddyfile
if [ ! -x /usr/local/bin/temporal ]; then curl -sSf https://temporal.download/cli.sh | HOME=/root sh >/dev/null 2>&1 && install -m 0755 /root/.temporalio/bin/temporal /usr/local/bin/temporal; fi
if [ ! -x /usr/local/bin/uv ]; then curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1; fi
printf 'net.ipv4.tcp_keepalive_probes = 2\\n' > /etc/sysctl.d/90-tfd.conf; sysctl -q --system
systemctl daemon-reload
systemctl enable --now temporal >/dev/null 2>&1
# The Temporal UI, served under /temporal-ui by the proxy.
docker rm -f temporal-ui >/dev/null 2>&1 || true
docker run -d --name temporal-ui --restart unless-stopped --network host \\
  -e TEMPORAL_ADDRESS=127.0.0.1:7233 -e TEMPORAL_UI_PORT=8081 -e TEMPORAL_UI_PUBLIC_PATH=/temporal-ui \\
  -e TEMPORAL_DISABLE_WRITE_ACTIONS=true \\
  temporalio/ui:\${TEMPORAL_UI_VERSION:-2.58.1} >/dev/null
systemctl restart caddy
for i in \$(seq 1 20); do /usr/local/bin/temporal operator cluster health --address 127.0.0.1:7233 >/dev/null 2>&1 && break; sleep 2; done
/usr/local/bin/temporal operator namespace create -n default --address 127.0.0.1:7233 2>&1 | tail -1 || true
echo "temporal: \$(systemctl is-active temporal) caddy: \$(systemctl is-active caddy)"
ss -ltnH | awk '{print \$4}' | grep -E ':(443|7233|7243)\$' | sort | tr '\n' ' '; echo
/opt/temporal/temporal-server --version | head -1
EOF
bash -n vm-setup.sh
grep -c "blob.core.windows.net" vm-setup.sh

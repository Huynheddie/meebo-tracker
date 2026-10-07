#!/bin/sh
# Installs the tracker on a fresh Ubuntu server. Run as root. Safe to run again to update.
set -eu
apt-get update -q
apt-get install -y -q python3 git curl tzdata util-linux

id meebo >/dev/null 2>&1 || useradd --system --home-dir /var/lib/meebo --shell /usr/sbin/nologin meebo
install -d -o meebo -g meebo -m 750 /var/lib/meebo

if [ -d /opt/meebo-tracker/.git ]; then
  git -C /opt/meebo-tracker pull -q
else
  git clone -q https://github.com/Huynheddie/meebo-tracker /opt/meebo-tracker
fi

# Start from the state GitHub has, so nothing already seen alerts again. Never overwrite it later.
[ -f /var/lib/meebo/state.json ] || install -o meebo -g meebo -m 640 /opt/meebo-tracker/state.json /var/lib/meebo/state.json
# Keys live here, readable only by root and the tracker. Never overwritten once it exists.
[ -f /etc/meebo.env ] || install -o root -g meebo -m 640 /opt/meebo-tracker/server/meebo.env.example /etc/meebo.env

install -m 644 /opt/meebo-tracker/server/meebo@.service /opt/meebo-tracker/server/meebo-x.timer \
  /opt/meebo-tracker/server/meebo-sites.timer /opt/meebo-tracker/server/meebo-reminder.service \
  /opt/meebo-tracker/server/meebo-reminder.timer /etc/systemd/system/
systemctl daemon-reload
echo "Installed. Fill in /etc/meebo.env, then: systemctl enable --now meebo-x.timer meebo-sites.timer meebo-reminder.timer"

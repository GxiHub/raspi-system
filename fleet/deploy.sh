#!/bin/bash
# Fleet deploy: ./deploy.sh <node> <module>
set -e
FLEET_DIR="$(cd "$(dirname "$0")" && pwd)"
NODE=$1
MODULE=$2

if [ -z "$NODE" ] || [ -z "$MODULE" ]; then
  echo "Usage: $0 <node> <module>"
  echo "Nodes: $(ls $FLEET_DIR/nodes/ | sed s/.conf//g | tr "\n" " ")"
  echo "Modules: $(ls $FLEET_DIR/modules/ | tr "\n" " ")"
  exit 1
fi

NODE_CONF="$FLEET_DIR/nodes/$NODE.conf"
[ -f "$NODE_CONF" ] || { echo "ERROR: $NODE_CONF not found"; exit 1; }
source "$NODE_CONF"

MODULE_DIR="$FLEET_DIR/modules/$MODULE"
[ -d "$MODULE_DIR" ] || { echo "ERROR: module $MODULE not found"; exit 1; }

echo "[fleet] Deploying $MODULE → $NODE ($NODE_HOST)"
rsync -av --exclude="*.pyc" --exclude="__pycache__" \
  "$MODULE_DIR/files/" \
  "$NODE_USER@$NODE_HOST:$NODE_HOME/"

if [ -f "$MODULE_DIR/post_deploy.sh" ]; then
  echo "[fleet] Running post_deploy on $NODE..."
  ssh "$NODE_USER@$NODE_HOST" "bash -s" < "$MODULE_DIR/post_deploy.sh"
fi
echo "[fleet] Done: $MODULE → $NODE"

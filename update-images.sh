#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${1:-docker-compose.yml}"
DRY_RUN=false

for arg in "$@"; do
  [[ "$arg" == "--dry-run" ]] && DRY_RUN=true
done

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Error: $COMPOSE_FILE not found" >&2
  exit 1
fi

for cmd in curl jq; do
  command -v "$cmd" &>/dev/null || { echo "Error: $cmd is required" >&2; exit 1; }
done

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

UPDATES=()

fetch_dockerhub_tags() {
  local namespace="$1" repo="$2"
  local url="https://hub.docker.com/v2/repositories/${namespace}/${repo}/tags?page_size=100&ordering=last_updated"
  curl -sf "$url" 2>/dev/null | jq -r '.results[].name // empty' 2>/dev/null
}

fetch_gcr_tags() {
  local path="$1"
  local token
  token=$(curl -sf "https://gcr.io/v2/token?scope=repository:${path}:pull&service=gcr.io" 2>/dev/null | jq -r '.token // empty' 2>/dev/null)
  [[ -z "$token" ]] && return 1
  curl -sf -H "Authorization: Bearer $token" "https://gcr.io/v2/${path}/tags/list" 2>/dev/null | jq -r '.tags[] // empty' 2>/dev/null
}

filter_stable_versions() {
  local suffix="${1:-}"
  if [[ -n "$suffix" ]]; then
    grep -E "^[0-9]+\.[0-9]+\.[0-9]+(-${suffix})?\$" | grep -vE '(rc|beta|alpha|dev|nightly|test)'
  else
    grep -E '^v?[0-9]+\.[0-9]+\.[0-9]+$' | grep -vE '(rc|beta|alpha|dev|nightly|test)'
  fi
}

version_sort() {
  sed 's/^v//' | sort -t. -k1,1n -k2,2n -k3,3n
}

get_latest_tag() {
  local image="$1"
  local current_tag="$2"
  local registry="" path="" namespace="" repo=""

  if [[ "$image" == gcr.io/* ]]; then
    registry="gcr"
    path="${image#gcr.io/}"
    path="${path%:*}"
  else
    registry="dockerhub"
    local name="${image%:*}"
    namespace="${name%/*}"
    repo="${name##*/}"
  fi

  local arch_suffix=""
  if [[ "$current_tag" =~ ^[0-9]+\.[0-9]+\.[0-9]+-(.+)$ ]]; then
    arch_suffix="${BASH_REMATCH[1]}"
  fi

  local tags
  if [[ "$registry" == "gcr" ]]; then
    tags=$(fetch_gcr_tags "$path") || return 1
  else
    tags=$(fetch_dockerhub_tags "$namespace" "$repo") || return 1
  fi

  [[ -z "$tags" ]] && return 1

  local latest
  if [[ -n "$arch_suffix" ]]; then
    latest=$(echo "$tags" | filter_stable_versions "$arch_suffix" | sed "s/-${arch_suffix}$//" | version_sort | tail -1)
    [[ -n "$latest" ]] && latest="${latest}-${arch_suffix}"
  else
    latest=$(echo "$tags" | filter_stable_versions | version_sort | tail -1)
    local has_v_prefix
    has_v_prefix=$(echo "$tags" | grep -c "^v[0-9]" || true)
    if [[ "$has_v_prefix" -gt 0 && ! "$latest" =~ ^v ]]; then
      latest="v${latest}"
    fi
  fi

  echo "$latest"
}

echo -e "${BOLD}Checking for image updates...${NC}"
echo ""

while IFS= read -r line; do
  image=$(echo "$line" | sed 's/.*image:\s*//' | tr -d '"' | tr -d "'" | xargs)

  current_tag="${image##*:}"
  image_name="${image%:*}"

  printf "  %-40s " "$image_name"

  if [[ "$current_tag" == "latest" ]]; then
    latest=$(get_latest_tag "$image" "latest" 2>/dev/null) || latest=""
    if [[ -n "$latest" ]]; then
      echo -e "${YELLOW}latest${NC} → ${GREEN}${latest}${NC}"
      UPDATES+=("${image}|${image_name}:${latest}")
    else
      echo -e "${YELLOW}latest${NC} (could not resolve)"
    fi
  else
    latest=$(get_latest_tag "$image" "$current_tag" 2>/dev/null) || latest=""
    if [[ -z "$latest" ]]; then
      echo -e "${current_tag} (could not resolve)"
    elif [[ "$latest" == "$current_tag" ]]; then
      echo -e "${GREEN}${current_tag} (up to date)${NC}"
    else
      echo -e "${RED}${current_tag}${NC} → ${GREEN}${latest}${NC}"
      UPDATES+=("${image}|${image_name}:${latest}")
    fi
  fi
done < <(grep 'image:' "$COMPOSE_FILE")

echo ""

if [[ ${#UPDATES[@]} -eq 0 ]]; then
  echo -e "${GREEN}All images are up to date.${NC}"
  exit 0
fi

echo -e "${BOLD}${#UPDATES[@]} update(s) available.${NC}"

if $DRY_RUN; then
  echo -e "${CYAN}Dry run — no changes made.${NC}"
  exit 0
fi

echo ""
read -rp "Apply updates? [y/N] " confirm
if [[ "$confirm" != [yY] ]]; then
  echo "Aborted."
  exit 0
fi

for update in "${UPDATES[@]}"; do
  old="${update%%|*}"
  new="${update##*|}"
  sed -i.bak "s|${old}|${new}|g" "$COMPOSE_FILE"
done

rm -f "${COMPOSE_FILE}.bak"
echo -e "${GREEN}Updated ${#UPDATES[@]} image(s) in ${COMPOSE_FILE}.${NC}"

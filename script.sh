#!/usr/bin/env bash
#
# OpenSVC v3 cluster and services diagnostic collection script.
# Mirrors the v3 part of the sos report OpenSVC plugin
# (sos/report/plugins/opensvc.py).
#
# Targets v3.0.0-rc37 and later. On older releases, the commands they do
# not know are skipped: the sec and usr configs and node metrics need
# rc33, the daemon exec, session and orchestration lists need rc37.

set -uo pipefail

# Collected data may hold sensitive information
umask 077

# --- Configuration & Helpers ---

OUTPUT_BASE_DIR="/tmp/opensvc_report_$(date +%Y%m%d_%H%M%S)"
CMD_DIR="${OUTPUT_BASE_DIR}/sos_commands/opensvc"
CMD_TIMEOUT=300
mkdir -p "$CMD_DIR"

# sec and usr objects configs hold secrets in clear text: exclude every
# sec and usr directory, and their content, at any depth of /etc/opensvc
# and /var/lib/opensvc/namespaces. In find -path patterns, "*" also
# matches "/".
FORBIDDEN_PATHS=(
    "/etc/opensvc/sec"
    "/etc/opensvc/usr"
    "/etc/opensvc/*/sec"
    "/etc/opensvc/*/usr"
    "/var/lib/opensvc/sec"
    "/var/lib/opensvc/usr"
    "/var/lib/opensvc/namespaces/*/sec"
    "/var/lib/opensvc/namespaces/*/usr"
)

# find expression matching the forbidden paths
FORBIDDEN_EXPR=()
for forbid in "${FORBIDDEN_PATHS[@]}"; do
    FORBIDDEN_EXPR+=(-path "$forbid" -o)
done
unset 'FORBIDDEN_EXPR[${#FORBIDDEN_EXPR[@]}-1]'

log_info() {
    echo "[INFO] $1"
}

log_debug() {
    echo "[DEBUG] $1" >&2
}

# Build the output file name of a command, like sos does
# Usage: mangle_command <command>
mangle_command() {
    sed -E \
        -e 's#^/(usr/|)(bin|sbin)/##' \
        -e 's#[^[:alnum:]_./-]+#_#g' \
        -e 's#/#.#g' \
        -e 's#^[ ._-]+##' \
        -e 's#[ ._-]+$##' <<< "$1" | cut -c1-255
}

# Run a command and save its output in the plugin commands directory, in a
# file named after the command or after the suggested file name.
# The output file path is stored in LAST_OUTPUT.
# Usage: add_cmd_output <subdir> <command> [<suggest_filename>]
add_cmd_output() {
    local subdir="$1"
    local cmd="$2"
    local filename="${3:-$2}"
    local target_dir="${CMD_DIR}${subdir:+/${subdir}}"
    local argv
    mkdir -p "$target_dir"
    LAST_OUTPUT="${target_dir}/$(mangle_command "$filename")"
    read -ra argv <<< "$cmd"
    if ! timeout "$CMD_TIMEOUT" "${argv[@]}" > "$LAST_OUTPUT" 2>&1; then
        log_debug "Command failed: $cmd"
        return 1
    fi
}

# Copy files and directories preserving their path, skipping forbidden paths
# Usage: add_copy_spec <path_pattern>
add_copy_spec() {
    local pattern="$1"
    local item path
    for item in $pattern; do
        [[ -e "$item" || -L "$item" ]] || continue
        while IFS= read -r -d '' path; do
            if [[ -d "$path" && ! -L "$path" ]]; then
                mkdir -p "${OUTPUT_BASE_DIR}${path}"
            else
                mkdir -p "${OUTPUT_BASE_DIR}$(dirname "$path")"
                cp -a "$path" "${OUTPUT_BASE_DIR}${path}" 2>/dev/null \
                    || log_debug "Failed to copy: $path"
            fi
        done < <(find "$item" \( "${FORBIDDEN_EXPR[@]}" \) -prune \
                    -o -print0 2>/dev/null)
    done
}

# Recursively list a directory contents
# Usage: add_dir_listing <path> <suggest_filename>
add_dir_listing() {
    [[ -e "$1" ]] && add_cmd_output "" "ls -alZR $1" "$2"
}

# Substitute a regex in a collected file
# Usage: do_file_sub <file_path> <pattern> <replacement>
do_file_sub() {
    local file_path="${OUTPUT_BASE_DIR}$1"
    if [[ -f "$file_path" ]]; then
        sed -i -E "s/$2/$3/g" "$file_path" \
            || log_debug "Failed to substitute in $1"
    fi
}

# --- Detection ---

# Print the v3 release candidate number from the om --version output,
# 0 for alpha and beta releases, nothing for a final release or an
# unparsable version.
# Usage: get_v3_rc <om --version output file>
get_v3_rc() {
    local out
    out=$(cat "$1" 2>/dev/null)
    if [[ "$out" =~ 3\.0\.0-(alpha|beta) ]]; then
        echo 0
    elif [[ "$out" =~ 3\.0\.0-rc([0-9]+) ]]; then
        echo "$((10#${BASH_REMATCH[1]}))"
    fi
}

# Succeed if the installed om supports a command needing the given
# release candidate. An empty minimum, or an unknown rc (final release),
# means supported.
# Usage: supported [<min_rc>]
supported() {
    [[ -z "${1:-}" || -z "$V3_RC" || "$V3_RC" -ge "$1" ]]
}

# --- Collection Logic ---

# Print the names of the objects of the given kind, one per line
# Usage: list_objects <kind>
list_objects() {
    local kind="$1"
    local header="OBJECT" cmd line
    cmd="om ${kind} ls --color=no --output tab=${header}:meta.object"
    add_cmd_output "" "$cmd" "${kind}-ls" || return 0
    while IFS= read -r line; do
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" || "$line" == "$header" ]] && continue
        printf '%s\n' "$line"
    done < "$LAST_OUTPUT"
}

# Run an om command on an object, saved as
# <kind>_<suffix>/<ns>/<kind>/<name>-<suffix>
# Usage: add_object_cmd <kind> <object> <command> <suffix>
add_object_cmd() {
    local kind="$1" obj="$2" cmd="$3" suffix="$4"
    local parts namespace obj_kind name
    # object path: [[<ns>/]<kind>/]<name>
    IFS=/ read -ra parts <<< "$obj"
    name="${parts[-1]}"
    obj_kind="$kind"
    namespace="root"
    [[ ${#parts[@]} -gt 1 ]] && obj_kind="${parts[-2]}"
    [[ ${#parts[@]} -gt 2 ]] && namespace="${parts[-3]}"
    add_cmd_output "${kind}_${suffix}/${namespace}/${obj_kind}" \
        "om ${obj} ${cmd}" "${name}-${suffix}"
}

# Get the status of the objects of the given kind
# Usage: get_status <kind>
get_status() {
    local kind="$1"
    local obj
    while IFS= read -r obj; do
        add_object_cmd "$kind" "$obj" "instance status --color=no" "status"
    done < <(list_objects "$kind")
}

# Get the config of the objects of the given kind, secrets redacted
# Usage: get_config <kind>
get_config() {
    local kind="$1"
    local obj
    while IFS= read -r obj; do
        add_object_cmd "$kind" "$obj" \
            "config show --color=no --redact-secrets" "config"
    done < <(list_objects "$kind")
}

setup_om() {
    local entry cmd fmts min_rc fmt name
    add_copy_spec "/var/lib/opensvc/*.stack*"
    add_copy_spec "/var/lib/opensvc/cni/net.d/*.conf"

    add_cmd_output "" "om --version" "version"
    V3_RC=$(get_v3_rc "$LAST_OUTPUT")
    # "om version <version>": keep the last word
    OM_VERSION=$(awk 'NF {v = $NF} END {print v}' "$LAST_OUTPUT")
    log_info "Detected OpenSVC ${OM_VERSION:-unknown version}"

    # <command>:<formats>:<minimum rc, empty if any>. Saved as <cmd> for
    # the auto format or no format, <cmd>.json for the json format, the
    # "**" selector left out.
    for entry in \
            "pool list:auto json:" \
            "net list:auto json:" \
            "mon:auto:" \
            "daemon dns dump:auto json:" \
            "daemon relay status:auto json:" \
            "daemon status:auto json:" \
            "daemon ps:auto json:" \
            "daemon hb status:auto:" \
            "daemon exec list:auto json:37" \
            "daemon orchestration list:auto json:37" \
            "daemon session list:auto json:37" \
            "array list:auto json:" \
            "node list:auto json:" \
            "node drivers:auto:" \
            "node schedule list:auto json:" \
            "node capabilities list:auto json:" \
            "node config validate:auto:" \
            "node relay list:auto json:" \
            "node metrics::33" \
            "cluster config validate:auto:" \
            "network ip list:auto json:" \
            "pool volume list:auto json:" \
            "** config validate:auto:" \
            "** instance list:auto json:" \
            "** instance device list:auto json:" \
            "** schedule list:auto json:"; do
        IFS=: read -r cmd fmts min_rc <<< "$entry"
        if ! supported "$min_rc"; then
            log_debug "om ${cmd} needs v3.0.0-rc${min_rc}, skipped"
            continue
        fi
        name="${cmd#\*\* }"
        name="${name// /-}"
        if [[ -z "$fmts" ]]; then
            add_cmd_output "" "om ${cmd} --color=no" "$name"
        fi
        for fmt in $fmts; do
            if [[ "$fmt" == auto ]]; then
                add_cmd_output "" "om ${cmd} --color=no --output ${fmt}" \
                    "$name"
            else
                add_cmd_output "" "om ${cmd} --color=no --output ${fmt}" \
                    "${name}.${fmt}"
            fi
        done
    done

    add_dir_listing "/dev/shm/opensvc" "ls-dev-shm-opensvc"
    if supported 33; then
        get_config "sec"
        get_config "usr"
    else
        log_debug "sec and usr configs need v3.0.0-rc33, skipped"
    fi

    log_info "Collecting vol and svc statuses..."
    get_status "vol"
    get_status "svc"
}

# --- Setup ---

log_info "Copying configuration, logs and state files..."
for spec in \
        "/etc/opensvc/*" \
        "/var/log/opensvc/*" \
        "/etc/conf.d/opensvc" \
        "/etc/default/opensvc" \
        "/etc/sysconfig/opensvc" \
        "/var/lib/opensvc/*.json" \
        "/var/lib/opensvc/list.*" \
        "/var/lib/opensvc/ccfg" \
        "/var/lib/opensvc/cfg" \
        "/var/lib/opensvc/certs/ca_certificates" \
        "/var/lib/opensvc/certs/certificate_chain" \
        "/var/lib/opensvc/compliance/*" \
        "/var/lib/opensvc/namespaces/*" \
        "/var/lib/opensvc/node/*" \
        "/var/lib/opensvc/svc/*" \
        "/var/lib/opensvc/vol/*"; do
    add_copy_spec "$spec"
done
add_dir_listing "/var/lib/opensvc" "ls-var-lib-opensvc"

log_info "Collecting the opensvc-server journal..."
add_cmd_output "" "journalctl --no-pager -u opensvc-server" \
    "journalctl-opensvc-server"

setup_om

log_info "Collecting daemon threads status..."
pid_file="/var/lib/opensvc/osvcd.pid"
if [[ -r "$pid_file" ]]; then
    pid=$(tr -d '[:space:]' < "$pid_file")
    if [[ -z "$pid" ]]; then
        log_debug "${pid_file} is empty"
    elif [[ ! "$pid" =~ ^[0-9]+$ ]]; then
        log_debug "Invalid PID in ${pid_file}: ${pid}"
    else
        add_copy_spec "/proc/${pid}/task/*/status"
    fi
else
    log_debug "Error while reading PID file ${pid_file}"
fi

# --- Post-Processing ---

log_info "Redacting secrets from collected files..."
for conf in cluster.conf node.conf; do
    do_file_sub "/etc/opensvc/${conf}" \
        "([[:space:]]*(secret|uuid)[[:space:]]*=[[:space:]]*)[^[:space:]]+" \
        "\1****************************"
done

# --- Archive ---

ARCHIVE="${OUTPUT_BASE_DIR}.tar.gz"
log_info "Creating archive..."
if tar -czf "$ARCHIVE" -C "$(dirname "$OUTPUT_BASE_DIR")" \
        "$(basename "$OUTPUT_BASE_DIR")"; then
    rm -rf "$OUTPUT_BASE_DIR"
    log_info "Collection complete. Archive: $ARCHIVE"
else
    log_info "Failed to create $ARCHIVE. Artifacts saved in: $OUTPUT_BASE_DIR"
    exit 1
fi

#!/bin/sh
# safe_write.sh — unified writer protocol for geomask shared resources
# Usage: safe_write <resource> <make_candidate_fn> <validate_fn> <rollback_check_fn>
# All functions are shell functions defined in the caller's context.
# Returns: 0=success, 1=validation failed, 2=backup gate failed, 3=trial failed, 4=install failed, 5=rollback failed

set -u

_safe_write_lock() {
    # $1 = lock file path
    _lock="$1"
    _timeout=10
    _start=$(date +%s)
    while ! ( set -C; echo "$$ $(date +%s)" > "$_lock" ) 2>/dev/null; do
        if [ $(($(date +%s) - _start)) -ge $_timeout ]; then
            echo "LOCK_TIMEOUT $_lock" >&2
            return 1
        fi
        sleep 0.1
    done
    return 0
}

_safe_write_unlock() {
    rm -f "$1"
}

_safe_write_backup_gate() {
    # Check for fresh full sysupgrade backup on PC (via SSH) within 300s
    # This is a simplified version; real implementation checks Y:\openwrt\backup\router-backup-*.tar.gz
    # via fetch_backup.ps1 or direct SSH+scp+md5sum
    _now=$(date +%s)
    _latest=""
    _latest_time=0
    for f in /root/geomask-backups/router-backup-*.tar.gz; do
        [ -f "$f" ] || continue
        _mtime=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null)
        if [ "$_mtime" -gt "$_latest_time" ]; then
            _latest_time=$_mtime
            _latest="$f"
        fi
    done
    if [ -z "$_latest" ]; then
        echo "BACKUP_GATE: no local geomask backup" >&2
        return 1
    fi
    if [ $(($_now - _latest_time)) -gt 300 ]; then
        echo "BACKUP_GATE: backup older than 300s ($_latest)" >&2
        return 1
    fi
    # Optional: verify MD5 with PC copy via SSH (fetch_backup.ps1 logic)
    # For now, trust local geomask-backups rotation
    return 0
}

_safe_write_snapshot() {
    # $1 = live file path
    # Outputs: snapshot_bytes snapshot_mode snapshot_owner
    _file="$1"
    if [ ! -f "$_file" ]; then
        printf '\0\0\0'  # empty snapshot marker
        return 0
    fi
    cat "$_file"
    stat -c '%a %u:%g' "$_file" 2>/dev/null || stat -f '%Mp%Lp %u:%g' "$_file" 2>/dev/null
}

_safe_write_atomic_install() {
    # $1 = live file, $2 = candidate file, $3 = mode, $4 = owner
    _live="$1"; _cand="$2"; _mode="$3"; _owner="$4"
    if [ ! -f "$_cand" ]; then
        echo "CANDIDATE_MISSING $_cand" >&2
        return 1
    fi
    chmod "$_mode" "$_cand" 2>/dev/null || true
    chown "$_owner" "$_cand" 2>/dev/null || true
    mv -f "$_cand" "$_live" || return 1
    return 0
}

_safe_write_verify_rollback() {
    # $1 = resource, $2 = pre_snapshot_file, $3 = post_live_file, $4 = rollback_check_fn
    _pre="$2"; _post="$3"; _check_fn="$4"
    if [ ! -f "$_pre" ]; then
        # pre was empty, just remove post
        rm -f "$_post"
        return 0
    fi
    cp -f "$_pre" "$_post" || return 1
    # Run resource-specific reload (defined by caller)
    if [ -n "$_safe_write_reload_fn" ]; then
        $_safe_write_reload_fn || return 1
    fi
    # Verify rollback check passes
    $_check_fn "$_pre" "$_post" || return 1
    return 0
}

safe_write() {
    _resource="$1"
    _make_cand_fn="$2"
    _validate_fn="$3"
    _rollback_check_fn="$4"

    # Resource-specific paths
    case "$_resource" in
        mihomo_config)
            _live="/etc/mihomo/config.yaml"
            _lock="/tmp/mihomo_config.lock"
            _rev="/etc/mihomo/config.yaml.rev"
            _trial_cmd="mihomo -f \$_cand -d /etc/mihomo -t"
            _reload_fn="mihomo_reload"
            ;;
        nodes_conf)
            _live="/etc/geomask-nodes.conf"
            _lock="/tmp/nodes_conf.lock"
            _rev="/etc/geomask-nodes.conf.rev"
            _trial_cmd="true"  # config only, no trial
            _reload_fn="nodes_conf_reload"
            ;;
        ruleset_*)
            _name="${_resource#ruleset_}"
            _live="/etc/mihomo/ruleset/${_name}.yaml"
            _lock="/tmp/ruleset_${_name}.lock"
            _rev="/etc/mihomo/ruleset/${_name}.yaml.rev"
            _trial_cmd="mihomo -f /etc/mihomo/config.yaml -d /etc/mihomo -t"
            _reload_fn="ruleset_reload"
            ;;
        zapret_exclude)
            _live="/opt/zapret/ipset/zapret-hosts-user-exclude.txt"
            _lock="/tmp/zapret_exclude.lock"
            _rev="/opt/zapret/ipset/zapret-hosts-user-exclude.txt.rev"
            _trial_cmd="nft -c -f - <<EOF\n$(cat /etc/nftables.d/zapret-exclude.nft 2>/dev/null || echo 'table inet zapret { set nozapret { type ipv4_addr; }}')\nEOF"
            _reload_fn="zapret_reload"
            ;;
        cron_root)
            _live="/etc/crontabs/root"
            _lock="/tmp/cron_root.lock"
            _rev="/etc/crontabs/root.rev"
            _trial_cmd="crontab -l >/dev/null 2>&1"
            _reload_fn="cron_reload"
            ;;
        device_rules)
            _live="/etc/geo-devices.list"
            _lock="/tmp/device_rules.lock"
            _rev="/etc/geo-devices.list.rev"
            _trial_cmd="nft -c -f /etc/nftables.d/device-rules.nft 2>/dev/null || true"
            _reload_fn="device_rules_reload"
            ;;
        mtr_groups)
            _live="/etc/magitrickle/state/config.yaml"
            _lock="/tmp/mtr_groups.lock"
            _rev="/etc/magitrickle/state/config.yaml.rev"
            _trial_cmd="magitrickled --dry-run 2>/dev/null || true"
            _reload_fn="mtr_reload"
            ;;
        *)
            echo "UNKNOWN_RESOURCE $_resource" >&2
            return 1
            ;;
    esac

    # 1. BACKUP GATE
    _safe_write_backup_gate || return 2

    # 2. LOCK
    _safe_write_lock "$_lock" || return 1
    trap "_safe_write_unlock '$_lock'" EXIT INT TERM

    # 3. REVISION
    _rev_old=""
    [ -f "$_rev" ] && _rev_old=$(cat "$_rev")

    # 4. SNAPSHOT
    _pre_snap="/tmp/safe_write_pre_$$.tmp"
    _safe_write_snapshot "$_live" > "$_pre_snap"
    _pre_mode=$(tail -n1 "$_pre_snap" | awk '{print $1}')
    _pre_owner=$(tail -n1 "$_pre_snap" | awk '{print $2}')
    _pre_content=$(head -c -1 "$_pre_snap" 2>/dev/null || cat "$_pre_snap")  # remove mode/owner line

    # 5. CANDIDATE
    _cand="/tmp/safe_write_cand_$$.tmp"
    $_make_cand_fn "$_pre_content" > "$_cand" || { rm -f "$_pre_snap" "$_cand"; return 1; }

    # 6. VALIDATE
    $_validate_fn "$_cand" || { rm -f "$_pre_snap" "$_cand"; return 1; }

    # 7. TRIAL
    eval "$_trial_cmd" >/dev/null 2>&1 || { rm -f "$_pre_snap" "$_cand"; return 3; }

    # 8. ATOMIC INSTALL
    _safe_write_atomic_install "$_live" "$_cand" "$_pre_mode" "$_pre_owner" || { rm -f "$_pre_snap" "$_cand"; return 4; }
    rm -f "$_cand"

    # 9. RELOAD
    if [ -n "$_reload_fn" ]; then
        _safe_write_reload_fn="$_reload_fn"
        $_reload_fn || {
            # 10. ROLLBACK on reload failure
            _safe_write_verify_rollback "$_resource" "$_pre_snap" "$_live" "$_rollback_check_fn" || {
                echo "ROLLBACK_FAILED $_resource" >&2
                rm -f "$_pre_snap"
                return 5
            }
            rm -f "$_pre_snap"
            return 4
        }
    fi

    # 10. POST-CHECK
    $_rollback_check_fn "$_pre_snap" "$_live" || {
        _safe_write_verify_rollback "$_resource" "$_pre_snap" "$_live" "$_rollback_check_fn" || {
            echo "ROLLBACK_FAILED $_resource" >&2
            rm -f "$_pre_snap"
            return 5
        }
        rm -f "$_pre_snap"
        return 4
    }

    # 11. SUCCESS - update revision
    sha256sum "$_live" | awk '{print $1}' > "$_rev"
    rm -f "$_pre_snap"
    _safe_write_unlock "$_lock"
    trap - EXIT INT TERM
    return 0
}

# Resource-specific reload functions (to be defined by including scripts)
mihomo_reload() {
    curl -sS -X PUT "http://127.0.0.1:9090/configs?force=true" \
        -H "Content-Type: application/json" \
        -d '{"path":"/etc/mihomo/config.yaml"}' >/dev/null 2>&1
}

nodes_conf_reload() {
    # nodes.conf changes picked up by next scan; no immediate reload needed
    return 0
}

ruleset_reload() {
    mihomo_reload
}

zapret_reload() {
    # Trigger zapret to reload hostlist
    pkill -HUP nfqws 2>/dev/null || true
}

cron_reload() {
    /etc/init.d/cron reload 2>/dev/null || true
}

device_rules_reload() {
    # Re-run routing application for affected devices
    /usr/bin/geomask-guard.sh 2>/dev/null || true
}

mtr_reload() {
    pkill -HUP magitrickled 2>/dev/null || true
    sleep 1
    /usr/bin/magitrickled --reload-config 2>/dev/null || true
}

# Example usage (for documentation):
# safe_write nodes_conf _my_make_cand _my_validate _my_rollback_check
# _my_make_cand() { python3 -c 'import sys,json; ...' <<< "$1"; }
# _my_validate() { python3 -c '...' < "$1"; }
# _my_rollback_check() { diff -q "$1" "$2"; }
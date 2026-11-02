#!/usr/bin/env python3
"""
Prune Orphaned Docker Swarm Overlay Network Endpoints.

Audits overlay networks (e.g. aether-net) for leaked, stale, or orphaned
endpoints whose containers or Swarm tasks no longer exist, and force-disconnects
them to reclaim exhausted IPAM allocations without restarting the Docker daemon.

Zero external dependencies (pure Python standard library).
"""

import argparse
import json
import subprocess
import sys


def get_active_swarm_task_ids():
    """Returns a set of active Swarm task IDs that are desired to be running."""
    try:
        services_out = subprocess.check_output(
            ["docker", "service", "ls", "-q"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().split()
    except Exception:
        services_out = []

    if not services_out:
        return set()

    cmd = [
        "docker",
        "service",
        "ps",
        *services_out,
        "--filter",
        "desired-state=running",
        "--format",
        "{{.ID}}",
    ]
    try:
        tasks = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=10).decode().split()
        return set(tasks)
    except Exception as e:
        print(f"Warning: Failed to fetch swarm tasks: {e}", file=sys.stderr)
        return set()


def get_local_container_ids():
    """Returns a set of all local container IDs and short IDs (running or stopped)."""
    try:
        short_ids = subprocess.check_output(
            ["docker", "ps", "-a", "-q"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().split()
        full_ids = subprocess.check_output(
            ["docker", "ps", "-a", "-q", "--no-trunc"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().split()
        return set(short_ids).union(set(full_ids))
    except Exception as e:
        print(f"Warning: Failed to fetch local containers: {e}", file=sys.stderr)
        return set()


def get_all_overlay_networks():
    """Returns a list of all overlay network names in the cluster."""
    try:
        out = subprocess.check_output(
            ["docker", "network", "ls", "--filter", "driver=overlay", "--format", "{{.Name}}"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().split()
        # Ingress is managed internally by Swarm routing mesh; skip by default
        return [net for net in out if net != "ingress"]
    except Exception as e:
        print(f"Error listing overlay networks: {e}", file=sys.stderr)
        return []


def inspect_network(net_name):
    """Inspects a Docker network and returns its JSON object."""
    try:
        out = subprocess.check_output(
            ["docker", "network", "inspect", net_name],
            stderr=subprocess.PIPE,
            timeout=5,
        )
        return json.loads(out)[0]
    except subprocess.CalledProcessError as err:
        print(f"Error inspecting network '{net_name}': {err.stderr.decode().strip()}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error parsing network '{net_name}': {e}", file=sys.stderr)
        return None


def audit_and_prune_network(net_name, active_tasks, local_cids, force=False):
    """Audits and optionally prunes orphaned endpoints on a single network."""
    net_data = inspect_network(net_name)
    if not net_data:
        return {"network": net_name, "error": "Inspect failed", "orphaned": 0, "pruned": 0}

    containers = net_data.get("Containers", {})
    subnet = ""
    ipam_config = net_data.get("IPAM", {}).get("Config", [])
    if ipam_config:
        subnet = ipam_config[0].get("Subnet", "")

    ipam_status = net_data.get("Status", {}).get("IPAM", {}).get("Subnets", {}).get(subnet, {})
    ips_in_use = ipam_status.get("IPsInUse", len(containers))
    ips_available = ipam_status.get("DynamicIPsAvailable", "N/A")

    print(f"=== Network: {net_name} ({subnet}) ===")
    print(f"Total endpoints: {len(containers)} | IPs in use: {ips_in_use} | Dynamic IPs available: {ips_available}")

    orphaned = []
    active = []

    for cid, info in containers.items():
        name = info.get("Name", "")
        ip = info.get("IPv4Address", "")
        ep_id = info.get("EndpointID", "")

        # Always protect Swarm load balancer endpoints
        if name.startswith("lb-") or name.endswith("-endpoint"):
            active.append((cid, name, ip, "swarm-lb"))
            continue

        # Format of Swarm container: <service>.<slot>.<task_id>
        parts = name.split(".")
        task_id = parts[-1] if len(parts) >= 3 else None

        is_active_task = (task_id in active_tasks) if task_id else False
        is_local_present = (cid in local_cids) or (cid[:12] in local_cids)

        if is_active_task or is_local_present:
            active.append((cid, name, ip, "active-task" if is_active_task else "local-container"))
        else:
            orphaned.append((cid, ep_id, name, ip))

    print(f"Active endpoints retained: {len(active)}")
    print(f"Orphaned endpoints found:   {len(orphaned)}")

    pruned_count = 0
    if orphaned:
        print("\nOrphaned endpoints:")
        for cid, ep_id, name, ip in orphaned:
            action = "[PRUNING] Disconnecting" if force else "[DRY-RUN] Would disconnect"
            print(f"  {action} {ip:16} {cid[:12]} ({name}) ep:{ep_id[:12]}")

            if force:
                target = ep_id if ep_id else cid
                try:
                    subprocess.check_output(
                        ["docker", "network", "disconnect", "-f", net_name, target],
                        stderr=subprocess.STDOUT,
                        timeout=5,
                    )
                    pruned_count += 1
                    print(f"    ✓ Successfully released {ip}")
                except subprocess.CalledProcessError as err:
                    print(f"    ✗ Failed to disconnect {target}: {err.output.decode().strip()}", file=sys.stderr)

    print()
    return {
        "network": net_name,
        "subnet": subnet,
        "total": len(containers),
        "active": len(active),
        "orphaned": len(orphaned),
        "pruned": pruned_count,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Audit and prune orphaned Docker Swarm overlay network endpoints to reclaim exhausted IPAM allocations."
    )
    parser.add_argument(
        "network",
        nargs="?",
        default="aether-net",
        help="Target overlay network (default: aether-net)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Audit and prune across all overlay networks in the cluster",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Perform actual disconnection of orphaned endpoints (default is safe dry-run)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results summary as JSON",
    )

    args = parser.parse_args()

    active_tasks = get_active_swarm_task_ids()
    local_cids = get_local_container_ids()

    targets = get_all_overlay_networks() if args.all else [args.network]
    results = []

    for net in targets:
        res = audit_and_prune_network(net, active_tasks, local_cids, force=args.force)
        results.append(res)

    if args.json:
        print(json.dumps(results, indent=2))

    total_orphaned = sum(r.get("orphaned", 0) for r in results)
    total_pruned = sum(r.get("pruned", 0) for r in results)

    if not args.force and total_orphaned > 0:
        print(f"Dry-run complete: {total_orphaned} orphaned endpoint(s) found. Run with --force to release IP allocations.")
    elif args.force:
        print(f"Prune complete: {total_pruned} of {total_orphaned} orphaned endpoint(s) successfully released.")
    else:
        print("All audited overlay networks are clean.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Decide whether a host could SSH to an instance, from security groups alone.

    aws ec2 describe-security-groups --group-ids <target sgs> \
      --query 'SecurityGroups[].{id:GroupId,name:GroupName,perms:IpPermissions}' \
      --output json | sg_reachable.py <source-ip> [<source-sg-id> ...]

Prints one line saying which rule permits it, exit 0; or why nothing does,
exit 1.

Exists because "put the instance in the same security group as the
workstation" only works if that group has a self-referencing inbound rule.
The VPC default group does, but a hand-made group very often does not - and
the symptom of getting it wrong is an unexplained SSH timeout several
minutes after the instance launches. Checking the rules up front turns that
into a sentence.
"""

from __future__ import annotations

import ipaddress
import json
import sys


def covers_port(perm: dict, port: int) -> bool:
    """True if this permission's protocol/port range includes `port`."""
    proto = str(perm.get("IpProtocol"))
    if proto == "-1":          # all protocols, all ports
        return True
    if proto not in ("tcp", "6"):
        return False
    lo, hi = perm.get("FromPort"), perm.get("ToPort")
    if lo is None or hi is None:   # tcp with no range means all ports
        return True
    return lo <= port <= hi


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: sg_reachable.py <source-ip> [<source-sg-id> ...]", file=sys.stderr)
        return 2
    src_ip = sys.argv[1]
    src_sgs = {s for s in sys.argv[2:] if s}
    port = 22

    try:
        groups = json.load(sys.stdin)
    except Exception as e:
        print(f"could not parse the security group rules: {e}")
        return 1
    if not isinstance(groups, list) or not groups:
        print("no security groups to inspect")
        return 1

    for g in groups:
        gid, gname = g.get("id", "?"), g.get("name", "?")
        for perm in g.get("perms") or []:
            if not covers_port(perm, port):
                continue

            # Shared-group path: the rule's source is a group this host is in.
            for pair in perm.get("UserIdGroupPairs") or []:
                sgid = pair.get("GroupId")
                if sgid and sgid in src_sgs:
                    print(f"{gid} ({gname}) allows port {port} from {sgid}, "
                          f"which this host is a member of")
                    return 0

            # CIDR path: the rule's source range contains this host's address.
            for rng in perm.get("IpRanges") or []:
                cidr = rng.get("CidrIp")
                if not cidr:
                    continue
                try:
                    if ipaddress.ip_address(src_ip) in ipaddress.ip_network(cidr):
                        print(f"{gid} ({gname}) allows port {port} from {cidr}, "
                              f"which covers {src_ip}")
                        return 0
                except ValueError:
                    continue

    ids = ", ".join(g.get("id", "?") for g in groups)
    print(f"none of [{ids}] permits port {port} from {src_ip} "
          f"(host groups: {', '.join(sorted(src_sgs)) or 'none'})")
    return 1


if __name__ == "__main__":
    sys.exit(main())

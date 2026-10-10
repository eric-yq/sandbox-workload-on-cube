"""Apply key-level overrides to a TOML config, idempotently.

Called by scripts/ensure-cube.sh to tune Cubelet's config.toml.

Why this exists
---------------
The obvious approach - append a block of `[section] key = value` to the end
of the file - is wrong, and fails in the most disruptive possible way. The
shipped config already defines every section we need to touch, so appending
them again produces duplicate TOML tables and the service then refuses to
start with

    TOML parse error at line N, column 2: duplicate key

Because the tuning is applied to a *running* installation, that takes out a
previously working node. (Learned the hard way on a live host, on the
AgentENV build of this suite.)

So this edits at key granularity instead:

* key present in its section  -> replace the value in place
* section present, key absent -> insert the key after the section header
* section absent              -> append the section with the key

Everything else in the file is preserved byte for byte, and re-running with
the same arguments is a no-op. The result is parsed with tomllib before
being committed; on any failure the original is restored and the exit code
is non-zero, so a bad edit can never leave an unstartable config behind.

Usage:
    python3 tune_config.py <config.toml> <dotted.key>=<value> ...

The last dotted component is the key, the rest is the section. Dots inside
double quotes are *not* separators, which matters because containerd-style
plugin names are themselves dotted:

    plugins."io.cubelet.internal.v1.network".tap_init_num=1200
        -> [plugins."io.cubelet.internal.v1.network"]  tap_init_num
    plugins."io.cubelet.workflow.v1.workflow".flows.create.concurrent=256
        -> [plugins."io.cubelet.workflow.v1.workflow".flows.create]  concurrent

Array-of-tables headers (`[[section]]`) are deliberately never matched, so
repeated tables such as the volume-plugin list cannot be corrupted.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def split_dotted(dotted: str) -> list[str]:
    """Split on dots that are not inside double quotes.

    Needed because containerd-style plugin names are dotted *inside* a
    quoted TOML key: `plugins."io.cubelet.internal.v1.network"` is two
    path components, not five.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in dotted:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "." and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    if in_quotes:
        raise SystemExit(f"unbalanced quotes in override path {dotted!r}")
    return [p for p in parts if p != ""]


def parse_arg(arg: str) -> tuple[str, str, str]:
    if "=" not in arg:
        raise SystemExit(f"bad override {arg!r}, expected dotted.key=value")
    dotted, _, value = arg.partition("=")
    parts = split_dotted(dotted.strip())
    if len(parts) < 2:
        raise SystemExit(f"bad override {arg!r}: need at least section.key")
    return ".".join(parts[:-1]), parts[-1], value.strip()


def section_ranges(lines: list[str]) -> dict[str, tuple[int, int]]:
    """section name -> (header_index, end_index_exclusive)."""
    starts: list[tuple[str, int]] = []
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m:
            starts.append((m.group(1).strip(), i))
    out: dict[str, tuple[int, int]] = {}
    for n, (name, idx) in enumerate(starts):
        end = starts[n + 1][1] if n + 1 < len(starts) else len(lines)
        # First definition wins; a duplicate in the input is pre-existing
        # breakage we should not compound.
        out.setdefault(name, (idx, end))
    return out


def apply_override(lines: list[str], section: str, key: str, value: str) -> str:
    ranges = section_ranges(lines)
    key_re = re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*).*$")

    if section in ranges:
        start, end = ranges[section]
        for i in range(start + 1, end):
            m = key_re.match(lines[i])
            if m:
                new = f"{m.group(1)}{key}{m.group(2)}{value}"
                if lines[i] == new:
                    return "unchanged"
                lines[i] = new
                return "updated"
        # Section exists but the key does not: insert just after the header.
        lines.insert(start + 1, f"{key} = {value}")
        return "inserted"

    # Section absent entirely: append it.
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"[{section}]")
    lines.append(f"{key} = {value}")
    return "added-section"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path, overrides = sys.argv[1], sys.argv[2:]

    original = open(path, encoding="utf-8").read()
    lines = original.split("\n")
    # A trailing newline shows up as a final empty element; drop it so we do
    # not accumulate blank lines across runs, then restore it on write.
    trailing_nl = lines and lines[-1] == ""
    if trailing_nl:
        lines.pop()

    dupes = {}
    seen = set()
    for line in lines:
        m = SECTION_RE.match(line)
        if m:
            name = m.group(1).strip()
            if name in seen:
                dupes[name] = True
            seen.add(name)
    if dupes:
        print(f"  WARNING: config already has duplicate sections: "
              f"{', '.join(sorted(dupes))}", file=sys.stderr)

    for arg in overrides:
        section, key, value = parse_arg(arg)
        action = apply_override(lines, section, key, value)
        print(f"  {action:<14} [{section}] {key} = {value}")

    new_text = "\n".join(lines) + ("\n" if trailing_nl else "")
    if new_text == original:
        print("  config already matches; nothing written")
        return 0

    # Validate before committing. tomllib is stdlib on 3.11+; if it is not
    # available we still write, but the caller's health check is then the
    # only safety net.
    try:
        import tomllib
        try:
            tomllib.loads(new_text)
        except tomllib.TOMLDecodeError as e:
            print(f"  ERROR: edit would produce invalid TOML: {e}", file=sys.stderr)
            print("  original left untouched", file=sys.stderr)
            return 1
    except ImportError:
        print("  note: tomllib unavailable, skipping validation", file=sys.stderr)

    # Capture ownership *and* mode before touching anything. This script runs
    # under sudo but the config may be owned by a service account, so a
    # naive atomic replace leaves the new file root-owned. If its mode is
    # 0600 the service then dies with
    #     IO error ... config.toml: Permission denied (os error 13)
    # which looks nothing like "your edit was fine but the owner changed".
    st = os.stat(path)

    backup = f"{path}.arl-bak"
    shutil.copy2(path, backup)
    try:
        os.chown(backup, st.st_uid, st.st_gid)
    except (PermissionError, OSError):
        pass

    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=os.path.dirname(path) or ".", delete=False,
        ) as tmp:
            tmp.write(new_text)
            tmp_path = tmp.name
        os.chmod(tmp_path, stat.S_IMODE(st.st_mode))
        try:
            os.chown(tmp_path, st.st_uid, st.st_gid)
        except PermissionError:
            # Not root and not the owner: the replace would change ownership,
            # so refuse rather than silently breaking the service.
            raise SystemExit(
                f"  ERROR: cannot preserve ownership (uid={st.st_uid} gid={st.st_gid}) "
                f"on {path}; re-run with sudo"
            )
        os.replace(tmp_path, path)
        tmp_path = ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    after = os.stat(path)
    print(f"  written (backup: {backup}) "
          f"uid={after.st_uid} gid={after.st_gid} mode={stat.S_IMODE(after.st_mode):04o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

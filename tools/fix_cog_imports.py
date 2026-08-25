"""Rewrite absolute intra-cog imports to relative ones.

Third-party cogs are written to be installed by Downloader, which puts the cog
folder on sys.path so `from plcontroller.cog import X` resolves. Vendored into
redbot/cogs/ the package is redbot.cogs.plcontroller, that top-level name does
not exist, and the import raises ModuleNotFoundError.

Only rewrites when the file lives inside the cog it names AND the target
actually exists on disk, so stdlib collisions (a `warnings` cog vs the
`warnings` module) are left alone.
"""
import ast
import pathlib
import sys

BASE = pathlib.Path("redbot/cogs")


def target_exists(cog: str, sub: tuple[str, ...]) -> bool:
    root = BASE / cog
    if not sub:
        return root.is_dir()
    p = root.joinpath(*sub)
    return p.with_suffix(".py").is_file() or (p / "__init__.py").is_file()


def process(path: pathlib.Path, cogs: set[str]) -> int:
    rel = path.relative_to(BASE)
    cog = rel.parts[0]
    depth = len(rel.parts) - 2  # dirs between the cog root and this file
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    offsets, total = [0], 0
    for ln in lines:
        total += len(ln)
        offsets.append(total)

    edits = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.ImportFrom) or node.level != 0 or not node.module:
            continue
        parts = node.module.split(".")
        if parts[0] != cog:            # not a self-reference
            continue
        if parts[0] not in cogs:
            continue
        sub = tuple(parts[1:])
        if not target_exists(cog, sub):  # stdlib collision, or dead import
            continue

        dots = "." * (depth + 1)
        new_module = dots + ".".join(sub)
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        segment = src[start:end]
        replaced = segment.replace(f"from {node.module}", f"from {new_module}", 1)
        if replaced != segment:
            edits.append((start, end, replaced))

    if not edits:
        return 0
    for start, end, text in sorted(edits, reverse=True):
        src = src[:start] + text + src[end:]
    path.write_text(src, encoding="utf-8")
    return len(edits)


def main() -> int:
    cogs = {d.name for d in BASE.iterdir() if d.is_dir() and d.name != "locales"}
    files = changes = 0
    for path in sorted(BASE.rglob("*.py")):
        n = process(path, cogs)
        if n:
            files += 1
            changes += n
            print(f"  {path.relative_to(BASE)}: {n}")
    print(f"\nrewrote {changes} imports across {files} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

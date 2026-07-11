"""One-shot repair: reverse cp1252/latin-1 double-encoded UTF-8 in source files."""
from pathlib import Path

FILES = [
    "algo_cli/main.py",
    "algo_cli/tools.py",
    "algo_cli/harness.py",
    "algo_cli/animations.py",
]


def demojibake(text: str) -> str:
    out_bytes = bytearray()
    for ch in text:
        try:
            out_bytes += ch.encode("cp1252")
        except UnicodeEncodeError:
            cp = ord(ch)
            if cp < 0x100:
                out_bytes.append(cp)
            else:
                out_bytes += ch.encode("utf-8")
    try:
        return out_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return text


def fix_file(path: Path) -> None:
    original = path.read_text(encoding="utf-8")
    # Only repair runs that look like mojibake; leave clean text alone.
    import re

    pattern = re.compile(r"[Â-Ãâ][\x80-\xffŒœ–—‘’“”†‡•…‰‹›ˆ˜ŠšŸŽž€™ƒ]{1,3}")

    def repl(m: re.Match) -> str:
        fixed = demojibake(m.group(0))
        return fixed if fixed != m.group(0) else m.group(0)

    repaired = pattern.sub(repl, original)
    if repaired != original:
        path.write_text(repaired, encoding="utf-8", newline="\n")
        print(f"fixed: {path}")
    else:
        print(f"clean: {path}")


if __name__ == "__main__":
    root = Path(__file__).parent
    for rel in FILES:
        fix_file(root / rel)

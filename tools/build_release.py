"""Build a reproducible installation ZIP and SHA-256 checksum."""

import hashlib
import json
import zipfile
from pathlib import Path


def build_release(root: Path) -> Path:
    component = root / "custom_components/bilresa_dial"
    version = json.loads((component / "manifest.json").read_text())["version"]
    output = root / "dist"
    output.mkdir(exist_ok=True)
    archive_path = output / f"bilresa-dial-{version}.zip"
    files = [root / name for name in ("README.md", "LICENSE", "hacs.json")]
    files.extend(file for file in component.rglob("*") if file.is_file() and file.suffix in (".py", ".json", ".png"))
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files):
            relative = file.relative_to(root)
            if file.is_symlink() or any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                raise ValueError(f"Unexpected packaged path: {relative}")
            info = zipfile.ZipInfo(relative.as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, file.read_bytes())
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_path.with_suffix(".zip.sha256").write_text(f"{digest}  {archive_path.name}\n")
    return archive_path


if __name__ == "__main__":
    print(build_release(Path(__file__).resolve().parents[1]))

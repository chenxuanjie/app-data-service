"""Generate GHCR tags from the checked-out GitHub ref and project version."""

import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def image_metadata(repository, revision, ref, version):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub repository must be owner/name")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected the complete checked-out Git commit")
    image = "ghcr.io/" + repository.lower()
    tags = [f"{image}:sha-{revision}"]
    if ref.startswith("refs/tags/"):
        tag = ref.removeprefix("refs/tags/")
        if tag != f"v{version}":
            raise ValueError("Release tag must match the pyproject.toml version")
        tags.append(f"{image}:{version}")
        if re.fullmatch(r"\d+\.\d+\.\d+", version):
            tags.append(f"{image}:latest")
    elif ref.startswith("refs/heads/"):
        branch = ref.removeprefix("refs/heads/")
        slug = re.sub(r"[^a-z0-9_.-]+", "-", branch.lower()).strip(".-")[:100]
        if not slug:
            raise ValueError("Branch name has no valid Docker tag characters")
        tags.append(f"{image}:branch-{slug}")
        if branch == "main":
            tags.append(f"{image}:latest")
    return image, tags


def main():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    image, tags = image_metadata(
        os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_SHA"],
        os.environ["GITHUB_REF"],
        version,
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"image={image}\nversion={version}\ntags={','.join(tags)}\n")
    print("Image tags: " + ", ".join(tags))


if __name__ == "__main__":
    main()

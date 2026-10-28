"""Standard-library Docker archive identity/platform checks, shared with WSL worker."""

import hashlib
import json
import tarfile


def validate_archive(path, expected_tag, expected_image_id):
    """Reject wrong images/platforms and incomplete archives before delivery."""
    with tarfile.open(path, "r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        if len(manifest) != 1 or manifest[0].get("RepoTags") != [expected_tag]:
            raise ValueError("Archive must contain exactly its designated image tag")
        entry = manifest[0]
        config_bytes = archive.extractfile(entry["Config"]).read()
        config = json.loads(config_bytes)
        if config["os"] != "linux" or config["architecture"] != "amd64":
            raise ValueError("Unexpected archived image platform")
        config_id = "sha256:" + hashlib.sha256(config_bytes).hexdigest()

        def references_config(identity, visited):
            if identity == config_id:
                return True
            if identity in visited or not identity.startswith("sha256:"):
                return False
            visited.add(identity)
            try:
                payload = archive.extractfile(
                    "blobs/sha256/" + identity.removeprefix("sha256:")
                ).read()
            except KeyError:
                return False
            if "sha256:" + hashlib.sha256(payload).hexdigest() != identity:
                return False
            descriptor = json.loads(payload)
            if descriptor.get("config", {}).get("digest") == config_id:
                return True
            return any(
                references_config(child["digest"], visited)
                for child in descriptor.get("manifests", [])
            )

        # Classic IDs identify configs; Docker 29's containerd store can instead
        # identify an OCI manifest/index. Bind either to the same image config.
        if not references_config(expected_image_id, set()):
            raise ValueError("Archived image identity differs from inspected image")
        if not entry["Layers"]:
            raise ValueError("Image layers are missing")
        for layer in entry["Layers"]:
            if not archive.getmember(layer).isfile():
                raise ValueError("Image layer is missing")

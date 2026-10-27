"""Read-only legacy conversion into a typed plan; no NAS/network access or writes."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog_models as m

GAME_KEYS = {
    "id",
    "slug",
    "editionLabel",
    "aliases",
    "designers",
    "publishers",
    "keywords",
    "coverAsset",
    "bannerAsset",
    "galleryAssets",
    "cardAccent",
    "score",
    "supportedPlayers",
    "recommendedPlayer",
    "documents",
    "locales",
}
RESOURCE_KEYS = {
    "id",
    "path",
    "documentType",
    "sourceClass",
    "origin",
    "language",
    "edition",
    "status",
    "enabled",
    "aiEnabled",
    "priority",
    "derivedFrom",
    "sourceUrl",
    "reviewStatus",
    "notes",
    "imageRole",
    "variant",
    "width",
    "height",
}
LOCALE_TEXT = {
    re.sub(r"_([a-z])", lambda x: x[1].upper(), key): key
    for key in m.TEXT_FIELDS
    if key != "edition_label"
}
LOCALE_LIST = {
    re.sub(r"_([a-z])", lambda x: x[1].upper(), key): key
    for key in m.LIST_FIELDS
    if not key.startswith("local_")
}
PUBLIC_TYPES = {".pdf", ".md", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".html"}
MAX_FILE = 512 * 1024 * 1024
DEFAULT_SQL_FILE_MAX = 4 * 1024 * 1024
TABLES = {t.name: t for t in m.DRAFT_TABLES}


def digest_file(path):
    before = path.stat()
    if not 0 < before.st_size <= MAX_FILE:
        raise ValueError("file_size_out_of_range")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE:
                raise ValueError("file_size_out_of_range")
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ) or size != before.st_size:
        raise ValueError("source_changed_while_reading")
    return digest.hexdigest(), size


def safe_path(root, relative):
    if not isinstance(relative, str) or len(relative) > 1024 or "\\" in relative:
        raise ValueError("invalid_relative_path")
    parts = relative.split("/")
    if not parts or any(not p or p in {".", ".."} or p.startswith(".") or ":" in p for p in parts):
        raise ValueError("invalid_relative_path")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("path_outside_source")
    for candidate in (path, *path.parents):
        if candidate.is_relative_to(root) and (
            candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)()
        ):
            raise ValueError("linked_source_path")
    return path


def strings(value):
    if not isinstance(value, list) or any(not isinstance(x, str) or "\x00" in x for x in value):
        raise ValueError("expected_string_list")
    return list(value)


def text(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("expected_text")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value):
        raise ValueError("invalid_identifier")
    return value


def integer(value):
    if type(value) is not int or not -(2**31) <= value < 2**31:
        raise ValueError("expected_integer")
    return value


def positive(value):
    result = integer(value)
    if result <= 0:
        raise ValueError("positive_integer_required")
    return result


def boolean(value):
    if type(value) is not bool:
        raise ValueError("expected_boolean")
    return value


def known(data, keys, label, plan):
    if not isinstance(data, dict):
        raise ValueError(label + ":expected_object")
    extra = set(data) - set(keys)
    if extra:
        plan.warnings.append(
            {"code": "extra_fields_retained", "section": label, "fields": sorted(extra)}
        )


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_field:" + key)
        result[key] = value
    return result


@dataclass
class LibraryPlan:
    source: Path
    sql_file_max: int = DEFAULT_SQL_FILE_MAX
    rows: dict = field(default_factory=lambda: {key: [] for key in TABLES})
    files: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    _paths: dict = field(default_factory=dict)
    _json_bytes: int = 0

    def summary(self):
        counts = {name.removeprefix("library_"): len(rows) for name, rows in self.rows.items()}
        return {
            "protocol": 1,
            "source_hash": self.source_hash,
            "counts": counts,
            "files": len(self.files),
            "source_documents": len(self.sources),
            "sql_file_candidates": sum(f["sql_inline"] for f in self.files.values()),
            "sql_file_max_bytes": self.sql_file_max,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "ready": not self.errors,
            "database_written": False,
            "published": False,
        }

    @property
    def source_hash(self):
        return hashlib.sha256(
            json.dumps(
                {
                    "rows": self.rows,
                    "sql_file_max_bytes": self.sql_file_max,
                    "source_files": sorted(self._paths.items()),
                    "sources": [{"path": s["path"], "sha256": s["sha256"]} for s in self.sources],
                    "files": [
                        {k: f[k] for k in ("sha256", "size", "storage_key")}
                        for f in sorted(self.files.values(), key=lambda f: f["sha256"])
                    ],
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def json_source(self, relative):
        path = safe_path(self.source, relative)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("source_json_too_large")
        with path.open("rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("source_json_too_large")
        self._json_bytes += len(raw)
        if self._json_bytes > 64 * 1024 * 1024:
            raise ValueError("source_metadata_budget_split_batch")
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_json_object)
        self.sources.append(
            {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "snapshot": data}
        )
        if self.file(relative) != hashlib.sha256(raw).hexdigest():
            raise ValueError("source_changed_while_reading")
        return data

    def file(self, relative):
        path = safe_path(self.source, relative)
        try:
            path.stat()
        except FileNotFoundError:
            return None
        if not path.is_file():
            raise ValueError("resource_is_not_file")
        if relative in self._paths:
            return self._paths[relative]
        if len(self.files) >= 10000:
            raise ValueError("library_file_budget")
        digest, size = digest_file(path)
        self.files.setdefault(
            digest,
            {
                "sha256": digest,
                "size": size,
                "storage_key": f"blobs/{digest[:2]}/{digest}",
                "source_path": relative,
                "sql_inline": size <= self.sql_file_max,
            },
        )
        self._paths[relative] = digest
        return digest


def plan_library(source, selected=None, *, sql_file_max=DEFAULT_SQL_FILE_MAX):
    original = Path(source)
    if original.is_symlink() or getattr(original, "is_junction", lambda: False)():
        raise ValueError("linked_source_root")
    source = original.resolve()
    if not source.is_dir():
        raise ValueError("source_directory_required")
    if type(sql_file_max) is not int or not 0 <= sql_file_max <= 16 * 1024 * 1024:
        raise ValueError("sql_file_max_must_be_between_0_and_16_mib")
    plan = LibraryPlan(source, sql_file_max=sql_file_max)
    try:
        catalog = plan.json_source("assets/catalog.json")
        known(catalog, {"version", "games"}, "catalog", plan)
        if type(catalog.get("version")) is not int or catalog["version"] not in {1, 2}:
            raise ValueError("unsupported_catalog_version")
        if not isinstance(catalog.get("games"), list) or len(catalog["games"]) > 10000:
            raise ValueError("catalog_entry_budget")
        wanted = set(selected or [])
        seen = set()
        for entry in catalog["games"]:
            known(entry, {"slug", "order", "enabled", "browseCategories"}, "catalog_entry", plan)
            slug = identifier(entry["slug"])
            if slug in seen:
                raise ValueError("duplicate_catalog_slug:" + slug)
            seen.add(slug)
            if wanted and slug not in wanted:
                continue
            try:
                _game(plan, entry)
            except (ValueError, KeyError, TypeError, OSError) as error:
                code = str(error) if isinstance(error, ValueError) else type(error).__name__
                plan.errors.append({"game": slug, "code": code})
        if wanted - seen:
            plan.errors.append({"code": "unknown_selected_games", "games": sorted(wanted - seen)})
        # Keep small library-wide documents such as ASSET_SPEC.md too. No access
        # to sibling release, account, comments or backup directories is implied.
        for path in sorted(safe_path(source, "assets").iterdir()):
            if path.is_file() and not path.name.startswith("."):
                plan.file("assets/" + path.name)
        resource_ids = [r["id"] for r in plan.rows[m.resources.name]]
        if len(resource_ids) != len(set(resource_ids)):
            plan.errors.append({"code": "duplicate_resource_id"})
        ids = set(resource_ids)
        for row in plan.rows[m.derivations.name]:
            row["resolved_source_id"] = (
                row["source_reference"] if row["source_reference"] in ids else None
            )
            if row["resolved_source_id"] is None:
                plan.warnings.append(
                    {
                        "code": "unresolved_derivation",
                        "resource": row["resource_id"],
                        "source_reference": row["source_reference"],
                    }
                )
        if len(resource_ids) > 10000 or len(plan.files) > 10000:
            plan.errors.append({"code": "library_entry_budget"})
        if not plan.rows[m.editions.name]:
            plan.errors.append({"code": "empty_library_plan"})
    except (ValueError, KeyError, TypeError, OSError) as error:
        code = str(error) if isinstance(error, ValueError) else type(error).__name__
        plan.errors.append({"code": code})
    return plan


def _localized(value, locale, default):
    if isinstance(value, dict):
        return value.get(locale, default)
    return value if value is not None else default


def _game(plan, entry):
    slug = entry["slug"]
    prefix = f"assets/games/{slug}/"
    game = plan.json_source(prefix + "game.json")
    manifest = plan.json_source(prefix + "manifest.json")
    known(game, GAME_KEYS, "game", plan)
    known(manifest, {"schemaVersion", "game", "resources", "validation"}, "manifest", plan)
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] not in {1, 2, 3}:
        raise ValueError("unsupported_manifest_version")
    known(manifest.get("game"), {"id", "slug", "edition", "title"}, "manifest_game", plan)
    key = identifier(game["id"])
    header = manifest["game"]
    if game.get("slug") != slug or header.get("slug") != slug or header.get("id") != key:
        raise ValueError("game_identity_mismatch")
    if any(row["id"] == key for row in plan.rows[m.editions.name]):
        raise ValueError("duplicate_edition_id")
    titles = header.get("title", {})
    if not isinstance(titles, dict) or any(not isinstance(v, str) for v in titles.values()):
        raise ValueError("invalid_manifest_titles")
    edition_code = text(header.get("edition", "unknown"))
    players = game.get("supportedPlayers", [])
    if not isinstance(players, list) or any(integer(p) <= 0 for p in players):
        raise ValueError("invalid_player_counts")
    plan.rows[m.games.name].append({"id": key, "slug": slug})
    plan.rows[m.editions.name].append(
        {
            "id": key,
            "game_id": key,
            "slug": slug,
            "edition_code": edition_code,
            "sort_order": integer(entry.get("order", 0)),
            "enabled": boolean(entry.get("enabled", False)),
            "card_accent": text(game.get("cardAccent", "")),
            "external_score_text": text(game.get("score", "")),
            "supported_players": players,
            "recommended_player": integer(game.get("recommendedPlayer", 0)),
            "manifest_titles": titles,
        }
    )
    categories = strings(entry.get("browseCategories", []))
    if len(categories) != len(set(categories)):
        raise ValueError("duplicate_category")
    if game.get("recommendedPlayer", 0) < 0:
        raise ValueError("invalid_recommended_player")
    for position, category in enumerate(categories):
        plan.rows[m.categories.name].append(
            {"edition_id": key, "category": identifier(category), "position": position}
        )
    locales = game.get("locales", {})
    if not isinstance(locales, dict) or not locales:
        raise ValueError("game_locales_required")
    # Top-level localized aliases/labels must not silently lose an extra language.
    for field_name in ("editionLabel", "aliases", "designers", "publishers", "keywords"):
        value = game.get(field_name)
        if isinstance(value, dict) and set(value) - set(locales):
            raise ValueError("localized_field_without_locale:" + field_name)
    for locale, content in locales.items():
        identifier(locale)
        known(content, set(LOCALE_TEXT) | set(LOCALE_LIST), "locale", plan)
        row = {
            "edition_id": key,
            "locale": locale,
            "edition_label": text(_localized(game.get("editionLabel"), locale, "")),
        }
        for camel, column in LOCALE_TEXT.items():
            row[column] = text(content.get(camel, ""))
        for camel, column in LOCALE_LIST.items():
            if column in {"aliases", "designers", "publishers", "keywords"}:
                row[column] = strings(_localized(game.get(camel), locale, []))
                row["local_" + column] = strings(content.get(camel, []))
            else:
                row[column] = strings(content.get(camel, []))
        plan.rows[m.texts.name].append(row)
    records = manifest.get("resources", [])
    if not isinstance(records, list):
        raise ValueError("resources_required")
    by_path = {}
    for resource in records:
        known(resource, RESOURCE_KEYS, "resource", plan)
        _resource(plan, key, prefix, resource)
        path = resource["path"]
        by_path.setdefault(path if path.startswith("assets/") else prefix + path, resource["id"])
    _bindings(plan, key, prefix, game, by_path)
    validation = manifest.get("validation", {})
    known(validation, {"checkedAgainst", "checkedAt", "issues"}, "validation", plan)
    plan.rows[m.validations.name].append(
        {
            "edition_id": key,
            "checked_against": text(validation.get("checkedAgainst", "")),
            "checked_at": text(validation.get("checkedAt", "")),
            "issues": strings(validation.get("issues", [])),
        }
    )
    # Explicit one-time source inventory. Unregistered files are archived, not lost
    # or promoted into public/AI content by their filename.
    directory = safe_path(plan.source, prefix.rstrip("/"))
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("linked_source_path")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in {"game.json", "manifest.json"} or prefix + relative in plan._paths:
            continue
        archive_id = "archive:" + key + ":" + hashlib.sha256(relative.encode()).hexdigest()[:24]
        _resource(
            plan,
            key,
            prefix,
            {
                "id": archive_id,
                "path": relative,
                "enabled": False,
                "aiEnabled": False,
                "documentType": "other",
                "status": "available",
                "notes": "Unregistered source file retained as an archive",
            },
            archive=True,
        )
        plan.warnings.append({"game": slug, "code": "unregistered_file_archived", "path": relative})


def _resource(plan, edition, prefix, source, *, archive=False):
    if len(plan.rows[m.resources.name]) >= 10000:
        raise ValueError("library_resource_budget")
    key, path = identifier(source["id"]), text(source["path"])
    relative = path if path.startswith("assets/") else prefix + path
    digest = plan.file(relative)
    kind = text(source.get("documentType", "other"))
    status = text(source.get("status", "missing"))
    if status not in {"available", "missing", "superseded", "blocked"}:
        raise ValueError("unknown_resource_status:" + key)
    enabled = boolean(source.get("enabled", False))
    is_archive = (
        archive
        or "/docs/others/" in "/" + relative
        or Path(path).suffix.lower() not in PUBLIC_TYPES
    )
    if digest is None:
        warning = {"resource": key, "path": path, "code": "declared_file_not_in_source"}
        plan.warnings.append(warning)
    old_image = kind in {"cover", "background", "gallery"}
    row = {
        "id": key,
        "edition_id": edition,
        "path": path,
        "document_type": "image" if old_image else kind,
        "legacy_document_type": kind if old_image else None,
        "source_class": text(source.get("sourceClass", "unknown")),
        "origin": text(source.get("origin", "unknown")),
        "language": text(source.get("language", "none")),
        "edition_code": text(source.get("edition", "unknown")),
        "status": status,
        "enabled": enabled,
        "ai_enabled": boolean(source.get("aiEnabled", False)),
        "priority": integer(source.get("priority", 500)),
        "source_url": text(source.get("sourceUrl"), nullable=True),
        "review_status": text(source.get("reviewStatus", "unreviewed")),
        "notes": text(source.get("notes"), nullable=True),
        "image_role": text(source.get("imageRole", kind if old_image else None), nullable=True),
        "variant": text(source.get("variant"), nullable=True),
        "width": None if source.get("width") is None else positive(source["width"]),
        "height": None if source.get("height") is None else positive(source["height"]),
        "visibility": "archive" if is_archive else "public",
        "observed_file": "present" if digest else "absent",
        "file_sha256": digest,
    }
    plan.rows[m.resources.name].append(row)
    for position, reference in enumerate(strings(source.get("derivedFrom", []))):
        plan.rows[m.derivations.name].append(
            {
                "resource_id": key,
                "position": position,
                "source_reference": reference,
                "resolved_source_id": None,
            }
        )


def _bindings(plan, edition, prefix, game, by_path):
    values = []
    for kind, field_name in (("cover", "coverAsset"), ("hero", "bannerAsset")):
        if game.get(field_name):
            values.append((kind, "", 0, text(game[field_name])))
    for position, path in enumerate(strings(game.get("galleryAssets", []))):
        values.append(("gallery", "", position, path))
    documents = {} if game.get("documents") is None else game["documents"]
    known(documents, {"rulebook", "faq", "knowledge"}, "documents", plan)
    for kind, localized in documents.items():
        if kind not in {"rulebook", "faq", "knowledge"}:
            continue
        if not isinstance(localized, dict):
            raise ValueError("localized_document_map_required")
        for locale, paths in localized.items():
            paths = [paths] if isinstance(paths, str) else strings(paths)
            for position, path in enumerate(paths):
                values.append((kind, identifier(locale), position, path))
    for kind, locale, position, path in values:
        relative = path if path.startswith("assets/") else prefix + path
        resource = by_path.get(relative)
        if resource is None:
            resource = "binding:" + edition + ":" + hashlib.sha256(path.encode()).hexdigest()[:24]
            if relative not in by_path:
                _resource(
                    plan,
                    edition,
                    prefix,
                    {
                        "id": resource,
                        "path": path,
                        "documentType": "image" if kind in {"cover", "hero", "gallery"} else kind,
                        "enabled": True,
                        "aiEnabled": False,
                        "status": "available",
                        "imageRole": kind if kind in {"cover", "hero", "gallery"} else None,
                    },
                )
                by_path[relative] = resource
                plan.warnings.append(
                    {"code": "binding_registered", "edition": edition, "path": path}
                )
        plan.rows[m.bindings.name].append(
            {
                "edition_id": edition,
                "kind": kind,
                "locale": locale,
                "position": position,
                "path": path,
                "resource_id": resource,
            }
        )

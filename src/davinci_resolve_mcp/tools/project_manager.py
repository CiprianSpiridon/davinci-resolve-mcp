"""ProjectManager lifecycle tools for the DaVinci Resolve MCP server.

Covers project listing/creation/loading/saving/closing/deleting, the
ProjectManager's folder navigation ("Project Library"), database
list/current/switch tools, and project import/export/restore.

All tools go through ``ResolveConnection.get_project_manager()`` (see
``connection.py``), which lazily connects to Resolve on first use — nothing
here touches ``DaVinciResolveScript`` or a live Resolve instance at import
time.

Destructive tools (``delete_project``, ``close_project``, ``set_current_database``)
require an explicit, unambiguous target and report a clear failure string
rather than raising or silently no-oping when Resolve's API returns a falsy
result (which it commonly does instead of raising on failure).
"""

from __future__ import annotations

from ..app import mcp
from ..helpers import _check_choice, _conn


def _pm():
    """Shorthand: current connection's ProjectManager, or raise."""
    return _conn().get_project_manager()


def _format_list(items, empty_msg: str, header: str) -> str:
    if not items:
        return empty_msg
    lines = [header]
    for item in items:
        lines.append(f"  - {item}")
    return "\n".join(lines)


# ── Project listing / lifecycle ─────────────────────────────────────────


@mcp.tool()
def get_project_list() -> str:
    """List project names in the ProjectManager's current folder.

    Returns a newline-separated list of project names, or a message stating
    the current folder has no projects.
    """
    try:
        pm = _pm()
        projects = pm.GetProjectListInCurrentFolder()
        return _format_list(
            projects, "No projects found in the current folder.", "Projects in current folder:"
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def create_project(name: str) -> str:
    """Create a new project in the ProjectManager's current folder.

    Parameters:
    - name: name for the new project. Must be non-empty and not already
      exist in the current folder (Resolve's CreateProject returns falsy
      on collision or invalid name).
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        pm = _pm()
        project = pm.CreateProject(name)
        if project is None:
            return (
                f"Error: Failed to create project '{name}'. A project with "
                "that name may already exist in the current folder, or the "
                "name is invalid."
            )
        return f"Project '{name}' created and opened successfully."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def load_project(name: str) -> str:
    """Load (open) an existing project by name from the current folder.

    Parameters:
    - name: exact project name as returned by get_project_list().

    Returns a clear not-found message rather than raising when no project
    with that name exists in the current folder.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        pm = _pm()
        project = pm.LoadProject(name)
        if project is None:
            return (
                f"Error: Project '{name}' not found in the current folder, "
                "or it could not be loaded. Use get_project_list() to see "
                "available projects."
            )
        return f"Project '{name}' loaded successfully."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def save_project() -> str:
    """Save the currently open project."""
    try:
        conn = _conn()
        pm = conn.get_project_manager()
        project = conn.get_project()
        # SaveProject() lives on the ProjectManager, not the Project object.
        result = pm.SaveProject()
        if result:
            return f"Project '{project.GetName()}' saved successfully."
        return "Error: SaveProject reported failure — the project may not have been saved."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def close_project(save_before_close: bool = True) -> str:
    """Close the currently open project.

    Parameters:
    - save_before_close: if True (default), attempt to save the project
      before closing it. If the save fails, closing still proceeds but the
      returned message notes the save failure so the caller is not misled
      into thinking work was preserved.

    Destructive (discards the open project from the app's active session,
    though the project itself remains on disk/database). Never raises —
    returns a clear failure string if there is no project open or the
    close call itself fails.
    """
    try:
        conn = _conn()
        pm = conn.get_project_manager()
        project = pm.GetCurrentProject()
        if project is None:
            return "Error: No project is currently open."
        name = project.GetName()

        save_warning = ""
        if save_before_close:
            try:
                # SaveProject() lives on the ProjectManager, not the Project object.
                if not pm.SaveProject():
                    save_warning = " (warning: SaveProject reported failure before close)"
            except Exception as save_exc:  # noqa: BLE001
                save_warning = f" (warning: save before close failed: {save_exc})"

        closed = pm.CloseProject(project)
        if not closed:
            return f"Error: Failed to close project '{name}'.{save_warning}"
        return f"Project '{name}' closed successfully.{save_warning}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def delete_project(name: str) -> str:
    """Permanently delete a project by name from the current folder.

    Parameters:
    - name: exact project name to delete. Required and must be non-empty —
      this tool never deletes an implicit/"current" project by omission.

    DESTRUCTIVE and irreversible. If the named project is currently open,
    Resolve's DeleteProject call commonly fails silently (returns False);
    this tool reports that as a clear failure string rather than raising,
    and does not attempt to close the project first (close it explicitly
    with close_project() before deleting it).
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string identifying the project to delete."
        pm = _pm()
        deleted = pm.DeleteProject(name)
        if not deleted:
            return (
                f"Error: Failed to delete project '{name}'. It may not exist "
                "in the current folder, or it may currently be open — close "
                "it first with close_project()."
            )
        return f"Project '{name}' deleted successfully."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── ProjectManager folder navigation (Project Library) ──────────────────


@mcp.tool()
def get_folder_list() -> str:
    """List subfolder names in the ProjectManager's current folder."""
    try:
        pm = _pm()
        folders = pm.GetFolderListInCurrentFolder()
        return _format_list(
            folders, "No subfolders found in the current folder.", "Folders in current folder:"
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def create_project_folder(name: str) -> str:
    """Create a new folder in the ProjectManager's current folder.

    Parameters:
    - name: name for the new folder.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        pm = _pm()
        result = pm.CreateFolder(name)
        if result:
            return f"Folder '{name}' created successfully."
        return f"Error: Failed to create folder '{name}'. It may already exist."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def goto_project_folder(name: str) -> str:
    """Navigate into a named subfolder of the ProjectManager's current folder.

    Parameters:
    - name: exact subfolder name, as returned by get_folder_list().
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        pm = _pm()
        result = pm.OpenFolder(name)
        if result:
            return f"Navigated into folder '{name}'."
        return f"Error: Failed to open folder '{name}'. It may not exist here."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def goto_root_folder() -> str:
    """Navigate the ProjectManager back to the root folder of the Project Library."""
    try:
        pm = _pm()
        result = pm.GotoRootFolder()
        if result:
            return "Navigated to the root folder."
        return "Error: Failed to navigate to the root folder."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def goto_parent_folder() -> str:
    """Navigate the ProjectManager up to the parent of the current folder."""
    try:
        pm = _pm()
        result = pm.GotoParentFolder()
        if result:
            return "Navigated to the parent folder."
        return "Error: Failed to navigate to the parent folder (already at root?)."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Database tools ───────────────────────────────────────────────────────


@mcp.tool()
def get_database_list() -> str:
    """List database connections configured in Resolve's Project Manager.

    Not available on all Resolve versions — returns an explanatory string
    when the API method is missing rather than raising.
    """
    try:
        pm = _pm()
        if not hasattr(pm, "GetDatabaseList"):
            return "Error: GetDatabaseList is not available on this version of DaVinci Resolve."
        databases = pm.GetDatabaseList()
        if not databases:
            return "No databases found."
        lines = ["Databases:"]
        for db in databases:
            if isinstance(db, dict):
                db_name = db.get("DbName", "(unknown)")
                db_type = db.get("DbType", "")
                ip = db.get("IpAddress", "")
                detail = f"{db_name} ({db_type}" + (f" @ {ip}" if ip else "") + ")"
                lines.append(f"  - {detail}")
            else:
                lines.append(f"  - {db}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def get_current_database() -> str:
    """Get the database connection Resolve's Project Manager is currently using.

    Not available on all Resolve versions — returns an explanatory string
    when the API method is missing rather than raising.
    """
    try:
        pm = _pm()
        if not hasattr(pm, "GetCurrentDatabase"):
            return "Error: GetCurrentDatabase is not available on this version of DaVinci Resolve."
        db = pm.GetCurrentDatabase()
        if not db:
            return "Error: Could not determine the current database."
        if isinstance(db, dict):
            db_name = db.get("DbName", "(unknown)")
            db_type = db.get("DbType", "")
            ip = db.get("IpAddress", "")
            detail = f"{db_name} ({db_type}" + (f" @ {ip}" if ip else "") + ")"
            return f"Current database: {detail}"
        return f"Current database: {db}"
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def set_current_database(db_type: str, db_name: str, ip_address: str = "") -> str:
    """Switch Resolve's Project Manager to a different database connection.

    Parameters:
    - db_type: database type string as reported by get_database_list(),
      typically "Disk" or "PostgreSQL".
    - db_name: database name as reported by get_database_list().
    - ip_address: optional IP address for a remote (PostgreSQL) database;
      leave empty for local/Disk databases.

    DESTRUCTIVE-ish: switching databases closes any project currently open
    in Resolve. Requires both db_type and db_name explicitly — never
    switches based on partial/implicit input. Not available on all Resolve
    versions — returns an explanatory string when the API method is
    missing rather than raising.
    """
    try:
        if not db_type or not db_type.strip():
            return "Error: db_type must be a non-empty string (e.g. 'Disk' or 'PostgreSQL')."
        if not db_name or not db_name.strip():
            return "Error: db_name must be a non-empty string."
        pm = _pm()
        if not hasattr(pm, "SetCurrentDatabase"):
            return "Error: SetCurrentDatabase is not available on this version of DaVinci Resolve."
        db_info = {"DbType": db_type, "DbName": db_name}
        if ip_address:
            db_info["IpAddress"] = ip_address
        result = pm.SetCurrentDatabase(db_info)
        if result:
            return f"Switched current database to '{db_name}' ({db_type})."
        return (
            f"Error: Failed to switch to database '{db_name}' ({db_type}). "
            "Check that db_type/db_name/ip_address match an entry from "
            "get_database_list()."
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Import / export / restore ────────────────────────────────────────────


@mcp.tool()
def import_project(file_path: str, project_name: str = "") -> str:
    """Import a project (.drp) file into the current database/folder.

    Parameters:
    - file_path: absolute path to the .drp project file to import.
    - project_name: optional name to give the imported project; if empty,
      Resolve uses the name embedded in the file.
    """
    try:
        if not file_path or not file_path.strip():
            return "Error: file_path must be a non-empty string."
        pm = _pm()
        if project_name:
            result = pm.ImportProject(file_path, project_name)
        else:
            result = pm.ImportProject(file_path)
        if result:
            return f"Project imported successfully from '{file_path}'."
        return f"Error: Failed to import project from '{file_path}'."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def export_project(name: str, file_path: str, with_stills_and_luts: bool = True) -> str:
    """Export a project by name to a .drp file.

    Parameters:
    - name: exact project name to export (must exist in the current folder).
    - file_path: destination path for the exported .drp file.
    - with_stills_and_luts: whether to include stills and LUTs in the
      exported archive (default True).
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string identifying the project to export."
        if not file_path or not file_path.strip():
            return "Error: file_path must be a non-empty string."
        pm = _pm()
        result = pm.ExportProject(name, file_path, with_stills_and_luts)
        if result:
            return f"Project '{name}' exported to '{file_path}'."
        return f"Error: Failed to export project '{name}' to '{file_path}'."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def restore_project(file_path: str, project_name: str = "") -> str:
    """Restore a project from a project backup/archive file.

    Parameters:
    - file_path: absolute path to the project archive to restore from.
    - project_name: optional name to give the restored project; if empty,
      Resolve uses the name embedded in the archive.
    """
    try:
        if not file_path or not file_path.strip():
            return "Error: file_path must be a non-empty string."
        pm = _pm()
        if not hasattr(pm, "RestoreProject"):
            return "Error: RestoreProject is not available on this version of DaVinci Resolve."
        if project_name:
            result = pm.RestoreProject(file_path, project_name)
        else:
            result = pm.RestoreProject(file_path)
        if result:
            return f"Project restored successfully from '{file_path}'."
        return f"Error: Failed to restore project from '{file_path}'."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ── Archive & Blackmagic Cloud project lifecycle ─────────────────────────

# Attribute names on the Resolve object that hold the cloud-settings dict keys
# (available on Studio, Resolve 18.5+). Guarded with hasattr at call time.
_CLOUD_SETTING_KEYS = {
    "project_name": "CLOUD_SETTING_PROJECT_NAME",
    "project_media_path": "CLOUD_SETTING_PROJECT_MEDIA_PATH",
    "is_collab": "CLOUD_SETTING_IS_COLLAB",
    "sync_mode": "CLOUD_SETTING_SYNC_MODE",
    "is_camera_access": "CLOUD_SETTING_IS_CAMERA_ACCESS",
}

# Friendly sync_mode -> attribute name on the Resolve object (resolve.CLOUD_SYNC_*).
_CLOUD_SYNC_MODES = {
    "none": "CLOUD_SYNC_NONE",
    "proxy_only": "CLOUD_SYNC_PROXY_ONLY",
    "proxy_and_orig": "CLOUD_SYNC_PROXY_AND_ORIG",
}


def _build_cloud_settings(resolve, values: dict) -> tuple:
    """Build a resolve-native cloud-settings dict from friendly ``values``.

    Resolves each ``CLOUD_SETTING_*`` key attribute (and the ``CLOUD_SYNC_*``
    value for ``sync_mode``) off the live ``resolve`` object via ``getattr``,
    guarding each lookup with ``hasattr``. Returns ``(settings_dict, None)`` on
    success, or ``(None, error_message)`` if this Resolve build lacks the
    required constants.

    ``values`` maps friendly setting names (keys of ``_CLOUD_SETTING_KEYS``) to
    already-validated values; entries whose value is ``None`` are skipped so a
    caller can omit optional settings (e.g. on a subsequent cloud-project load).
    """
    settings = {}
    for friendly, value in values.items():
        if value is None:
            continue
        attr = _CLOUD_SETTING_KEYS[friendly]
        if not hasattr(resolve, attr):
            return None, (
                "Error: Blackmagic Cloud project settings are not available in "
                "this Resolve version (missing "
                f"'{attr}'). Requires DaVinci Resolve Studio 18.5 or newer."
            )
        key = getattr(resolve, attr)
        if friendly == "sync_mode":
            sync_attr = _CLOUD_SYNC_MODES[value]
            if not hasattr(resolve, sync_attr):
                return None, (
                    "Error: cloud sync modes are not available in this Resolve "
                    f"version (missing '{sync_attr}'). Requires DaVinci Resolve "
                    "Studio 18.5 or newer."
                )
            settings[key] = getattr(resolve, sync_attr)
        else:
            settings[key] = value
    return settings, None


@mcp.tool()
def archive_project(
    name: str,
    path: str,
    archive_src_media: bool = True,
    archive_render_cache: bool = True,
    archive_proxy_media: bool = False,
) -> str:
    """Archive a project by name to a ``.dra`` archive on disk.

    Parameters:
    - name: exact project name to archive (must exist in the current folder).
    - path: destination path for the archive.
    - archive_src_media: include source media (default True).
    - archive_render_cache: include render cache (default True).
    - archive_proxy_media: include proxy media (default False).

    Resolve's ArchiveProject returns falsy (rather than raising) when the
    named project does not exist or the archive cannot be written — this is
    reported as a clear failure string.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string identifying the project to archive."
        if not path or not path.strip():
            return "Error: path must be a non-empty string."
        pm = _pm()
        if not hasattr(pm, "ArchiveProject"):
            return "Error: ArchiveProject is not available on this version of DaVinci Resolve."
        result = pm.ArchiveProject(
            name, path, archive_src_media, archive_render_cache, archive_proxy_media
        )
        if result:
            return f"Project '{name}' archived to '{path}'."
        return (
            f"Error: Failed to archive project '{name}' to '{path}'. The "
            "project may not exist in the current folder, or the destination "
            "path may not be writable."
        )
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def create_cloud_project(
    name: str,
    media_path: str,
    sync_mode: str = "proxy_only",
    is_collab: bool = False,
    is_camera_access: bool = False,
) -> str:
    """Create a new Blackmagic Cloud project (DaVinci Resolve Studio 18.5+).

    Parameters:
    - name: name for the new cloud project.
    - media_path: media location path for the cloud project (required).
    - sync_mode: one of 'none', 'proxy_only', 'proxy_and_orig'
      (default 'proxy_only').
    - is_collab: enable collaboration mode (default False).
    - is_camera_access: enable camera access (default False).

    Not available on all Resolve builds — returns a 'not available in this
    Resolve version' string (rather than raising) when CreateCloudProject or
    the CLOUD_SETTING_*/CLOUD_SYNC_* constants are missing.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        if not media_path or not media_path.strip():
            return "Error: media_path must be a non-empty string."
        mode, err = _check_choice(sync_mode, _CLOUD_SYNC_MODES.keys(), "sync_mode")
        if err:
            return f"Error: {err}"

        conn = _conn()
        pm = conn.get_project_manager()
        if not hasattr(pm, "CreateCloudProject"):
            return (
                "Error: CreateCloudProject is not available in this Resolve "
                "version. Blackmagic Cloud projects require DaVinci Resolve "
                "Studio 18.5 or newer."
            )
        resolve = conn.get_resolve()
        settings, cloud_err = _build_cloud_settings(
            resolve,
            {
                "project_name": name,
                "project_media_path": media_path,
                "sync_mode": mode,
                "is_collab": is_collab,
                "is_camera_access": is_camera_access,
            },
        )
        if cloud_err:
            return cloud_err
        project = pm.CreateCloudProject(settings)
        if project is None:
            return (
                f"Error: Failed to create cloud project '{name}'. Check that "
                "you are signed in to Blackmagic Cloud and that media_path is "
                "valid."
            )
        return f"Cloud project '{name}' created and opened successfully."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@mcp.tool()
def load_cloud_project(
    name: str,
    media_path: str = "",
    sync_mode: str = "",
) -> str:
    """Load an existing Blackmagic Cloud project (DaVinci Resolve Studio 18.5+).

    Parameters:
    - name: name of the cloud project to load.
    - media_path: media location path — used on the first load only; leave
      empty on subsequent loads.
    - sync_mode: one of 'none', 'proxy_only', 'proxy_and_orig' — used on the
      first load only; leave empty to omit.

    Not available on all Resolve builds — returns a 'not available in this
    Resolve version' string (rather than raising) when LoadCloudProject or the
    CLOUD_SETTING_*/CLOUD_SYNC_* constants are missing.
    """
    try:
        if not name or not name.strip():
            return "Error: name must be a non-empty string."
        mode = None
        if sync_mode:
            mode, err = _check_choice(sync_mode, _CLOUD_SYNC_MODES.keys(), "sync_mode")
            if err:
                return f"Error: {err}"

        conn = _conn()
        pm = conn.get_project_manager()
        if not hasattr(pm, "LoadCloudProject"):
            return (
                "Error: LoadCloudProject is not available in this Resolve "
                "version. Blackmagic Cloud projects require DaVinci Resolve "
                "Studio 18.5 or newer."
            )
        resolve = conn.get_resolve()
        settings, cloud_err = _build_cloud_settings(
            resolve,
            {
                "project_name": name,
                "project_media_path": media_path or None,
                "sync_mode": mode,
            },
        )
        if cloud_err:
            return cloud_err
        project = pm.LoadCloudProject(settings)
        if project is None:
            return (
                f"Error: Failed to load cloud project '{name}'. It may not "
                "exist, or you may not be signed in to Blackmagic Cloud."
            )
        return f"Cloud project '{name}' loaded successfully."
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"

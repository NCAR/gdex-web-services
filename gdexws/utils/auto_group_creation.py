"""
This module provides utility functions for managing group settings in a directory structure.
It allows automatic discovery of group information by scanning the source directory tree,
and supports maintaining stable group indices using existing group settings.
"""

from fnmatch import fnmatchcase
from pathlib import Path
import os
import json

from gdexws.utils import service_log


def is_excluded(rel_path: str, exclude_list: list = None) -> bool:
    """Whether a path relative to the scan root matches any exclude pattern.

    - A pattern without ``/`` (e.g. ``tmp*``, ``*.log``) matches the name of the
      file or of any folder above it, at any depth.
    - A pattern with ``/`` (e.g. ``runA/scratch``) matches the relative path itself
      or any folder above it.

    Matching is case-sensitive with shell-style wildcards (``*``, ``?``, ``[abc]``).

    Parameters
    ----------
    rel_path : str
        Path relative to the scan root, using ``/``, e.g. ``runA/out/diag.1.nc``.
    exclude_list : list of str, optional
        Exclude patterns. Default: nothing is excluded.

    Examples
    --------
    >>> is_excluded("runA/tmp/diag.1.nc", ["tmp"])
    True
    >>> is_excluded("runA/diag.1.log", ["*.log"])
    True
    >>> is_excluded("runA/scratch/x.nc", ["runA/scratch"])
    True
    >>> is_excluded("runB/scratch/x.nc", ["runA/scratch"])
    False
    """
    if not exclude_list:
        return False
    parts = rel_path.strip("/").split("/")
    for pattern in exclude_list:
        pattern = pattern.strip("/")
        if "/" in pattern:
            # compare against runA, runA/scratch, runA/scratch/x.nc, ...
            for depth in range(1, len(parts) + 1):
                if fnmatchcase("/".join(parts[:depth]), pattern):
                    return True
        else:
            for part in parts:
                if fnmatchcase(part, pattern):
                    return True
    return False


def _add_exclude(group: dict, exclude_list: list) -> None:
    """Record the exclude patterns in a group, keeping any it already has.

    Exclusions are only ever added (never dropped), because removing one could make
    a later copy include files that were meant to stay out.
    """
    current = group.get('exclude_pattern') or []
    for pattern in exclude_list:
        if pattern not in current:
            current.append(pattern)
    group['exclude_pattern'] = current


def _load_settings(group_setting_json: Path) -> dict:
    """Load persisted group settings from disk.
    
    Parameters
    ----------
    group_setting_json : Path
        Path to the group settings JSON file to load.

    Returns
    -------
    dict
        Dictionary containing the loaded group settings. Returns an empty dictionary if the file does not exist or if the path is None.
    """
    if group_setting_json is None:
        return {}

    if os.path.exists(group_setting_json):
        with open(group_setting_json, 'r', encoding='utf-8') as f:
            return json.load(f)

    return {}

def _save_settings(group_settings: dict, group_setting_json: Path) -> None:
    """Save group settings to disk.

    Parameters
    ----------
    group_settings : dict
        Dictionary containing the group settings to save.
    group_setting_json : Path
        Path to the group settings JSON file to save to.
    """
    with open(group_setting_json, 'w', encoding='utf-8') as f:
        json.dump(group_settings, f, indent=2)

def create(
    orignal_data_location: Path,
    exist_group_json: Path = None,
    new_group_json: Path = None,
    filename_wcard_list: list = None,
    one_group: bool = False,
    logger: object = None,
    exclude_list: list = None
) -> dict:
    """
    Discover group info settings by walking the source directory tree under orignal_data_location.
    The function also have the flexibility of using existing group settings to maintain stable group indices.
    Give a new group settings JSON file path, the function will save the discovered group settings to that file.
    Give a existing group settings JSON file path, the function will use it to maintain group indices already existing.

    Parameters
    ----------
    orignal_data_location : Path
        root directory of the original data to be scanned
    exist_group_json : Path
        path to the existing group settings JSON file
    new_group_json : Path
        path to save the new group settings JSON file. if not provided no JSON file will be saved
    filename_wcard_list : list, optional
        list of filename wildcards to match within each group directory, by default ["*"],
        adding subdirectory paths to the wildcard patterns also works if needed.
    one_group : bool, optional
        whether to treat the root directory as a single group, by default False
    logger : object, optional
        logger object for logging messages, by default None
        set to logging.getLogger() to use the root logger in the main script.
    exclude_list : list, optional
        folder or file patterns to leave out, see ``is_excluded``, by default None.
        Excluded folders are not walked (so they never become groups) and excluded
        files are ignored. The patterns are also saved in each group as
        ``exclude_pattern``, so tools that copy the group's files can skip them too.


    Returns
    -------
    dict
        group settings dictionary with parent_index correctly chained back to
        the nearest ancestor group.
    """
    # if filename_wcard_list is not provided, default to ["*"] for including all files
    if filename_wcard_list is None:
        filename_wcard_list = ["*"]
    if exclude_list is None:
        exclude_list = []

    if exist_group_json:
        if not exist_group_json.exists():
            service_log(
                command_name="auto-group-creation",
                level="ERROR",
                process_message="The existing group settings JSON file does not exist"
            )
            raise FileNotFoundError(f"Existing group settings JSON file not found: {exist_group_json}")
        
        dict_group_settings = _load_settings(exist_group_json)
        next_group_index = max((v['group_index'] for v in dict_group_settings.values()), default=0) + 1
    else:
        dict_group_settings = {}
        next_group_index = 1

    # initialize path-to-group mapping dictionaries
    scan_root_path = os.path.normpath(orignal_data_location)
    dict_path_to_gid = {scan_root_path: 0}      # start from group index 0 for the root
    dict_path_to_gname = {scan_root_path: None} # no group name for the root

    # if one_group is True, treat the root directory as a single group
    # use the basename dir as the group name for the single group scenario
    if one_group:
        # descend through directories that hold only a single subdirectory and no files,
        # until reaching the directory that actually contains files
        while True:
            # sort this level's entries into kept files and kept subdirectories
            has_files = False
            subdirs = []
            for name in os.listdir(scan_root_path):
                full_path = os.path.join(scan_root_path, name)
                rel_path = os.path.relpath(full_path, orignal_data_location).replace(os.sep, '/')
                if is_excluded(rel_path, exclude_list):
                    continue
                if os.path.isfile(full_path):
                    has_files = True
                elif os.path.isdir(full_path):
                    subdirs.append(name)

            # this directory holds the data files: it is the single group
            if has_files:
                break

            if len(subdirs) > 1:
                raise ValueError("Multiple subdirectories found, unable to determine which one to scan.")
            if not subdirs:
                # empty directory: nothing left to descend into
                break
            scan_root_path = os.path.join(scan_root_path, subdirs[0])


        # treat the (now located) directory as a single group (move one level up)
        subdir = os.path.basename(scan_root_path)
        scan_root_path = os.path.dirname(scan_root_path)
        rel_path = os.path.relpath(os.path.join(scan_root_path, subdir), scan_root_path)
        group_name_key = rel_path.replace(os.sep, '_')
        group_name = rel_path.split(os.sep)[-1]  # use the last part of the relative path as the group name
        group_name = f"{group_name}"

        if group_name_key not in dict_group_settings:
            # new folder: assign the next sequential index
            # (a group already in the existing JSON keeps its index and settings)
            group_index = next_group_index
            dict_group_settings[group_name_key] = {
                'group_index': group_index,
                'group_name': f"{group_name}_gidx_{group_index}",
                'parent_index': 0,
                'title': f"Group {group_index} - {group_name}",
                'web_path': f"{rel_path}",
                'save_path': f"{rel_path}",
                'file_pattern': None,
                'webfile_pattern': None,
                'scan_root_path': scan_root_path,
            }

        # attach the file pattern to whichever group this directory belongs to
        rel_path = subdir

        if dict_group_settings[group_name_key]['file_pattern'] is None:
            file_pattern_list = []
            for filename_wcard in filename_wcard_list:
                file_pattern_list.append(os.path.join(rel_path, filename_wcard))
            dict_group_settings[group_name_key]['file_pattern'] = file_pattern_list

        if dict_group_settings[group_name_key]['webfile_pattern'] is None:
            webfile_pattern_list = []
            for filename_wcard in filename_wcard_list:
                webfile_pattern_list.append(os.path.join(rel_path, filename_wcard))
            dict_group_settings[group_name_key]['webfile_pattern'] = webfile_pattern_list

        if exclude_list:
            _add_exclude(dict_group_settings[group_name_key], exclude_list)

        # save the updated group settings to disk
        if new_group_json:
            _save_settings(dict_group_settings, new_group_json)

        return dict_group_settings

    # if one_group is False, we need to traverse the directory tree and create groups for each subdirectory as needed
    for root, dirs, files in os.walk(scan_root_path):
        # find current folder level (with group index defined) for parent group index in
        root = os.path.normpath(root)
        parent_index = dict_path_to_gid[root]
        parent_name = dict_path_to_gname[root]

        # drop excluded folders and files; editing dirs in place stops os.walk entering them,
        # and an excluded folder does not count when deciding whether the tree branches here
        rel_root = os.path.relpath(root, scan_root_path).replace(os.sep, '/')
        if rel_root == '.':
            # the scan root itself: names need no folder in front
            rel_root = ''
        else:
            rel_root = rel_root + '/'
        kept_dirs = []
        for dir_name in dirs:
            if not is_excluded(rel_root + dir_name, exclude_list):
                kept_dirs.append(dir_name)
        dirs[:] = kept_dirs  # replace the contents (not the list) so os.walk sees it

        kept_files = []
        for file_name in files:
            if not is_excluded(rel_root + file_name, exclude_list):
                kept_files.append(file_name)
        files = kept_files

        # for directory branching, create a new group for each subfolder
        if len(dirs) > 1:
            # sort the subdirectories to ensure consistent group indexing
            dirs.sort()
            # branch point: each subfolder becomes its own group
            for subdir in dirs:
                rel_path = os.path.relpath(os.path.join(root, subdir), scan_root_path)
                group_name_key = rel_path.replace(os.sep, '_')
                group_name = rel_path.split(os.sep)[-1]  # use the last part of the relative path as the group name
                group_name = f"{group_name}"

                if group_name_key not in dict_group_settings:
                    # new folder: assign the next sequential index
                    group_index = next_group_index
                    next_group_index += 1
                    dict_group_settings[group_name_key] = {
                        'group_index': group_index,
                        'group_name': f"{group_name}_gidx_{group_index}",
                        'parent_index': parent_index,
                        'title': f"Group {group_index} - {group_name}", # title is the related to group index for simplicity
                        'web_path': f"{rel_path}",
                        'save_path': f"{rel_path}",
                        'file_pattern': None,
                        'webfile_pattern': None,
                        'scan_root_path': scan_root_path
                    }
                else:
                    group_index = dict_group_settings[group_name_key]['group_index']

                child_path = os.path.normpath(os.path.join(root, subdir))
                dict_path_to_gid[child_path] = group_index
                dict_path_to_gname[child_path] = group_name_key

        elif len(dirs) == 1:
            # single child: no new group, it inherits the parent group index and name
            child_path = os.path.normpath(os.path.join(root, dirs[0]))
            dict_path_to_gid[child_path] = parent_index
            dict_path_to_gname[child_path] = parent_name

        else:
            # no subdirectories: but with files which should run the next if block
            pass

        # if there are files in the current directory, attach them to the current group
        if files:
            # for file find under
            if parent_name is None:
                log_error = f"Files found directly under scan root with no group assigned: {root}"
                if logger:
                    logger.error(log_error)
                continue

            # attach the file pattern to whichever group this directory belongs to
            rel_path = os.path.relpath(root, scan_root_path)
            if dict_group_settings[parent_name]['file_pattern'] is None:
                file_pattern_list = []
                for filename_wcard in filename_wcard_list:
                    file_pattern_list.append(os.path.join(rel_path, filename_wcard))
                dict_group_settings[parent_name]['file_pattern'] = file_pattern_list

            if dict_group_settings[parent_name]['webfile_pattern'] is None:
                webfile_pattern_list = []
                for filename_wcard in filename_wcard_list:
                    webfile_pattern_list.append(os.path.join(rel_path, filename_wcard))
                dict_group_settings[parent_name]['webfile_pattern'] = webfile_pattern_list

    if exclude_list:
        for group in dict_group_settings.values():
            _add_exclude(group, exclude_list)

    # save the updated group settings to disk
    if new_group_json:
        _save_settings(dict_group_settings, new_group_json)

    return dict_group_settings
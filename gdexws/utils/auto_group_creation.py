"""
This module provides utility functions for managing group settings in a directory structure.
It allows automatic discovery of group information by scanning the source directory tree,
and supports maintaining stable group indices using existing group settings.
"""

from pathlib import Path
import os
import json

from gdexws.utils import service_log


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
    logger: object = None
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


    Returns
    -------
    dict
        group settings dictionary with parent_index correctly chained back to
        the nearest ancestor group.
    """
    # if filename_wcard_list is not provided, default to ["*"] for including all files
    if filename_wcard_list is None:
        filename_wcard_list = ["*"]

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
        while not any(os.path.isfile(os.path.join(scan_root_path, f)) for f in os.listdir(scan_root_path)):
            subdirs = [
                d for d in os.listdir(scan_root_path)
                if os.path.isdir(os.path.join(scan_root_path, d))
            ]
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

    # save the updated group settings to disk
    if new_group_json:
        _save_settings(dict_group_settings, new_group_json)

    return dict_group_settings
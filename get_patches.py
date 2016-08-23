#!/usr/bin/env python3

"""
Small utility to retrieve all patches currently available in the database.
"""

from argparse import ArgumentParser
from octopus.server.python_shell_interface import PythonShellInterface
from octopus.shell.octopus_shell_utils import reload_dir
import sys
import os


DESCRIPTION = """Retrieve all security patches from the database."""

if __name__ == '__main__':
    argParser = ArgumentParser(description = DESCRIPTION)
    argParser.add_argument('project')
    args = argParser.parse_args()
    projectName = args.project

    # Setup
    dbConnection = PythonShellInterface()
    dbConnection.setDatabaseName(projectName)
    dbConnection.connectToDatabase()

    # Load additional groovy files (mute any output in the meantime).
    old_stdout = sys.stdout
    sys.stdout = open(os.devnull, "w")
    reload_dir(dbConnection.shell_connection, "steps")
    sys.stdout.close()
    sys.stdout = old_stdout

    # Get the current code base location.
    patch_nodes_results = dbConnection.runGremlinQuery('queryPatchIndex("/.*patch/").transform{"" + it.id + "\t" + it.filepath + "\t" + it.reversed}')
    for patch_node_result in patch_nodes_results:
        (patch_node_id, patch_filepath, patch_reversed) = patch_node_result.split("\t")
        print(str(patch_node_id) + "\t" + patch_reversed + "\t" + patch_filepath)
